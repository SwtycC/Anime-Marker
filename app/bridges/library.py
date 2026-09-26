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
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from app.core.bangumi_api import COLLECT_TYPE_DOING, is_studio_name
from app.core.database import Database, Episode, Subject
from app.utils.cover_cache import cover_path_for, known_cover_files
from app.utils.cover_cache import download as download_cover
from app.utils.paths import covers_dir

log = logging.getLogger(__name__)

# 多季展示模式
DISPLAY_FLAT = "flat"
DISPLAY_GROUPED = "grouped"


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


class LibraryBridge(QObject):
    """媒体库数据源。"""

    # 数据变化通知（QML 侧用它触发列表重建）
    subjectsChanged = Signal()
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
        # 而 QML 的 Image 按 URL 缓存解码结果 —— URL 不变就一直显示旧图（踩坑）。
        # 按条目记一个递增版本号，以 `?v=N` 追加到 file:// URL 后（query 不参与
        # 本地文件寻址，只作为缓存键的一部分），每次海报变更 +1 强制重新解码。
        self._cover_revs: dict[int, int] = {}
        # 标签自动补拉：同一时刻最多一个 worker；期间新请求只记最后一个
        self._tag_worker: Optional[_TagFetchWorker] = None
        self._tag_fetch_pending: Optional[int] = None

    def set_api(self, api: BangumiClient) -> None:
        """注入 Bangumi 客户端（QmlApp 在启动 / 配置重建时调用）。"""
        self._api = api

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

    @Slot()
    def reloadInProgress(self) -> None:
        """仅刷在看列表（在线拉取完成后由 InProgressBridge 调用）。"""
        self._inprogress_dirty = True
        self.inProgressChanged.emit()

    # ---------- Bangumi 收藏列表（阶段 7）----------
    @Property("QVariantList", notify=inProgressChanged)
    def inProgress(self) -> list[dict]:
        """Bangumi「在看」收藏（来自 `inprogress_cache`，由 InProgressBridge 写入）。

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

            # 没有本地缓存就**不回落在线 URL**（踩坑，见下方说明）。
            #
            # 早期写的是 `local_cover or it.cover_url`：本地未入库 / 封面还没
            # 下载的条目，会把 `https://lain.bgm.tv/...` 直接交给 QML 的
            # `Image.source` —— 让 QML 引擎自己去联网取图。后果：
            #   ① `lain.bgm.tv` 在内网/被墙环境下连不上 → 控制台刷
            #      `QML QQuickImage: Connection timed out`（QML 的报错**输出到
            #      控制台而非日志**，看着像程序出了问题）；
            #   ② 每个可见行都发一次请求，几十行就是几十个连接，页面卡顿；
            #   ③ 封面本该由 `cover_cache` 下载到本地后经 `as_file_url()`
            #      使用（见 §5.12.5），绕过这条链路等于丢了缓存与失败兜底。
            # 现在只用本地 URL，拿不到就交给 QML 显示占位底色（原本就有的
            # 空态），不再让界面层承担联网职责。

            # 「下一集」按钮：要播的集 ID（0 = 播不了）+ 播不了时的原因文案。
            # 按钮**不隐藏**，点不动时把原因报到状态栏（见 _next_episode）。
            next_ep_id, next_ep_hint = self._next_episode(local_id, it.ep_status)
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
                "epStatus": int(it.ep_status or 0),
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
        return out

    @Slot(result="QVariantList")
    def pendingUploads(self) -> list[dict]:
        """「本地看过、Bangumi 未标」的**逐集**清单 —— 动态页「上传」小窗的数据源。

        **一行 = 一集**（小窗里勾选的就是"这一集"），字段：
            {episodeId, bangumiEpId, subjectId, title, epIndex, epTitle}
        `title` 是动漫名 —— 小窗里与集名一起显示成「碧蓝之海 第三季 · EP7 妈妈」，
        否则用户根本看不出待传的是哪一集 ✗（实测反馈）。

        判据统一由 `Database.pending_uploads()` 给出 —— 与实际上传时的筛选
        **是同一个查询** ✓（否则会出现"显示 3 条只传了 1 条"）。
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

        实测本机 1446 集里有 455 集属于这种情况（扫描时没拿到集数元数据）——
        它们必须被**明确告知**，不能静默 ✗。
        """
        try:
            return sum(self._db.blocked_upload_counts().values())
        except Exception as e:
            log.exception("统计无法补传的集数失败: %s", e)
            return 0

    def _next_episode(self, subject_id: int, ep_status: int) -> tuple[int, str]:
        """「下一集」对应的本地集 ID 与**播不了时的原因**；可播时原因为空串。

        **按 Bangumi 的 `ep_status`（已看到第 N 集）算，不用本地的 `watched`
        标记** —— 实测本账号「在看」的 11 部里本地 `watched` 全是 0（那些集是
        在 Bangumi 网页 / 别的设备上标的，本程序没有播放记录），若按本地标记
        取"第一个未看过的"，会一律算成第 1 集 ✗。按 `ep_status` 算与页面上的
        进度条（读的也是 `ep_status`，如「5 / 12 集」）口径一致。

        规则：优先取 `ep_index == ep_status + 1` 的那一集（"接着看"的那集）；
        编号对不上（本地缺集/续篇从中间编号）时退回"第一集 ep_index 大于
        ep_status 的"；都没有则返回 0，并给出一句能解释清楚的原因 ——
        界面上按钮**不隐藏**，点不动时把原因报到状态栏（实测要求）。

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

        want = float(ep_status or 0) + 1
        for e in eps:                       # list_episodes 已按 ep_index 升序
            if abs(float(e.ep_index or 0) - want) < 1e-6:
                return int(e.id), ""
        later = [e for e in eps if float(e.ep_index or 0) > want]
        if later:
            return int(later[0].id), ""
        # 走到这里说明本地没有任何"第 want 集及以后"的集。两种成因要分开说：
        if len({float(e.ep_index or 0) for e in eps}) == 1:
            # 所有集号一模一样 → 扫描时标题没解析出来（实测：某部 12 集全是 2.0）
            return 0, ("本地集号异常（%d 集全是第 %g 集，扫描解析可能失败），"
                       "无法判断下一集" % (len(eps), float(eps[0].ep_index or 0)))
        return 0, ("本地没有更靠后的集了（Bangumi 进度 %d 集，本地共 %d 集）"
                   % (int(ep_status or 0), len(eps)))

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

    @Slot(result="QVariantMap")
    def inProgressMeta(self) -> dict:
        """收藏页的元信息（缓存年龄 + 条数），页面标题区展示用。

        条数只算「在看」—— 与 `inProgress` 的过滤保持一致，
        否则页头数字会比列表实际条数大（差额是"看过"的那批，上百部）。
        """
        try:
            age = self._db.inprogress_cache_age()
            count = len(self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DOING))
        except Exception as e:
            log.exception("读取在看缓存元信息失败: %s", e)
            return {"count": 0, "ageSeconds": -1, "stale": True}
        # 超过 1 小时视为"数据较旧"，页面提示用户刷新
        stale = age is None or age > 3600
        return {
            "count": count,
            "ageSeconds": -1 if age is None else int(age),
            "stale": stale,
        }

    # ---------- 条目列表 ----------
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
        model 一变就会**销毁重建全部卡片**（每张都要重新解码封面，见
        PosterWallPage.qml 里"每敲一个字卡一次"的踩坑）。单独一个 Property
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
        """同系列的其他季（详情页「同系列」切换用）。"""
        try:
            cur = self._db.get_subject(subject_id)
            if cur is None or not (cur.series_name or "").strip():
                return []
            series = cur.series_name.strip()
            return [
                self._subject_to_dict(s)
                for s in self._db.list_subjects()
                if (s.series_name or "").strip() == series and s.id != subject_id
            ]
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

    def _original_cover_path(self, s: Subject) -> Path:
        """原版海报的本地缓存路径（按实际情况探测，不靠猜）。

        **踩坑（两种命名混用，导致"更换海报"一打开就显示已自定义）**：
        原版缓存文件是 `scanner` / `match` 写的，两者传给
        `cover_cache.download()` 的都是 **bangumi_id**（见 scanner.py 与
        match.py 的调用点），所以真实文件名是 `covers/<bangumi_id>.<ext>`。
        这里如果按 `s.id` 反推，只要本地 id 与 bangumi_id 不等（几乎所有
        条目都如此，如 id=2 ↔ bangumi_id=302189），就会算出一个**不存在的
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
            "matchState": s.match_state or "auto",
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
            # ② 只收别名也不够（**实测踩到**）：下面的 update 会把
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
