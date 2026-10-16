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

import copy
import difflib
import logging
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from app.core.bangumi_api import extract_aliases
from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.matcher import SubjectMatcher
from app.core.rss_feed import RssError, fetch_and_parse
from app.core.rss_matcher import RssMatcher
from app.utils.title_parser import guess_anime_names
from app.utils.qt_thread import NamedThread, retire_worker

log = logging.getLogger(__name__)


def _with_save_subject(src: RssSource, subject_id: int) -> RssSource:
    """返回一个「把保存条目换成 subject_id」的**副本**（不落库）。

    用于「下载器」弹窗的路径预览：用户在下拉里改选了条目、还没点保存，
    界面就要显示那个条目的路径。直接改原对象会把没保存的选择写进内存
    里的订阅，之后 `reload()` 又拿数据库覆盖 —— 状态会漂移。
    复制一份既拿到结果，又不影响任何真实状态。
    """
    try:
        clone = copy.copy(src)
        clone.save_subject_id = int(subject_id)
        return clone
    except Exception:               # pragma: no cover - dataclass 不可写时
        return src


# 判新规则（决定"哪些集才下载"）
#
# 两者的**语义差别**（详见 rss_matcher.judge 里那段说明）：
#   new_only —— 本地媒体库**已经有这一集**就跳过（"新"= 我还没有的）
#   all      —— 不看本地有什么，只要没下载过就下（补齐全集用）
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
# "completed"，于是"已完成"的记录会显示成英文 `done`。
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


class _FeedTitleWorker(NamedThread):
    """抓取订阅源，从最近几条条目里推断出**动漫名**，并顺手预匹配 Bangumi。

    为什么要线程：抓 RSS 是网络请求（超时 15s），同步做会把界面卡住；
    加了预匹配之后还多一次 Bangumi 搜索，更不能放主线程。

    **为什么是"推断"而不是"取第一条标题"**：RSS 的条目标题是**资源标题**，
    形如 `[Nekomoe kissaten&LoliHouse] 碧蓝之海 第三季 [09][WebRip 1080p]` ——
    带字幕组、集数、画质参数。直接拿去搜 Bangumi 基本搜不到，必须先清洗
    （`guess_anime_names` 会去掉方括号块、集数、画质词）。

    推断策略：
      ① 最近几条 item 标题各自清洗+**拆候选**，取"候选合起来最长"的那条 ——
         最长的那条通常信息最全（最短的可能是"第 10 集"这种只有集号的）；
         清洗用 `guess_anime_names`：标题通篇是括号时（`【组名】【番名】
         [01][1080p]`，comicat 等站点整源如此），它会回头从括号里挑番名，
         否则这一步会全军覆没；
      ② 有 Token 时拿**整组候选**搜一次 Bangumi（预匹配）——
         候选之间是独立关键词，`青之芦苇 第二季 / Ao Ashi Season 2`
         整串搜不到、拆开各自都能命中，见 `guess_anime_names`；
      ③ 全失败 → 返回空串，让用户手填。

    **预匹配为什么要在这里做**：做完之后"认成了哪一部"就能在弹窗里显示，
    用户看着原名/别名决定要不要改名字，再点「新建并绑定」——
    否则要等条目都写进库了才知道匹配错了。`createSubjectFromSource`
    会复用这一步的结果，**不再搜第二次**，"看到的"与"建出来的"必然同一部。
    """

    #: (来源 id, 推断出的名字, 错误信息, **请求用的地址**,
    #:  多候选关键词, 预匹配结果 dict)
    #:
    #: **地址要原样带回来**：这一步的 `source_id` 恒为 0（订阅还没入库），
    #: 拿不到 id 就无从反查地址 —— 不给地址的话，日志与提示里只能说
    #: "没能推断出名称"，用户不知道是哪个订阅、更不知道是网络问题还是
    #: 地址填错了。
    #:
    #: 预匹配结果 dict 的字段：
    #:   matched/subject —— 是否匹配上、匹配到的 Bangumi 条目（原样响应）
    #:   reason          —— `MatchResult.reason`（打印用）
    #:   network_failed/network_error —— 是"搜不到"还是"根本没搜成"（要分开说）
    #:   skipped         —— 没 Token/没开匹配，这一步被跳过
    #: 用 `object` 而不是 `dict`：PySide 会把 `dict` 当 QVariantMap 深拷贝，
    #: 而我们要原样把 Bangumi 响应（含 infobox 列表）带回主线程。
    done = Signal(int, str, str, str, list, object)

    def __init__(self, source_id: int, url: str, proxy: str = "",
                 user_agent: str = "AnimeMarker/1.0",
                 api=None, accept_score: int = 60, accept_gap: int = 20,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._source_id = source_id
        self._url = url
        self._proxy = proxy
        self._ua = user_agent
        self._api = api                      # None = 没 Token，跳过预匹配
        self._accept_score = accept_score
        self._accept_gap = accept_gap

    def run(self) -> None:
        # 开工先记一行：把**实际要抓的地址**、代理写下来。
        # 排查"获取失败"时第一件要确认的就是"到底请求了哪个地址"。
        log.info("「获取名称」开始抓取订阅源：%s（代理=%s）",
                 self._url, self._proxy or "无")
        try:
            entries = fetch_and_parse(self._url, proxy=self._proxy,
                                      user_agent=self._ua)
        except RssError as e:
            log.warning("「获取名称」抓取订阅源失败：%s —— %s", self._url, e)
            self.done.emit(self._source_id, "", str(e), self._url, [], {})
            return
        except Exception as e:              # pragma: no cover - 防御性
            log.exception("「获取名称」抓取订阅源异常：%s", self._url)
            self.done.emit(self._source_id, "", f"抓取失败：{e}", self._url, [], {})
            return

        if not entries:
            log.warning("「获取名称」订阅源没有条目：%s", self._url)
            self.done.emit(self._source_id, "", "该订阅源没有条目", self._url, [], {})
            return

        # 最近若干条（新的通常在前）拆出候选后比长度，取候选最全的那条。
        # 只取前 10 条：更早的多半是同一部番的历史集，长度不提供新信息。
        #
        # 用 `guess_anime_names` 而不是 `clean_anime_title`：后者对
        # 「【组名】【番名】[85][1080p]」这种通篇括号的标题会清成一个字都不剩
        # ，前者会回头从括号里挑出番名。
        best: list[str] = []
        best_raw = ""
        for e in entries[:10]:
            raw = (e.title or "").strip()
            names = guess_anime_names(raw)
            if sum(len(n) for n in names) > sum(len(n) for n in best):
                best, best_raw = names, raw
        # 采样与结果都记下来：清洗结果为空时，光看"没推断出来"无从判断
        # 是标题格式变了、还是这个源本身没有可用标题。
        # **认不出时必须把原样标题写进日志**：早期这里只记"取自标题 ''"，
        # 于是日志看不出源长什么样、还得再联网抓一次才知道是括号的问题。
        if best:
            log.info("「获取名称」抓取成功：%s（共 %d 条）；候选 %s（取自标题 %r）",
                     self._url, len(entries), best, best_raw[:80])
        else:
            log.warning("「获取名称」抓取成功：%s（共 %d 条）但推不出名字；"
                        "前几条标题原样：%s",
                        self._url, len(entries),
                        [e.title for e in entries[:3]])

        preview = self._match(best)
        self.done.emit(self._source_id, best[0] if best else "", "", self._url,
                       best, preview)

    def _match(self, names: list[str]) -> dict:
        """拿候选搜一次 Bangumi（预匹配）。任何失败都只记日志、不打断"获取"。"""
        if not names:
            return {}
        if self._api is None:
            # 没 Token 就跳过 —— 与「新建并绑定」的取舍一致：核心诉求是
            # "把订阅和条目对应起来"，匹配是加分项，不能因此让「获取」失效。
            log.info("「获取名称」跳过预匹配（未配置 Token）")
            return {"matched": False, "subject": None, "skipped": True}
        try:
            matcher = SubjectMatcher(
                self._api, season_mode="cn",
                accept_score=self._accept_score, accept_gap=self._accept_gap,
            )
            result = matcher.search_best(names, 0)
        except Exception as e:
            # 匹配失败不影响"名字已经拿到了"这件事，界面照样预填
            log.warning("「获取名称」预匹配异常（名字仍会填入）：%s", e)
            return {"matched": False, "subject": None,
                    "network_failed": True, "network_error": str(e)}
        log.info("「获取名称」预匹配：候选 %s → %s（%s）",
                 names, (result.subject or {}).get("name_cn")
                 or (result.subject or {}).get("name") or "未匹配",
                 result.reason)
        return {
            "matched": result.subject is not None,
            "subject": result.subject,
            "reason": result.reason,
            "network_failed": result.network_failed,
            "network_error": result.network_error,
        }


class _CreateSubjectWorker(NamedThread):
    """从订阅源新建本地条目（可带 Bangumi 匹配）。

    需求："条目名先从订阅源中获取，自动填入文本框，然后支持
    手动修改。需要匹配，没填 token 则跳过。一个订阅源一个条目。"

    所以这个 worker 做两件事，取决于有没有 Token：
      ① 有 Token → 拿用户确认的名字搜 Bangumi，匹配上就**带封面/集数/别名/
         tag 入库**（与扫描入库同一套字段），没匹配上则退化为本地条目；
      ② 没 Token → **跳过匹配**，直接建本地条目（match_state='manual'）。

    **`keywords` 是整组候选**（`guess_anime_names` 拆出来的"中文名 / 原名"），
    不是只有输入框里那一个 —— RSS 标题常把两个名字并排写在一起，整串搜
    什么都搜不到，拆开逐个搜才命中（见 `guess_anime_names`）。
    输入框里的名字按用户意图排在最前。

    **`pre_matched` 是「获取」时已经搜好的结果**：给了它就**不再搜第二次**。
    这样"弹窗里显示的匹配"与"实际入库的条目"必然是同一部 ——
    否则用户看到的是 A、两次搜索之间结果浮动到 B，差异不可解释。

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
                 keywords: Optional[list] = None,
                 pre_matched: Optional[dict] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._source_id = source_id
        self._name = name
        self._do_match = do_match
        self._accept_score = accept_score
        self._accept_gap = accept_gap
        self._keywords = list(keywords or [])
        self._pre_matched = pre_matched

    def run(self) -> None:
        name = (self._name or "").strip()
        if not name:
            self.done.emit(self._source_id, 0, "条目名不能为空")
            return

        subj = None
        if self._pre_matched is not None:
            # 「获取」已经搜过并显示给用户了，直接用 —— 保证"看到的=建出来的"
            subj = self._pre_matched
            log.info("从订阅源新建条目：沿用「获取」时的预匹配结果（%s）",
                     subj.get("name_cn") or subj.get("name") or name)
        elif self._do_match and self._api is not None:
            try:
                matcher = SubjectMatcher(
                    self._api, season_mode="cn",
                    accept_score=self._accept_score,
                    accept_gap=self._accept_gap,
                )
                keywords = self._keywords or [name]
                if name not in keywords:
                    keywords = [name] + keywords
                result = matcher.search_best(keywords, 0)
                subj = result.subject
                if subj is None:
                    log.info("从订阅源新建条目：未匹配到 Bangumi（%s，候选=%s），"
                             "按本地条目入库", result.reason, keywords)
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


class _PreviewWorker(NamedThread):
    """「下载器」里的预览：抓一次订阅源，跑完整判新，列出**会下载哪些集**。

    **为什么必须真抓 RSS 而不是只过滤词**：预览的意义就是
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


class _TorrentStatusWorker(NamedThread):
    """向 qBittorrent 查一次任务列表，供下载记录显示进度。

    订阅页有个 5 秒定时器在调这里，所以 qBittorrent 没开时这笔账会**反复**
    收。`_note_failure` 的冷却窗口把它压成「30 秒内只真连一次」，窗口内
    的调用立刻返回 —— 这个 worker 因此多数时候是瞬间结束的。

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
# 只看"紧邻一位"而不是大窗口。
_EP_SUFFIX_BAD_RE = re.compile(r"(?i)^(?:[pP]\b|bit|fps|k\b|[xX]\d)")


def _ep_of(text: str) -> int:
    """从标题里取集号（拿不到返回 0）。

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
    差异很大，但**作品名那几个字是相同的**。
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


def _release_similar(a: str, b: str) -> float:
    """**对称**的发布名相似度（0~1）—— 专门给"拿文件名去认条目"用。

    `_match_torrent` 那边不显这个毛病：它比的是两个**完整种子名**，
    短串不会短到没有信息量，所以那边保持原样、不动。
    """
    if not a or not b:
        return 0.0
    sa, sb = _norm_title(a), _norm_title(b)
    if not sa or not sb:
        return 0.0
    return difflib.SequenceMatcher(None, sa, sb).ratio()


def _match_torrent(rec_title: str, torrents: dict) -> Optional[dict]:
    """把一条下载记录匹配到 qBittorrent 里的任务（拿不到返回 None）。

    **为什么要这么麻烦**：
      ① `torrent_hash` 拿不到 —— `torrents_add` 只返回 "Ok."，
         不回传 hash，所以库里那列一直是 NULL；
      ② 按标题**精确匹配**也不行 （qB 用的是**种子内的原始名**，而我们用的是 RSS 标题）。

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


#: "按目录/已有集认条目"时要求的最低标题重合度（与 `_match_torrent` 的
#: 第一级同一个阈值）—— 宁可少认，也不要把集归到错的条目上。
_FOLDER_MATCH_MIN = 0.5

#: 启动补偿那一趟的延迟（毫秒）：等首帧渲染完再动扫描。
#:
#: 扫描是"读目录 + 匹配 + 写库 + 联网"的重操作，而启动补偿可能**立刻**
#: 就发起一次；与窗口首帧抢资源会让用户看到窗口"卡一下才出来"。
START_SCAN_DELAY_MS = 2000


def _ever_bound(rec, source) -> bool:
    """这条下载记录**曾经**绑定过本地条目吗（哪怕那个条目现在已经被删）。

    **为什么要区分"从没绑过"与"绑过但死了"**：两者的处置完全不同 ——
      - 从没绑过（用户在试订阅源而已）：扫也扫不出对应关系，照旧静默标记，
        每次轮询替它重匹配一遍标题没有任何意义；
      - 绑过但死了（条目被删 / 记录指向陈旧 id）：这是**要用户出手**的情况
        （重新绑定该订阅），必须留日志、并在界面上提示，不能像旧代码那样
        一声不吭地每 5 秒重试到天荒地老。

    判据取"任何一个绑定字段曾经有值"，包括已经失效的值 —— 失效本身
    正是我们要识别的信号。
    """
    if int(getattr(rec, "subject_id", 0) or 0):
        return True
    if source is None:
        return False
    return any(int(getattr(source, f, 0) or 0)
               for f in ("local_subject_id", "save_subject_id", "bangumi_id"))


#: `_scan_subject_ok` 的三种结论 —— **必须区分"暂时"与"永久"**。
_SCAN_OK = "ok"        # 可以发
_SCAN_BUSY = "busy"    # 扫描器正忙 / 读库抖动 —— 暂时，下一轮再来
_SCAN_GONE = "gone"    # 解析不出活着的条目 —— 永久，上层记一笔后跳过


class RssBridge(QObject):
    """RSS 订阅源与下载记录。"""

    sourcesChanged = Signal()
    downloadsChanged = Signal()
    failed = Signal(str)
    message = Signal(str)
    #: 推断出订阅源的动漫名（参数：来源 id, 名字）—— 界面据此预填输入框
    titleSuggested = Signal(int, str)
    #: 「获取」的**预匹配**结果（参数：是否匹配成功, 回显文案）。
    #:
    #: 「获取」抓完订阅源后会顺手搜一次 Bangumi（有 Token 时），把"认成了
    #: 哪一部"当场告诉用户 —— 否则要等点了「新建并绑定」，参数都写库了才
    #: 知道匹配错了。文案里带**原名与别名**，因为 RSS 标题给的常常是简称，
    #: 只看中文名分不出是哪一季/哪一部。
    #:
    #: 弹窗没打开时（在"添加订阅"表单里点的「获取」）这条也会发，
    #: 界面侧忽略即可 —— 与 `titleSuggested` 同一取舍，见
    #: `SubscriptionPage.onTitleSuggested` 的说明。
    matchPreviewed = Signal(bool, str)
    #: 「从订阅源新建条目」完成（参数：新条目本地 id, 名称）。
    #:
    #: 媒体库桥接的`subjects` 是缓存，新建后必须让 QML 侧知道"条目变了" —— 既要刷新
    #: 下载器弹窗的保存位置列表，也要让订阅卡片上的绑定标签立刻出现。
    #: QmlApp 接到后调用 `library_bridge.reload()`（跨桥连线在 qml_app）。
    subjectCreated = Signal(int, str)
    #: 界面请求「立即检查」（QmlApp 接到后转给 RssService.poll()）。
    #:
    #: 参数 `source_id`：0 = 检查**全部**启用订阅（「立即检查」按钮）；
    #: >0 = 只检查该订阅（「下载器」保存后自动触发，见 `setDownloader`）。
    #: 为什么带上它：保存下载器后要**立刻**按新规则拉一次，
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
    #: 某个下载任务**下完了**，请求扫描对应条目（参数：本地 subjects.id）。
    #:
    #: **为什么需要这个信号**（"下载好的也会推送吗 —— 会，而且是重复推"）：
    #: `new_only` 只剩两层查重 —— ① 本地媒体库（`episodes` 表里有这一集）
    #: ② qBittorrent 里的同名任务。第②层只在**任务还在 qB 里**时有效：
    #: 用户下完就删种子 / 清理列表，那一层就空了；而第①层要求"先扫描入库" ——
    #: 下载完并不会自动扫描，于是 `episodes` 里始终没有这些集。
    #: 两层全落空 → 下一轮轮询又把同一集当成"新集"推一遍。
    #:
    #: 修法是**下载完就自动扫这一部**：让数据真正进入
    #: 媒体库，查重第①层从此稳定命中。扫描复用详情页「重新扫描」那套
    #: （`RssBridge` 不该认识 `ScannerBridge`，跨桥连线放在 qml_app）。
    scanRequested = Signal(int)
    #: 「有下载记录找不到对应本地条目」发生变化（订阅页据此显示重新绑定提示）。
    #:
    #: **为什么需要这个信号**：解析不出条目时，那条记录会**永久**卡在
    #: "下完了却扫不进库"，而唯一的自愈手段是**用户重新绑定那个订阅**
    #: （代码不会自动改绑定 —— 绑定决定 `plan_save_path` 的落点，猜错会把
    #: 以后的新集下进别的番的目录）。既然要靠用户，就必须让他看得见，
    #: 不能只写日志。用**被动提示**而不是弹窗：这发生在后台轮询里，弹窗会成灾。
    unresolvedChanged = Signal()

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
        # ---- 上一次「获取」的结果（供「新建并绑定」复用）----
        #
        # `_suggest_name` 是**填进输入框的那个名字**（匹配成功时是 Bangumi
        # 中文名）：只有当用户没改过输入框（`name == _suggest_name`）时，
        # 下面两项才成立 —— 用户改了名字，就等于放弃了这次预匹配。
        self._suggest_name = ""
        #: 「获取」拆出的整组候选（中文名 / 原名），按用户意图排序
        self._suggest_keywords: list[str] = []
        #: 「获取」预匹配到的 Bangumi 条目（None = 没匹配上）
        self._suggest_subject: Optional[dict] = None
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
        #: 界面**最新一次**的预览请求：`(订阅 id, 必须包含, 必须不包含)`。
        #:
        #: 为什么单独立一份（而不是"调用了就直接开工"）：抓一次订阅源要 2~4 秒，
        #: 而界面的防抖只有 0.7 秒 —— worker 在跑时后来的请求**不能丢**，
        #: 见 `requestPreview`。
        self._preview_want: Optional[tuple[int, str, str]] = None
        #: 正在跑（或刚跑完）的那个 worker 用的是哪一组参数。
        #: `_on_preview` 靠它与 `_preview_want` 比对，判断结果是否已过时。
        self._preview_key: Optional[tuple[int, str, str]] = None
        # ---- 「下载完自动扫描」----
        #
        # 记"已经因为下完而请求过扫描"的 download_history.id（**不是**
        # hash：hash 我们拿不到，见 _match_torrent 的说明）。
        #
        # **为什么用 record.id 做键**：一条记录 = 订阅里的一集，它在
        # qBittorrent 里对应且只对应一个任务。下完扫过一次就记下，
        # 之后每 5 秒的轮询不会再重复触发；而用户以后下了**新的一集**，
        # 那是一条新记录、新 id，自然会再扫一次。
        self._scan_triggered: set[int] = set()
        #: 后台定时器：哪怕订阅页没打开，也要能发现"下完了"。
        #:
        #: **为什么不能只靠 QML 那个 5 秒定时器**：它绑的是
        #: `showDownloads && visible`（见 SubscriptionPage 的 progressTimer），
        #: 语义是"用户正盯着下载面板"。而"下完 → 扫描入库"是个**后台**
        #: 动作：用户很可能下完就切去看番了，这时 QML 的定时器是停的，
        #: 我们永远不知道任务已经完成 —— 于是新集照旧不入库、照旧被重复推送。
        #: 频率取 30 秒：扫描是"匹配 + 写库 + 联网"的重操作，判"下完没有"
        #: 晚半分钟没有任何影响（而且真正下完那一刻用户通常也不在等）。
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(30 * 1000)
        self._scan_timer.timeout.connect(self._auto_refresh_torrents)
        #: "扫描是否正在进行"的查询回调（QmlApp 注入 `scanner_bridge.running`）。
        #:
        #: 本桥接不认识 ScannerBridge（见 scanRequested 的说明），但"要不要
        #: 现在发起扫描"必须提前知道 —— 被拒绝时不能标记为已扫过，否则
        #: 那一条就永远漏扫了（见 _scan_subject_ok）。没注入时按"不忙"处理。
        self._scan_busy = None
        #: 本会话已**断定扫不成**的 download_history.id（解析不出活着的条目）。
        #:
        #: 与 `_scan_triggered` 的区别：那个是"真的发出去了"，这个是"放弃了"
        #: —— 两者都不该在同一会话里重复尝试，但**只有前者配写 `scanned_at`**。
        #: `scanned_at` 是**跨会话**的永久标记，一旦写上就把"用户重新绑定后
        #: 补扫"这条路堵死了（那正是要修的病），所以这里只做会话级：
        #: 每次启动重试一次，是"不刷屏"与"能自愈"之间的折中。
        self._scan_skipped: set[int] = set()
        #: 解析不出条目的记录数，按订阅聚合 `{source_id: 条数}`。
        #: 订阅页据此提示"请重新绑定"（见 unresolvedChanged）。
        self._unresolved: dict[int, int] = {}

    def set_scan_busy_hook(self, hook) -> None:
        """注入「扫描是否正在进行」的查询函数（QmlApp 调用）。"""
        self._scan_busy = hook

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

    def startScanWatch(self) -> None:
        """启动"下完自动扫描"：**先补扫一趟历史记录，再挂定时器**
        （QmlApp 在界面加载完成后调用）。

        **为什么不再"没配 qBittorrent 就整个不启动"**：补偿那一趟
        （`scanPendingOnStart`）**不需要 qBittorrent** —— 它靠库里已经落成
        `done` 的记录判断"下完了"。而没配 qB 的用户恰恰最需要它：那种情况下
        永远不会有第一轮 qB 轮询来补扫，历史记录就一直躺在那里。

        定时器也照挂：`_auto_refresh_torrents` 会**先判 `_qb`、再判有没有
        在途任务**，所以没配 qB 时它只跑扫描、一次网都不连。它的第二个职责
        是**重试**那些"当时扫描器正忙 / 条目解析不出"的记录 —— 早前
        `_qb is None` 直接 return，连这个重试机会都没有，等于回到旧毛病。
        """
        # 延后一点执行：见 START_SCAN_DELAY_MS 的说明（别和窗口首帧抢资源）
        QTimer.singleShot(START_SCAN_DELAY_MS, self.scanPendingOnStart)
        self._scan_timer.start()
        log.info("「下载完自动扫描」已启动（每 %s 秒检查一次%s）",
                 self._scan_timer.interval() // 1000,
                 "" if self._qb is not None
                 else "；未配置 qBittorrent，只补扫已完成的记录")

    def stopScanWatch(self) -> None:
        """停止后台轮询（退出时调用）。"""
        self._scan_timer.stop()

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
                # `completed` —— 导致界面上"完成"永远显示 0。
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
        """界面点「立即检查」→ 转给 QmlApp（它持有 RssService）。

        **顺带强刷一次 qBittorrent 进度**（不带 `_has_inflight_downloads`
        的闸门）。后台定时器在没有在途任务时是不连 qBittorrent 的，而
        "手动检查"就是用户明确要求的那个恢复点 —— 他点了，就该拿一次
        真数据（顺便也能把"下完了但还没扫上"的条目补扫掉）。
        """
        if self._poll_running:
            self.message.emit("正在检查，请稍候…")
            return
        self.refreshTorrents()
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

        # ---- 保存后**立即启动下载**----
        if self._poll_running:
            self.message.emit("下载器设置已保存；当前正在检查，稍后自动生效")
        else:
            self.message.emit("下载器设置已保存，正在按新规则检查…")
            self.pollRequested.emit(int(source_id))
        return True

    # ---------- 下载器预览 ----------
    @Slot(int)
    def logDownloaderOpened(self, source_id: int) -> None:
        """「下载器」弹窗打开时记一行日志（订阅 + 绑定条目 + 已存设置）。

        记三件事：订阅（id + 名称）、绑定的条目（若有）、当前的过滤词 ——
        过滤词直接解释了后面那批"按规则过滤"的日志从哪来。
        """
        try:
            src = next((s for s in self._db.list_rss_sources()
                        if s.id == int(source_id)), None)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("记录下载器打开日志失败（读订阅 #%s）：%s",
                        source_id, e)
            return
        if src is None:
            log.warning("打开下载器：订阅 #%s 不存在", source_id)
            return

        bound = int(getattr(src, "local_subject_id", 0) or 0)
        try:
            subj = self._db.get_subject(bound) if bound else None
        except Exception:               # pragma: no cover - 防御性
            subj = None
        bound_desc = ((subj.name_cn or subj.name or "") if subj else "") or "未绑定"

        save_id = int(getattr(src, "save_subject_id", 0) or 0)
        save_desc = "未指定（用 qBittorrent 全局路径）"
        if save_id:
            try:
                s2 = self._db.get_subject(save_id)
                save_desc = (s2.name_cn or s2.name or "") if s2 else f"#{save_id}（已不存在）"
            except Exception:           # pragma: no cover - 防御性
                save_desc = f"#{save_id}"

        log.info(
            "打开「下载器」：订阅 #%s %s（%s）｜绑定条目：%s｜保存位置：%s"
            "｜必须包含=%r 必须不包含=%r",
            src.id, src.name or "(无名)", src.url or "(无地址)",
            bound_desc, save_desc,
            getattr(src, "must_include", "") or "",
            getattr(src, "must_exclude", "") or "",
        )

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

    @Property("QVariantList", notify=unresolvedChanged)
    def unresolvedDownloads(self) -> list[dict]:
        """哪些订阅有"找不到对应条目"的下载记录（订阅页据此提示重新绑定）。

        返回 `[{"sourceId": int, "count": int}]`，条数多的在前。
        空列表 = 一切正常（绝大多数时候）。

        **这是"下载完自动扫描"唯一的自愈入口**：记录解析不出条目时，代码
        不会自动改绑定（绑定决定 `plan_save_path` 的落点，猜错会把以后的新集
        下进别的番的目录），只能请用户重新绑定。所以必须让它可见。
        """
        return [{"sourceId": sid, "count": n}
                for sid, n in sorted(self._unresolved.items(),
                                     key=lambda kv: (-kv[1], kv[0]))
                if n > 0]

    @Slot(int, str, str)
    def requestPreview(self, source_id: int, must_include: str,
                       must_exclude: str) -> None:
        """抓一次订阅源并跑判新，把"会下载哪些集"回传给界面。

        **由界面在过滤词变化后调用（带防抖）** —— 用户要求"填完过滤词
        实时出现"。防抖在 QML 侧做（700ms）。

        **同一时刻只跑一个 worker，但后来的请求一个都不丢**：
        抓一次订阅源要 2~4 秒，而防抖只有 0.7 秒 —— 用户删掉过滤词
        时，上一次抓取多半**还在飞**。早先这里遇到"正在跑"就直接 `return`，
        那次请求于是永久消失（没有任何补跑）：界面从此停在上一次的结论上。
        所以改成**合并**：最新一组参数记进 `_preview_want`，正在跑的那个
        结束了由 `_finish_preview` 补跑一次。
        """
        self._preview_want = (int(source_id), must_include or "",
                              must_exclude or "")
        self._start_preview()

    def _start_preview(self) -> None:
        """按 `_preview_want` 起一次抓取；有 worker 在跑就先不打搅它。

        **被"正在跑"挡下不算丢弃**：参数还留在 `_preview_want` 里，
        `_finish_preview`（挂 `finished`）会拿它补跑。
        """
        w = self._preview_worker
        if w is not None and w.isRunning():
            log.debug("预览请求排队（上一次仍在抓取）：%s", self._preview_want)
            return
        want = self._preview_want
        if want is None:
            return
        source_id, must_include, must_exclude = want
        # 这一轮用的就是 `want` —— 先记下来：`_on_preview` 靠它判"结果过没过时"
        self._preview_key = want
        try:
            src = next((s for s in self._db.list_rss_sources()
                        if s.id == source_id), None)
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
        log.info("预览抓取开始：订阅 #%s 必须包含=%r 必须不包含=%r",
                 source_id, must_include, must_exclude)
        w = _PreviewWorker(self._db, self._config, src,
                           must_include, must_exclude)
        w.done.connect(self._on_preview)
        # 引用留到 `finished` 才摘（不能挂在 `done` 上，见 utils/qt_thread.py）
        w.finished.connect(lambda: self._finish_preview(w))
        self._preview_worker = w
        w.start()

    def _finish_preview(self, worker: "_PreviewWorker") -> None:
        """线程**真正结束**后摘引用；期间改过过滤词就在这里补跑一次。

        **补跑只能挂 `finished`，不能挂 `done`**：`done` 是 `run()` 内部发的，
        那时线程还在跑 —— 此时新建 worker 覆盖 `_preview_worker`，等于把唯一
        引用从运行中的旧线程上摘掉，撞上 Qt 6 的 `qFatal` 当场退出
        （见 utils/qt_thread.py 的模块说明②）。
        """
        retire_worker(self, "_preview_worker", worker)
        if (self._preview_want is not None
                and self._preview_want != self._preview_key):
            log.info("预览：抓取期间过滤词又改过，补跑一次 %s", self._preview_want)
            self._start_preview()

    def _on_preview(self, source_id: int, items: list, error: str) -> None:
        # 不在这里摘 `_preview_worker`：`done` 时线程还在跑，见 utils/qt_thread.py
        self._preview_running = False
        # 这份结果是不是**当前**这组过滤词的？不是就别发布 ——
        # 发了就是"预览停在旧过滤词上"（用户报的那个现象）。
        # 补跑的那次马上开始（见 `_finish_preview`）。
        if self._preview_want is None or self._preview_key != self._preview_want:
            log.info("预览结果已过时（期间过滤词改过 / 弹窗已关），丢弃：%s",
                     self._preview_key)
            self.previewChanged.emit()      # running 状态要跟着变
            return
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
        # **连"待补跑的那次"一起作废**：弹窗已经关了，再抓一次订阅源纯属浪费，
        # 抓回来的结果也只会在关窗之后落到界面上（`_on_preview` 见
        # `_preview_want is None` 会丢弃）。
        self._preview_want = None
        self._preview_key = None
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

        绑定后「两层判新」才能知道该订阅对应哪部动漫、本地已有哪些集。
        `subject_id=0` 表示解除绑定。

        现在权威标识是 `local_subject_id`，`bangumi_id` 改为顺带冗余存一份
        （有就存，没有就留空），两种条目一视同仁。
        """
        try:
            # ---- 保存位置「跟着绑定走」----
            #
            # 下载器弹窗现在会**按绑定条目预选保存位置**（见
            # RssDownloaderDialog.pickOnOpen），而那个框点一次「保存」就会
            # 写成显式的 `save_subject_id`。于是绑定关系一变，那个值就成了
            # 陈旧的指向 —— 解绑后下载仍进那部番的目录、改绑后仍进旧条目的
            # 目录。两条都不是用户要的。
            #
            # 规则：**`save_subject_id` 等于旧绑定条目时，跟着改**（解绑则
            # 清空，回落到 qBittorrent 全局路径）。等于不代表用户显式选过，
            # 因为预选写下去的就是这个值 —— 无法区分，按"跟随"处理。
            # 用户显式选了**别的**条目时不动它：那是独立的选择。
            old = self._find_source(source_id)
            old_bound = int(getattr(old, "local_subject_id", 0) or 0)
            old_save = int(getattr(old, "save_subject_id", 0) or 0)
            follow = {}
            if old_bound and old_save == old_bound:
                follow["save_subject_id"] = int(subject_id) or None
                log.info("订阅 #%s 的保存位置跟随绑定条目变更：#%s → %s",
                         source_id, old_save, follow["save_subject_id"] or "未指定")

            if subject_id <= 0:
                self._db.update_rss_source(source_id,
                                           local_subject_id=None,
                                           bangumi_id=None,
                                           **follow)
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
                    **follow
                )
                self.message.emit(
                    f"已绑定到「{subj.name_cn or subj.name}」")
            # ---- 绑定一变，"解析不出条目"的旧判定立刻作废 ----
            #
            # 这是"下载完自动扫描"的自愈闭环：那些因为**绑定条目被删**而
            # 解析不出的记录，本来要等重启才会重试一次（`_scan_skipped` 是
            # 会话级的）。用户重新绑定的这一刻就把闸门清掉，下一轮 5 秒/
            # 30 秒定时器立刻能解析出条目并补扫 —— **不必重启**。
            self._scan_skipped.clear()
            self._clear_unresolved(source_id)
        except Exception as e:
            log.exception("绑定订阅 %s 失败: %s", source_id, e)
            self.failed.emit(f"绑定失败：{e}")
            return
        self.reload()

    @Slot(int, result=bool)
    def removeSource(self, source_id: int) -> bool:
        """删除订阅源（连带删除其下载记录，外键 CASCADE）。"""
        # **先取名字再删**——
        # 删完那一行就没了，之后只能看到 `#25` 这种 ID，出问题（删错订阅）
        # 时完全对不上号。取名字失败不影响删除本身，只让日志退化回 ID。
        name = self._source_name(source_id)
        try:
            # 外键 ON DELETE CASCADE 已在 schema 里声明，
            # 但 download_history 的 source_id 是 REFERENCES rss_sources(id)
            # 且 PRAGMA foreign_keys=ON 已开，因此会级联删除。
            self._db.delete_rss_source(source_id)
        except Exception as e:
            log.exception("删除订阅源 %s 失败: %s", source_id, e)
            self.failed.emit(f"删除失败：{e}")
            return False
        log.info("已删除订阅源 #%s「%s」", source_id, name)
        self.reload()
        self.message.emit(f"订阅「{name}」已删除")
        return True

    def _find_source(self, source_id: int):
        """按 id 取订阅源（查不到 / 读失败都返回 None）。

        数据库没有单个订阅的 getter，而订阅是**几十级别**的量，为它单开一条
        SELECT 不如现查一遍 `list_rss_sources()`（与 `_source_name` 同一取舍）。
        """
        try:
            return next((s for s in self._db.list_rss_sources()
                         if s.id == int(source_id)), None)
        except Exception as e:              # pragma: no cover - 防御性
            log.warning("读取订阅 #%s 失败: %s", source_id, e)
            return None

    def _source_name(self, source_id: int) -> str:
        """订阅源显示名；查不到退回 `#id`。

        与 `logDownloaderOpened` 一样走 `list_rss_sources()` 现查 ——
        订阅数量是几十级别，为它单独加一条 SELECT 不划算。
        """
        try:
            src = next((s for s in self._db.list_rss_sources()
                        if s.id == int(source_id)), None)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("读取订阅 #%s 名称失败: %s", source_id, e)
            return f"#{source_id}"
        if src is None:
            return f"#{source_id}"
        return (getattr(src, "name", "") or "").strip() or f"#{source_id}"

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
            # 两者都对不上时会让"已完成"显示成英文 done，
            # 这里补一条 done → 已完成 的映射兜住。
            label = STATUS_LABELS.get(status, "")
            if not label:
                label = "已完成" if status == "done" else status

            # 叠加 qBittorrent 的真实进度。
            #
            # **匹配方式：先 hash、再按标题**。
            #
            # `torrent_hash` 是 qBittorrent 按种子**内容**算出来的，我们
            # 下发时拿不到（`torrents_add` 只返回 "Ok."，不回传 hash），
            # 所以库里那一列一直是 NULL —— 只按 hash 匹配的话**永远查不到**，
            # 界面上进度条一根都不会出现。
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
                "canPush": (status == "pending"
                            and int(r.ep_index or 0) >= 0
                            and info is None),
                # 原先只有「失败 10」这个计数，用户完全无从下手。
                "lastError": r.last_error or "",
                "createdAt": r.created_at or "",
            })
        return out

    @Slot()
    def scanPendingOnStart(self) -> None:
        """启动补偿：**不依赖 qBittorrent**，把"已确认下完、但没扫成"的补一遍。

        **为什么必须有不依赖 qB 的这一趟**：『软件关着的时候下完了』——
        关软件那一刻 qB 还在下、记录停在 `pushed`；等它真下完时进程早就不在
        了，没有任何人去写 `done`、更没人触发扫描。下次启动时 `self._torrents`
        还是空的（要等第一轮轮询），而**没配 qBittorrent 的用户根本就不会有
        第一轮轮询**。于是这些集永远不入库、永远被 `new_only` 当成新集重复下。

        它就是 `_maybe_scan_completed` 拿**空任务列表**跑一趟 —— 判据会自动
        落到"库里已经是 `done`"那条分支（见那里的 finished 说明）。
        单独包一层是为了：① 日志能一眼看出这是启动那一趟；② 给自动化验证
        一个稳定入口（无头环境下不用真连 qBittorrent）。
        """
        log.info("启动补偿：检查「已确认下完但未扫描」的记录")
        # **把"其实没事可干"也说出来**：`_maybe_scan_completed` 在没什么可扫时
        # 是**完全静默**的（每个分支都只是 continue），于是"启动补偿：…"后面
        # 空无一物 —— 看起来像功能坏了，实际是正常。
        try:
            pending = [r for r in self._db.list_downloads()
                       if not r.scanned_at and r.status == "done"]
        except Exception:               # pragma: no cover - 防御性
            pending = []
        if not pending:
            log.info("启动补偿：没有需要补扫的记录（下完的都扫过了）")
            return
        log.info("启动补偿：发现 %s 条「已确认下完但未扫描」的记录，开始补扫",
                 len(pending))
        self._maybe_scan_completed({})

    @Slot()
    def _auto_refresh_torrents(self) -> None:
        """后台 30 秒定时器的入口：**没有在途任务就不去碰 qBittorrent**。

        **为什么必须挡这一层**：这个定时器原先直接接 `refreshTorrents`，于是
        只要配了 qBittorrent，它就会**每 30 秒无条件连一次** —— 不管有没有
        东西在下载、也不管有没有东西要显示。qBittorrent 没开时，日志里就
        会一直出现"读取 qBittorrent 任务失败"，用户完全不知道为什么。

        它服务两件事，都只在"有在途任务"时才有意义：
          - 进度条显示（`_load_downloads` 要拿真实 progress）；
          - **下完自动扫描**（`_maybe_scan_completed` 靠 progress≥1 判定
            "下完了"，再触发扫描 —— 扫描才用到 Bangumi 接口）。
        两者都以一条"已交给 qBittorrent 且结论未落定"的记录为前提。
        没有这种记录时，连上去也什么都做不了。

        **手动路径不受影响**：订阅页可见时以及下载面板的 5 秒定时器仍然
        直接调 `refreshTorrents` —— 用户正看着，就该给他真数据。

        **qBittorrent 也不会因此"该启动时不启动"**：按需启动只发生在
        `RssService` 的下发路径上（见那里的说明），与这里无关。

        **恢复连接的条件有两个**：出现新任务（新记录是 `pushed` 且未扫，
        闸门自然放行），或者用户点「立即检查」（`requestPoll` 直接调
        `refreshTorrents`，绕过闸门）。
        """
        # 先用**上次拿到的**任务列表补扫一遍，**不发任何请求**：
        # 有些记录已经确认下完、只是那一次的扫描没能发出去（扫描器正忙 /
        # 条目暂时读不到），这些只需要重试扫描，不必再问一次 qBittorrent。
        # 少了这一步，那些记录就永远等着"下一次轮询" —— 可轮询恰恰被
        # 闸门停掉了，于是"下完自动扫描"会静默失效。
        self._maybe_scan_completed(self._torrents)

        # **先判 `_qb`、再判在途任务**（顺序与早前相反）：没配 qBittorrent 时
        # 也必须每 30 秒进来一次 —— 上面的 `_maybe_scan_completed` 承担了
        # 不依赖 qB 的补扫与**重试**（"当时扫描器正忙 / 条目解析不出"的记录
        # 靠它重来）。早退在这之后，那些记录就再没有第二次机会。
        if self._qb is None:
            return
        if not self._has_inflight_downloads():
            return
        self.refreshTorrents()

    def _has_inflight_downloads(self) -> bool:
        """有没有"已经交给 qBittorrent、但结论还没落定"的记录。

        判据取**两个条件同时成立**：
          - 状态是 `pushed` / `downloading` —— 我们确实把种子交给 qB 了，
            而且**还没看到它下完**。`pending`（还没下发）、`failed`（下发
            失败）、`done`（已确认下完，见 `_maybe_scan_completed`）都不必问；
          - `scanned_at` 为空 —— "下完自动扫描"还没处理过这条。

        第二条是兜底：一条下完后扫过的记录会一直留在库里，只看状态的话
        （万一状态没来得及落成 `done`）**按需连接会退化成永久轮询**。

        读库失败时返回 True（保持旧行为）—— 宁可多连一次，也不要因为
        读不到库就把"下完自动扫描"整个停掉。
        """
        try:
            records = self._db.list_downloads()
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("读取下载记录失败（仍去查 qBittorrent）：%s", e)
            return True
        return any(r.status in ("pushed", "downloading") and not r.scanned_at
                   for r in records)

    @Slot()
    def refreshTorrents(self) -> None:
        """异步拉一次 qBittorrent 任务列表（订阅页显示 / 5 秒定时器调用）。

        **重入保护**：同一时刻只跑一个 worker。订阅页有个 5 秒定时器在
        调这里（见 QML 的 progressTimer），若上一次还没回来（qBittorrent
        没开时单次要等 3 秒超时），直接跳过本次 —— 否则会不断新建线程，
        超时叠加时线程数会滚起来。

        **注意判据是 `isRunning()` 而不是 `is not None`**：引用要留到
        `finished` 才由 `retire_worker` 摘掉，中间存在"引用还在、但已经跑完"
        的瞬间；只看非 None 会白白跳过一轮。
        """
        w = self._torrent_worker
        if w is not None and w.isRunning():
            return
        w = _TorrentStatusWorker(self._qb)
        w.done.connect(self._on_torrents)
        # 引用留到 `finished` 才摘（不能挂在 `done` 上，见 utils/qt_thread.py）
        w.finished.connect(lambda: retire_worker(self, "_torrent_worker", w))
        self._torrent_worker = w
        w.start()

    #: 进度/状态变化的判定精度（百分点）。qBittorrent 的 `progress` 是
    #: 浮点，每 5 秒几乎一定在动（0.1701 → 0.1703），若按精确值比较，
    #: 这个"只在变化时通知"的优化就形同虚设。
    _PROGRESS_EPS = 0.01

    def _on_torrents(self, torrents: dict, error: str) -> None:
        # **这里不摘 `self._torrent_worker`**：`done` 是 run() 内部发的，
        # 那时线程还在跑，此刻丢引用就是 Qt6 的 qFatal（进程当场没，
        # 见 utils/qt_thread.py）。收尾统一交给 `retire_worker`（挂 finished）。
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
        # ---- 下完就扫描----
        #
        # 放在"是否有变化"的判断**之前**：上面那套判断是为了"少重建列表"，
        # 而扫描判定只关心"有没有任务达到 100%"，两者目的不同。而且这个
        # 方法每 5 秒被调一次，重复触发由 `_scan_triggered` 兜住。
        self._maybe_scan_completed(new)
        if not changed:
            return
        self._downloads_dirty = True
        self.downloadsChanged.emit()

    def _maybe_scan_completed(self, torrents: dict) -> None:
        """发现"下完了"的任务 → 请 QmlApp 扫描它对应的条目。

        **为什么在这里做**（而不是在 `RssService` 里）：这里是唯一能拿到
        qBittorrent **真实进度**的地方 —— `download_history.status` 停在
        `pushed`（我们只是把种子交给了 qB，"下完没有"它并不知道，见
        `_load_downloads` 里那段"有真实进度时以它为准"）。

        **流程**：判断"下完了没有" → 反查记录该归哪个条目
        （`_resolve_subject_id`）→ `scanRequested`。

        **"下完了没有"有两套判据**（缺一不可）：
          - 有 qB 任务可比 → `progress ≥ 1` 且不是 `moving`（权威口径）；
          - 没有 qB 任务可比（没配 qB / 任务已被清理 / 启动补偿那一趟）→
            **只有库里已经落成 `done` 的才算**。`pushed`/`downloading` 在途时
            没有任何东西能证实它下完了，贸然扫会读到下到一半的目录。
            这条判据正是"**软件关着的时候下完了**"能补回来的前提
            （见 `scanPendingOnStart`）。

        **去重依据是库里的 `scanned_at`，不是内存集合**：内存集合一重启就没了，
        而已下完的 qB 任务还在，于是每次启动都重扫一遍。见迁移 v14。
        """
        # **没有 qBittorrent 任务列表也要往下走**（早前这里直接 return）：
        # 启动补偿那一趟就是拿空列表进来的（见 scanPendingOnStart），
        # 它靠"库里已经落成 `done`"判断下完了 —— 那正是"软件关着的时候
        # 下完了"唯一能补回来的依据。
        try:
            records = self._db.list_downloads()
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("读取下载记录失败（跳过自动扫描）：%s", e)
            return
        if not records:
            return

        # 订阅表**按需取一次**：重解析要用它的权威绑定，而同一轮里多条记录
        # 常常属于同一个订阅 —— 逐条查是白跑 N 遍 LIST。
        sources = None
        #: 本轮**已经发过扫描**的条目：同一 subject 只发一次。
        #:
        #: 为什么要去重：`scanRequested` 是**直连**（见 qml_app 的连线），
        #: emit 返回时 `scanner_bridge.running` 已置真 —— 同一条目的第二条
        #: 记录会在 `_scan_subject_ok` 那里撞上"扫描器忙"而被推迟到下一轮
        #: （白等 30 秒）。一次扫描本来就覆盖该条目下所有记录，直接记账更利落。
        seen_sids: set[int] = set()

        for rec in records:
            # 已扫过（跨会话）→ 不再重复；内存里那两个集合是**本会话的**
            # 补充闸门：`scanned_at` 写库是异步排队生效的，同一秒内的
            # 多次轮询（QML 5 秒定时器 + 后台 30 秒定时器可能重叠）
            # 会看到同一份旧数据，靠它挡住。
            # `_scan_skipped` = 本会话已断定"扫不成"（解析不出条目）。
            if (rec.scanned_at or rec.id in self._scan_triggered
                    or rec.id in self._scan_skipped):
                continue

            # ---- 这条到底下完了没有 ----
            #
            # 有 qBittorrent 任务可比 → 按它的**真实进度**判（权威口径；
            # 库里那个 `status` 只是"我们下发过"的记录，不反映下完没有）。
            info = _match_torrent(rec.torrent_title or "", torrents)
            if info is not None:
                try:
                    progress = float(info.get("progress") or 0.0)
                except (TypeError, ValueError):
                    continue
                # 1.0 是 qBittorrent 的"下载本身完成"，但**文件可能还在移动**
                # （state=moving）—— 那期间扫描会读到不完整的目录。
                # 移动态直接跳过，等下一次轮询再看。
                if progress < 1.0:
                    continue
                if str(info.get("state") or "").lower() == "moving":
                    continue
                finished = True
            else:
                # 没有 qB 任务可比（没配 qB / 任务已被清理 / 启动补偿那一趟）
                # —— **只有库里已经落成 `done` 的记录才敢当成下完了**。
                #
                # `pushed`/`downloading` 在途时没有任何东西能证实它下完了：
                # 贸然去扫会读到下到一半的目录，更糟的是会把它标成"已处理"
                # （`scanned_at` 是**跨会话**的永久标记），等它真下完时那次
                # 扫描被挡掉，这一集就永远漏了。
                finished = (rec.status == "done")
            if not finished:
                continue

            # ---- 到这一步：确认下完了，把它落库成 `done` ----
            #
            # 这不只是显示好看 —— 它是"**这条不用再盯着了**"的持久结论，
            # 后台定时器的闸门（`_has_inflight_downloads`）就靠它放行停机。
            # 而且落成 `done` 之后，**下次启动的补偿扫描不需要 qBittorrent**
            # 就能认出这条已经下完（上面的 finished 判据）—— 这正是
            # "软件关着的时候下完了"能补回来的关键。
            #
            # **为什么不能只靠 `scanned_at`**：扫描未必发得出去 —— 扫描器
            # 正忙或这个条目暂时读不到时，下面的代码**故意不标记**，好留到
            # 下一轮重试。那种记录会一直停在 `pushed`，把"按需连接"拖成永久轮询。
            #
            # 只在变化时写：这个方法每 5 秒被调一次，不能每轮都写库。
            if rec.status != "done":
                try:
                    self._db.update_download_status(int(rec.id), "done")
                except Exception as e:      # pragma: no cover - 防御性
                    log.warning("标记下载记录 #%s 已完成失败：%s", rec.id, e)

            # ---- 解析成"当前还活着"的条目 ----
            if sources is None:
                try:
                    sources = {s.id: s for s in self._db.list_rss_sources()}
                except Exception as e:      # pragma: no cover - 防御性
                    log.warning("读取订阅源失败（本轮暂不重解析陈旧指向）：%s", e)
                    return
            src = sources.get(int(rec.source_id or 0))

            try:
                sid = self._resolve_subject_id(rec, src)
            except Exception as e:          # pragma: no cover - 防御性
                # 读库失败是**暂时**的：不记 _scan_skipped，下一轮再来
                log.warning("重解析记录 #%s 的条目失败（本轮跳过）：%s",
                            rec.id, e)
                continue

            if not sid:
                if not _ever_bound(rec, src):
                    # 这条订阅**从来没绑定过**本地条目（用户只是在试订阅源）：
                    # 扫也扫不出对应关系。**照旧标记为已处理** —— 否则每轮
                    # 都要重新匹配一遍标题，还会刷一屏无意义的告警。
                    self._scan_triggered.add(int(rec.id))
                    self._mark_scanned(int(rec.id))
                    continue
                # 曾经绑定过、现在解析不出来了 —— **这是要用户出手的情况**。
                #
                # 只记会话级跳过、**绝不写 `scanned_at`**：写上就把"用户
                # 重新绑定后补扫"的路堵死了（那正是本次要修的病）。每次启动
                # 重试一次，换来"不刷屏"（一条记录一个会话只报一次）与"能自愈"。
                self._scan_skipped.add(int(rec.id))
                self._note_unresolved(int(rec.source_id or 0))
                log.warning(
                    "下载 #%s 已完成，但解析不出对应条目（subject_id=%s，"
                    "订阅 #%s 的绑定条目可能已被删除）—— 本轮跳过；"
                    "重新绑定该订阅后会自动补扫：%s",
                    rec.id, rec.subject_id, rec.source_id,
                    (rec.torrent_title or "")[:60])
                continue

            if sid != int(rec.subject_id or 0):
                # 解析到了**不同的**活条目 → 顺手把这一行的指向修正过来
                # （同一轮的兄弟记录下一轮自然收敛，不必在这里成组处理）
                try:
                    if self._db.set_download_records_subject([int(rec.id)], sid):
                        log.info("修正下载记录 #%s 的条目指向：%s → %s",
                                 rec.id, rec.subject_id, sid)
                except Exception as e:      # pragma: no cover - 防御性
                    log.warning("修正记录 #%s 的条目指向失败：%s", rec.id, e)

            if sid in seen_sids:
                # 该条目的扫描本轮已经发过了，那次扫描覆盖这一条 —— 直接记账
                self._scan_triggered.add(int(rec.id))
                self._mark_scanned(int(rec.id))
                continue

            gate = self._scan_subject_ok(sid)
            if gate == _SCAN_BUSY:
                # 暂时：扫描器正忙。用 debug —— 一次扫描可能跑几分钟，
                # info 会把日志刷满（这也是旧代码唯一的"静默"来源）
                log.debug("扫描器忙，推迟「下载完自动扫描」记录 #%s → subject_id=%s",
                          rec.id, sid)
                continue
            if gate == _SCAN_GONE:
                # 永久：刚解析出来的条目在两次读库之间也没了（并发删除）
                self._scan_skipped.add(int(rec.id))
                self._note_unresolved(int(rec.source_id or 0))
                log.warning("「下载完自动扫描」：条目 #%s 已不存在，跳过记录 #%s",
                            sid, rec.id)
                continue

            seen_sids.add(sid)
            self._scan_triggered.add(int(rec.id))
            log.info("下载已完成，请求扫描条目以入媒体库：记录 #%s → subject_id=%s（%s）",
                     rec.id, sid, (rec.torrent_title or "")[:60])
            self.scanRequested.emit(sid)
            self._mark_scanned(int(rec.id))

    def _scan_subject_ok(self, subject_id: int) -> str:
        """这次能不能真的发起扫描该条目（返回 `_SCAN_OK` / `_SCAN_BUSY` / `_SCAN_GONE`）。

        **为什么要在这里判、而不是交给 QmlApp**：标记"已扫过"的时机必须与
        "扫描真的发起了"一致 —— 若先把 `scanned_at` 写上、再发现扫描被拒绝，
        那一条就**永远不会再被扫**，新集直接漏掉（比不修还糟）。
        这里先判一次，通过之后 QmlApp 那边不会再被拒（它同一套判据）。

        **为什么返回三态而不是 bool**：忙是**暂时**的（下一轮该重试），
        条目不存在是**永久**的（重试一万次也不会变）。混成一个 False 时
        调用方只能二选一，怎么选都是错的 —— 要么永久失败被无限重试刷屏，
        要么暂时失败被误当成永久、错过扫描。旧版正是前者：静默重试到天荒地老。
        """
        if self._scan_busy is not None and self._scan_busy():
            return _SCAN_BUSY
        try:
            if self._db.get_subject(int(subject_id)) is None:
                return _SCAN_GONE
        except Exception as e:          # pragma: no cover - 防御性
            # 读库失败是**暂时**的：绝不能当成"条目没了"，
            # 否则一次 SQLite 抖动就把这一条永久跳过。
            log.warning("读取条目 %s 失败（本轮跳过自动扫描）：%s", subject_id, e)
            return _SCAN_BUSY
        return _SCAN_OK

    def _resolve_subject_id(self, rec, source) -> int:
        """把一条下载记录解析成**当前还活着**的本地条目 id（0 = 解析不出）。

        **为什么不能直接用 `rec.subject_id`**：它是**写入那一刻的快照**，而订阅的绑定
        之后可能变过 —— 用户改绑、或者条目被删后重新加进来。快照一旦指向
        不存在的条目，"下载完自动扫描"就永远解析不出该扫谁。

        逐级**验活**（每级都用 `get_subject` 验，写法与 `_load_sources`
        里"以 local_subject_id 为权威"的那段一致）：

          ① 记录自己的 `subject_id` —— 还活着就直接用，**不动库**；
          ② 订阅的 `local_subject_id` —— 现在的权威绑定；
          ③ 订阅的 `save_subject_id` —— "存到哪个条目的目录"通常就是它；
          ④ 订阅的 `bangumi_id` 反查本地条目 —— 兜底（老库/未回填）；
          ⑤ 按目录唯一命中 —— 见 `_match_subject_by_folder`。

        读库异常**故意不在这里吞**：调用方要据此区分"暂时读不到"与
        "条目真没了"，否则会把一次抖动当成永久失败。
        """
        sid = int(rec.subject_id or 0)
        if sid and self._db.get_subject(sid) is not None:
            return sid
        if source is None:
            return 0
        for cand in (getattr(source, "local_subject_id", None),
                     getattr(source, "save_subject_id", None)):
            cid = int(cand or 0)
            if cid and self._db.get_subject(cid) is not None:
                return cid
        bgm = int(getattr(source, "bangumi_id", 0) or 0)
        if bgm:
            lid = self._db.find_subject_by_bangumi_id(bgm)
            if lid and self._db.get_subject(int(lid)) is not None:
                return int(lid)
        return self._match_subject_by_folder(rec, source)

    def _match_subject_by_folder(self, rec, source) -> int:
        """末级兜底：按**目录**认这条下载记录属于哪个条目（0 = 认不出）。

        只在前面四级全断时才走到这里。它能补的洞只有一个，但那个洞别的
        办法都补不了：**条目被删之后又被重新加成本地条目**（新 id、而且
        没匹配上 Bangumi）—— 此时绑定链全断，只有目录还对得上。

        **为什么不能反过来拿它当主力**：
          - 文件夹本身就是从**活着的条目**算出来的。`plan_save_path` 第一步
            就是取 `save_subject_id/local_subject_id` 对应的条目，取不到就
            返回空；`_guess_folder` 也是造个假订阅指着活条目借规则。
            换句话说：绑定活着就不需要文件夹，绑定死了也拿不到文件夹。
          - `download_history` **没有路径列**，没有"文件到底进了哪个目录"
            的事实可对，只能靠订阅名去猜，猜不准。

        所以判据从**最确凿的证据**开始，猜的部分放在最后，且要求**唯一命中**：

          ① 本地库里已经有同一集（`episodes` 里集号相同、文件基名与种子标题
             重合度达标）→ 它属于哪个条目就是答案。文件已经在某个条目下，
             这是硬证据，一条 SQL 的事；
          ② 都不中才按**订阅名推算目录**（与 `plan_save_path` 的"没有路径时
             按系列归位"同一套规则），要求算出的目录真实存在**且**
             `find_subject_by_folder` **恰好命中一个**。命中多个一律放弃 ——
             猜错了会把集写进别人的条目。
        """
        title = rec.torrent_title or ""
        ep = _ep_of(title)
        if ep > 0 and title:
            try:
                rows = self._db.list_episodes_by_index(ep)
            except Exception as e:      # pragma: no cover - 防御性
                log.warning("按本地已有集反查条目失败（记录 #%s）：%s", rec.id, e)
                rows = []
            # 集号相同还要**发布名对得上**才算数：同一个集号在别的番里也有
            # （"第 12 集"遍地都是），只按集号认会把集归到错的条目上。
            # 用**对称**口径 `_release_similar`（不能用 `_title_similar`：
            # 那是包含率）。
            owner, best = 0, 0.0
            for subj_id, path in rows:
                score = _release_similar(title, Path(path).name)
                if score > best:
                    owner, best = int(subj_id), score
            if owner and best >= _FOLDER_MATCH_MIN:
                log.info("记录 #%s 按「本地已有同一集」归属到条目 #%s"
                         "（发布名相似度 %.2f）", rec.id, owner, best)
                return owner
            log.debug("记录 #%s 的同集候选里没有够像的（最高 %.2f），改用目录推算",
                      rec.id, best)

        folder = self._guess_folder_of_source(source)
        if not folder:
            return 0
        try:
            subj = self._db.find_subject_by_folder(folder)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("按目录 %s 反查条目失败：%s", folder, e)
            return 0
        if subj is None:
            return 0
        # **唯一性**：目录不是主键，`find_subject_by_folder` 取的是
        # `ORDER BY id LIMIT 1`（`subjects.folder_path` 上没有唯一约束）。
        # 万一有第二条也指着同一个目录，宁可不认 —— 认错会把集写进别人的条目。
        same = self._db.count_subjects_by_folder(folder)
        if same > 1:
            log.warning("订阅名推算出的目录 %s 对应 %s 个条目，不敢认（记录 #%s）",
                        folder, same, rec.id)
            return 0
        log.info("记录 #%s 按目录 %s 归属到条目 #%s", rec.id, folder, subj.id)
        return int(subj.id)

    def _guess_folder_of_source(self, source) -> str:
        """按**订阅名**推算它该往哪个目录下（0 命中就返回空串）。

        复用 `plan_save_path` 那套"没有 `folder_path` 时按系列归位"的规则
        （媒体库根 / 系列名 / 季子目录），只是这里没有本地条目可依，只能拿
        订阅名当输入 —— 所以**算不准是常态**，调用方必须再判 `is_dir()`。
        """
        if source is None or not self._config:
            return ""
        try:
            from app.core.rss_matcher import (
                _safe_dir_name, _season_dir_name, _split_series_season,
            )

            raw = _safe_dir_name(source.name or "")
            if not raw:
                return ""
            roots = [p.strip() for p in
                     (self._config.get("general", "library_path", "") or "").split(";")
                     if p.strip()]
            if not roots:
                return ""
            root = Path(roots[0])
            series, season = _split_series_season(raw)
            series = _safe_dir_name(series)
            if series:
                if not season:
                    season = _season_dir_name(raw, series)
                target = root / series / _safe_dir_name(season)
            else:
                target = root / raw
            return str(target) if target.is_dir() else ""
        except Exception as e:          # pragma: no cover - 防御性
            log.debug("按订阅名推算目录失败：%s", e)
            return ""

    def _note_unresolved(self, source_id: int) -> None:
        """记一笔"这条记录解析不出条目"，供订阅页提示用户重新绑定。"""
        self._unresolved[int(source_id or 0)] = \
            self._unresolved.get(int(source_id or 0), 0) + 1
        self.unresolvedChanged.emit()

    def _clear_unresolved(self, source_id: int) -> None:
        """绑定变化后作废该订阅的"解析不出"计数（下一轮自然会重试）。"""
        sid = int(source_id or 0)
        if self._unresolved.pop(sid, None) is not None:
            self.unresolvedChanged.emit()

    def _mark_scanned(self, record_id: int) -> None:
        """把"已扫过"写进下载记录（失败只记日志，不影响扫描本身）。"""
        try:
            self._db.mark_download_scanned(int(record_id))
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("标记下载记录 #%s 已扫描失败：%s", record_id, e)



    @Slot(int, int, result="QVariantMap")
    def savePathPreview(self, source_id: int, subject_id: int) -> dict:
        """给「下载器」弹窗用：**算一遍"会下到哪"**，但不创建任何目录。

        返回 `{"path": 完整路径, "note": 界面显示的两行文案, "exists": 是否存在}`；
        `path` 为空表示"不干预，用 qBittorrent 自己的保存路径"。

        `subject_id` 传 0 时用订阅自己绑定的条目（与真正下发时的口径一致）。
        """
        try:
            src = next((s for s in self._db.list_rss_sources()
                        if s.id == int(source_id)), None)
            if src is None:
                return {"path": "", "note": "", "exists": False}
            if subject_id > 0:
                # 用户在弹窗里改选了别的条目 → 临时用它算（不落库）
                src = _with_save_subject(src, int(subject_id))
            matcher = RssMatcher(self._db, None, self._config)
            path, note, exists = matcher.plan_save_path(src)
            return {"path": path, "note": note, "exists": bool(exists)}
        except Exception as e:          # pragma: no cover - 防御性
            log.exception("推算保存路径失败 source=%s subject=%s: %s",
                          source_id, subject_id, e)
            return {"path": "", "note": "", "exists": False}

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
            log.warning("「获取名称」失败：订阅地址为空")
            self.failed.emit("请先填写订阅地址")
            return False
        if not (url.startswith("http://") or url.startswith("https://")):
            # **这条路径必须有日志**。
            hint = ""
            if "%" in url and "://" not in url:
                hint = "（看起来是地址被截断了：只有百分号编码部分、缺 https:// 开头）"
            log.warning("「获取名称」失败：地址不以 http(s):// 开头 —— %s%s",
                        url[:120], hint)
            self.failed.emit("订阅地址需以 http:// 或 https:// 开头" + hint)
            return False
        w = self._title_worker
        if w is not None and w.isRunning():
            log.info("「获取名称」忽略本次请求（上一次仍在抓取）：%s", url)
            self.message.emit("正在获取，请稍候…")
            return False

        proxy = self._config.get("bangumi", "proxy", "") if self._config else ""
        ua = self._config.get("bangumi", "user_agent",
                              "AnimeMarker/1.0") if self._config else "AnimeMarker/1.0"
        log.info("「获取名称」开始：%s", url)
        # 上一次的预匹配结果就此作废：本次抓的是另一个地址，
        # 若这次没匹配上而旧结果还留着，「新建并绑定」会绑到上一部去。
        self._suggest_name = ""
        self._suggest_keywords = []
        self._suggest_subject = None
        # 没有 Token 就不做预匹配（`_api is None` 让 worker 直接跳过）——
        # 「获取名称」本身不依赖 Token，没配也能用
        api = self._api if self._can_match() else None
        score = self._config.getint("scanner", "accept_score", 60) if self._config else 60
        gap = self._config.getint("scanner", "accept_gap", 20) if self._config else 20
        # source_id 传 0：这一步还没入库，不需要 id
        w = _FeedTitleWorker(0, url, proxy=proxy, user_agent=ua,
                             api=api, accept_score=score, accept_gap=gap)
        w.done.connect(self._on_title_suggested)
        # 引用留到 `finished` 才摘（不能挂在 `done` 上，见 utils/qt_thread.py）
        w.finished.connect(lambda: retire_worker(self, "_title_worker", w))
        self._title_worker = w
        w.start()
        self.message.emit("正在获取订阅源标题…")
        return True

    def _on_title_suggested(self, source_id: int, name: str, error: str,
                            url: str = "", keywords: Optional[list] = None,
                            preview: Optional[dict] = None) -> None:
        """「获取名称」收尾。`url` 是本次**实际抓取的地址**（见信号定义）。

        每一步都写日志 —— 这个方法有三条出口（失败 / 推断为空 / 成功），
        以前**一条日志都没有**，用户只能看到状态栏一句话。

        **不在这里摘 `_title_worker`**：`done` 是 run() 内部发的，那时线程
        还在跑，此刻丢引用就是 Qt6 的 qFatal（见 utils/qt_thread.py）。
        收尾交给 `retire_worker`（挂在 `finished` 上）。
        """
        if error:
            log.warning("「获取名称」失败：%s —— %s", url or "(未知地址)", error)
            self.failed.emit(error)
            return
        if not name:
            # 抓到了但清洗后为空（标题全是噪声）：不算错误，让用户手填。
            # **但必须记日志**：否则和"根本没抓到"在日志里长得一模一样。
            log.warning("「获取名称」抓取成功但未能推断出名字"
                        "（标题清洗后为空）：%s", url or "(未知地址)")
            self.message.emit("没能从订阅源推断出名称，请手动填写")
            return
        log.info("「获取名称」成功：%s → 候选 %s", url or "(未知地址)",
                 list(keywords or [name]))
        self._on_title_ready(int(source_id), name, list(keywords or []),
                             dict(preview or {}))

    def _on_title_ready(self, source_id: int, name: str, keywords: list,
                        preview: dict) -> None:
        """名字与预匹配都已到手 → 缓存下来、通知界面。

        **为什么输入框填的不一定是 `name`**：匹配成功时改填 Bangumi 的
        中文名。RSS 标题给的是资源写法（`青之芦苇 第二季 / Ao Ashi Season -`），
        而库里"中文名优先"的口径以 Bangumi 正名为准；用户看到回显后照样能改。

        **缓存是为了「新建并绑定」复用**：见 `createSubjectFromSource`。
        """
        subj = preview.get("subject") or None
        display = name
        if subj is not None:
            display = (subj.get("name_cn") or subj.get("name") or name).strip() or name

        # 输入框里那个名字排最前（用户可能已经改过），其余候选跟着
        cands = [c for c in keywords if c and c != display]
        if display:
            cands.insert(0, display)

        self._suggest_name = display
        self._suggest_keywords = cands
        self._suggest_subject = subj

        self.titleSuggested.emit(int(source_id), display)

        # 状态栏也带上原名：弹窗没打开时（在"添加/编辑订阅"表单里点的「获取」）
        # 这是用户唯一能看到"认成了哪一部"的地方。
        msg = f"已获取名称：{display}"
        if subj is not None:
            origin = (subj.get("name") or "").strip()
            if origin and origin != display:
                msg += f"（原名：{origin}）"
        self.message.emit(msg)
        self.matchPreviewed.emit(subj is not None,
                                 self._preview_text(preview, display))

    @staticmethod
    def _preview_parts(subj: dict, display: str) -> list[str]:
        """预匹配回显里的细节：原名 / 别名个数 / 集数。

        **别名个数要显示**：RSS 标题给的常是俗称或罗马音，只看中文名
        仍可能认错；别名列表是"这一部到底是不是我要的"最直接的证据。
        别名就在本次搜索响应里（`extract_aliases` 读的是 infobox），
        零额外请求。
        """
        parts: list[str] = []
        origin = (subj.get("name") or "").strip()
        if origin and origin != display:
            parts.append(f"原名：{origin}")
        count = len(extract_aliases(subj))
        if count:
            parts.append(f"别名 {count} 个")
        eps = subj.get("total_episodes") or subj.get("eps_count") or 0
        if eps:
            parts.append(f"{int(eps)} 集")
        return parts

    def _preview_text(self, preview: dict, display: str) -> str:
        """预匹配结果的一句话回显（弹窗里那行小字）。"""
        subj = preview.get("subject") or None
        if subj is not None:
            tail = "、".join(self._preview_parts(subj, display))
            return (f"匹配到「{display}」" + (f"（{tail}）" if tail else "")
                    + "；点「新建并绑定」就会用它")
        if preview.get("skipped"):
            return "未配置 Token，跳过预匹配（不影响新建条目）"
        if preview.get("network_failed"):
            return (f"预匹配失败：{preview.get('network_error') or '网络不通'}"
                    "；名字已填入，可手动改")
        reason = preview.get("reason") or ""
        return ("没能匹配到 Bangumi 条目"
                + (f"（{reason}）" if reason else "")
                + "，将按本地条目新建")

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
        # ---- 复用「获取」的结果（只对**它当时填进去的那个名字**有效）----
        #
        # 用户改过输入框就说明他不认可那次识别，**必须重新搜** ——
        # 否则"改了名字却仍绑到原来那一部"，比不匹配更让人困惑。
        reuse = bool(name) and name == self._suggest_name
        confirmed = self._suggest_subject if (do_match and reuse) else None
        keywords = list(self._suggest_keywords) if reuse else []
        if confirmed is not None:
            log.info("「新建并绑定」沿用「获取」时的预匹配（不再搜第二次）：%s", name)
        w = _CreateSubjectWorker(self._db, self._api, source_id, name,
                                 do_match=do_match,
                                 accept_score=score, accept_gap=gap,
                                 keywords=keywords, pre_matched=confirmed)
        self._create_workers.append(w)
        w.done.connect(self._on_subject_created)
        w.finished.connect(lambda: self._drop_create_worker(w))
        w.start()
        # 只用一次：留着的话，下次给别的订阅建条目时名字恰好相同，
        # 就会莫名其妙地复用到上一部
        self._suggest_name = ""
        self._suggest_keywords = []
        self._suggest_subject = None
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
            self.message.emit(f"已新建并绑定条目「{label}」"
                              + self._created_detail(subj, label))

        # ---- 通知媒体库刷新----
        #
        # 订阅列表（`sourcesChanged`）也要通知：绑定后卡片上会显示
        # 「→ 番名」的小标签，不刷新就会一直空着。
        self.reload()
        self.subjectCreated.emit(int(subject_id), label)

    @staticmethod
    def _created_detail(subj, label: str) -> str:
        """新建成功后的补充说明（原名 / 别名个数 / 集数）。

        弹窗一点「新建并绑定」就关了，这行是**唯一**的事后反馈，
        所以要把"连到哪一部"说全：RSS 标题给的多半是简称或罗马音，
        只报中文名用户认不出是哪一部，**原名**才是他心里的那一个。
        未匹配到 Bangumi 时不加 —— 那些数据本来就没有。
        """
        if subj is None or not subj.bangumi_id:
            return ""
        aliases = Database.aliases_from_text(getattr(subj, "aliases", "") or "")
        parts: list[str] = []
        name = (subj.name or "").strip()
        if name and name != (label or "").strip():
            parts.append(f"原名：{name}")
        if aliases:
            parts.append(f"别名 {len(aliases)} 个")
        if subj.total_eps:
            parts.append(f"{int(subj.total_eps)} 集")
        return f"（{'，'.join(parts)}）" if parts else ""

    def waitWorkers(self, ms: int = 3000) -> None:
        """退出时等待进行中的网络线程（QmlApp.shutdown 调用）。"""
        for w in [self._title_worker, self._torrent_worker,
                  self._preview_worker, *self._create_workers]:
            if w is not None and w.isRunning():
                log.info("等待订阅相关线程结束…")
                w.wait(ms)
