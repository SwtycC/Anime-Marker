"""媒体库桥接层：把 Database 的只读查询暴露给 QML。

设计原则（§5.12.5）：
1. **只暴露数据，不暴露 ORM 对象** —— QML 拿不到 Python dataclass，
   一律转成 `dict`（QML 侧直接当 JS 对象用）。
2. **返回值必须是 QML 能理解的基本类型** —— `QVariantList` / `QVariantMap`
   会自动转换 list[dict]，不要返回自定义类实例。
3. 耗时操作（扫描、网络）走 QThread + Signal，见 `scanner.py`；
   本地 SQLite 查询很快，直接同步返回即可（避免过度设计）。

封面路径：QML 的 `Image.source` 需要 URL 形式，且**不认 Windows 反斜杠**，
因此统一用 `as_file_url()` 转成 `file:///D:/...`（见 §5.12.5 的路径坑）。
"""

from __future__ import annotations

import logging
import re
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from PySide6.QtCore import QObject, Property, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtWidgets import QFileDialog

from app.core.bangumi_api import (
    COLLECT_TYPE_DOING, COLLECT_TYPE_NAMES, BangumiClient, BangumiNotFound,
    is_studio_name,
)
from app.core.database import Database, Episode, Subject
# 同系列排序用：把「第X季 / S1 / II」统一解析成季数（见 series_siblings）
from app.core.matcher import extract_season
from app.utils.cover_cache import cover_path_for, known_cover_files
from app.utils.cover_cache import download as download_cover
from app.utils.paths import covers_dir

log = logging.getLogger(__name__)

# 多季展示模式
DISPLAY_FLAT = "flat"
DISPLAY_GROUPED = "grouped"

# 收藏缓存超过这个年龄就算"数据较旧"。
#
# **唯一权威定义**：`inProgressMeta` 拿它决定页头要不要写「数据较旧，建议刷新」，
# `needsRemoteRefresh` 拿它决定播放结束后要不要自动重拉 —— 两处必须同一个数，
# 否则会出现"页头说数据旧、后台却认为不用刷新"（或反之）的自相矛盾。
STALE_CACHE_SECONDS = 3600


# 放送日期 tag 的形状：Bangumi 会给每个条目打一个「2015年7月」这样的 tag。
# 兼容几种变体：`2015年7月` / `2015年7月番` / `2015-07` / `2015/07`
_AIR_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*[年\-/]\s*(\d{1,2})\s*月?")


def _air_date_key(tags: list[str]) -> Optional[int]:
    """从 tag 列表里提取放送日期，返回**可比较的整数** `YYYYMM`。

    用途：同系列排序（见 `LibraryBridge.series_siblings`）。
    返回 None 表示这些 tag 里没有日期信息。

    **为什么要单独抽出来**：日期 tag 混在题材/公司等一堆 tag 里，位置不定
    ，必须逐个匹配而不是只看某个固定位置。

    只取**最早**的那个：个别条目会同时带「2022年7月」与「2022年」这类
    年月不全的 tag，取最小月份组合最稳（年份相同、月份小的更接近真实首播）。
    """
    best: Optional[int] = None
    for t in tags or []:
        m = _AIR_DATE_RE.search(str(t))
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        if not (1900 <= year <= 2200 and 1 <= month <= 12):
            continue
        key = year * 100 + month
        if best is None or key < best:
            best = key
    return best


def as_file_url(path: str) -> str:
    """本地路径 → QML 可用的 file:/// URL。

    必须做两件事，否则含中文/空格的封面加载不出来：
    1. 反斜杠转正斜杠（QML 的 Image.source 不认 `\\`）
    2. 按 URL 规则转义（空格、中文），保留 `/` 与 `:` 作为路径分隔
    """
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        # as_posix() 得到 D:/a/b，前面补三个斜杠构成 file:///D:/a/b
        return "file:///" + quote(p.as_posix(), safe="/:")
    except OSError:
        return ""


class _TagFetchWorker(QThread):
    """后台拉取单个条目的前 10 个 tag + 动画制作公司（详情页进入时的自动补录）。

    为什么独立成 worker：进入详情页就要发一次网络请求（存量条目），
    不能阻塞 UI 线程。每个条目只需成功一次，之后全部走本地库，
    因此不值得做成常驻线程池 —— 用完即弃的 QThread 足够。

    **公司也在这里补**：`get_subject` 的响应里 infobox 本来就有，白拿 ——
    存量条目（scan 的新逻辑之前入库的）不用整库重扫，翻到详情页就补上了。
    """

    #: (subject_id, ok, message, studio_written) —— message 供状态栏展示；
    #: studio_written 为真时调用方要刷新海报墙（subjects 变了）
    done = Signal(int, bool, str, bool)

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        subject_id: int,
        bangumi_id: int,
        need_tags: bool = True,
        need_studio: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._subject_id = subject_id
        self._bangumi_id = bangumi_id
        # 各取所需：只有缺的那部分才写库、才通知（都齐了时不会走到这里）
        self._need_tags = need_tags
        self._need_studio = need_studio

    def run(self) -> None:
        try:
            subj = self._api.get_subject(self._bangumi_id)
        except Exception as e:
            log.warning("拉取条目标签失败 subject_id=%s: %s", self._subject_id, e)
            self.done.emit(self._subject_id, False,
                           "获取标签失败，请检查网络后重试", False)
            return

        tags = Database.tags_from_subject(subj or {}) if self._need_tags else []
        # 公司与 tag 同源同价：infobox 有就白拿，没有才多查一次 /persons
        studio = (self._api.studio_for(subj or {}, self._bangumi_id)
                  if self._need_studio else "")
        if not tags and not studio:
            # 请求成功但确实没有可取的数据：也算"完成"，只是没有数据可写 ——
            # 不写任何行，下次进入还会再试（与 F20 水位的"成功 0 行"不同，
            # 这点数据极廉价，不值得为此建水位表）。提示要说清缺的是哪一样：
            # 「动画制作」这一栏只有一部分条目有，说成"没有标签"会让人以为
            # 标签也没了。
            msg = ("该条目在 Bangumi 上没有标签" if self._need_tags
                   else "该条目的 infobox 里没有「动画制作」")
            self.done.emit(self._subject_id, False, msg, False)
            return
        try:
            if tags:
                self._db.replace_subject_tags(self._subject_id, tags)
            if studio:
                self._db.set_subject_studio(self._subject_id, studio)
        except Exception as e:
            log.exception("写入条目标签失败 subject_id=%s: %s", self._subject_id, e)
            self.done.emit(self._subject_id, False, "写入标签失败：%s" % e, False)
            return

        log.info("已拉取条目标签：subject_id=%s（bgm=%s）tag %s 个 / 公司 %s",
                 self._subject_id, self._bangumi_id, len(tags), studio or "无")
        if tags:
            msg = "已获取 %d 个标签" % len(tags)
        else:
            msg = "已获取制作公司：%s" % studio
        self.done.emit(self._subject_id, True, msg, bool(studio))


class _CollectTypeWorker(QThread):
    """读 / 写单个条目的 Bangumi 收藏状态（详情页那排状态选择器）。

    **为什么要线程**：两件事都要发网络请求 ——
      ① 进入详情页时本地没有状态（新建/未匹配过收藏的条目），
         要 `GET /v0/users/-/collections/{id}` 回查真实状态；
      ② 用户点某个状态时要 `POST` 写远端。
    同步做会把 UI 卡住 0.3~1s（与 `_TagFetchWorker` 同理）。

    **写远端的顺序**：先请求远端，**成功才回写本地**。反过来会导致
    "界面显示改了、Bangumi 上其实没改" —— 用户下次打开网页版发现对不上，
    而且本地这份快照此后一直是错的。

    `done` 的 message 用于状态栏；`ok=False` 时 QML 侧把选中项**弹回原值**
    （不能保留乐观更新的结果，理由同上）。
    """

    #: (subject_id, ok, message, new_type)
    done = Signal(int, bool, str, int)

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        subject_id: int,
        bangumi_id: int,
        write_type: int = 0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._subject_id = subject_id
        self._bangumi_id = bangumi_id
        # 0 = 只读（回查并写本地）；1~5 = 写入该状态
        self._write_type = int(write_type or 0)

    def run(self) -> None:
        if self._write_type:
            try:
                self._api.set_collection_type(self._bangumi_id, self._write_type)
            except Exception as e:
                log.warning("设置收藏状态失败 subject_id=%s（bgm=%s）→ %s: %s",
                            self._subject_id, self._bangumi_id,
                            self._write_type, e)
                self.done.emit(self._subject_id, False,
                               "设置失败：%s" % e, 0)
                return
            name = COLLECT_TYPE_NAMES.get(self._write_type, "")
            # 远端成功后才落本地快照
            try:
                self._db.set_subject_collect_type(self._subject_id,
                                                  self._write_type)
            except Exception as e:
                # 远端已改成功，本地写失败：**仍算成功**（权威在远端），
                # 只是下次进详情页会再回查一次补上
                log.warning("写入收藏状态快照失败 subject_id=%s: %s",
                            self._subject_id, e)
            self.done.emit(self._subject_id, True, "已标记为「%s」" % name,
                           self._write_type)
            return

        # ---- 只读：回查真实状态 ----
        #
        # **三种结果要分开处理**（原先混在一起，是"回查永远 404"之外的第二
        # 层问题）：
        #   ① 未收藏 —— 官方返回 404，明确结论 → 写 0 快照，静默；
        #   ② 查到了   —— 写真实状态；
        #   ③ 没查成   —— 网络 / 鉴权 / 用户名解析失败 → **什么都不写**。
        # ③ 若也按"未收藏"写 0，就等于把一次断网永久记成"这条没收藏"，
        # 而且本地一旦有值后端就不再回查（见 requestCollectType），
        # 这份错快照会一直留着。写 0 的诱惑是"下次进详情页别再请求"，
        # 但防重复请求已有 `_collect_queried` 兜底，不需要拿准确性来换。
        try:
            data = self._api.get_collection(self._bangumi_id)
        except BangumiNotFound:
            data = None
        except Exception as e:
            log.warning("回查收藏状态失败 subject_id=%s: %s",
                        self._subject_id, e)
            self.done.emit(self._subject_id, False, "", 0)
            return
        if not isinstance(data, dict):
            # ① 未收藏（get_collection 用 None 表示"确实没有"）
            try:
                # local_change=False：这是"回查到的远端值"，不是用户改的
                # （见 Database.set_subject_collect_type）
                self._db.set_subject_collect_type(self._subject_id, 0,
                                                  local_change=False)
            except Exception:
                pass
            self.done.emit(self._subject_id, False, "", 0)
            return
        # ② 查到了
        ctype = int(data.get("type") or 0)
        if ctype not in COLLECT_TYPE_NAMES:
            ctype = 0
        try:
            # local_change=False：同上，回查结果只是抄回本地
            self._db.set_subject_collect_type(self._subject_id, ctype,
                                              local_change=False)
        except Exception as e:
            log.warning("写入收藏状态快照失败 subject_id=%s: %s",
                        self._subject_id, e)
        self.done.emit(self._subject_id, ctype > 0, "", ctype)


class LibraryBridge(QObject):
    """媒体库数据源。"""

    # 数据变化通知（QML 侧用它触发列表重建）
    subjectsChanged = Signal()
    #: 某条目的收藏状态变化（参数：subject_id）—— 详情页那排状态选择器刷新
    collectTypeChanged = Signal(int)
    episodesChanged = Signal()
    inProgressChanged = Signal()
    watchedEpsChanged = Signal()
    #: 海报变更（参数：subject_id），更换海报小窗与详情页据此刷新
    coverChanged = Signal(int)
    #: 条目标签变更（参数：subject_id），详情页据此重取 tag 列表
    tagsChanged = Signal(int)
    #: 全库 tag 映射变更（无参数），海报墙的 tag 筛选据此重算分类目录
    tagsBySubjectChanged = Signal()
    #: 标签拉取的进度/结果提示（状态栏展示）
    statusMessage = Signal(str)

    def __init__(
        self,
        db: Database,
        api: Optional[BangumiClient] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._display_mode = DISPLAY_FLAT
        # 查询缓存：Property 会被 QML 频繁读取（每次绑定重算都会调 getter），
        # 若每次都跑 SQL + 组装 dict 会很浪费。改为「变更时失效」。
        self._subjects_cache: list[dict] = []
        self._dirty = True
        # 全库 tag 映射（海报墙筛选用）：{subject_id(str): [tag 名, ...]}
        self._tags_map: dict[str, list[str]] = {}
        self._tags_dirty = True
        self._inprogress_cache: list[dict] = []
        self._inprogress_dirty = True
        self._eps_cache: list[dict] = []
        self._eps_dirty = True
        # 海报版本号：换海报后 cover_path 可能指回**同一个文件**（恢复原版），
        # 而 QML 的 Image 按 URL 缓存解码结果 —— URL 不变就一直显示旧图。
        # 按条目记一个递增版本号，以 `?v=N` 追加到 file:// URL 后（query 不参与
        # 本地文件寻址，只作为缓存键的一部分），每次海报变更 +1 强制重新解码。
        self._cover_revs: dict[int, int] = {}
        # 标签自动补拉：同一时刻最多一个 worker；期间新请求只记最后一个
        self._tag_worker: Optional[_TagFetchWorker] = None
        self._tag_fetch_pending: Optional[int] = None
        # 收藏状态（读/写）worker。**每个条目各自独立**，不做排队：
        # 用户在一部番上点状态时不会有并发的第二部（详情页一次只显示一条），
        # 而进入详情页时的"回查"与"点击写入"可能重叠 —— 后者必须能立刻执行，
        # 排队会让点击延迟到回查结束（体感很差）。两者写的是同一列，
        # 但写入方以**远端结果**为准，最后落地的一定是用户点的那次。
        self._collect_workers: list[_CollectTypeWorker] = []
        # 本次运行**已经回查过**收藏状态的条目（本地主键）。防重入的闸门，
        # 见 requestCollectType —— 没有它，回查结果为 0 的条目会自激成
        # 无限请求循环。写在内存而不是库里：它是"这一次运行做没做过"的
        # 事实，与"库里存的值"无关。
        self._collect_queried: set[int] = set()

    def set_api(self, api: BangumiClient) -> None:
        """注入 Bangumi 客户端（QmlApp 在配置重建时调用）。"""
        self._api = api

    # 注：**曾经**在启动时做过一次"清掉缓存里没有的条目"的对齐，已删除。
    # 它用"最近一次同步的收藏缓存"当权威，而那个缓存可能比本地值更旧。
    # 缓存只能当"秒显"的加速，不能当**删数据**的依据 —— 现在真值一律
    # 由详情页回查网络得到（见 requestCollectType），同步时那次清理
    # 仍然保留（那次缓存是刚拉的全量，判据成立）。

    # ---------- 配置 ----------
    def set_display_mode(self, mode: str) -> None:
        """由 QmlApp 在启动 / 设置保存后调用。"""
        mode = mode if mode in (DISPLAY_FLAT, DISPLAY_GROUPED) else DISPLAY_FLAT
        if mode != self._display_mode:
            self._display_mode = mode
            self.reload()

    # ---------- 刷新 ----------
    @Slot()
    def reload(self) -> None:
        """标记缓存失效并通知 QML 重新取数。"""
        self._dirty = True
        self._inprogress_dirty = True
        self._eps_dirty = True
        self._tags_dirty = True
        self.subjectsChanged.emit()
        self.inProgressChanged.emit()
        self.watchedEpsChanged.emit()
        # 扫描 / 手动匹配都会写 tag（scanner.py、match.py），海报墙的筛选
        # 目录要跟着变 —— 否则"新增了动漫却筛不出来"。
        self.tagsBySubjectChanged.emit()
        # **集数列表也要通知**
        #
        # `episodes(subject_id)` 是一个普通 Slot，不是 Property —— 它没有
        # 任何可追踪依赖，QML 只在 `DetailPage.load()` 里主动调一次。于是
        # 扫描（尤其新的"下载完自动扫描"）把新集写进库之后，**已经打开的
        # 详情页仍然显示旧列表**，非要退出去再进来才会重新 load。
        self.episodesChanged.emit()

    @Slot()
    def reloadInProgress(self) -> None:
        """仅刷在看列表（在线拉取完成后由 InProgressBridge 调用）。"""
        self._inprogress_dirty = True
        self.inProgressChanged.emit()

    # ---------- Bangumi 收藏列表（阶段 7）----------
    @Property("QVariantList", notify=inProgressChanged)
    def inProgress(self) -> list[dict]:
        """Bangumi「在看」收藏 ＋ 本地手动标成「在看」的条目。

        服务端那部分来自 `inprogress_cache`（由 InProgressBridge 写入），
        本地那部分来自 `subjects.collect_type`（见 `_load_inprogress` 末尾
        的合并段）—— 合并是为了让**没配 Token / 没匹配 Bangumi** 的用户
        也能在用选择器标了「在看」之后，真的在这一页看到那部番。

        > 命名说明：表名/属性名一直是 F18 的 `inprogress*`，**语义从一开始就是
        > 「在看」**，中途一度改成「看过」，现在改回本意（`collect_type = 3`）——
        > 导航栏那一项也一直写着「在看」。
        >
        > **只取「在看」**：同一张表里还存着「看过」的番（那是给动态页当候选
        > 用的，见 `InProgressBridge`）。「看过」往往上百部，堆在本页既长又
        > 没用；它们的时间线在「动态」页里更合适。

        缓存表字段：bangumi_id / name / name_cn / cover_url /
        ep_status / total_eps / collect_type / updated_at /
        collection_updated_at。
        这里额外补两个 QML 侧要用的字段：
          - `localSubjectId`：本地是否有对应条目（用于「播放/详情」按钮）
          - `coverUrl`：本地缓存的封面（`cover_path`）优先，否则在线 URL

        **`updatedAt` 对外给的是 `collection_updated_at`**（Bangumi 的收藏
        修改时间），而不是表里的 `updated_at`（那是缓存写入时刻，整批相同，
        对界面没有意义）。老库该列为空时回落到缓存写入时间。
        """
        return self._inprogress_items()

    def _inprogress_items(self) -> list[dict]:
        """在看列表（带缓存，`inProgress` 与 `inProgressMeta` 共用同一份）。"""
        if self._inprogress_dirty:
            self._inprogress_cache = self._load_inprogress()
            self._inprogress_dirty = False
        return self._inprogress_cache

    def _load_inprogress(self) -> list[dict]:
        try:
            # 只取「在看」（type=3）：缓存表里两类都存着（「看过」是给动态页
            # 当候选用的，见 InProgressBridge），本页显式过滤 —— 页面语义是
            # "我正在追的番"，与导航栏标签一致。
            items = self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DOING)
        except Exception as e:
            log.exception("读取在看缓存失败: %s", e)
            return []

        # 本地快照的"真实状态"（bangumi_id → collect_type）：**本地比缓存新**
        # （用户一点就改写，同步时也会跟着对齐），所以缓存里标着「在看」、
        # 本地却已经改成「想看/看过」的那些要**立刻**从本页去掉，
        # 而不是等下一次同步。值 0 是"查过、确实没收藏"= 不知道 → 不据此过滤。
        try:
            local_types = self._db.subject_collect_types()
        except Exception as e:
            log.exception("读取本地收藏状态失败: %s", e)
            local_types = {}
        items = [
            it for it in items
            if local_types.get(int(it.bangumi_id or 0), COLLECT_TYPE_DOING)
            in (0, COLLECT_TYPE_DOING)
        ]

        # 「上传」统计：**一次查全量**再按条目取（逐条查会变成 2N 次查询）
        try:
            pend_map = self._db.pending_uploads()
            block_map = self._db.blocked_upload_counts()
        except Exception as e:
            log.exception("统计待补传失败: %s", e)
            pend_map, block_map = {}, {}

        out: list[dict] = []
        for it in items:
            # 本地关联：有同 bangumi_id 的条目就能一键跳详情
            local_id = 0
            try:
                local_id = self._db.find_subject_by_bangumi_id(it.bangumi_id) or 0
            except Exception:
                pass

            local_cover = ""
            if local_id:
                try:
                    s = self._db.get_subject(local_id)
                    local_cover = self._cover_file_url(s.cover_path or "", s.id) if s else ""
                except Exception:
                    pass

            # 没有本地缓存就**不回落在线 URL**。
            # 只用本地 URL，拿不到就交给 QML 显示占位底色（原本就有的
            # 空态），不再让界面层承担联网职责。

            # ---- 用**本地已看的最大集号**修正进度----
            #
            # 为什么取 `max(缓存, 本地最大集号)`：
            # 两者各有覆盖不到的地方 —— 用户在**手机/网页**上标了第 10 集，
            # 本地可能一条 watched 都没有（没在这台电脑上看过）；反过来
            # 刚在本地看完、还没同步到 Bangumi 时，缓存又落后。取大的那个
            # 才同时兼顾两种情况，且进度只会"前进"不会"回退"
            # （回退会被用户当成 bug）。
            #
            # **但两者必须同一口径才谈得上取 max**：
            #     ep_status          Bangumi 收藏接口的计数 = **本季**第几集
            #                        （1~total_eps，这里正是 17）
            #     本地 ep_index      扫描时写入的**全系列累计序号**
            #                        （跨季连续编号的番从 73 起，这里最大 89）
            # 直接 max 就把 89 当成"本季第 89 集"显示了，而分母是本季的 24。
            # 判据：本地那个数 **≤ `total_eps`** 才可能是本季口径（正片编号
            # 不会超过本季集数）；超过就说明是另一套编号，宁可不修正 ——
            # 退化成显示缓存的 `ep_status`，最多"慢一集"，不会显示成假的。
            #
            # 注意这里**不改库**：只修正本次返回给界面的值。缓存该由
            # 「刷新」重建，不要在只读路径上偷偷写数据。
            #
            # **位置要求**：必须在下面 `_next_episode` 之前算出来 ——
            # 它也要用这个值（两处口径必须一致，否则会出现"进度显示 9
            # 但「下一集」还是第 9 集"的自相矛盾）。
            ep_status = int(it.ep_status or 0)
            if local_id:
                try:
                    local_max = self._db.max_watched_ep_index(local_id)
                    total = int(it.total_eps or 0)
                    if total <= 0 or local_max <= total:
                        ep_status = max(ep_status, local_max)
                    elif local_max > ep_status:
                        log.debug(
                            "本地最大集号 %s 超出本季集数 %s，判为另一套编号，"
                            "进度仍用 Bangumi 的 %s（bgm=%s）",
                            local_max, total, ep_status, it.bangumi_id)
                except Exception as e:
                    log.warning("读取本地已看最大集号失败 subject_id=%s: %s",
                                local_id, e)

            next_ep_id, next_ep_hint = self._next_episode(local_id, ep_status)
            # 「上传」按钮：本地看过但 Bangumi 未标的集数（0 = 无事可做），
            # 以及本地看过却没有 bangumi_ep_id、压根传不了的集数
            pending_up = len(pend_map.get(local_id, []))
            blocked_up = int(block_map.get(local_id, 0))

            out.append({
                "bangumiId": int(it.bangumi_id or 0),
                "name": it.name or "",
                "nameCn": it.name_cn or "",
                "title": it.name_cn or it.name or "",
                # 只用本地缓存 URL（见上方说明：不回落在线的 lain.bgm.tv，
                # 否则 QML 引擎会自己联网取图并刷超时错误）
                "coverUrl": local_cover,
                # 已修正的进度（见上方 max(缓存, 本地) 的说明）。
                # **必须用这个变量**，不要再写 `int(it.ep_status or 0)` ——
                # 否则页面上的进度条与标题又会回到旧值。
                "epStatus": ep_status,
                "totalEps": int(it.total_eps or 0),
                # 2 = 看过（当前唯一使用的类型，见 InProgressBridge）
                "collectType": int(it.collect_type or 2),
                # 收藏的最后修改时间（见上方说明：不是缓存写入时刻）
                "updatedAt": it.collection_updated_at or it.updated_at or "",
                "localSubjectId": local_id,
                "inLibrary": local_id > 0,
                "nextEpisodeId": next_ep_id,
                "nextEpisodeHint": next_ep_hint,
                "pendingUpload": pending_up,
                "blockedUpload": blocked_up,
            })

        # ---- 本地标记的「在看」（没连 Bangumi 的那些）----
        #
        # 没配 Token、或条目压根没匹配到 Bangumi 的用户也能在详情页手动标
        # 「在看」；这些条目不在收藏缓存里（那张表是服务端收藏的镜像），
        # 只有 subjects.collect_type 记着。不补进来，"我在看、但没连
        # Bangumi"的番就永远不出现在这一页。
        #
        # 排序：**追加在末尾、按条目名排**。不掺进上面那批的排序里 ——
        # 它们的 `updatedAt` 是"Bangumi 收藏修改时间"，而本地条目只有
        # `subjects.updated_at`（元数据变更时间），拿两种时间混排是假顺序。
        listed = {int(it.get("bangumiId") or 0) for it in out}
        try:
            locals_ = self._db.subjects_by_collect_type(COLLECT_TYPE_DOING)
        except Exception as e:
            log.exception("读取本地「在看」条目失败: %s", e)
            locals_ = []
        locals_.sort(key=lambda s: (s.name_cn or s.name or ""))
        for s in locals_:
            if int(s.bangumi_id or 0) and int(s.bangumi_id) in listed:
                continue                 # 缓存里已经有这一部（那条更权威）
            local_id = int(s.id)
            # 进度看**本地已看集数**：本地条目没有 Bangumi 的 ep_status
            # 可看，而这一列正是详情页/订阅页同一口径（见 count_locally_watched_eps）
            try:
                ep_status = self._db.count_locally_watched_eps(local_id)
            except Exception:
                ep_status = 0
            next_ep_id, next_ep_hint = self._next_episode(local_id, ep_status)
            out.append({
                "bangumiId": int(s.bangumi_id or 0),
                "name": s.name or "",
                "nameCn": s.name_cn or "",
                "title": s.name_cn or s.name or "",
                "coverUrl": self._cover_file_url(s.cover_path or "", local_id),
                "epStatus": int(ep_status),
                "totalEps": int(s.total_eps or 0),
                "collectType": COLLECT_TYPE_DOING,
                "updatedAt": s.updated_at or "",
                "localSubjectId": local_id,
                "inLibrary": True,
                "nextEpisodeId": next_ep_id,
                "nextEpisodeHint": next_ep_hint,
                "pendingUpload": len(pend_map.get(local_id, [])),
                "blockedUpload": int(block_map.get(local_id, 0)),
            })
        return out

    @Slot(result="QVariantList")
    def pendingUploads(self) -> list[dict]:
        """「本地看过、Bangumi 未标」的**逐集**清单 —— 动态页「上传」小窗的数据源。

        **一行 = 一集**（小窗里勾选的就是"这一集"），字段：
            {episodeId, bangumiEpId, subjectId, title, epIndex, epTitle}
        `title` 是动漫名 —— 小窗里与集名一起显示成「碧蓝之海 第三季 · EP7 妈妈」，
        否则用户根本看不出待传的是哪一集 ✗。

        判据统一由 `Database.pending_uploads()` 给出 —— 与实际上传时的筛选
        **是同一个查询** ✓。
        """
        try:
            pend_map = self._db.pending_uploads()
        except Exception as e:
            log.exception("统计待补传失败: %s", e)
            return []
        out: list[dict] = []
        for sid, eps in pend_map.items():
            try:
                subj = self._db.get_subject(int(sid))
            except Exception:
                subj = None
            if subj is None or not subj.bangumi_id:
                continue            # 没匹配到 Bangumi 的传不了，不列
            title = subj.name_cn or subj.name or "（未命名条目）"
            for e in eps:
                out.append({
                    "episodeId": int(e["episode_id"]),
                    "bangumiEpId": int(e["bangumi_ep_id"]),
                    "subjectId": int(sid),
                    "title": title,
                    "epIndex": float(e["ep_index"] or 0),
                    "epTitle": e.get("ep_title") or "",
                })
        # 按动漫名 + 集号排：同一部的待传集挨在一起，便于逐部核对
        out.sort(key=lambda x: (x["title"], x["epIndex"]))
        return out

    @Slot(result=int)
    def blockedUploadCount(self) -> int:
        """本地看过但**没有 Bangumi 集号**、无法补传的集数（小窗里提示用）。
        """
        try:
            return sum(self._db.blocked_upload_counts().values())
        except Exception as e:
            log.exception("统计无法补传的集数失败: %s", e)
            return 0

    def _next_episode(self, subject_id: int, ep_status: int) -> tuple[int, str]:
        """「下一集」对应的本地集 ID 与**播不了时的原因**；可播时原因为空串。

        **"看到第几集"必须用 `watched_episodes` 的集号，不能用 `ep_status`**
        —— 这两个数走的是**两套不同的编号**：

            ep_status        Bangumi 收藏接口的 `ep_status` = 该条目**本季**
                             第几集（1~24）
            watched_episodes 逐集记录的 `ep_index` = Bangumi 集数的 `sort`
                             = **全系列累计序号**（73~96）
            本地 episodes     与 `sort` 同口径（那正是扫描时写入的值）

        拿 `ep_status=16` 去和本地 `ep_index`（73~93）比，永远对不上
        （`16+1=17` 不在 73~93 里），于是退回"第一个 > 17 的" → 永远给
        **第一集 73**。

        所以先用 `watched_episodes` 取"本地/远端已看的最大集号"（与本地同
        口径），它比 `ep_status` 更精确（逐集记录，而 `ep_status` 只是个
        计数器）；只有在没有任何逐集记录时才退回 `ep_status`（那时本地若
        恰好也是 1~N 编号，两者口径一致，能对上）。

        规则：优先取 `ep_index == 已看到 + 1` 的那一集（"接着看"的那集）；
        编号对不上（本地缺集/续篇从中间编号）时退回"第一集 ep_index 大于
        已看到的"；都没有则返回 0，并给出一句能解释清楚的原因 ——
        界面上按钮**不隐藏**，点不动时把原因报到状态栏。

        为什么要区分原因：三种"播不了"的处置完全不同 ——
        没入库（去入库）、本地已看到最新（正常，无需做什么）、
        集号数据坏了（是扫描解析的问题，得回去修）。
        """
        if subject_id <= 0:
            return 0, "未入库，无法播放"
        try:
            # 只考虑有本地文件的集（没文件没法播）
            eps = [e for e in self._db.list_episodes(subject_id)
                   if (e.file_path or "").strip()]
        except Exception as e:
            log.exception("查找下一集失败: %s", e)
            return 0, "读取本地集数失败（详见日志）"
        if not eps:
            return 0, "本地没有可播放的剧集文件"

        # 已看到的集号（与本地 ep_index 同口径）。见方法说明：优先逐集记录。
        seen = self._watched_max_index(subject_id)
        source = "watched_episodes"
        if seen <= 0:
            # 没有任何逐集记录（没同步过 / 在别处只改了收藏状态）→ 退回
            # `ep_status`。此时若本地是 1~N 编号，两者口径一致、能对上。
            seen = int(ep_status or 0)
            source = "ep_status"

        want = float(seen) + 1
        for e in eps:                       # list_episodes 已按 ep_index 升序
            if abs(float(e.ep_index or 0) - want) < 1e-6:
                return int(e.id), ""
        later = [e for e in eps if float(e.ep_index or 0) > want]
        if later:
            nxt = float(later[0].ep_index or 0)
            # **日志分两种情形，别一律说"缺集"**，
            # 看着像出错，其实完全正常）。
            #
            # 成因不同、用户该做的事也不同：
            #   ① **本地集号起点就不是 1**（续篇/跨篇章连续编号，如这部
            #      的 78~81）—— `want=1` 本就不该存在，取第一个可播的是
            #      **正确行为**，不该报警。
            #   ② 本地**缺中间某一集**（真有 1、3 却没有 2）—— 那才是
            #      值得记一笔的（用户可能要去补那一集）。
            # 判据：`want` 比本地最小集号还小 → 情形 ①。
            first = float(eps[0].ep_index or 0)
            if want < first:
                log.debug("「下一集」按连续编号取第一集：本地从第 %g 集起编"
                          "（进度 %g 不在本地编号内，subject_id=%s，"
                          "进度取自 %s）", first, want, subject_id, source)
            else:
                log.info("「下一集」本地缺第 %g 集，跳过后取第 %g 集"
                         "（subject_id=%s，进度取自 %s）",
                         want, nxt, subject_id, source)
            return int(later[0].id), ""
        # 走到这里说明本地没有任何"第 want 集及以后"的集。两种成因要分开说：
        if len({float(e.ep_index or 0) for e in eps}) == 1:
            # 所有集号一模一样 → 扫描时标题没解析出来
            return 0, ("本地集号异常（%d 集全是第 %g 集，扫描解析可能失败），"
                       "无法判断下一集" % (len(eps), float(eps[0].ep_index or 0)))
        return 0, ("本地没有更靠后的集了（已看到第 %d 集，本地共 %d 集）"
                   % (int(seen), len(eps)))

    def _watched_max_index(self, subject_id: int) -> int:
        """该条目**已看过的最大集号**（`sort` 口径，取本地与远端逐集记录的较大者）。

        **只查逐集记录、不带 `ep_status` 兜底**（与 `max_watched_ep_index`
        取的是同一个最大值，区别在"没有逐集数据时返回什么"）：调用方要能
        分辨"到底有没有逐集数据"，好在没有时切换口径。

        返回 0 = **一条观看记录都没有**（返回 0 是可信的，见
        `Database.max_ep_index_from_watched`）。
        """
        try:
            return int(self._db.max_ep_index_from_watched(subject_id))
        except Exception as e:
            log.warning("读取已看最大集号失败 subject_id=%s: %s", subject_id, e)
            return 0

    # ---------- F20：集级观看记录（动态页时间线）----------
    @Property("QVariantList", notify=watchedEpsChanged)
    def watchedEpisodes(self) -> list[dict]:
        """集级观看记录，按标记时间倒序（动态页 merged 模式的数据源）。

        数据来自 `watched_episodes` 表，由 InProgressBridge 的第二阶段
        拉取写入（见 §5.12.11.5）。字段与 `timeline()` 对齐，
        便于动态页复用同一套聚合逻辑。
        """
        if self._eps_dirty:
            self._eps_cache = self._load_watched_eps()
            self._eps_dirty = False
        return self._eps_cache

    def _load_watched_eps(self) -> list[dict]:
        try:
            # 上限 5000：全量同步后本表约 1000+ 条（旧上限 800 会截断历史）。
            # 动态页的"加载更多"在 QML 侧切片，所以这里一次给全。
            rows = self._db.list_watched_episodes(limit=5000)
        except Exception as e:
            log.exception("读取集级观看记录失败: %s", e)
            return []
        out: list[dict] = []
        for r in rows:
            # 本地关联：优先用表里存的 subject_id，其次按 bangumi_id 现查
            local_id = int(r.get("subject_id") or 0)
            if not local_id:
                try:
                    local_id = self._db.find_subject_by_bangumi_id(
                        int(r.get("bangumi_id") or 0)) or 0
                except Exception:
                    pass
            out.append({
                "episodeId": int(r.get("bangumi_ep_id") or 0),
                "subjectId": local_id,
                "subjectName": r.get("subject_name") or "",
                "epIndex": float(r.get("ep_index") or 0),
                "epTitle": r.get("ep_name") or "",
                "watchedAt": r.get("watched_at") or "",
                "bangumiId": int(r.get("bangumi_id") or 0),
                "inLibrary": local_id > 0,
                "isInProgress": False,   # 复用动态页既有字段
                "isBangumi": True,        # 标记来源是 Bangumi（非本地播放记录）
            })
        return out

    @Slot()
    def reloadWatchedEpisodes(self) -> None:
        """标记集级记录缓存失效并通知 QML。"""
        self._eps_dirty = True
        self.watchedEpsChanged.emit()

    @Slot(result="QVariantMap")
    def watchedEpisodesMeta(self) -> dict:
        """集级记录的元信息（条数 + 数据年龄），页面标题区展示用。"""
        try:
            count = self._db.count_watched_episodes()
        except Exception:
            return {"count": 0, "ageSeconds": -1}
        return {"count": count, "ageSeconds": -1}

    @Property("QVariantMap", notify=inProgressChanged)
    def inProgressMeta(self) -> dict:
        """收藏页的元信息（缓存年龄 + 条数），页面标题区展示用。

        条数只算「在看」—— 与 `inProgress` 的过滤保持一致，
        否则页头数字会比列表实际条数大（差额是"看过"的那批，上百部）。
        **直接数页面那一份**（`_inprogress_items`）而不是再查一次缓存表：
        那份里还含"本地标记的「在看」"（没连 Bangumi 的条目），
        另查一遍会少算它们，页头数字与列表对不上。

        **必须是带 `notify` 的 Property，不能是 `@Slot`**（改过一处）：
        QML 里原先写 `library.inProgressMeta()` —— 那是"在绑定里调一次函数"，
        没有任何可追踪的依赖，于是**只在页面创建时算一次**，此后列表怎么变
        页头数字都不动。改成 Property 后 QML 用 `library.inProgressMeta`，
        列表一变就重算。
        """
        try:
            age = self._db.inprogress_cache_age()
            count = len(self._inprogress_items())
        except Exception as e:
            log.exception("读取在看缓存元信息失败: %s", e)
            return {"count": 0, "ageSeconds": -1, "stale": True}
        stale = age is None or age > STALE_CACHE_SECONDS
        return {
            "count": count,
            "ageSeconds": -1 if age is None else int(age),
            "stale": stale,
        }

    @Slot(int, result=bool)
    def needsRemoteRefresh(self, local_subject_id: int) -> bool:
        """播放结束后要不要**联网**重拉一次收藏数据（`qml_app` 的收尾会问）。

        两个条件满足其一即可：

        ① **页面自己都标着"数据较旧"**（缓存超过 `STALE_CACHE_SECONDS`）——
           页头写着「建议刷新」却只提示不做事，用户点不点都难说；播放结束
           正是"数据刚变过"的时刻，顺手拉一次最自然。

        ② **这一条的进度只能来自 Bangumi**（跨篇章编号）。
           `_load_inprogress` 遇到"本地集号超出本季集数"会**故意不改**进度
           （宁可不修正也不显示假的） —— 用户看到的结论就是"没同步"。这类条目本地推不出来，
           只能回查（或多等一次手动刷新）。

        **不做单条回查**：那要多一个 worker + 单行缓存更新 + 用户名解析；
        整批拉取本来就只有十几条请求、跑在后台线程，且**顺带**让页头的
        "数据较旧"消失。真嫌请求多再加单条优化也不迟。
        """
        # 没配 Token 就别自动拉：拉也是 401，只会在状态栏刷一条错
        # （用户压根没用 Bangumi 的话，那纯属打扰）
        if not getattr(self._api, "has_token", False):
            return False

        try:
            age = self._db.inprogress_cache_age()
        except Exception as e:                 # pragma: no cover - 防御性
            log.warning("读取收藏缓存年龄失败: %s", e)
            age = None
        if age is None or age > STALE_CACHE_SECONDS:
            return True

        if local_subject_id <= 0:
            return False
        try:
            s = self._db.get_subject(int(local_subject_id))
            if s is None:
                return False
            total = int(s.total_eps or 0)
            if total <= 0:
                return False        # 集数未知 → 与 _load_inprogress 同口径：用本地值
            return self._db.max_watched_ep_index(int(local_subject_id)) > total
        except Exception as e:                 # pragma: no cover - 防御性
            log.warning("判断是否需回查收藏数据失败 subject_id=%s: %s",
                        local_subject_id, e)
            return False

    # ---------- 条目列表 ----------
    @Property("QVariantList", notify=collectTypeChanged)
    def collectOptions(self) -> list[dict]:
        """收藏状态的可选项：`[{value: 1, text: "想看"}, ...]`（**唯一来源**）。

        详情页那排状态按钮、海报墙筛选面板的「收藏状态」栏都从这里取。
        顺序取 `COLLECT_TYPE_NAMES` 的定义顺序（想看 → 看过 → 在看 → 搁置
        → 抛弃），与 Bangumi 官网一致。

        **为什么由后端给而不是各 QML 各写一份**：原先只有详情页用，写死
        在 QML 里还能接受（那里也留了注释说明）。现在筛选栏也要用，
        两处各写一份就等于同一个枚举有 3 份字面量（QML×2 + 后端），
        哪天改一个漏一个，表现是"筛出来的和详情页选的对不上"这种
        很难发现的错。这里从 `COLLECT_TYPE_NAMES` 派生，只有一处定义。
        """
        # notify 用 collectTypeChanged：它是"某个条目的状态变了"的通知，
        # 与这份**静态**选项列表无关 —— 但 Property 需要挂一个信号才能
        # 在 QML 里正常绑定（挂 subjectsChanged 之类同理）。
        # 选项本身不会变，这个 notify 事实上永远不会触发，无害。
        return [{"value": int(v), "text": t}
                for v, t in sorted(COLLECT_TYPE_NAMES.items())]

    @Property("QVariantMap", notify=collectTypeChanged)
    def collectTypes(self) -> dict:
        """`本地主键 → 收藏状态`（海报墙筛选用，随 `collectTypeChanged` 实时更新）。

        **为什么要单独给一份、而不是让墙去读 `subjects` 里那个字段**：
        `subjects` 是长生命周期的缓存（重取会重建整墙卡片，见
        `_on_collect_type_done` 的说明），收藏状态变了它不一定重取 ——
        于是出现"详情页已经显示「看过」、筛选里还算「未标记」，
        要重扫才好"。这份映射只有两列、重建极廉价，
        且挂在 `collectTypeChanged` 上，状态一变就跟着变。

        key 用字符串：QML 里 JS 对象的键一律是字符串（`map[item.id]` 会被
        自动转成字符串索引，取得到）。
        """
        try:
            data = self._db.collect_types_by_subject_id()
        except Exception as e:
            log.exception("读取收藏状态映射失败: %s", e)
            return {}
        return {str(k): int(v) for k, v in data.items()}

    @Property("QVariantList", notify=subjectsChanged)
    def subjects(self) -> list[dict]:
        """全部条目（海报墙用）。

        `display_mode == grouped` 时按 series_name 聚合为「系列卡片」，
        字段与单条一致，额外带 `isGroup` / `childCount` / `childIds`。
        """
        if self._dirty:
            self._subjects_cache = self._load_subjects()
            self._dirty = False
        return self._subjects_cache

    def _load_subjects(self) -> list[dict]:
        try:
            rows = self._db.list_subjects()
        except Exception as e:
            log.exception("读取条目失败: %s", e)
            return []
        if self._display_mode == DISPLAY_GROUPED:
            return self._build_groups(rows)
        return [self._subject_to_dict(s) for s in rows]

    @Property("QVariantMap", notify=tagsBySubjectChanged)
    def tagsBySubject(self) -> dict:
        """条目 id → tag 名列表（海报墙 tag 筛选的数据源）。

        键是**字符串**形式的 subject_id：QVariantMap 的键只能是字符串，
        这里显式转好过让 QML 侧猜（QML 里按 `String(item.id)` 取）。

        **为什么不做进 `subjects` 的条目字典里**：tag 是**独立变化**的数据 ——
        ① 每次进详情页都会异步补拉该条目的 tag（见 requestTagFetch），
        ② 手动匹配、重新扫描也会重写 tag。
        若塞进 `subjects`，上面每件事都得发 subjectsChanged，而 Repeater 的
        model 一变就会**销毁重建全部卡片**。单独一个 Property
        让筛选目录刷新时海报卡片毫发无损，也让新增 tag 能实时出现在面板里。
        """
        if self._tags_dirty:
            self._tags_map = self._load_tags_map()
            self._tags_dirty = False
        return self._tags_map

    def _load_tags_map(self) -> dict:
        """全库 tag（**已剔除制作公司 tag**，见下）。

        公司 tag（京阿尼 / MAPPA / A-1Pictures…）在这里就摘掉，不让它们进
        海报墙的标签筛选 —— 公司由 subjects 的 `studio` 字段在"制作公司"栏
        单独成栏，再以 tag 形式散落在"其他"栏里就是同一个东西出现两次。
        判定用 `bangumi_api.is_studio_name`（带词表，见那里的说明）。

        注意：**列表页的 tag 展示不受影响**（详情页走 `subjectTags()`），
        这里只是筛选目录的取数口径。
        """
        try:
            raw = self._db.all_subject_tags()
        except Exception as e:
            log.exception("读取条目 tag 映射失败: %s", e)
            return {}
        return {str(sid): [n for n in names if not is_studio_name(n)]
                for sid, names in raw.items()}

    # ---------- 单条 ----------
    @Slot(int, result="QVariantMap")
    def subject(self, subject_id: int) -> dict:
        """按本地主键取单条详情。"""
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {}
        return self._subject_to_dict(s) if s else {}

    @Slot(int, result=bool)
    def openSubjectPage(self, subject_id: int) -> bool:
        """在系统默认浏览器里打开该条目的 Bangumi 页面。

        地址形如 `https://bangumi.tv/subject/<bangumi_id>`（详情页标题右侧
        的链接按钮用）。返回是否成功发起打开。

        **为什么用本地主键而不是直接传 URL**（两条理由）：
          ① QML 不该知道站点地址 —— 域名将来若变（bangumi.tv / bgm.tv），
             改一处即可；
          ② 传 URL 等于给 QML 开了"打开任意链接"的口子，这里只放行
             "本条目在官方站点的页面"这一种语义。

        没有 bangumi_id 的条目（pending / 未匹配）直接返回 False，
        QML 侧据此不显示按钮 —— 避免点开一个 404 页面。
        """
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return False
        bangumi_id = int(s.bangumi_id or 0) if s else 0
        if bangumi_id <= 0:
            log.info("条目 %s 未关联 Bangumi，无法打开网页", subject_id)
            return False
        url = f"https://bangumi.tv/subject/{bangumi_id}"
        ok = QDesktopServices.openUrl(QUrl(url))
        log.info("打开 Bangumi 页面：%s（%s）", url, "成功" if ok else "失败")
        return bool(ok)

    @Slot(int, result="QVariantList")
    def episodes(self, subject_id: int) -> list[dict]:
        """条目的集数列表（详情页用），按 ep_index 升序。"""
        try:
            eps = self._db.list_episodes(subject_id)
        except Exception as e:
            log.exception("读取集数 %s 失败: %s", subject_id, e)
            return []
        return [self._episode_to_dict(e) for e in eps]

    @Slot(int, result="QVariantList")
    def series_siblings(self, subject_id: int) -> list[dict]:
        """同系列的其他季（详情页「同系列」切换用），按**放送时间**升序。

        **以 tag 里的放送日期为主键**。
        Bangumi 会给每个条目打一个形如「2015年7月」的 tag，
        它是"第几季"的**客观依据** —— 第一季的日期一定最早，
        不依赖标题里有没有写季数。取不到日期时才回退到季数，再兜底字典序。

        排序键 = (有无日期, 日期, 有无季数, 季数, 标题)，逐级兜底：
          - 有日期的排前面，按日期升序（最早的季在最前）
          - 无日期但有季数的次之
          - 两者都没有的（剧场版/OVA/外传）排最后，按名称保持稳定
        """
        try:
            cur = self._db.get_subject(subject_id)
            if cur is None or not (cur.series_name or "").strip():
                return []
            series = cur.series_name.strip()
            siblings = [
                s for s in self._db.list_subjects()
                if (s.series_name or "").strip() == series and s.id != subject_id
            ]
            # 一次取全量 tag（海报墙也在用这个接口，避免逐条查库）
            tags_map = self._db.all_subject_tags()

            def sort_key(s) -> tuple:
                title = s.name_cn or s.name or ""
                tags = tags_map.get(s.id) or []
                date_key = _air_date_key(tags)
                season = extract_season(title, "all")
                return (
                    0 if date_key is not None else 1,
                    date_key if date_key is not None else 0,
                    0 if season is not None else 1,
                    float(season) if season is not None else 0.0,
                    title,
                )

            siblings.sort(key=sort_key)
            return [self._subject_to_dict(s) for s in siblings]
        except Exception as e:
            log.exception("读取同系列失败: %s", e)
            return []

    @Slot(int, result="QVariantList")
    def timeline(self, limit: int = 200) -> list[dict]:
        """观看动态时间线（动态页用）。"""
        try:
            entries = self._db.list_watched_timeline(limit=limit)
        except Exception as e:
            log.exception("读取时间线失败: %s", e)
            return []
        return [
            {
                "episodeId": e.episode_id,
                "subjectId": e.subject_id,
                "subjectName": e.subject_name or "",
                "epIndex": float(e.ep_index or 0),
                "epTitle": e.ep_title or "",
                "watchedAt": e.watched_at or "",
            }
            for e in entries
        ]

    @Slot(int, result="QVariantMap")
    def stats(self, subject_id: int) -> dict:
        """条目统计：已看 / 总数。海报卡片副标题用。"""
        try:
            eps = self._db.list_episodes(subject_id)
        except Exception as e:
            log.exception("统计条目 %s 失败: %s", subject_id, e)
            return {"watched": 0, "total": 0}
        return {
            "watched": sum(1 for e in eps if e.watched),
            "total": len(eps),
        }

    # ---------- 海报管理（更换 / 恢复原版）----------
    #
    # 设计：**不新建数据库字段**。原版海报 = Bangumi 封面的本地缓存
    # （scanner / 手动匹配都经 cover_cache.cover_path_for() 写到
    # `covers/<sid>.<ext>`，路径由 subject_id + URL 扩展名唯一确定，可反推）；
    # 自定义海报是**另存**的 `covers/<sid>_custom.<ext>`，更换只是把
    # cover_path 指过去。因此恢复 = 指回原路径（零下载、原文件不动）。

    @Slot(int, result="QVariantMap")
    def coverInfo(self, subject_id: int) -> dict:
        """当前海报状态（更换海报小窗的数据源）。

        返回字段：
            coverUrl     —— 当前海报的 file:// URL（带 `?v=` 版本号）
            isCustom     —— **当前用的是不是用户自定义的那张**
            hasOriginal  —— 有没有 Bangumi 原版可恢复（未匹配时没有）

        `isCustom` 的判定（四条，缺一都会误报，见下）：
          1. **根本没有封面**（`cover_path` 为空）→ `False`。
             旧实现只看"路径不相等"，而没封面时 `cover_path` 是空串，
             与任何原版路径都不等，于是无封面的条目被判成"自定义"，
             「恢复原版海报」按钮错误地可点。这不是自定义，是"还没有图"。
          2. `cover_path` **指向的文件已不存在**（缓存被清理、换过盘符、
             手工删过图）→ `False`，并按"没有自定义海报"处理。
             此时界面读不到图，用户点「恢复原版」才有意义；若判成
             `isCustom=True`，恢复按钮虽可点，但底下显示的是"破损的自定义
             海报"，语义是错的。
          3. 当前路径与「原版缓存文件」**是同一个文件** → `False`。
          4. 其余情况（指向一个真实存在的、非原版的文件）→ `True`。

        比较用 `Path.resolve()` 归一（大小写、分隔符、相对路径差异
        不该影响判定）。
        """
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {}
        if s is None:
            return {}

        original = self._original_cover_path(s)
        has_original = bool(s.cover_url)
        cur_str = (s.cover_path or "").strip()

        # 1. 没有封面
        cur_exists = False
        if cur_str:
            try:
                cur_exists = Path(cur_str).exists()
            except OSError:
                cur_exists = False
        if not cur_str or not cur_exists:
            # 兜底：文件没了但原版还在 → 直接把显示指向原版，
            # 免得详情页/海报墙留着一块空白（覆盖式写库，幂等）
            fallback_url = ""
            if has_original and original.exists():
                fallback_url = self._cover_file_url(str(original), s.id)
            return {
                "coverUrl": fallback_url,
                "isCustom": False,
                "hasOriginal": has_original,
                "missing": True,        # 供小窗提示"原海报文件已丢失"
            }

        # 3/4. 与原版比对
        cur = Path(cur_str)
        try:
            is_custom = cur.resolve() != original.resolve()
        except OSError:
            is_custom = str(cur) != str(original)

        return {
            "coverUrl": self._cover_file_url(cur_str, s.id),
            "isCustom": is_custom,
            "hasOriginal": has_original,
            "missing": False,
        }

    @Slot(result=str)
    def pickImage(self) -> str:
        """选择本地图片（自定义海报）。取消返回空串。"""
        path, _ = QFileDialog.getOpenFileName(
            None, "选择海报图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.bmp *.gif);;所有文件 (*)",
        )
        return path or ""

    @Slot(int, str, result="QVariantMap")
    def setCustomCover(self, subject_id: int, image_path: str) -> dict:
        """把用户选的图片设为该条目的海报。"""
        src = Path(image_path or "")
        if not src.exists():
            return {"ok": False, "message": "图片文件不存在"}
        # 后缀不可信（用户可能选到改名的非图片），实际解码一次再收
        if QImage(str(src)).isNull():
            return {"ok": False, "message": "无法读取该图片文件"}

        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {"ok": False, "message": "读取条目失败（详见日志）"}
        if s is None:
            return {"ok": False, "message": "条目不存在"}

        ext = src.suffix.lower() or ".jpg"
        dst = covers_dir() / f"{subject_id}_custom{ext}"
        try:
            if src.resolve() != dst.resolve():      # 重复选同一张时跳过自拷贝
                shutil.copyfile(src, dst)
            # 换了扩展名后旧的自定义文件会残留，顺手清掉（保留刚写入的）
            self._remove_custom_files(subject_id, keep=dst)
        except Exception as e:
            log.exception("复制自定义海报失败：《%s》（subject_id=%s）",
                          self._subject_label(s), subject_id)
            return {"ok": False, "message": "复制图片失败：%s" % e}

        try:
            self._db.set_cover_path(subject_id, str(dst))
        except Exception as e:
            log.exception("写入自定义海报失败：《%s》（subject_id=%s）",
                          self._subject_label(s), subject_id)
            return {"ok": False, "message": "写入失败：%s" % e}

        log.info("自定义海报已设置：《%s》（subject_id=%s）→ %s",
                 self._subject_label(s), subject_id, dst)
        self._notify_cover_changed(subject_id)
        return {"ok": True, "message": "海报已更换"}

    @Slot(int, result="QVariantMap")
    def restoreCover(self, subject_id: int) -> dict:
        """恢复 Bangumi 原版海报。

        正常情况只是把 cover_path 指回原版缓存文件（瞬时完成，不联网）；
        缓存被清理过才需要重新下载 —— 罕见路径，同步执行可接受
        （本类的设计原则是「轻操作直接同步」，见文件头注释）。
        """
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {"ok": False, "message": "读取条目失败（详见日志）"}
        if s is None:
            return {"ok": False, "message": "条目不存在"}
        if not s.cover_url:
            return {"ok": False, "message": "没有原版海报可恢复（条目未匹配 Bangumi）"}

        label = self._subject_label(s)
        original = self._original_cover_path(s)
        if not original.exists():
            # 缓存被清理过：按 **bangumi_id** 重新下载（与 scanner / match
            # 的命名保持一致，否则下次又会找不到 —— 见 _original_cover_path）
            log.info("原版海报缓存不存在，重新下载：《%s》（subject_id=%s）← %s",
                     label, subject_id, s.cover_url)
            try:
                original = download_cover(
                    s.bangumi_id or s.id, s.cover_url, timeout=10.0,
                    label=label)
            except Exception as e:
                log.warning("原版海报重新下载失败：《%s》（subject_id=%s）: %s",
                            label, subject_id, e)
            # download 失败时返回占位图路径而非抛异常，所以这里再查一次落盘结果
            if not original.exists():
                return {"ok": False, "message": "原版海报下载失败，请检查网络后重试"}

        try:
            self._db.set_cover_path(subject_id, str(original))
        except Exception as e:
            log.exception("恢复原版海报失败：《%s》（subject_id=%s）",
                          label, subject_id)
            return {"ok": False, "message": "写入失败：%s" % e}

        # 顺手清掉自定义海报文件。**放在写库之后**：先把 cover_path 指回
        # 原版（此时数据已一致），再删文件；万一删除失败也只是留下一个
        # 孤儿文件，不会出现"库里指着自定义、文件却没了"的坏状态。
        self._remove_custom_files(subject_id)

        log.info("已恢复原版海报：《%s》（subject_id=%s）→ %s",
                 label, subject_id, original)
        self._notify_cover_changed(subject_id)
        return {"ok": True, "message": "已恢复原版海报"}

    @Slot(int, result="QVariantMap")
    def deleteSubject(self, subject_id: int) -> dict:
        """从库中删除该条目（海报墙上随即消失）。

        **只删库里的记录，不动磁盘上的视频文件**（重要）：
        「添加动漫」是把已有目录**登记**进来，删除自然只该撤销登记。
        真去删文件是破坏性操作，误点一下就没了 —— 那种事必须由用户
        自己在资源管理器里做。返回消息里也明确写出这一点。

        删除范围见 `Database.delete_subject`（episodes + subject_tags +
        subjects，不依赖级联，因为 SQLite 未开外键）。另外顺手清掉该条目的
        自定义海报文件，避免 `covers/` 里留下永远用不上的孤儿图。

        返回：{ok: bool, message: str}（QML 侧转成提示条）
        """
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {"ok": False, "message": "读取条目失败（详见日志）"}
        if s is None:
            return {"ok": False, "message": "条目不存在（可能已被删除）"}

        label = self._subject_label(s)
        try:
            self._db.delete_subject(subject_id)
        except Exception as e:
            log.exception("删除条目失败：《%s》（subject_id=%s）", label, subject_id)
            return {"ok": False, "message": "删除失败：%s" % e}

        # 自定义海报文件（`covers/<sid>_custom.*`）随条目一起清掉。
        # 放在写库**之后**：失败了也只是留个孤儿文件，不会出现"库里没了、
        # 文件还在被引用"的坏状态。
        self._remove_custom_files(subject_id)

        log.info("已删除条目：《%s》（subject_id=%s，media files kept on disk）",
                 label, subject_id)

        # 通知海报墙重新取数（同既有做法：标记 dirty + 发信号）
        self.reload()
        return {"ok": True,
                "message": "已从库中删除「%s」（磁盘上的文件未删除）" % label}

    @staticmethod
    def _remove_custom_files(subject_id: int, keep: Optional[Path] = None) -> None:
        """删除该条目的自定义海报文件（`covers/<sid>_custom.*`）。

        `keep` 用于「重新选图」场景：新旧扩展名可能不同，留下新的、删掉旧的。
        """
        try:
            for old in covers_dir().glob(f"{subject_id}_custom.*"):
                if keep is not None and old == keep:
                    continue
                old.unlink(missing_ok=True)
                log.debug("已删除旧的自定义海报文件：%s", old)
        except OSError as e:
            log.warning("清理自定义海报文件失败 subject_id=%s: %s", subject_id, e)

    @staticmethod
    def _subject_label(s: Subject) -> str:
        """日志里用的条目名：中文名优先，退回原名，再退回占位。

        为什么要它：只看 `subject_id=8` 根本不知道是哪部动漫（见下方日志
        示例），排查"某张海报怎么被换掉了"时要回数据库查 id，很费事。
        名字可能为空（pending 条目），所以要有兜底，不能直接拼。
        """
        return (s.name_cn or s.name or "").strip() or "（未命名条目）"

    # ---------- 条目标签（详情页展示 + 编辑）----------
    @Slot(int, result="QVariantList")
    def subjectTags(self, subject_id: int) -> list[dict]:
        """该条目的 tag 列表（详情页展示与编辑的数据源）。

        返回项：`{ "name": str, "isApi": bool, "deleted": bool }`。
        `deleted=True` 的项**不参与展示**，只在编辑模式置灰可见。
        """
        try:
            return self._db.list_subject_tags(subject_id)
        except Exception as e:
            log.exception("读取条目标签失败 subject_id=%s: %s", subject_id, e)
            return []

    @Slot(int, "QVariantList", "QVariantList", result="QVariantMap")
    def saveSubjectTags(
        self,
        subject_id: int,
        api_states: list,
        user_names: list,
    ) -> dict:
        """保存详情页的 tag 编辑结果（「确定」按钮）。

        参数（都由 QML 组装）：
            api_states: `[{"name": str, "deleted": bool}]`
                接口 tag 的最终状态 —— 只更新删除标记；
            user_names: `[str]`
                用户 tag 的最终名单（顺序即展示顺序），整体替换。

        返回 `{ok, message}`，message 供状态栏展示。
        """
        try:
            # 清洗：去空白、去重（大小写不敏感 —— Bangumi tag 没有大小写
            # 区分的语义，"TV" 和 "tv" 应视为同一个）
            states: list[dict] = []
            seen: set[str] = set()
            for st in api_states or []:
                if not isinstance(st, dict):
                    continue
                name = str(st.get("name") or "").strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                states.append({"name": name, "deleted": bool(st.get("deleted"))})

            names: list[str] = []
            for n in user_names or []:
                name = str(n or "").strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                names.append(name)

            self._db.save_edited_tags(subject_id, states, names)
        except Exception as e:
            log.exception("保存条目标签失败 subject_id=%s: %s", subject_id, e)
            return {"ok": False, "message": "保存失败：%s" % e}

        log.info("条目标签已保存：subject_id=%s（api %s 项，user %s 项）",
                 subject_id, len(states), len(names))
        self.tagsChanged.emit(subject_id)
        return {"ok": True, "message": "标签已保存"}

    @Slot(int)
    def requestTagFetch(self, subject_id: int) -> None:
        """异步补拉该条目的前 10 个 tag（详情页进入时自动 / 按钮手动）。

        为什么是异步：进入详情页就会触发（存量条目自动补录），
        同步网络请求会把 UI 卡住 0.3~1s，不可接受。
        完成后经 tagsChanged 通知详情页重取，消息走 statusMessage。

        幂等：tag 与制作公司**都齐了**才跳过，缺哪样取哪样 ——
        已有 tag（含"全被用户删掉"的标记状态）时不会重复写 tag，
        这也保证用户删过的 tag 不会被重新拉回来。

        快速翻页时的并发治理：同一时刻只跑一个 worker，期间的请求
        只记下**最后一个** subject_id，当前完成后接续（防请求风暴）。
        """
        if self._tag_worker is not None and self._tag_worker.isRunning():
            self._tag_fetch_pending = int(subject_id)
            log.info("标签拉取进行中，%s 排队等待", subject_id)
            return
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return
        if s is None or not s.bangumi_id:
            return                      # 未匹配 Bangumi：无可拉取（QML 侧不会触发）
        need_tags = not self._db.has_subject_tags(subject_id)
        # 公司来自 infobox 的「动画制作」，只有一部分条目有 —— 取不到时
        # 这个条件恒为真，每次进详情页都会多试一次（极廉价，与 tag 同理）
        need_studio = not (s.studio or "").strip()
        if not need_tags and not need_studio:
            return                      # 都齐了（幂等）
        if self._api is None:
            self.statusMessage.emit("Bangumi 客户端未就绪，无法获取标签")
            return

        self.statusMessage.emit("正在获取标签…")
        self._start_tag_worker(int(subject_id), int(s.bangumi_id),
                               need_tags, need_studio)

    def _start_tag_worker(self, subject_id: int, bangumi_id: int,
                          need_tags: bool = True,
                          need_studio: bool = True) -> None:
        self._tag_worker = _TagFetchWorker(self._db, self._api,
                                           subject_id, bangumi_id,
                                           need_tags, need_studio)
        self._tag_worker.done.connect(self._on_tag_fetch_done)
        self._tag_worker.finished.connect(self._tag_worker.deleteLater)
        self._tag_worker.start()

    def _on_tag_fetch_done(self, subject_id: int, ok: bool, message: str,
                           studio_written: bool = False) -> None:
        # done 在 finished 之前发出，这里先摘掉引用再让 deleteLater 生效
        self._tag_worker = None
        if ok:
            self.tagsChanged.emit(subject_id)
            # 新拉到的 tag 同样要进海报墙的筛选目录（见 tagsBySubject），
            # 否则"刚进过详情页的条目"仍筛不出来
            self._tags_dirty = True
            self.tagsBySubjectChanged.emit()
        if studio_written:
            # 公司写在 subjects 里（不是那份 tag 映射），得让海报墙重取 ——
            # 这会重建全部卡片，但每次进详情页最多发生一次，且发生时代
            # 海报墙不可见；补完之后再进就不会了（幂等）
            self._dirty = True
            self.subjectsChanged.emit()
        self.statusMessage.emit(message)
        pending = self._tag_fetch_pending
        self._tag_fetch_pending = None
        if pending:
            self.requestTagFetch(pending)

    def waitTagWorker(self, ms: int = 3000) -> None:
        """退出时等待进行中的标签拉取线程（QmlApp.shutdown 调用）。"""
        w = self._tag_worker
        if w is not None and w.isRunning():
            log.info("等待标签拉取线程结束…")
            w.wait(ms)

    # ---------- 收藏状态（想看 / 看过 / 在看 / 搁置 / 抛弃）----------
    @Slot(int)
    def requestCollectType(self, subject_id: int) -> None:
        """进入详情页时把该条目的收藏状态补齐。

        **两段式，缺一不可**：
          ① 收藏同步缓存里若已有答案 → 先落库 + 通知，**同一帧**把界面点亮
             （不发请求，见下）；
          ② 有 Token → 再回查一次网络（每条目每会话最多一次），**以网络为准**。

        ① 是为了"快"：网络往返数百毫秒，而海报是立刻出现的；只用 ② 的话
        选中态会明显慢半拍。

        ② 是为了"对"：缓存只是上次同步时的状态，**不是权威**——
           - 缓存里没有它：可能确实没收藏，也可能只是还没同步过
             （用户刚在网站上标的就属于这种）；
           - 缓存里有它：用户此后在网页/手机上改过就已过期。
        两种都只有网络知道答案（用户原话："有 token 按 bangumi 上处理"）。
        **本地快照一律不参与判断**：CLANNAD 那次就是"本地有值就信任"
        让一个早已取消收藏的「在看」一直错下去。

        未匹配 Bangumi 的条目直接返回（没有条目 ID 可查，状态只由用户
        手动标记，见 setCollectType 的模式 ②）。

        **`_collect_queried` 这道闸门是必需的，不是优化**。原先只有"本地值非 0 才跳过"一条
        判据，于是**回查结果为 0 的条目（就是没收藏的）永远满足"该回查"**，
        而回查完成会发 `collectTypeChanged` → 详情页 reload → reload 又调
        本函数 → 再回查 …… 每个 404 都能自激出一个新请求，日志被同一行
        404 刷屏、服务端也被反复打。判据改成"**每个条目每次运行最多查
        一次**"后，无论 QML 那边怎么重入都只会有一次请求。

        代价：本次运行期间在网页版新收藏的条目不会立刻反映到详情页
        （要重启才补）—— 但这本来就是"本地有快照就信任"的既定取舍，
        与本次运行无关的旧快照同样不会回查。
        """
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return
        if s is None or not s.bangumi_id:
            return
        if int(subject_id) in self._collect_queried:
            return                       # 本次运行已查过（含"未收藏"）
        # 先登记再动手：两个连续的 load() 会在回写前都走到这里，
        # 只靠"DB 里已写 0/已有值"挡不住（那时还没写）。
        self._collect_queried.add(int(subject_id))

        # ① 收藏同步缓存里就有答案 → **同步**落库 + 通知，一个请求都不发。
        #
        # 这条路的全部意义是"快"：emit 是主线程内的直接调用，QML 收到后
        # 立刻 reload，选中态**与海报同一帧**出现。走网络的话要等几百毫秒，
        # 用户看到的是"海报早就在了、状态还在转"。
        #
        # **这一步不看本地快照**：快照可能是错的。缓存里有答案时以它为准，
        # 值没变就什么都不做（不写库、不发信号，QML 本来就显示对了）。
        cached = 0
        try:
            cached = self._db.cached_collect_type(int(s.bangumi_id))
        except Exception as e:
            log.warning("读取收藏缓存失败 subject_id=%s: %s", subject_id, e)
        if cached:
            if cached != int(s.collect_type or 0):
                try:
                    # local_change=False：值来自**同步缓存**（服务端镜像），
                    # 本地只是抄一遍用来秒显 —— 不是用户改的，不能盖
                    # `local_at`（见 Database.set_subject_collect_type）
                    self._db.set_subject_collect_type(int(subject_id), cached,
                                                      local_change=False)
                except Exception as e:
                    log.warning("写入收藏状态快照失败 subject_id=%s: %s",
                                subject_id, e)
                self._dirty = True
                self.collectTypeChanged.emit(int(subject_id))
            return

        # ② 有 Token → **再回查一次网络**（每条目每会话最多一次），以它为准。
        #
        # 缓存只是"秒显"用的加速，**不能当权威**：
        #   - 缓存里没有这个条目 → 可能确实没收藏，也可能只是还没同步过
        #     （用户刚在网站上标的就属于这种）；
        #   - 缓存里有 → 那是**上次同步时**的状态，用户此后改过就过期了。
        # 两种都只有网络知道答案。用户原话："有 token 按 bangumi 上处理"。
        # 回查结果与显示不同时，worker 会写库并通知（选择器/筛选跟着变）。
        #
        # 对没配 Token 的用户**直接返回**：那种情况下 GET 必然 401，回查只
        # 会在日志里刷"回查收藏状态失败"，而本地那个值（用户手动标的
        # 「在看」）本来就是唯一的事实来源 —— 见 setCollectType 模式 ②。
        if not self._can_write_remote():
            return
        self._start_collect_worker(int(subject_id), int(s.bangumi_id), 0)

    @Slot(int, int)
    def setCollectType(self, subject_id: int, collect_type: int) -> None:
        """把该条目设为某个收藏状态（详情页点击那排按钮时调用）。

        校验在两端都做：这里先挡一次非法值（QML 传错时不发请求），
        `BangumiClient.set_collection_type` 里再挡一次。

        **两种模式**：
          ① 有条目 ID 且配了 Token → 先写远端，成功才落本地（见
             `_CollectTypeWorker`：反过来会造成"界面改了、Bangumi 没改"）；
          ② **要么没配 Token、要么条目没匹配到 Bangumi** → 只写本地，
             并在状态栏说明"仅本地、不会同步到 Bangumi"。

        ② 是必须有的（"如果没 token 的用户，也能使用状态栏，
        可以手动选择在看，然后在「在看」页看到该动漫"）。这些用户压根没有
        远端可写，若照旧拒绝，选择器就是个永远点不动的摆设；
        写本地之后「在看」页会把他们标过的番列出来（见 `_load_inprogress`
        里合并 `subjects.collect_type` 的那一段）。**注意别把这条并进 ①
        的失败回滚里**：远端写失败仍然要回滚（那才是"假象"），
        而这里本来就没有远端。

        **任何一条提前返回都必须发 `collectTypeChanged`**（QML 侧靠它解锁
        "写入中"并清掉乐观更新的待确认值）。漏发的话按钮会永久灰着 ——
        看起来就是"点了没反应、之后也点不动了"。
        """
        if collect_type not in COLLECT_TYPE_NAMES:
            log.warning("非法的收藏状态：%s（subject_id=%s）",
                        collect_type, subject_id)
            self.collectTypeChanged.emit(int(subject_id))
            return
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            self.collectTypeChanged.emit(int(subject_id))
            return
        if s is None:
            self.collectTypeChanged.emit(int(subject_id))
            return
        if int(s.collect_type or 0) == int(collect_type):
            # 点的是当前状态：无变化，不打扰（QML 侧也会先挡一道）。
            # 但仍然要发通知把界面解锁（见 docstring）。
            self.collectTypeChanged.emit(int(subject_id))
            return
        if not s.bangumi_id or not self._can_write_remote():
            self._set_collect_type_local(
                int(subject_id), int(collect_type),
                "该条目未关联 Bangumi" if not s.bangumi_id
                else "未配置 Bangumi Token")
            return
        self._start_collect_worker(int(subject_id), int(s.bangumi_id),
                                   int(collect_type))

    def _can_write_remote(self) -> bool:
        """能不能把收藏状态写到 Bangumi（客户端在、且配了 Token）。

        **判的是 Token 而不是"客户端是否存在"** —— QmlApp 永远会构造一个
        客户端，没配 Token 时它只是不带 Authorization 头，
        那种情况下 POST 必然 401（见 BangumiClient.has_token）。
        """
        return self._api is not None and bool(
            getattr(self._api, "has_token", True))

    def _set_collect_type_local(self, subject_id: int, collect_type: int,
                                why: str) -> None:
        """只写本地快照（见 setCollectType 的模式 ②）。

        提示语**只说结果**："已标记为「在看」· 仅本地"。
        技术原因（没配 Token / 条目没匹配）只进日志。
        """
        try:
            self._db.set_subject_collect_type(subject_id, collect_type)
        except Exception as e:
            log.exception("写入收藏状态失败 subject_id=%s: %s", subject_id, e)
            self.statusMessage.emit("保存失败（详见日志）")
            self.collectTypeChanged.emit(int(subject_id))
            return
        log.info("收藏状态仅写本地：subject_id=%s → %s（%s）",
                 subject_id, collect_type, why)
        self._dirty = True
        # 「在看」页要立刻反映（标成在看就出现、从在看改走就消失）
        self._inprogress_dirty = True
        self.inProgressChanged.emit()
        self.collectTypeChanged.emit(int(subject_id))
        self.statusMessage.emit(
            "已标记为「%s」· 仅本地" % COLLECT_TYPE_NAMES.get(collect_type, ""))

    def _start_collect_worker(self, subject_id: int, bangumi_id: int,
                              write_type: int) -> None:
        w = _CollectTypeWorker(self._db, self._api, subject_id, bangumi_id,
                               write_type)
        self._collect_workers.append(w)
        w.done.connect(self._on_collect_type_done)
        w.finished.connect(lambda: self._drop_collect_worker(w))
        w.start()

    def _drop_collect_worker(self, w: "_CollectTypeWorker") -> None:
        try:
            self._collect_workers.remove(w)
        except ValueError:
            pass
        w.deleteLater()

    def _on_collect_type_done(self, subject_id: int, ok: bool, message: str,
                              new_type: int) -> None:
        # **无论 new_type 是否为 0 都要通知 QML**。
        #
        # 原先写的是 `if new_type: emit(...)` —— 于是回查结果为 0
        # （条目在 Bangumi 上确实没收藏）时**什么都不发**，选择器停在
        # "一个都没选中"且界面无从知道"已经查完了"。虽然此时视觉结果一样，
        # 但 QML 侧的 `collectBusy` 也解不了锁，用户点过一次写失败后
        # 选择器会一直灰着。
        #
        # 现在一律 emit：QML 那边重取一次即可，值没变等于空操作，代价可忽略。
        # **不能改成 subjectsChanged**：那会重建全部海报卡片（每张重新解码
        # 封面），而收藏状态只影响详情页那排按钮。
        self._dirty = True            # subjects 缓存要失效（值可能变了）
        # 「在看」列表也要失效：状态在「在看」↔其它之间变化时，
        # 这一部要不要出现在那一页正是由它决定的。
        self._inprogress_dirty = True
        self.inProgressChanged.emit()
        self.collectTypeChanged.emit(subject_id)
        if message:
            self.statusMessage.emit(message)

    def waitCollectWorkers(self, ms: int = 3000) -> None:
        """退出时等待进行中的收藏状态线程（QmlApp.shutdown 调用）。"""
        for w in list(self._collect_workers):
            if w.isRunning():
                log.info("等待收藏状态线程结束…")
                w.wait(ms)

    def _original_cover_path(self, s: Subject) -> Path:
        """原版海报的本地缓存路径（按实际情况探测，不靠猜）。

        **踩坑（两种命名混用，导致"更换海报"一打开就显示已自定义）**：
        原版缓存文件是 `scanner` / `match` 写的，两者传给
        `cover_cache.download()` 的都是 **bangumi_id**（见 scanner.py 与
        match.py 的调用点），所以真实文件名是 `covers/<bangumi_id>.<ext>`。
        这里如果按 `s.id` 反推，只要本地 id 与 bangumi_id 不等，就会算出一个**不存在的
        路径**，于是：
          - `coverInfo().isCustom` 恒为 True → 小窗一打开就打上「自定义」
            徽标、「恢复原版海报」按钮错误地可点；
          - `restoreCover()` 认为原版不存在 → 白下载一次（或断网直接失败）。

        改为按优先级探测真实文件：
          bangumi_id → subject_id（兼容早期按本地 id 命名的数据）
        都找不到时退回"按 bangumi_id + URL 推断的扩展名"，交给
        `restoreCover()` 走"缓存被清理 → 重新下载"的分支。

        **未匹配 Bangumi 的条目（bangumi_id 为空）特殊处理**：只用
        `subject_id` 探测，且**不以 subject_id 兜底造路径** —— 否则
        `covers/<sid>.jpg` 可能恰好命中别的条目的文件（历史数据里
        原版按 bangumi_id 命名，而 bangumi_id 就是些小整数），
        会把不相干的图当成"原版"。
        """
        keys = [k for k in (s.bangumi_id, s.id) if k]
        found = known_cover_files(*keys)
        if found:
            return found[0]
        if not s.bangumi_id:
            # 没有 Bangumi 来源，就没有"原版"可言 —— 给一个必定不存在的
            # 路径，让 restoreCover() 的分支走"无原版可恢复"的提示
            return covers_dir() / f"{s.id}_no_original"
        return cover_path_for(s.bangumi_id, s.cover_url or "")

    def _notify_cover_changed(self, subject_id: int) -> None:
        """海报写库后的统一收尾：版本号 +1、失效条目缓存、通知 QML。

        海报墙与详情页都读 subjects Property，一条 subjectsChanged
        两处同时刷新（emit 即触发 QML 重取，无需调用方再 reload）。
        """
        self._cover_revs[subject_id] = self._cover_revs.get(subject_id, 0) + 1
        self._dirty = True
        self.subjectsChanged.emit()
        self.coverChanged.emit(subject_id)

    def _cover_file_url(self, path: str, subject_id: int) -> str:
        """封面路径 → file:// URL，附 `?v=` 版本号防 QML 图片缓存。

        换海报后 URL 字符串可能不变（恢复原版 = 指回同一个文件），
        `Image.source` 相同就会命中解码缓存、界面一直显示旧图；
        版本号拼进 URL 后每次变更都拿到新缓存键，本地读取不受影响。
        """
        url = as_file_url(path)
        if not url:
            return ""
        rev = self._cover_revs.get(subject_id, 0)
        return url + "?v=%d" % rev if rev > 0 else url

    # ---------- 转换 ----------
    def _subject_to_dict(self, s: Subject) -> dict:
        return {
            "id": s.id,
            "bangumiId": s.bangumi_id or 0,
            "name": s.name or "",
            "nameCn": s.name_cn or "",
            "title": s.name_cn or s.name or "",
            "seriesName": s.series_name or "",
            # Bangumi infobox 的别名（**空格拼接**，见下）。
            #
            # 存储里是换行分隔（database.aliases_to_text），这里换成空格再给
            # QML：搜索是 `haystack.indexOf(q) >= 0` 的**子串**匹配，
            # QML 侧再 split 一遍纯属多余；而换行符在 QML 的 JS 字符串里
            # 只是普通字符，保留它反而会让"跨别名的连续子串"意外命中
            # （如搜 "花\n那朵" 也成立），用空格归一更符合"分词"的直觉。
            "aliases": " ".join(
                Database.aliases_from_text(s.aliases)),
            # 动画制作公司（infobox 的「动画制作」，扫描 / 手动匹配时顺路写入）。
            # 合作署名形如 "WIT STUDIO / CloverWorks"，海报墙按 ` / ` 拆开
            # 分别匹配（QML 侧 itemStudios）。没有的条目为空串 —— 在
            # "制作公司"筛选栏里不出现，不拿别处的数据凑。
            "studio": s.studio or "",
            # 集数是否为「按顺序」与官方配对（scanner._align_by_order 写入）。
            # 详情页打开时据此弹黄色提示：这种对应是数量一致下的猜测，
            # 没有按号匹配可靠。
            "epAlignOrder": (s.ep_align or "") == "order",
            # Bangumi 收藏状态（0 = 未知/未收藏，1 想看 / 2 看过 / 3 在看 /
            # 4 搁置 / 5 抛弃）。详情页海报下方那排状态按钮的当前选中项。
            # 0 时界面**一个都不选中**，并由 requestCollectType 异步回查。
            "collectType": int(s.collect_type or 0),
            "matchState": s.match_state or "auto",
            # Bangumi 条目 id（0 = 没连上 Bangumi，纯本地条目）。
            #
            # **海报墙要的就是这个，不是 `matchState`**：角标与
            # 「已匹配/未匹配」筛选问的都是"这条到底连没连上 Bangumi"，而
            # `match_state='manual'` 只说明"关联是用户手动指定的"—— 它既可能
            # 有 bangumi_id（新建并绑定 + 匹配成功、手动匹配成功），也可能是
            # 一条纯本地条目。拿 match_state 回答这个问题的后果就是：一条
            # 已经拉到封面/集数/tag 的条目，海报上却挂着绿色「手动」，用户
            # 只能来问"这什么意思"。
            "bangumiId": int(s.bangumi_id or 0),
            "totalEps": int(s.total_eps or 0),
            "folderPath": s.folder_path or "",
            # 封面转 URL，QML 的 Image 才能加载（含中文路径也能用）；
            # 带 ?v= 版本号，换海报后同路径也能刷新（见 _cover_file_url）
            "coverUrl": self._cover_file_url(s.cover_path or "", s.id),
            "isGroup": False,
            "childCount": 1,
            "childIds": [s.id],
        }

    @staticmethod
    def _episode_to_dict(e: Episode) -> dict:
        return {
            "id": e.id,
            "subjectId": e.subject_id,
            "epIndex": float(e.ep_index or 0),
            # 非正片的显示标签（`SP01`/`OVA01`/`NCOP3`/`WEB予告 #02`…）。
            # 非空时 QML 侧直接显示它，**不显示 epIndex** —— 因为附加内容的
            # 排序值（main_max + 1000 + n）只是给排序用的，"SP 显示成 1013"
            # 毫无意义。正片该字段为空串，QML 侧照旧显示数字。
            "epLabel": e.ep_label or "",
            "title": e.title or "",
            "filePath": e.file_path or "",
            "watched": bool(e.watched),
            "progress": float(e.watch_progress or 0.0),
            "watchedAt": e.watched_at or "",
        }

    def _build_groups(self, rows: list[Subject]) -> list[dict]:
        """按 series_name 聚合为系列卡片。"""
        groups: "OrderedDict[str, list[Subject]]" = OrderedDict()
        for s in rows:
            groups.setdefault((s.series_name or "").strip(), []).append(s)

        out: list[dict] = []
        for series, items in groups.items():
            # 无系列名或只有一部 → 平铺展示
            if not series or len(items) == 1:
                out.append(self._subject_to_dict(items[0]))
                continue
            watched = total = 0
            for s in items:
                eps = self._db.list_episodes(s.id)
                total += len(eps) or (s.total_eps or 0)
                watched += sum(1 for e in eps if e.watched)
            cover_subj = next((s for s in items if s.cover_path), None)
            d = self._subject_to_dict(items[0])
            # 可搜索文本要**合并整个系列**，而且必须同时收**正式名与别名**。
            #
            # 两件事各有一个坑：
            #
            # ① 只用 items[0] 不够 —— 别名可能登记在别的季上，用户搜那个
            #    别名就搜不到这张卡 ✗。
            # ② 只收别名也不够：下面的 update 会把
            #    title/name/nameCn 三个字段**统一替换成 series_name**，
            #    于是各成员原本的正式名被彻底覆盖掉。实例：
            #       组成员 1（TV）   name_cn="链锯人"     别名=[Chainsaw Man, 电锯人]
            #       组成员 2（剧场版）name_cn="电锯人 剧场版 蕾塞篇"
            #       series_name="电锯人"
            #    替换后卡片只剩「电锯人」，而「链锯人」**既不是系列名
            #    也不在任何别名里** → 搜「链锯人」直接搜不到 ✗。
            #    所以每个成员的 name_cn / name 也要一并并入。
            #
            # 排除 series 本身：它已经是 title/name/nameCn 了，重复无意义。
            merged: list[str] = []
            seen_alias: set[str] = set()
            for s in items:
                for a in (*Database.aliases_from_text(s.aliases),
                          s.name_cn or "", s.name or ""):
                    a = a.strip()
                    if a and a not in seen_alias and a != series:
                        seen_alias.add(a)
                        merged.append(a)
            # 制作公司同样**合并整个系列**：各季可能换过公司（如续篇转手），
            # 任一季的公司都该能筛到这张卡。QML 侧按 childIds 取不到 studio
            # （分组模式下子条目不在列表里），所以在这里先拼好。
            studios: list[str] = []
            for s in items:
                for name in (s.studio or "").split("/"):
                    name = name.strip()
                    if name and name not in studios:
                        studios.append(name)
            d.update({
                "title": series,
                "name": series,
                "nameCn": series,
                "aliases": " ".join(merged),
                "coverUrl": self._cover_file_url(cover_subj.cover_path, cover_subj.id)
                            if cover_subj else "",
                "studio": " / ".join(studios),
                "isGroup": True,
                "childCount": len(items),
                "childIds": [s.id for s in items],
                "watchedEps": watched,
                "totalEps": total,
            })
            out.append(d)
        return out
