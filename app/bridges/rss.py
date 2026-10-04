"""RSS 订阅桥接（阶段 7 · F19 的 QML 版本）。

职责范围（本阶段只做**管理**，不做实际轮询下载）：
1. 订阅源 CRUD：`sources` / `addSource()` / `updateSource()` / `removeSource()`
2. 下载记录查询：`downloads()` / `downloadStats()`
3. 关联 Bangumi 条目：`linkSubject()`（把订阅绑定到本地条目）

**为什么本阶段不实现轮询下载**：
下载链路需要 qBittorrent WebAPI 客户端（登录 / 添加种子 / 查询状态）与
RSS 解析器（feedparser 或 xml.etree）配合，且需要「三层判新」逻辑。
这是一块独立且较大的功能，数据库表（`rss_sources` / `download_history`）
与配置项已就绪，但为了本阶段能交付可用的界面，先只做订阅源管理 ——
用户可以先建好订阅、绑定条目，下载器在后续阶段接入。

数据表结构见 `database.py` 的 `rss_sources` / `download_history`。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.matcher import SubjectMatcher
from app.core.rss_feed import RssError, fetch_and_parse
from app.core.rss_matcher import RssMatcher
from app.utils.title_parser import clean_anime_title

log = logging.getLogger(__name__)

# 判新规则（决定"哪些集才下载"）
#
# 两者的**语义差别**（详见 rss_matcher.judge 里那段说明）：
#   new_only —— 本地媒体库**已经有这一集**就跳过（"新"= 我还没有的）
#   all      —— 不看本地有什么，只要没下载过就下（补齐全集用）
# 早期两者在代码里没有分支差异、效果完全相同，是名不副实的两个选项。
RULE_NEW_ONLY = "new_only"
RULE_ALL = "all"
RULES = (RULE_NEW_ONLY, RULE_ALL)

# 界面文案（设置页、订阅卡片共用，避免两处写不同的字）
RULE_LABELS = {
    RULE_NEW_ONLY: "只下新集",
    RULE_ALL: "全部下载",
}

# 下载状态 → 中文（QML 侧展示用）
#
# 键取自 `rss_service.STATUS_*`（那是**我们下发流程**的状态）。
# 注意里面有 `done` 而不是 `completed` —— 早期这里写的是
# "completed"，于是"已完成"的记录会显示成英文 `done`（踩坑）。
# 两个键都留着：历史库里可能两种值都存在。
STATUS_LABELS = {
    "pending": "待确认",
    "pushed": "已下发",
    "downloading": "下载中",
    "done": "已完成",
    "completed": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
}

# qBittorrent 的任务状态 → 中文（`torrents_info()` 的 state 字段）。
# 取值见 qBittorrent WebAPI 文档（torrent state）：
#   error / missingFiles / uploading / pausedUP / queuedUP / stalledUP
#   / checkingUP / forcedUP / allocating / downloading / metaDL
#   / pausedDL / queuedDL / stalledDL / checkingDL / forcedDL
#   / checkingResumeData / moving / unknown
_QB_STATE_LABELS = {
    "downloading": "下载中",
    "forceddl": "下载中",
    "metadl": "获取元数据",
    "allocating": "分配空间",
    "stalleddl": "等待资源",
    "queueddl": "排队中",
    "pauseddl": "已暂停",
    "checkingdl": "校验中",
    "checkingup": "校验中",
    "uploading": "做种中",
    "forcedup": "做种中",
    "stalledup": "做种中",
    "queuedup": "排队中",
    "pausedup": "已完成（暂停）",
    "checkingresumedata": "校验中",
    "moving": "移动文件",
    "error": "出错",
    "missingfiles": "文件缺失",
    "unknown": "未知",
}


class _FeedTitleWorker(QThread):
    """抓取订阅源，从最近几条条目里推断出**动漫名**，交给界面预填。

    为什么要线程：抓 RSS 是网络请求（超时 15s），同步做会把界面卡住。

    **为什么是"推断"而不是"取第一条标题"**：RSS 的条目标题是**资源标题**，
    形如 `[Nekomoe kissaten&LoliHouse] 碧蓝之海 第三季 [09][WebRip 1080p]` ——
    带字幕组、集数、画质参数。直接拿去搜 Bangumi 基本搜不到，必须先清洗
    （`clean_anime_title` 会去掉方括号块、集数、画质词）。

    推断策略（按可信度）：
      ① `feed` 的 channel title —— 有些站点的频道名就是番名；
      ② 最近几条 item 标题各自清洗后，**取最长的一条** —— 最长的那条
         通常信息最全（最短的可能是"第 10 集"这种只有集号的）；
      ③ 全失败 → 返回空串，让用户手填。
    """

    #: (来源 id, 推断出的名字, 错误信息) —— 名字为空且无错误 = 没推断出来
    done = Signal(int, str, str)

    def __init__(self, source_id: int, url: str, proxy: str = "",
                 user_agent: str = "AnimeMarker/1.0",
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._source_id = source_id
        self._url = url
        self._proxy = proxy
        self._ua = user_agent

    def run(self) -> None:
        try:
            entries = fetch_and_parse(self._url, proxy=self._proxy,
                                      user_agent=self._ua)
        except RssError as e:
            log.warning("抓取订阅源失败 %s: %s", self._url, e)
            self.done.emit(self._source_id, "", str(e))
            return
        except Exception as e:              # pragma: no cover - 防御性
            log.exception("抓取订阅源异常 %s", self._url)
            self.done.emit(self._source_id, "", f"抓取失败：{e}")
            return

        if not entries:
            self.done.emit(self._source_id, "", "该订阅源没有条目")
            return

        # 最近若干条（新的通常在前）清洗后比长度，取最长的那条作番名。
        # 只取前 10 条：更早的多半是同一部番的历史集，长度不提供新信息。
        best = ""
        for e in entries[:10]:
            cleaned = clean_anime_title(e.title or "")
            if len(cleaned) > len(best):
                best = cleaned
        self.done.emit(self._source_id, best, "")


class _CreateSubjectWorker(QThread):
    """从订阅源新建本地条目（可带 Bangumi 匹配）。

    需求（用户原话）："条目名先从订阅源中获取，自动填入文本框，然后支持
    手动修改。需要匹配，没填 token 则跳过。一个订阅源一个条目。"

    所以这个 worker 做两件事，取决于有没有 Token：
      ① 有 Token → 拿用户确认的名字搜 Bangumi，匹配上就**带封面/集数/别名/
         tag 入库**（与扫描入库同一套字段），没匹配上则退化为本地条目；
      ② 没 Token → **跳过匹配**，直接建本地条目（match_state='manual'）。

    **复用 `upsert_local_subject`**（而不是 `upsert_subject`）：那个方法
    专门处理"没有 bangumi_id"的条目 —— 未匹配时 bangumi_id 必须写 NULL，
    因为该列有 UNIQUE 约束，用 0 当哨兵会让第二条未匹配番覆盖第一条
    （详见 database.upsert_local_subject 的说明）。
    """

    #: (来源 id, 新条目本地 id, 错误信息)
    done = Signal(int, int, str)

    def __init__(self, db: Database, api, source_id: int, name: str,
                 do_match: bool = True,
                 accept_score: int = 60, accept_gap: int = 20,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._source_id = source_id
        self._name = name
        self._do_match = do_match
        self._accept_score = accept_score
        self._accept_gap = accept_gap

    def run(self) -> None:
        name = (self._name or "").strip()
        if not name:
            self.done.emit(self._source_id, 0, "条目名不能为空")
            return

        subj = None
        if self._do_match and self._api is not None:
            try:
                matcher = SubjectMatcher(
                    self._api, season_mode="cn",
                    accept_score=self._accept_score,
                    accept_gap=self._accept_gap,
                )
                result = matcher.search_best([name], 0)
                subj = result.subject
                if subj is None:
                    log.info("从订阅源新建条目：未匹配到 Bangumi（%s），"
                             "按本地条目入库", result.reason)
            except Exception as e:
                # 匹配失败不阻断入库：用户要的是"把这个订阅对应起来"，
                # 匹配只是锦上添花（拿封面/集数）。退化成本地条目即可。
                log.warning("从订阅源匹配 Bangumi 失败（转本地条目）：%s", e)

        try:
            subject_id = self._insert(name, subj)
        except Exception as e:
            log.exception("从订阅源新建条目失败: %s", e)
            self.done.emit(self._source_id, 0, f"新建条目失败：{e}")
            return
        self.done.emit(self._source_id, subject_id, "")

    def _insert(self, name: str, subj: Optional[dict]) -> int:
        """写库并返回本地 subject id。"""
        if not subj:
            # 未匹配（或没 Token）：本地条目，bangumi_id 写 NULL
            return self._db.upsert_local_subject(
                folder_path="",            # 订阅条目还没有本地目录
                display_name=name,
                series_name="",
                total_eps=0,
            )

        bangumi_id = int(subj.get("id") or 0)
        name_cn = subj.get("name_cn") or ""
        name_jp = subj.get("name") or ""
        cover_url = (subj.get("images") or {}).get("large", "")
        total_eps = int(subj.get("total_episodes")
                        or subj.get("eps_count") or 0)

        # 封面下载失败不算错误 —— 与扫描入库的取舍一致（封面可事后补）
        cover_path = ""
        if cover_url and self._api is not None:
            from app.utils.cover_cache import download as download_cover
            try:
                cover_path = str(download_cover(
                    bangumi_id, cover_url, self._api.session,
                    label=name_cn or name_jp or name))
            except Exception as e:
                log.warning("订阅条目封面下载失败 %s: %s", name, e)

        return self._db.upsert_subject(
            bangumi_id=bangumi_id,
            name=name_jp or name,
            name_cn=name_cn or name,
            cover_url=cover_url,
            cover_path=cover_path,
            total_eps=total_eps,
            folder_path="",                # 见上：暂无本地目录
            series_name="",
            aliases=self._db.aliases_from_subject(subj),
            studio=self._api.studio_for(subj, bangumi_id)
                   if self._api is not None else "",
            # manual 而不是 auto：这个条目是**用户手动确认过的名字**建的，
            # 与"扫描自动匹配"来源不同。用 auto 的话，下次扫描若碰到同名
            # 目录会把它当自动结果覆盖。
            match_state="manual",
        )


class _PreviewWorker(QThread):
    """「下载器」里的预览：抓一次订阅源，跑完整判新，列出**会下载哪些集**。

    **为什么必须真抓 RSS 而不是只过滤词**（与用户确认过）：预览的意义就是
    "现在保存的话，会下什么" —— 只看过滤词的话，那些"本地已经有了 /
    已经下过"的集也会被列进来，预览就成了假的。所以这里跑的是与真正
    轮询**同一套** `judge` 逻辑。

    **为什么不复用 `PollWorker`**：那个会**真的下发**到 qBittorrent、
    也会写 `download_history`。预览必须纯只读 —— 用户还没点保存，
    不该产生任何副作用。
    """

    #: (订阅 id, 条目列表, 错误信息)
    #: 条目的字段：{ep, title, action, reason}
    #:   action = "download"（会下载）/ "skip"（会跳过）
    done = Signal(int, list, str)

    def __init__(self, db: Database, config: Config, source: RssSource,
                 must_include: str, must_exclude: str,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._source = source
        self._include = must_include or ""
        self._exclude = must_exclude or ""

    def run(self) -> None:
        # 用**界面上正在编辑的**过滤词预览（还没保存）——把这两个值
        # 临时塞进一个副本里，不碰数据库。
        import copy
        src = copy.copy(self._source)
        try:
            src.must_include = self._include
            src.must_exclude = self._exclude
        except Exception:               # pragma: no cover - dataclass 不可写时
            pass

        proxy = self._config.get("bangumi", "proxy", "")
        ua = self._config.get("bangumi", "user_agent", "AnimeMarker/1.0")
        try:
            entries = fetch_and_parse(src.url, proxy=proxy, user_agent=ua)
        except Exception as e:
            log.warning("预览抓取订阅源失败 %s: %s", src.url, e)
            self.done.emit(int(src.id), [], str(e))
            return

        # `assume_enabled=True`：预览的语义是"**保存后会**下哪些"，
        # 所以假装闸门已通过（不在还没保存时就全部报"未配置下载器"）。
        # 但查重、过滤词**照常生效** —— 那些才是用户真正想看的结果。
        # 见 RssMatcher.__init__ 的 `assume_enabled` 说明。
        matcher = RssMatcher(self._db, None, self._config,
                             assume_enabled=True)  # qb=None：不查 qB
        out: list[dict] = []
        for e in entries:
            filt = matcher.title_filtered(e.title or "", src)
            if filt:
                out.append({"ep": 0.0, "title": e.title or "",
                            "action": "filtered", "reason": filt})
                continue
            r = matcher.judge(e, src)
            out.append({
                "ep": float(r.ep_index),
                "title": e.title or "",
                "action": "download" if r.should_download else "skip",
                "reason": r.reason or "",
            })
        self.done.emit(int(src.id), out, "")


class _TorrentStatusWorker(QThread):
    """向 qBittorrent 查一次任务列表，供下载记录显示进度。

    为什么要线程：`torrents_info()` 是 HTTP 调用；qBittorrent 没开时
    要等到连接超时（3s）才失败 —— 同步做会让界面卡住 3 秒。

    **失败不算错误**：qBittorrent 没启动是很常见的状态（用户先开本程序），
    此时只是拿不到进度、记录仍按数据库里的状态显示，不该弹报错打扰用户。
    """

    #: (hash → progress 0~1, state 文本, 错误信息)
    done = Signal(dict, str)

    def __init__(self, qb, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._qb = qb

    def run(self) -> None:
        if self._qb is None:
            self.done.emit({}, "")
            return
        try:
            items = self._qb.list_torrents()
        except Exception as e:
            log.info("读取 qBittorrent 任务失败（按未运行处理）：%s", e)
            self.done.emit({}, "")
            return
        out: dict[str, dict] = {}
        for t in items:
            if not t.hash:
                continue
            out[str(t.hash).lower()] = {
                "progress": float(t.progress or 0.0),
                "state": str(t.state or ""),
                "dlspeed": int(t.dlspeed or 0),
                "name": t.name or "",
            }
        self.done.emit(out, "")


def _norm_title(text: str) -> str:
    """标题归一：去空白/标点、转小写，用于**按名字匹配** qB 任务。

    与 `QbClient._norm` 同一套口径（保持一致，避免两处判断不一致）。
    """
    import re
    return re.sub(r"[\s\-_\.\[\]【】()（）]+", "", text or "").lower()


# 集数是否"像集号"的过滤上限。正片集数不会超过这个数
# （超长篇的重播编号也很少过百）—— 用来挡掉被正则误抓的十六进制
# 哈希、年份、分辨率等。
_EP_MAX = 199

# 紧贴在数字**右边**的字符若是这些，说明它是画质/编码参数的一部分：
#   1080**p** / 1080**P** / 10**bit** / x26**4** / 1920**x**1080
# 只看"紧邻一位"而不是大窗口 —— 实测踩坑：一开始取了 ±12 字符的窗口，
# 结果 `- 12 (Baha 1920x1080 AVC AAC MP4)` 里那个**正确的 12** 也被
# 同一个窗口里的 `1080p` 命中而排除，10 条记录一条都匹配不上。
_EP_SUFFIX_BAD_RE = re.compile(r"(?i)^(?:[pP]\b|bit|fps|k\b|[xX]\d)")


def _ep_of(text: str) -> int:
    """从标题里取集号（拿不到返回 0）。

    实测样本：
        `...[年龄限制版] - 12 (Baha 1920x1080 AVC AAC MP4)`      → 12
        `...[年齡限制版] - 06 (...)[887F6688].mp4`              → 6  ★
        `[BeanSub][...S4][23_95][CHS][1080P][x264_AAC].mp4`      → 95
        `[hyakuhuyu&LoliHouse] Re Zero ... - 82 [WebRip 1080p]`  → 82

    规则：从右往左找第一个"像集号"的数字 ——
      - 1 ~ _EP_MAX（挡掉 `[887F6688]` 哈希与 `1080` 分辨率）；
      - 前后紧邻字符不是字母数字（避免从 `S4`、`x264` 里切出数字）；
      - **右侧紧邻**不是 p / bit / fps / x+数字（挡掉 1080p、10bit）。
    集号通常排在番名之后、画质参数之前，所以从右往左第一个合格的
    就是它（`1080p` 那类会被规则排除，继续往左找）。
    """
    s = text or ""
    for m in reversed(list(re.finditer(
            r"(?<![A-Za-z0-9])(\d{1,4})(?![A-Za-z0-9])", s))):
        val = int(m.group(1))
        if not (0 < val <= _EP_MAX):
            continue
        if _EP_SUFFIX_BAD_RE.match(s[m.end():]):
            continue
        return val
    return 0


def _title_similar(a: str, b: str) -> float:
    """两个标题的**字符重合度**（0~1，按短串算）。

    用"逐字包含"而不是编辑距离：中/日文标题里发布组名与画质参数
    差异很大，但**作品名那几个字是相同的**（实测 `从后面来的神威先生`
    vs `從後面來的神威先生` 虽然简繁不同、仍有「神威先生」等字重合）。
    编辑距离会被这些差异淹没，按字统计更稳。
    """
    if not a or not b:
        return 0.0
    sa, sb = _norm_title(a), _norm_title(b)
    if not sa or not sb:
        return 0.0
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    hit = sum(1 for ch in short if ch in long_)
    return hit / len(short)


def _match_torrent(rec_title: str, torrents: dict) -> Optional[dict]:
    """把一条下载记录匹配到 qBittorrent 里的任务（拿不到返回 None）。

    **为什么要这么麻烦**（踩坑，实测"看不到进度条"）：
      ① `torrent_hash` 拿不到 —— `torrents_add` 只返回 "Ok."，
         不回传 hash，所以库里那列一直是 NULL；
      ② 按标题**精确匹配**也不行 —— 实测同一条内容：
             我们记录：[黒ネズミたち] 从后面来的神威先生 [年龄限制版]...
             qB 任务名：[Dynamis One] 從後面來的神威先生 [年齡限制版]...
         发布组不同（黑ネズミたち vs Dynamis One）、简繁也不同
         （qB 用的是**种子内的原始名**，而我们用的是 RSS 标题）。

    所以改用**两级判定**（宁可少匹配，也不要把进度显示到错的集上）：
        第一级：集号相同 **且** 标题字符重合度 ≥ 0.5 → 直接采纳
        第二级：集号相同 **且** 该集号在本订阅里唯一 → 采纳（兜底）
    两级都不满足就返回 None（界面不显示进度条，而不是显示错的）。
    """
    if not rec_title:
        return None
    want_ep = _ep_of(rec_title)
    if want_ep <= 0:
        return None

    # 候选：集号相同，或（集号取不到时）原样比较
    同集 = [c for c in torrents.values() if _ep_of(c.get("name", "")) == want_ep]
    if not 同集:
        return None

    # 第一级：字符重合度
    best, best_score = None, 0.0
    for c in 同集:
        s = _title_similar(rec_title, c.get("name", ""))
        if s > best_score:
            best, best_score = c, s
    if best is not None and best_score >= 0.5:
        return best

    # 第二级：该集号唯一 → 可以放心采纳（同一订阅同一集不会有两个种子，
    # 除非用户手动下过别的版本；那种情况就是上面第一级失败、这里也
    # 有歧义，此时**仍然采纳重合度最高的那个**，因为至少集号是对的）
    if len(同集) == 1:
        return 同集[0]
    return best if best is not None and best_score > 0 else None


class RssBridge(QObject):
    """RSS 订阅源与下载记录。"""

    sourcesChanged = Signal()
    downloadsChanged = Signal()
    failed = Signal(str)
    message = Signal(str)
    #: 推断出订阅源的动漫名（参数：来源 id, 名字）—— 界面据此预填输入框
    titleSuggested = Signal(int, str)
    #: 界面请求「立即检查」（QmlApp 接到后转给 RssService.poll()）。
    #:
    #: 参数 `source_id`：0 = 检查**全部**启用订阅（「立即检查」按钮）；
    #: >0 = 只检查该订阅（「下载器」保存后自动触发，见 `setDownloader`）。
    #: 为什么带上它（实测需求）：保存下载器后要**立刻**按新规则拉一次，
    #: 否则用户得等下一次定时轮询（默认 30 分钟），体验上就是"点了保存
    #: 却什么都没发生"。只查该订阅而不是全量 —— 用户刚改的就这一个，
    #: 没必要把所有订阅重新跑一遍（都是网络请求）。
    pollRequested = Signal(int)
    #: 界面请求「下发」某条待确认记录（参数：download_history.id）
    pushRequested = Signal(int)
    #: 轮询状态变化（进度文本 / 上次检查时间 / 结果）—— 订阅页据此刷新
    pollStateChanged = Signal()
    #: 下载器预览结果变化（列表 / 汇总文案 / 是否加载中）
    previewChanged = Signal()

    def __init__(self, db: Database, config: Optional[Config] = None,
                 api=None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        # Config / api 用于「从订阅源新建条目」（见 createSubjectFromSource）。
        # 都做成可选：本桥接层早期只做订阅管理，不联网也能工作，
        # 调用方（QmlApp）注入后才具备新建条目的能力。
        self._config = config
        self._api = api
        self._sources_cache: list[dict] = []
        self._sources_dirty = True
        # 推断标题的 worker（同一时刻只允许一个，防连点重复抓取）
        self._title_worker: Optional[_FeedTitleWorker] = None
        # 新建条目的 worker 列表（与 library 的收藏状态 worker 同一套做法）
        self._create_workers: list["_CreateSubjectWorker"] = []
        # ---- 轮询状态（订阅页顶部展示）----
        self._poll_running = False
        self._poll_progress = ""
        self._poll_summary = ""      # 上次检查的结果摘要
        self._poll_at = ""           # 上次检查时间（本地 ISO）
        # ---- qBittorrent 任务进度缓存（torrent_hash → {progress, state…}）----
        # 下载记录表里只有"我们下发的状态"（pending/pushed…），**真实进度**
        # 只有 qBittorrent 知道。按 hash 缓存一份，展示时叠加进去。
        self._torrents: dict[str, dict] = {}
        self._torrent_worker: Optional[_TorrentStatusWorker] = None
        # 注入的 qB 客户端（由 QmlApp 在启动/配置重建时给）
        self._qb = None
        # ---- 下载器预览 ----
        self._preview_items: list[dict] = []
        self._preview_running = False
        self._preview_hint = ""
        self._preview_worker: Optional[_PreviewWorker] = None

    def set_api(self, api) -> None:
        """注入 Bangumi 客户端（QmlApp 在配置重建时调用）。"""
        self._api = api

    def set_qb(self, qb) -> None:
        """注入 qBittorrent 客户端（QmlApp 在启动/配置重建时调用）。

        只用于**查进度**（`refreshTorrents`）；实际下发由 `RssService` 做
        （它持有自己的那份引用）。两边都要更新，否则会出现"设置页改了
        地址、下发用新地址、查进度还在问旧地址"的错位。
        """
        self._qb = qb

    def _subject_label(self, subject_id) -> str:
        """本地条目 id → 展示名（取不到返回空串）。"""
        sid = int(subject_id or 0)
        if not sid:
            return ""
        try:
            s = self._db.get_subject(sid)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("读取条目 %s 失败: %s", sid, e)
            return ""
        if s is None:
            return ""
        return s.name_cn or s.name or ""

    # ---------- 订阅源 ----------
    @Property("QVariantList", notify=sourcesChanged)
    def sources(self) -> list[dict]:
        """订阅源列表（含绑定条目名与下载统计）。"""
        if self._sources_dirty:
            self._sources_cache = self._load_sources()
            self._sources_dirty = False
        return self._sources_cache

    def _load_sources(self) -> list[dict]:
        try:
            rows = self._db.list_rss_sources()
        except Exception as e:
            log.exception("读取订阅源失败: %s", e)
            return []

        out: list[dict] = []
        for s in rows:
            # 绑定条目的展示名（用于界面上显示"→ XX动漫"）。
            #
            # **优先用 local_subject_id**（现在的权威绑定，见 database 的
            # rss_sources 表说明）：它对"已匹配"和"未匹配"的条目一视同仁。
            # 老库迁移已回填过，但这里仍保留 bangumi_id 的兜底反查 ——
            # 万一有行没回填到（比如迁移时那条 subject 还没入库），
            # 也不会把绑定显示成"无"。
            subject_name = ""
            local_id = int(s.local_subject_id or 0)
            try:
                if not local_id and s.bangumi_id:
                    local_id = self._db.find_subject_by_bangumi_id(s.bangumi_id) or 0
                if local_id:
                    subj = self._db.get_subject(local_id)
                    subject_name = (subj.name_cn or subj.name) if subj else ""
                    if subj is None:
                        local_id = 0    # 条目已被删：按未绑定处理
            except Exception as e:
                log.warning("读取订阅 #%s 的绑定条目失败: %s", s.id, e)

            # 下载统计（各状态计数）
            try:
                stats = self._db.count_downloads_by_status(s.id)
            except Exception:
                stats = {}

            out.append({
                "id": s.id,
                "name": s.name or "",
                "url": s.url or "",
                "bangumiId": int(s.bangumi_id or 0),
                "enabled": bool(s.enabled),
                "rule": s.rule or RULE_NEW_ONLY,
                # 用共用的 RULE_LABELS，避免与设置页的文案漂移
                "ruleLabel": RULE_LABELS.get(s.rule or RULE_NEW_ONLY,
                                             RULE_LABELS[RULE_NEW_ONLY]),
                "lastPollAt": s.last_poll_at or "",
                "lastError": s.last_error or "",
                "createdAt": s.created_at or "",
                "localSubjectId": local_id,
                "subjectName": subject_name,
                # v12：标题过滤与保存目标
                "mustInclude": s.must_include or "",
                "mustExclude": s.must_exclude or "",
                "saveSubjectId": int(s.save_subject_id or 0),
                "saveSubjectName": self._subject_label(s.save_subject_id),
                # v13：是否已保存过下载器（未保存 → 检查时只记录不下发）
                "downloaderSaved": bool(s.downloader_saved),
                "downloadCount": sum(stats.values()),
                # **统计口径修正**：数据库里成功写入的是 `done`
                # （见 rss_service.STATUS_DONE），而这里原先只数
                # `completed` —— 导致界面上"完成"永远显示 0（实测截图
                # "下载 10（完成 0）"，其实 10 条都下完了）。
                # 两个键都算上，兼容历史数据。
                "completedCount": int(stats.get("done", 0))
                                  + int(stats.get("completed", 0)),
                "failedCount": int(stats.get("failed", 0)),
            })
        return out

    @Slot()
    def reload(self) -> None:
        self._sources_dirty = True
        self._downloads_dirty = True
        self.sourcesChanged.emit()
        self.downloadsChanged.emit()

    # ---------- 轮询状态（订阅页顶部）----------
    @Property(bool, notify=pollStateChanged)
    def pollRunning(self) -> bool:
        """是否正在检查（界面据此禁用「立即检查」并显示"检查中…"）。"""
        return self._poll_running

    @Property(str, notify=pollStateChanged)
    def pollStatus(self) -> str:
        """顶部那一行状态文本：检查中显示进度，闲时显示上次结果 + 时间。

        为什么合成一个字符串而不是拆成三个 Property：界面就是**一行字**，
        拆开之后 QML 侧还要在绑定里拼装、处理三者都为空的组合，
        不如在这里算好（也便于以后改文案时只有一处）。
        """
        if self._poll_running:
            return self._poll_progress or "正在检查订阅…"
        if not self._poll_at:
            return "尚未检查过"
        parts = ["上次检查 " + self._poll_at]
        if self._poll_summary:
            parts.append(self._poll_summary)
        return " · ".join(parts)

    @Slot()
    def requestPoll(self) -> None:
        """界面点「立即检查」→ 转给 QmlApp（它持有 RssService）。"""
        if self._poll_running:
            self.message.emit("正在检查，请稍候…")
            return
        self.pollRequested.emit(0)          # 0 = 全部启用订阅

    @Slot(int)
    def requestPush(self, record_id: int) -> None:
        """界面点某条记录的「下发」→ 转给 QmlApp 做实际推送。"""
        if record_id <= 0:
            return
        self.pushRequested.emit(int(record_id))

    # ---- 由 QmlApp 连接（RssService 的信号 → 这里）----
    @Slot(str)
    def setProgress(self, text: str) -> None:
        """轮询进度文本（RssService.progress）。

        **第一次收到进度就说明轮询已开始** —— 这里顺手把 running 置真，
        因为 RssService 没有单独的"开始"信号，而界面需要立刻能看到反馈
        （否则点了「立即检查」后几秒内毫无动静，用户会以为没生效）。
        """
        if not self._poll_running:
            self._poll_running = True
        self._poll_progress = text or ""
        self.pollStateChanged.emit()

    @Slot(object)
    def onPollFinished(self, summary) -> None:
        """轮询结束：记录时间与结果摘要，并刷新下载记录列表。

        `summary` 是 `rss_service.PollSummary`（普通 dataclass，不是
        QObject）—— 用 `object` 类型接收，逐字段取值时做防御性判断，
        避免它将来加字段/改结构时这里直接崩。
        """
        from datetime import datetime
        self._poll_running = False
        self._poll_progress = ""
        self._poll_at = datetime.now().astimezone().isoformat(
            timespec="seconds")[:19].replace("T", " ")
        try:
            new = int(getattr(summary, "new_entries", 0) or 0)
            pushed = int(getattr(summary, "pushed", 0) or 0)
            pend = int(getattr(summary, "pending", 0) or 0)
            filt = int(getattr(summary, "filtered", 0) or 0)
            errs = list(getattr(summary, "errors", []) or [])
            total = int(getattr(summary, "total_entries", 0) or 0)
        except Exception:            # pragma: no cover - 防御性
            new = pushed = pend = filt = total = 0
            errs = []

        if total == 0:
            self._poll_summary = "没有可检查的订阅源"
        elif new == 0:
            self._poll_summary = f"无新内容（共 {total} 条）"
        else:
            seg = [f"新 {new} 条"]
            if pushed:
                seg.append(f"已下发 {pushed}")
            if pend:
                seg.append(f"待确认 {pend}")
            # 过滤数量单独说一句：用户设了规则就想知道它有没有生效
            # （看到"过滤 8 条"才知道规则拦下了东西）
            if filt:
                seg.append(f"规则过滤 {filt} 条")
            self._poll_summary = "，".join(seg)

        # 有错误时把第一条带出来 —— 否则用户只看到"新 0 条"却发现订阅
        # 列表里挂着红字错误，不知道为什么（见订阅卡片的 lastError）
        if errs:
            self._poll_summary += f"；{errs[0]}"

        self._sources_dirty = True
        self._downloads_dirty = True
        self.pollStateChanged.emit()
        self.sourcesChanged.emit()
        self.downloadsChanged.emit()
        if new or errs or filt:
            self.message.emit(self._poll_summary)

    @Slot(int, bool, str)
    def onPushResult(self, record_id: int, ok: bool, msg: str) -> None:
        """「下发」的结果（QmlApp 调 rss_service.push_pending 之后回传）。"""
        self._downloads_dirty = True
        self.downloadsChanged.emit()
        if ok:
            self.message.emit(msg or "已下发")
        else:
            self.failed.emit(msg or "下发失败")

    @Slot(str, str, str, result=int)
    def addSource(self, name: str, url: str, rule: str = RULE_NEW_ONLY) -> int:
        """新增订阅源，返回新 ID（失败返回 0）。

        **新增时不能设过滤词与保存目标**（那两样在「下载器」弹窗里配，
        需要一个已存在的订阅 id），所以这里只建基本字段；用户建完订阅
        再点「下载器」补上即可。
        """
        name = (name or "").strip()
        url = (url or "").strip()
        if not url:
            self.failed.emit("订阅地址不能为空")
            return 0
        if not (url.startswith("http://") or url.startswith("https://")):
            self.failed.emit("订阅地址需以 http:// 或 https:// 开头")
            return 0
        if rule not in RULES:
            rule = RULE_NEW_ONLY
        # 名称留空时用域名兜底，避免列表里出现空白项
        if not name:
            name = url.split("//", 1)[-1].split("/", 1)[0]

        try:
            sid = self._db.add_rss_source(name=name, url=url, rule=rule)
        except Exception as e:
            log.exception("新增订阅源失败: %s", e)
            self.failed.emit(f"新增失败：{e}")
            return 0

        log.info("新增订阅源 #%s：%s", sid, url)
        self.reload()
        self.message.emit(f"已添加订阅「{name}」")
        return sid

    @Slot(int, str, str, str, bool, result=bool)
    def updateSource(
        self,
        source_id: int,
        name: str,
        url: str,
        rule: str,
        enabled: bool,
    ) -> bool:
        """更新订阅源（名称 / 地址 / 规则 / 启用状态）。"""
        url = (url or "").strip()
        if not url:
            self.failed.emit("订阅地址不能为空")
            return False
        if rule not in RULES:
            rule = RULE_NEW_ONLY
        try:
            self._db.update_rss_source(
                source_id,
                name=(name or "").strip(),
                url=url,
                rule=rule,
                enabled=1 if enabled else 0,
            )
        except Exception as e:
            log.exception("更新订阅源 %s 失败: %s", source_id, e)
            self.failed.emit(f"保存失败：{e}")
            return False
        self.reload()
        self.message.emit("订阅已保存")
        return True

    @Slot(int, str, str, int, result=bool)
    def setDownloader(self, source_id: int, must_include: str,
                      must_exclude: str, save_subject_id: int = 0) -> bool:
        """「下载器」弹窗的保存：标题过滤 + 保存到指定条目的目录。

        **为什么单独一个 Slot 而不是并进 `updateSource`**：`updateSource`
        是编辑表单用的（名称/地址/规则，`enabled` 还是它自己传死的
        `True`）；这里两个功能各弹各的窗、互不覆盖，混在一起会导致
        "改过滤词时把用户刚在编辑表单里改的名字冲掉"这类问题。

        `save_subject_id` 传 0 = 不指定（用 qBittorrent 的全局保存路径）。
        校验：指定的条目必须存在，否则拒绝（避免存下一个指向空条目的 id，
        之后每次下发都白算一遍路径）。
        """
        vals = {
            "must_include": (must_include or "").strip(),
            "must_exclude": (must_exclude or "").strip(),
            # **保存过 = 启用下载**（v13 的闸门，见 database 建表处说明）。
            # 用户需求："下载器必须点保存才能启用这个订阅的下载"。
            "downloader_saved": 1,
        }
        sid = int(save_subject_id or 0)
        if sid:
            try:
                subj = self._db.get_subject(sid)
            except Exception as e:
                log.exception("读取条目 %s 失败: %s", sid, e)
                self.failed.emit("读取条目失败，请重试")
                return False
            if subj is None:
                self.failed.emit("指定的条目不存在，请重新选择")
                return False
            vals["save_subject_id"] = sid
        else:
            vals["save_subject_id"] = None
        try:
            self._db.update_rss_source(source_id, **vals)
        except Exception as e:
            log.exception("保存下载器设置失败 #%s: %s", source_id, e)
            self.failed.emit(f"保存失败：{e}")
            return False
        log.info("订阅 #%s 下载器设置已保存：包含=%r 排除=%r 保存到=%s",
                 source_id, vals["must_include"], vals["must_exclude"],
                 sid or "默认")
        self.reload()

        # ---- 保存后**立即启动下载**（实测反馈"下载器保存后没开始下载"）----
        #
        # 原因：下载本来是**定时轮询**触发的（默认 30 分钟一次），保存
        # 设置只是改了数据库 —— 用户当然会觉得"点了保存却什么都没发生"。
        # 而"点保存"在语义上就是**启用**（v13 的闸门），启用后理应马上
        # 生效。这里只触发**该订阅**的检查，不重跑全部。
        #
        # 正在检查时**不排队**：`poll()` 内部对重入是"跳过本次"，此处
        # 提前告知用户，免得他以为按钮没反应。等当前这轮结束再点一次即可。
        if self._poll_running:
            self.message.emit("下载器设置已保存；当前正在检查，稍后自动生效")
        else:
            self.message.emit("下载器设置已保存，正在按新规则检查…")
            self.pollRequested.emit(int(source_id))
        return True

    # ---------- 下载器预览 ----------
    @Property("QVariantList", notify=previewChanged)
    def previewItems(self) -> list[dict]:
        """预览结果（「下载器」右侧列表）。"""
        return self._preview_items

    @Property(bool, notify=previewChanged)
    def previewRunning(self) -> bool:
        """是否正在抓取预览（界面据此显示"加载中"并防连点）。"""
        return self._preview_running

    @Property(str, notify=previewChanged)
    def previewSummary(self) -> str:
        """预览的汇总文案（"将下载 3 集 / 跳过 9 集"）。"""
        if self._preview_running:
            return "正在抓取订阅源…"
        if not self._preview_items:
            return self._preview_hint
        n_down = sum(1 for x in self._preview_items if x.get("action") == "download")
        n_skip = sum(1 for x in self._preview_items if x.get("action") == "skip")
        n_filt = sum(1 for x in self._preview_items
                     if x.get("action") == "filtered")
        seg = [f"将下载 {n_down} 集"]
        if n_skip:
            seg.append(f"跳过 {n_skip}")
        if n_filt:
            seg.append(f"被过滤 {n_filt}")
        return "，".join(seg)

    @Slot(int, str, str)
    def requestPreview(self, source_id: int, must_include: str,
                       must_exclude: str) -> None:
        """抓一次订阅源并跑判新，把"会下载哪些集"回传给界面。

        **由界面在过滤词变化后调用（带防抖）** —— 用户要求"填完过滤词
        实时出现"。防抖在 QML 侧做（600ms），这里只保证同一时刻只有一个
        worker（重复触发直接忽略）。
        """
        w = self._preview_worker
        if w is not None and w.isRunning():
            return
        try:
            src = next((s for s in self._db.list_rss_sources()
                        if s.id == int(source_id)), None)
        except Exception as e:
            log.exception("预览读取订阅失败: %s", e)
            src = None
        if src is None:
            self._preview_hint = "订阅不存在"
            self._preview_items = []
            self.previewChanged.emit()
            return
        if not (src.url or "").strip():
            self._preview_hint = "该订阅还没有填地址"
            self._preview_items = []
            self.previewChanged.emit()
            return

        self._preview_running = True
        self._preview_hint = ""
        self.previewChanged.emit()
        self._preview_worker = _PreviewWorker(
            self._db, self._config, src, must_include, must_exclude)
        self._preview_worker.done.connect(self._on_preview)
        self._preview_worker.finished.connect(self._preview_worker.deleteLater)
        self._preview_worker.start()

    def _on_preview(self, source_id: int, items: list, error: str) -> None:
        self._preview_worker = None
        self._preview_running = False
        if error:
            self._preview_items = []
            self._preview_hint = f"抓取失败：{error}"
        else:
            self._preview_items = items or []
            self._preview_hint = "订阅源里没有条目" if not items else ""
        self.previewChanged.emit()

    @Slot()
    def clearPreview(self) -> None:
        """关下载器弹窗时清空预览（下次打开重新算）。"""
        self._preview_items = []
        self._preview_hint = ""
        self._preview_running = False
        self.previewChanged.emit()

    @Slot(int, bool)
    def setEnabled(self, source_id: int, enabled: bool) -> None:
        """仅切换启用状态（列表里的开关）。"""
        try:
            self._db.update_rss_source(source_id, enabled=1 if enabled else 0)
        except Exception as e:
            log.exception("切换订阅状态失败: %s", e)
            self.failed.emit(f"切换失败：{e}")
            return
        self.reload()

    @Slot(int, int)
    def linkSubject(self, source_id: int, subject_id: int) -> None:
        """把订阅绑定到本地条目（`subject_id` 是**本地 subjects.id**）。

        绑定后「三层判新」才能知道该订阅对应哪部动漫、本地已有哪些集。
        `subject_id=0` 表示解除绑定。

        **不再要求条目已匹配 Bangumi**（踩坑，实测反馈）：早期这里有一道
        `if not subj.bangumi_id: 拒绝`，于是"没填 Token"或"匹配没成功"的
        条目**永远绑不上** —— 而「全部下载 + 从订阅源新建条目」这条链路上
        建出来的恰恰都是这种本地条目（新建成功了却报"无法绑定"）。
        现在权威标识是 `local_subject_id`，`bangumi_id` 改为顺带冗余存一份
        （有就存，没有就留空），两种条目一视同仁。
        """
        try:
            if subject_id <= 0:
                self._db.update_rss_source(source_id,
                                           local_subject_id=None,
                                           bangumi_id=None)
                self.message.emit("已解除绑定")
            else:
                subj = self._db.get_subject(subject_id)
                if subj is None:
                    self.failed.emit("条目不存在")
                    return
                # bangumi_id 可空：未匹配条目照样能绑（见 docstring）
                self._db.update_rss_source(
                    source_id,
                    local_subject_id=subj.id,
                    bangumi_id=subj.bangumi_id or None,
                )
                self.message.emit(
                    f"已绑定到「{subj.name_cn or subj.name}」")
        except Exception as e:
            log.exception("绑定订阅 %s 失败: %s", source_id, e)
            self.failed.emit(f"绑定失败：{e}")
            return
        self.reload()

    @Slot(int, result=bool)
    def removeSource(self, source_id: int) -> bool:
        """删除订阅源（连带删除其下载记录，外键 CASCADE）。"""
        try:
            # 外键 ON DELETE CASCADE 已在 schema 里声明，
            # 但 download_history 的 source_id 是 REFERENCES rss_sources(id)
            # 且 PRAGMA foreign_keys=ON 已开，因此会级联删除。
            self._db.delete_rss_source(source_id)
        except Exception as e:
            log.exception("删除订阅源 %s 失败: %s", source_id, e)
            self.failed.emit(f"删除失败：{e}")
            return False
        log.info("已删除订阅源 #%s", source_id)
        self.reload()
        self.message.emit("订阅已删除")
        return True

    # ---------- 下载记录 ----------
    _downloads_dirty = True
    _downloads_cache: list[dict] = []

    @Property("QVariantList", notify=downloadsChanged)
    def downloads(self) -> list[dict]:
        """全部下载记录（按集序号倒序）。"""
        if self._downloads_dirty:
            self._downloads_cache = self._load_downloads()
            self._downloads_dirty = False
        return self._downloads_cache

    def _load_downloads(self) -> list[dict]:
        out: list[dict] = []
        try:
            rows = self._db.list_downloads()
        except Exception as e:
            log.exception("读取下载记录失败: %s", e)
            return out
        for r in rows:
            status = r.status or "pending"
            # 本地下发状态 → 界面文案。注意数据库里存的值与 STATUS_LABELS
            # 的键**不完全一致**：`rss_service` 写入的是
            # pending/pushed/downloading/done/failed/skipped，而
            # STATUS_LABELS 里写的是 completed 而不是 done。
            # 两者都对不上时会让"已完成"显示成英文 done（踩坑），
            # 这里补一条 done → 已完成 的映射兜住。
            label = STATUS_LABELS.get(status, "")
            if not label:
                label = "已完成" if status == "done" else status

            # 叠加 qBittorrent 的真实进度。
            #
            # **匹配方式：先 hash、再按标题**（踩坑，实测"看不到进度条"）。
            #
            # `torrent_hash` 是 qBittorrent 按种子**内容**算出来的，我们
            # 下发时拿不到（`torrents_add` 只返回 "Ok."，不回传 hash），
            # 所以库里那一列一直是 NULL —— 只按 hash 匹配的话**永远查不到**，
            # 界面上进度条一根都不会出现（实测现象）。
            #
            # 按标题匹配是可行的：RSS 条目标题与 qBittorrent 里的任务名
            # 通常一致（qB 用种子里的 name，站点一般就用标题当 name）。
            # 归一后比较（去空格标点、小写），避免"差一个空格就不匹配"。
            info = None
            th = (r.torrent_hash or "").strip().lower()
            if th:
                info = self._torrents.get(th)
            if info is None and self._torrents:
                # hash 一般拿不到（见 _match_torrent 的说明），走"集号 +
                # 标题重合度"的两级匹配
                info = _match_torrent(r.torrent_title or "", self._torrents)

            progress = 0.0
            state_label = ""
            if info is not None:
                progress = float(info.get("progress") or 0.0)
                state_label = _QB_STATE_LABELS.get(
                    str(info.get("state") or "").lower(),
                    str(info.get("state") or ""))
                # 有真实进度时以它为准：数据库那个 pushed 只是"已交给
                # qBittorrent"，不代表正在下 / 下完了
                if progress >= 1.0:
                    label = "已完成"
                    state_label = ""

            out.append({
                "id": r.id,
                "sourceId": r.source_id,
                "subjectId": int(r.subject_id or 0),
                "epIndex": float(r.ep_index or 0),
                "torrentTitle": r.torrent_title or "",
                "status": status,
                "statusLabel": label,
                # 真实下载进度 0~1（-1 = 拿不到，QML 侧据此不画进度条）
                "progress": progress if info is not None else -1.0,
                "stateLabel": state_label,
                # 能不能点「下发」：**待确认** 且 qBittorrent 里**还没有**该任务。
                #
                # **为什么必须加 `info is None`**（实测 bug）：原先只看
                # 数据库的 `status == "pending"`，而数据库状态会滞后 ——
                # 记录先以 pending 入库、推送成功后才改成 pushed，中间
                # 有个窗口；更常见的是**旧版本留下的 pending 记录**
                # （当时的 setDownloader 不触发检查，记录一直挂在待确认）。
                # 此时 qBittorrent 里其实已经在下甚至下完了，界面会因为
                # `info` 匹配到真实进度而显示「已完成 100%」，可
                # `canPush` 仍为 true → 已完成的行上挂着一个「下发」按钮
                # （实测截图正是这样）。再点一次会**重复添加同一任务**。
                # 判据加上"qB 里没有"之后，这类行就不会再出现按钮。
                "canPush": (status == "pending"
                            and int(r.ep_index or 0) >= 0
                            and info is None),
                # 失败原因（v11 落库）。**失败记录必须能说清为什么** ——
                # 实测反馈"下载失败了，可以增加日志判断为什么失败吗"：
                # 原先只有「失败 10」这个计数，用户完全无从下手。
                "lastError": r.last_error or "",
                "createdAt": r.created_at or "",
            })
        return out

    @Slot()
    def refreshTorrents(self) -> None:
        """异步拉一次 qBittorrent 任务列表（订阅页显示 / 5 秒定时器调用）。

        **重入保护**：同一时刻只跑一个 worker。订阅页有个 5 秒定时器在
        调这里（见 QML 的 progressTimer），若上一次还没回来（qBittorrent
        没开时单次要等 3 秒超时），直接跳过本次 —— 否则会不断新建线程，
        超时叠加时线程数会滚起来。

        **注意判据是 `isRunning()` 而不是 `is not None`**：worker 结束后
        `_on_torrents` 里会把它置回 None，但 deleteLater 是排队执行的，
        中间存在"引用还在、但已经跑完"的瞬间；只看非 None 会白白跳过一轮。
        """
        w = self._torrent_worker
        if w is not None and w.isRunning():
            return
        self._torrent_worker = _TorrentStatusWorker(self._qb)
        self._torrent_worker.done.connect(self._on_torrents)
        self._torrent_worker.finished.connect(self._torrent_worker.deleteLater)
        self._torrent_worker.start()

    #: 进度/状态变化的判定精度（百分点）。qBittorrent 的 `progress` 是
    #: 浮点，每 5 秒几乎一定在动（0.1701 → 0.1703），若按精确值比较，
    #: 这个"只在变化时通知"的优化就形同虚设。
    _PROGRESS_EPS = 0.01

    def _on_torrents(self, torrents: dict, error: str) -> None:
        self._torrent_worker = None
        new = torrents or {}
        # **只在"看得见的变化"时才通知 QML**（重要：5 秒定时器在调这里）。
        #
        # 为什么必须挡一层：emit 会让 QML 重新读 `downloads` Property，
        # 拿到一个**新数组**，Repeater 的 model 因此变化 → **整个列表
        # 重建**。后果有两个：
        #   ① 进度条上的 `Behavior on width` 每 5 秒被重置一次，
        #      数字看起来一跳一跳而不是平滑走；
        #   ② 用户正在滚动列表时会被打断（内容高度重建）。
        #
        # 判定用"**有实质变化**"：进度差 ≥ 1 个百分点、状态变了、
        # 或任务数量变了（新增/完成）。低于这个阈值的变化用户看不出来，
        # 不值得重建整个列表。
        changed = len(new) != len(self._torrents)
        if not changed:
            for h, info in new.items():
                old = self._torrents.get(h)
                if old is None:
                    changed = True
                    break
                if old.get("state") != info.get("state"):
                    changed = True
                    break
                try:
                    if abs(float(old.get("progress") or 0.0)
                           - float(info.get("progress") or 0.0)) >= self._PROGRESS_EPS:
                        changed = True
                        break
                except (TypeError, ValueError):
                    changed = True
                    break
        self._torrents = new
        if not changed:
            return
        self._downloads_dirty = True
        self.downloadsChanged.emit()

    @Slot(int, result="QVariantMap")
    def downloadStats(self, source_id: int) -> dict:
        """单个订阅的下载统计（失败/完成计数）。"""
        try:
            stats = self._db.count_downloads_by_status(source_id)
        except Exception as e:
            log.exception("统计下载失败: %s", e)
            return {}
        return {k: int(v) for k, v in stats.items()}

    # ---------- 提示 ----------
    @Slot(result="QVariantList")
    def ruleOptions(self) -> list[dict]:
        """判新规则选项（QML 的下拉框用）。"""
        return [
            {"value": RULE_NEW_ONLY, "label": "只下新集（本地没有的）"},
            {"value": RULE_ALL, "label": "全部下载"},
        ]

    # ---------- 从订阅源新建条目（「全部下载」场景）----------
    @Slot(str, result=bool)
    def suggestSubjectName(self, url: str) -> bool:
        """抓一次订阅源，推断动漫名，经 `titleSuggested` 回传。

        为什么是异步：抓 RSS 是网络请求（超时 15s），同步做界面会卡住。
        返回 True 表示**已开始**（不是"已完成"）。

        用于界面上的「从订阅源获取名称」按钮：先自动填入文本框，用户
        再手动改成自己想要的名字。
        """
        url = (url or "").strip()
        if not url:
            self.failed.emit("请先填写订阅地址")
            return False
        if not (url.startswith("http://") or url.startswith("https://")):
            self.failed.emit("订阅地址需以 http:// 或 https:// 开头")
            return False
        w = self._title_worker
        if w is not None and w.isRunning():
            self.message.emit("正在获取，请稍候…")
            return False

        proxy = self._config.get("bangumi", "proxy", "") if self._config else ""
        ua = self._config.get("bangumi", "user_agent",
                              "AnimeMarker/1.0") if self._config else "AnimeMarker/1.0"
        # source_id 传 0：这一步还没入库，不需要 id
        self._title_worker = _FeedTitleWorker(0, url, proxy=proxy, user_agent=ua)
        self._title_worker.done.connect(self._on_title_suggested)
        self._title_worker.finished.connect(self._title_worker.deleteLater)
        self._title_worker.start()
        self.message.emit("正在获取订阅源标题…")
        return True

    def _on_title_suggested(self, source_id: int, name: str, error: str) -> None:
        self._title_worker = None
        if error:
            self.failed.emit(error)
            return
        if not name:
            # 抓到了但清洗后为空（标题全是噪声）：不算错误，让用户手填
            self.message.emit("没能从订阅源推断出名称，请手动填写")
            return
        self.titleSuggested.emit(int(source_id), name)
        self.message.emit(f"已获取名称：{name}")

    @Slot(int, str, bool, result=bool)
    def createSubjectFromSource(self, source_id: int, name: str,
                                match: bool = True) -> bool:
        """用给定名字新建本地条目，并**绑定到该订阅**。

        需求："需要匹配，没填 token 则跳过。一个订阅源一个条目。"

        `match=True` 且有 Token 时走 Bangumi 匹配（拿封面/集数/别名/tag）；
        否则直接建本地条目。匹配失败**不报错**，退化为本地条目 ——
        用户的核心诉求是"把订阅和条目对应起来"，匹配只是加分项。

        成功后自动 `linkSubject`（绑定），这样一次操作就完成"建条目 + 绑定"，
        用户不必再点一次「绑定条目」。

        返回 True 表示已开始（异步），结果经 `message`/`failed` 与
        `sourcesChanged` 通知。
        """
        name = (name or "").strip()
        if not name:
            self.failed.emit("条目名不能为空")
            return False
        if source_id <= 0:
            self.failed.emit("请先保存订阅源，再新建条目")
            return False
        has_token = bool(self._api is not None and self._can_match())
        do_match = bool(match) and has_token
        if match and not has_token:
            # 用户明确勾了匹配但没 Token：说清楚为什么不匹配，
            # 否则会以为程序坏了（用户要求"没填 token 则跳过"）
            self.message.emit("未配置 Token，跳过 Bangumi 匹配，仅新建本地条目")

        score = self._config.getint("scanner", "accept_score", 60) if self._config else 60
        gap = self._config.getint("scanner", "accept_gap", 20) if self._config else 20
        w = _CreateSubjectWorker(self._db, self._api, source_id, name,
                                 do_match=do_match,
                                 accept_score=score, accept_gap=gap)
        self._create_workers.append(w)
        w.done.connect(self._on_subject_created)
        w.finished.connect(lambda: self._drop_create_worker(w))
        w.start()
        return True

    def _can_match(self) -> bool:
        """能否走 Bangumi 匹配（有 Token 才行）。"""
        if self._config is None:
            return False
        return bool((self._config.get("bangumi", "token", "") or "").strip())

    def _drop_create_worker(self, w: "_CreateSubjectWorker") -> None:
        try:
            self._create_workers.remove(w)
        except ValueError:
            pass
        w.deleteLater()

    def _on_subject_created(self, source_id: int, subject_id: int,
                            error: str) -> None:
        if error:
            self.failed.emit(error)
            return
        # 建完立刻绑定（一次操作完成两件事）。linkSubject 现在**不再要求
        # 条目已匹配 Bangumi**，所以未匹配的本地条目也能绑上。
        self.linkSubject(int(source_id), int(subject_id))
        subj = self._db.get_subject(int(subject_id))
        label = (subj.name_cn or subj.name) if subj else ""
        if subj is not None and not subj.bangumi_id:
            self.message.emit(f"已新建并绑定条目「{label}」（未匹配 Bangumi）")
        else:
            self.message.emit(f"已新建并绑定条目「{label}」")

    def waitWorkers(self, ms: int = 3000) -> None:
        """退出时等待进行中的网络线程（QmlApp.shutdown 调用）。"""
        for w in [self._title_worker, self._torrent_worker,
                  self._preview_worker, *self._create_workers]:
            if w is not None and w.isRunning():
                log.info("等待订阅相关线程结束…")
                w.wait(ms)
