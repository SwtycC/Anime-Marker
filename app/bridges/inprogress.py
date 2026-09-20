"""Bangumi 收藏列表桥接（阶段 7 · F18 的 QML 版本）。

职责：从 Bangumi 拉取「当前用户 · **全部收藏状态**」收藏，写入
`inprogress_cache`，然后通知 `LibraryBridge` 刷新（QML 侧读 `library.inProgress`）。

> 一次拉全部状态（**不传 `type`**，见 `BangumiClient.get_user_collections`），
> 但消费方口径各不相同：
> **在看页只读「在看」**（`library.inProgress` → `collect_type=3`），
> **动态页的候选是全部状态**（逐集记录主要产生于追番期间，但看过/搁置的
> 条目同样可能有逐集标记）。

> **命名说明**：表名、Property 名都还叫 `inprogress*`（沿用 F18 的旧实现），
> 但**语义已改为「看过」**。之所以不改名，是因为要动 `inprogress_cache` 表、
> `library.inProgress`、`InProgressPage` 一整条链路，收益仅是"名字更好看"，
> 而风险是漏改某处导致静默失效。这里用注释显式记录，避免后人误判。

设计要点：
1. **网络走 QThread**：拉取可能耗时数秒（分页 + 代理），不能阻塞 UI。
2. **离线降级**：拉到数据就整体替换缓存；失败则保留旧缓存，
   只把错误通过 `failed` 信号告诉状态栏 —— 断网时页面仍有内容可看。
3. **用户名解析**：优先用 Token 调 `/v0/me` 拿 `username`
   （注意**不是昵称**，详见 `resolveUsername()` 的说明）。
4. **本地关联**：只按 `bangumi_id` 查本地条目，不做模糊匹配 ——
   关联结果用于「跳详情」按钮，宁可没有也不要指错。
5. **集级记录增量同步**：一部的逐集记录只在"没同步过 / 收藏变过 /
   在看 / 超期"时才重新请求，判据集中在 `sync_candidates()`（纯函数）。
   详见该函数与 `_EpisodeWorker` 的说明。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from PySide6.QtCore import QObject, Property, QThread, QTimer, Signal, Slot

from app.core.bangumi_api import (
    COLLECT_TYPE_DOING, COLLECT_TYPE_DONE, COLLECT_TYPE_DROPPED,
    COLLECT_TYPE_ON_HOLD, COLLECT_TYPE_WISH, BangumiAuthError, BangumiClient,
    BangumiError,
)
from app.core.config import Config
from app.core.database import Database, EpSyncState

log = logging.getLogger(__name__)

# 收藏分页上限。**注意口径变了**：以前是"每类各 600"（看过 600 + 在看 600），
# 现在一次拉全部状态，600 变成**所有状态共用**的额度 —— 触顶会在尾部静默
# 截断（`iter_user_collections` 会记 warning）。实测本账号 160 条，
# 2000 对个人用户足够，同时避免异常账号把内存打爆。
MAX_ITEMS = 2000

# 收藏状态的中文名（只用于日志）。枚举取值见 bangumi_api 的 COLLECT_TYPE_*。
_COLLECT_TYPE_NAMES = {
    COLLECT_TYPE_WISH: "想看",
    COLLECT_TYPE_DONE: "看过",
    COLLECT_TYPE_DOING: "在看",
    COLLECT_TYPE_ON_HOLD: "搁置",
    COLLECT_TYPE_DROPPED: "抛弃",
}


def _collection_time(item: dict) -> str:
    """取条目的「Bangumi 收藏修改时间」（ISO 串），缺失时回落到缓存写入时间。

    两个键的来历见 `Database.replace_inprogress_cache`：
    `collection_updated_at` 逐条不同（真正决定"最近看的是哪几部"），
    `updated_at` 是本次拉取时刻（整批相同，只能当兜底）。

    **只用于排序与时间兜底，绝不可用于同步水位比较** —— 兜底值每次都变，
    会让该部每次刷新都重拉。水位比较用 `sync_key()`。
    """
    return item.get("collection_updated_at") or item.get("updated_at") or ""


def _iso_from_epoch(ts) -> str:
    """Unix 秒 → 本地时区 ISO 串；非法值返回空串。

    注意：集级接口 `/v0/users/-/collections/{sid}/episodes` 的 `updated_at`
    与收藏列表接口的 **ISO 串不同**，它是 **Unix 秒整数**。
    """
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().isoformat(
            timespec="seconds")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


# ---- 集级记录的增量同步判定（纯函数，无 DB、无网络）----
#
# 这两个函数是整个增量逻辑的**唯一判据**，刻意写成模块级纯函数：
# 它们能脱离网络和数据库直接验（见 技术文档 的验证章节），
# 也是"某部为什么每次刷新都重拉"这类问题的第一现场。

def _field(item, name: str, default=None):
    """兼容两种行形态取值：缓存行是 dataclass（`InProgressItem`），
    拉取阶段的候选项是 dict（`_FetchWorker` 产出的）。

    纯函数要能被两边复用，又要在测试里能直接喂 dict，所以这里统一入口。
    """
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def sync_key(item) -> tuple[str, int]:
    """水位比较键：**(原始 collection_updated_at, ep_status)**。

    用 `item` 里的原始字段，**不要走 `_collection_time()`** —— 那个函数在
    字段缺失时会回落到"本次拉取时刻"，那个值每次都变，会让该部被判成
    "收藏变了"从而每次刷新都重拉。
    """
    return (_field(item, "collection_updated_at") or "",
            int(_field(item, "ep_status") or 0))


# 超期兜底的期限（天）。见 `sync_candidates` 的说明。
RESYNC_AFTER_DAYS = 30


def sync_candidates(
    collections: list[dict],
    state: dict[int, EpSyncState],
    now: Optional[datetime] = None,
) -> list[tuple[dict, str]]:
    """挑出**需要重新拉取集级记录**的条目，返回 `[(条目, 原因)]`。

    判定规则（合取的第一项 + 后面任一成立）：

        无水位行                         → "首次"
        水位.collection_updated_at 变了   → "收藏时间变"
        水位.ep_status 变了               → "ep_status 变"
        当前是「在看」                    → "在看"
        水位.synced_at 超过 30 天          → "超期"

    设计取舍（每条都踩过或差点踩）：

    1. **「在看」永远重拉**：增量依赖"标了集 → 收藏行会变"这个假设。
       在看番通常只有十几部，全部重拉的成本可忽略，换来"最新动态一定不漏"。
       这是整套设计里**唯一的兜底假设**。
    2. **故意没有"该部在 watched_episodes 里一行都没有 → 重拉"这条**：
       对"请求成功但确实没有逐集标记"的条目（只标了整部状态、剧场版）
       它会**每次刷新都重拉**，形成死循环。正确做法是"成功就写水位"
       （含 0 行，那是终态答案），失败不写 → 下次自然重试。
    3. **没有用 `ep_status > 0` 当门槛**：实测 `ep_status` 与逐集记录条数
       **不是一回事**（bangumi_id=515594 的 ep_status=16 却只有 11 条记录），
       它只省十几个请求，却引入一个不可证伪的假设。
    4. **30 天超期兜底**：覆盖"看过/搁置的番事后补标了几集、而 Bangumi
       没动整部收藏行"这种漏网情形。150 部摊到每天约 5 个请求。
    """
    now = now or datetime.now().astimezone()
    out: list[tuple[dict, str]] = []
    for item in collections:
        bid = int(_field(item, "bangumi_id") or 0)
        if not bid or not _field(item, "name"):
            # 没有 bangumi_id 的残行无法请求，跳过（日志由调用方汇总）
            continue
        cur = sync_key(item)
        st = state.get(bid)
        if st is None:
            out.append((item, "首次"))
            continue
        if st.collection_updated_at != cur[0]:
            out.append((item, "收藏时间变"))
            continue
        if int(st.ep_status or 0) != cur[1]:
            out.append((item, "ep_status 变"))
            continue
        if int(_field(item, "collect_type") or 0) == COLLECT_TYPE_DOING:
            out.append((item, "在看"))
            continue
        synced_at = _parse_iso(st.synced_at)
        if synced_at is None:
            out.append((item, "超期"))      # 水位时间不可解析 → 当作超期
            continue
        if (now - synced_at).days >= RESYNC_AFTER_DAYS:
            out.append((item, "超期"))
    return out


def _parse_iso(value: str) -> Optional[datetime]:
    """ISO 串 → datetime（带时区）；空值/非法返回 None。

    项目里的时间串一律是 `_now()` / `isoformat()` 写出的带偏移格式，
    但老库里可能有裸格式，故 `fromisoformat` 失败时补一次"按本地时区解释"。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


class _FetchWorker(QThread):
    """后台拉取收藏列表（**动画 · 看过 + 在看**，见 run() 里的说明）。

    两类都拉的原因：动态页的逐集记录主要产生于追番期间，只收「看过」
    会把最新记录全过滤掉。收藏页则只读「看过」（type=2）。
    """

    finished_items = Signal(object, str)   # (list[dict], error_message)

    def __init__(
        self,
        api: BangumiClient,
        username: str,
        max_items: int = MAX_ITEMS,
    ) -> None:
        super().__init__()
        self.api = api
        self.username = username
        self.max_items = max_items

    def run(self) -> None:
        try:
            # ---- 解析 username（不是昵称！）----
            #
            # 踩坑记录：Bangumi 的 `/v0/users/{username}` 路径参数要的是
            # **`username` 字段**，而不是界面上显示的**昵称**（`nickname`）。
            # 用昵称去请求会返回 **404「用户不存在」**，且错误信息极具误导性
            # （看起来像"接口挂了"或"用户没数据"）。
            #
            # 实测（GET /v0/me）：
            #     username: '123456'     ← API 要的是这个（可能也是数字）
            #     nickname: 'your-name'  ← 界面上显示的名字，填它必 404
            #
            # 因此策略是：**只要 Token 可用就优先自动解析**，配置里那一栏
            # 仅作兜底（Token 无效时的离线场景）。这样用户填了昵称也不会出错。
            username = ""
            me = self.api.get_me()
            if me:
                username = (me.get("username") or "").strip()
                if username:
                    log.info("已从 Token 解析 username：%s（昵称 %s）",
                             username, me.get("nickname") or "-")

            if not username:
                # Token 解析失败 → 用配置里的值兜底
                username = (self.username or "").strip()
                if username:
                    # 失败原因见上一条日志（`get_me` 会分类：401/403 才是 Token 问题，
                    # 5xx / 连接失败与 Token 无关）。这里不重复猜测原因 ——
                    # 「填 username 而非昵称」那条提醒已经由 404 分支负责。
                    log.warning(
                        "未能用 Token 解析 username，改用配置值：%r（原因见上一条日志）",
                        username)

            if not username:
                self.finished_items.emit(
                    [], "无法确定 Bangumi 用户名，请检查 Token 是否有效")
                return

            # **一次拉全部收藏状态**（subject_type=2 动画 + 不传 type）。
            #
            # 为什么不再分开拉「看过」+「在看」：`collect_type=None` 时
            # 请求里没有 `type` 参数，官方 API 的语义就是"全部状态"
            # （想看/在看/看过/搁置/抛弃），一次分页拿全 —— 比原来两次
            # 调用少一半请求，而且不会再漏掉搁置/抛弃的条目（它们同样可能
            # 有逐集标记，动态页要用）。
            #
            # 两个消费方口径不同，都靠 `collect_type` 列区分：
            # 在看页只读 3（见 LibraryBridge._load_inprogress），
            # 动态页的候选是全部（再按 sync_candidates 决定拉哪几部）。
            raw = self.api.iter_user_collections(
                username,
                collect_type=None,
                max_items=self.max_items,
            )
            items = [self._to_cache_item(r) for r in raw]
            # 按状态分组记一条日志 —— 这是"覆盖全部状态"唯一可观测的证据，
            # 出问题时（比如某种状态全丢了）一眼能看出来
            dist: dict[int, int] = {}
            for it in items:
                dist[int(it.get("collect_type") or 0)] = \
                    dist.get(int(it.get("collect_type") or 0), 0) + 1
            log.info("已拉取收藏 %s 条，按状态分布：%s", len(items),
                     " ".join(f"{_COLLECT_TYPE_NAMES.get(k, k)}={v}"
                              for k, v in sorted(dist.items())))

            self.finished_items.emit(items, "")
        except BangumiAuthError as e:
            log.warning("拉取收藏列表权限不足: %s", e)
            self.finished_items.emit([], f"Token 权限不足：{e}")
        except BangumiError as e:
            # 404 在这里几乎总是"用户名填成了昵称"，给出可操作的提示
            msg = str(e)
            if "404" in msg:
                log.warning("拉取收藏列表 404（通常是 username 填成了昵称）: %s", e)
                self.finished_items.emit(
                    [], "用户不存在：请确认「用户 ID」填的是 username（数字 ID）"
                        "而非昵称，或清空该栏让程序自动从 Token 解析")
                return
            log.warning("拉取收藏列表失败: %s", e)
            self.finished_items.emit([], msg)
        except Exception:
            log.exception("拉取收藏列表异常")
            self.finished_items.emit([], "拉取失败（详见日志）")

    @staticmethod
    def _to_cache_item(raw: dict) -> dict:
        """把 Bangumi collection 条目转成 `replace_inprogress_cache` 的入参。

        **`collect_type` 只能来自响应里的 `type` 字段**。以前这里有个
        「调用方请求的那一类」兜底（请求看过就按看过算），现在一次请求
        拿全部状态，没有"请求的那一类"可退了 —— 缺字段时只能按
        COLLECT_TYPE_DONE 兜底**并记一条 warning**（会把在看误标成看过，
        直接影响在看页的过滤，所以必须留痕）。

        Bangumi 的返回结构（/v0/users/{u}/collections）：
            { "subject_id": 123, "subject": { "name": ..., "name_cn": ...,
              "images": {...}, "eps": 12 }, "type": 2, "ep_status": 11,
              "updated_at": "2026-09-11T09:35:04+08:00" }
        注意**没有** total_eps 顶层字段，集数在 `subject.eps`。

        `updated_at` 是**该收藏的最后修改时间**（ISO 串，逐条不同），
        必须原样带到 `collection_updated_at` 列 —— 它既决定"最近看的是
        哪几部"的排序，也是**增量同步的水位依据**（见 sync_key）。
        早期版本把它丢掉了，排序只能退回"本次拉取时刻"（整批相同），
        「最近 N 部」实际变成「按名字排序的前 N 部」。
        """
        subject = raw.get("subject") or {}
        images = subject.get("images") or {}
        # 封面优先 large，其次 common（与旧版一致）
        cover = images.get("large") or images.get("common") or images.get("medium") or ""

        # 总集数兜底链：subject.eps → total_episodes → 已看集数。
        # 实测「碧蓝之海 第三季」的 eps 为 0（Bangumi 数据缺失），
        # 此时用 ep_status 兜底，至少不会显示成"共 0 集"。
        eps = subject.get("eps") or subject.get("total_episodes") or 0
        ep_status = raw.get("ep_status") or 0
        if not eps and ep_status:
            eps = ep_status

        raw_type = raw.get("type")
        if raw_type is None:
            # 见 docstring：一次拉全部状态后这里没有正确的兜底值
            log.warning("收藏条目 %s（%s）缺少 type 字段，按「看过」处理",
                        raw.get("subject_id"), subject.get("name_cn")
                        or subject.get("name"))

        return {
            "bangumi_id": raw.get("subject_id") or subject.get("id") or 0,
            "name": subject.get("name") or "",
            "name_cn": subject.get("name_cn") or "",
            "cover_url": cover,
            "ep_status": ep_status,
            "total_eps": eps,
            "collect_type": int(raw_type if raw_type is not None
                                else COLLECT_TYPE_DONE),
            # 收藏的最后修改时间（ISO 串）——排序与**增量水位**的依据
            "collection_updated_at": raw.get("updated_at") or "",
        }


class _EpisodeWorker(QThread):
    """并发拉取集级观看记录（F20）—— **全量同步 + 按批回传**。

    为什么需要并发：集级接口是**每部动漫一次请求**
    （`/v0/users/-/collections/{sid}/episodes`），串行拉 160 部要几十秒。
    线程池 8 并发，实测 120 个请求约 2 秒。

    **为什么不再"凑够即停"**：`ep_timeline_count` 只管"动态页显示多少条"，
    而动态页要的是**完整历史** —— 早停会让排在后面的上百部（尤其是"看过"
    的番，它们按收藏时间排序时排在最近追的番后面）永远拉不到。
    实测症状：库里有 149 部看过番，`watched_episodes` 却只有 4 部的记录。
    现在"该不该拉"由 `sync_candidates()` 决定（增量），不看显示条数。

    **为什么按批发信号**：首次全量约 20 秒，只在结束时发一次会让动态页
    20 秒白屏。每批（BATCH 部）发一次 `synced`，主线程收一批写一批。

    **worker 不碰数据库**：只负责网络与解析，结果交给主线程写库 ——
    保持这条边界可以让 SQLite 始终只在主线程被访问，`Database._cursor()`
    的全局锁也就不必被跨线程争用。

    为什么不复用主 `_FetchWorker`：它负责"拉收藏列表"，这是"再拉每部的
    集级明细"，是两阶段任务。拆开还有一个好处 —— 第一阶段失败（列表拿不到）
    时不必进入第二阶段，省掉必然失败的请求。
    """

    finished_eps = Signal(object, str)    # (汇总 dict, error)
    synced = Signal(object)               # (list[单部结果 dict]，每批一次)
    progress = Signal(int, int)           # (已完成部数, 总部数)

    # 每批提交的部数 —— **只决定进度上报与落库的粒度**，不再影响"拉几部"
    # （以前由目标条数反推是为了早停；现在全量拉，批次与屏幕上的条数无关）。
    # 10 部 ≈ 1 秒一批，进度够细腻，写库也不会一次写太多。
    BATCH = 10

    # 兜底上限：防御异常账号（收藏上千部）把请求数与内存打爆。
    # 正常个人账号远低于此值。
    MAX_SUBJECTS = 500

    def __init__(
        self,
        api: BangumiClient,
        subjects: list[dict],   # [{bangumi_id, title, local_id, collection_time,
                                #   collection_updated_at, ep_status}]
        workers: int = 8,
    ) -> None:
        super().__init__()
        self.api = api
        self.subjects = subjects[:self.MAX_SUBJECTS]
        self.workers = max(1, min(workers, 16))
        # 实际请求了几部（供上层在状态栏里说明）
        self.fetched_subjects = 0

    def run(self) -> None:
        planned = len(self.subjects)
        final: dict[int, dict] = {}       # bangumi_id → 该部**最终**的结果
        if not self.subjects:
            self.finished_eps.emit(self._summary(final), "")
            return

        try:
            results = self._sync_batches(self.subjects, planned)
            final.update({r["bangumi_id"]: r for r in results})

            failed = [r for r in results if not r["ok"]]
            if failed:
                # 线程本地 Session 是 max_retries=0（项目惯例：整批失败由外层
                # 统一处理，不逐条重试），160 个请求下偶发失败几乎必然。
                # 补跑一轮能明显改善首次全量的完整率 —— 只补一次，不递归。
                retry_ids = {r["bangumi_id"] for r in failed}
                retry = [s for s in self.subjects
                         if int(s["bangumi_id"]) in retry_ids]
                log.info("集级同步：%s 部失败，补跑一轮", len(retry))
                results2 = self._sync_batches(retry, planned)
                final.update({r["bangumi_id"]: r for r in results2})
        except Exception:
            log.exception("集级观看记录同步异常")
            self.finished_eps.emit(self._summary(final),
                                   "同步集级记录失败（详见日志）")
            return

        summary = self._summary(final)
        msg = ""
        if summary["failed"] and not summary["synced"]:
            msg = "全部条目的集级记录都拉取失败（可能是网络或 Token 权限）"
        elif summary["failed"]:
            msg = (f"{summary['failed']} 部拉取失败（其余成功，"
                   "下次刷新会自动重试）")
        if summary["skipped"]:
            log.warning("有 %s 部返回了原始记录但没有任何可用的逐集记录"
                        "（未写库、未记水位，下次会重试）", summary["skipped"])
        if self.fetched_subjects >= self.MAX_SUBJECTS and planned >= self.MAX_SUBJECTS:
            log.warning("本次同步达到上限 %s 部，剩余条目下次刷新继续",
                        self.MAX_SUBJECTS)
        log.info("集级记录同步完成：成功 %s 部 / %s 条，跳过 %s 部，失败 %s 部",
                 summary["synced"], summary["rows"],
                 summary["skipped"], summary["failed"])
        self.finished_eps.emit(summary, msg)

    def _sync_batches(self, subjects: list[dict], planned: int) -> list[dict]:
        """按批并发拉取，**每批发一次 `synced`**，返回全部单部结果。

        分批提交而不是把所有部一次丢进线程池：这样每批结束就能落库一次，
        动态页在首次全量的 20 秒里能持续长出记录，而不是最后一起出现。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # ---- 每个线程一个独立 Session ----
        #
        # **踩坑（性能）**：`requests.Session` **不是线程安全的**。
        # 多线程共享同一个 Session 时，内部连接池会成为瓶颈，
        # 实测 30 个请求用 8 并发要 **44 秒**（而独立 Session 只要 2 秒）。
        # 原因是共享 Session 的 `HTTPAdapter` 在多线程下会争用同一批连接，
        # 叠加本项目配置的 3 次重试与指数退避，耗时被放大 20 倍以上。
        #
        # 这里为每个线程建一个"精简版" Session：只复制认证与代理配置，
        # **不带重试适配器**（请求失败由上层统一重试/计数，不逐条重试）。
        local = threading.local()

        def session_for_thread():
            s = getattr(local, "session", None)
            if s is None:
                s = requests.Session()
                s.headers.update(dict(self.api.session.headers))
                if self.api.session.proxies:
                    s.proxies = dict(self.api.session.proxies)
                s.mount("https://", HTTPAdapter(max_retries=0))
                s.mount("http://", HTTPAdapter(max_retries=0))
                local.session = s
            return s

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for start in range(0, len(subjects), self.BATCH):
                batch = subjects[start:start + self.BATCH]
                futures = [pool.submit(self._fetch_one, session_for_thread, s)
                           for s in batch]
                batch_out: list[dict] = []
                for fut in as_completed(futures):
                    try:
                        batch_out.append(fut.result())
                    except Exception:
                        # `_fetch_one` 内部已把异常转成 ok=False，能到这里
                        # 说明是它自己出的意外（bug），记全栈别吞
                        log.exception("集级记录任务异常（不应发生）")
                self.fetched_subjects += len(batch)
                results.extend(batch_out)
                self.synced.emit(batch_out)
                self.progress.emit(min(self.fetched_subjects, planned), planned)
        return results

    def _fetch_one(self, session_for_thread, s: dict) -> dict:
        """拉取**单部**的逐集记录，返回结果字典（异常也转成 ok=False）。

        返回结构（`synced` 信号与主线程写库共用）：

            { "bangumi_id", "ok", "rows", "raw_count", "dropped",
              "collection_updated_at", "ep_status", "error" }

        `raw_count` 与 `rows` 的差别是**关键的护栏**：`raw_count > 0` 而
        `rows == 0` 说明服务端返回了记录、但过滤后一条不剩（历史上出现过
        "`updated_at` 恒为 0 → 整批被丢弃 → 动态页一片空白"的故障）。
        调用方据此**不写库、不记水位**，下次重试（见 `_on_ep_synced`）。

        `collection_updated_at` / `ep_status` 原样回传给写库方，**不要在
        写库时再从收藏缓存里查一次** —— 那会让"判断要不要同步"与"写水位"
        两个口径有机会漂移。
        """
        bid = int(s.get("bangumi_id") or 0)
        out = {
            "bangumi_id": bid,
            "ok": False,
            "rows": [],
            "raw_count": 0,
            "dropped": 0,
            "collection_updated_at": s.get("collection_updated_at") or "",
            "ep_status": int(s.get("ep_status") or 0),
            "error": "",
        }
        try:
            raw = self._fetch_with_session(session_for_thread(), bid)
        except Exception as e:
            # 单个条目失败不影响整体（网络抖动很常见）
            log.warning("拉取 %s 集级记录失败: %s", bid, e)
            out["error"] = str(e)
            return out

        out["raw_count"] = len(raw)
        # 兜底时间：优先用单集自己的时间戳，缺失时退回该动漫收藏级的
        # 时间（同一部动漫的每集时间都相同，视觉上仍按动漫聚拢）。
        # `collection_time` 由上层用 `_collection_time()` 算好传入 ——
        # 注意那是**排序/兜底**口径，不是水位口径（见 sync_key）。
        collection_time = s.get("collection_time") or ""
        rows: list[dict] = []
        for r in raw:
            # 只保留"看过"（type=2）。type 枚举与收藏一致。
            if (r.get("type") or 0) != 2:
                continue
            ep = r.get("episode") or {}
            watched_at = _iso_from_epoch(r.get("updated_at")) or collection_time
            if not watched_at:
                continue
            rows.append({
                "bangumi_ep_id": int(ep.get("id") or 0),
                "bangumi_id": bid,
                "subject_id": int(s.get("local_id") or 0),
                "subject_name": s.get("title") or "",
                "ep_index": float(ep.get("sort") or ep.get("ep") or 0),
                "ep_name": ep.get("name_cn") or ep.get("name") or "",
                "watched_at": watched_at,
            })
        out["rows"] = rows
        out["dropped"] = len(raw) - len(rows)
        out["ok"] = True
        return out

    @staticmethod
    def _summary(final: dict[int, dict]) -> dict:
        """把"每部的最终结果"汇总成计数（供状态栏文案与日志）。

        三类口径：
        - `synced`  —— 成功且可写库（含"成功但没有记录"的终态）
        - `skipped` —— 服务端返回了记录但过滤后一条不剩（不写库、不记水位）
        - `failed`  —— 请求失败（不写库、不记水位）
        """
        summary = {"synced": 0, "rows": 0, "skipped": 0, "failed": 0}
        for r in final.values():
            if not r["ok"]:
                summary["failed"] += 1
            elif r["raw_count"] and not r["rows"]:
                summary["skipped"] += 1
            else:
                summary["synced"] += 1
                summary["rows"] += len(r["rows"])
        return summary

    def _fetch_with_session(self, session, subject_id: int) -> list[dict]:
        """用指定 Session 拉某条目的逐集收藏（线程安全版）。

        与 `BangumiClient.get_subject_episode_collections` 的逻辑一致，
        但用调用方提供的 Session —— 这样每个线程各用各的连接池。
        分页循环保留（集数 >100 时仍能拿全）。

        **踩坑（时间字段，两次实测结论不同）**：返回的
        `[{episode, type, updated_at}]` 里，`updated_at` 的可用性变过：

        - 早期实测**恒为 0**（字段在、值没有），早期版本见 `not ts: continue`
          直接丢弃，导致整批记录写库为 0 条、动态页一片空白；
        - 2026-09-19 复查：该字段**有值**（本账号 30/30 行都是精确到秒的
          真实单集时间戳）。

        所以现在是"单集时间戳 → 动漫收藏级 updated_at"两级兜底（见 run()）：
        有真实值就用真实值（同一部的各集能落到不同时刻，更准），
        拿不到才退回收藏级时间。**兜底分支不能删** —— 它是那份"一片空白"
        故障的护栏，而服务端行为已经变过一次，不保证不会再变。
        """
        out: list[dict] = []
        offset = 0
        limit = 500
        while len(out) < limit:
            page_size = min(limit - len(out), 100)
            resp = session.get(
                f"{self.api.api_base}/v0/users/-/collections/{subject_id}/episodes",
                params={"episode_type": 0, "limit": page_size, "offset": offset},
                timeout=self.api.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                break
            page = data.get("data", [])
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out[:limit]


class InProgressBridge(QObject):
    """Bangumi「看过」收藏数据源（负责拉取与落库，展示交给 LibraryBridge）。"""

    # 状态通知
    runningChanged = Signal()
    started = Signal()
    finished = Signal(int)        # 拉到的条数
    failed = Signal(str)          # 错误文案
    message = Signal(str)         # 中间进度文案

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._api = api
        self._running = False
        self._worker: Optional[_FetchWorker] = None
        self._ep_worker: Optional[_EpisodeWorker] = None
        # 界面刷新节流：首次全量会写十几批，合并到最多 1 次/秒
        # （见 _schedule_ui_refresh）
        self._ui_refresh_pending = False
        # 拉取成功后由 QmlApp 接上，用来刷新 QML 的 library.inProgress
        self._done_hook = None
        # 集级记录拉完后额外通知（让动态页重算）
        self._ep_done_hook = None

    # ---------- 配置 ----------
    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    def set_done_hook(self, hook) -> None:
        """设置拉取成功后的回调（通常是 library.reloadInProgress）。"""
        self._done_hook = hook

    def set_episode_done_hook(self, hook) -> None:
        """设置集级记录拉完后的回调（通常是 library.reloadWatchedEpisodes）。"""
        self._ep_done_hook = hook

    # ---------- 状态 ----------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    def _set_running(self, value: bool) -> None:
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    # ---------- 拉取 ----------
    @Slot()
    def refresh(self) -> None:
        """拉取「动画 · 看过」并写入缓存。重复调用会被忽略。"""
        if self._running:
            log.info("收藏列表拉取中，忽略本次请求")
            return

        username = (self._config.get("bangumi", "username", "") or "").strip()
        self._set_running(True)
        self.started.emit()
        self.message.emit("正在拉取 Bangumi 看过列表…")

        self._worker = _FetchWorker(self._api, username, MAX_ITEMS)
        self._worker.finished_items.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_finished(self, items: list, error: str) -> None:
        self._set_running(False)

        if error:
            # 离线降级：保留旧缓存，只报错
            log.warning("收藏列表拉取失败，保留旧缓存: %s", error)
            self.failed.emit(error)
            return

        if not items:
            log.info("收藏列表为空")
            self._db.replace_inprogress_cache([])
            self._refresh_library()
            self.finished.emit(0)
            self.message.emit("Bangumi 上没有「看过」的动漫")
            return

        try:
            self._db.replace_inprogress_cache(items)
        except Exception as e:
            log.exception("写入在看缓存失败: %s", e)
            self.failed.emit(f"写入缓存失败：{e}")
            return

        # 两个状态分别计数：收藏页只显示「看过」，动态页两类都用，
        # 报清楚才看得出"在看的那几部也进来了"
        done_n = sum(1 for it in items
                     if int(it.get("collect_type") or COLLECT_TYPE_DONE)
                     == COLLECT_TYPE_DONE)
        doing_n = len(items) - done_n
        log.info("收藏列表已更新：%s 条（看过 %s / 在看 %s）",
                 len(items), done_n, doing_n)
        self._refresh_library()
        self.finished.emit(done_n)        # 对外沿用"看过条数"口径
        self.message.emit(
            f"Bangumi 收藏已更新（看过 {done_n} 部 · 在看 {doing_n} 部）")

        # ---- 第二阶段：拉「最近 N 部」的集级观看记录 ----
        self._start_episode_fetch(items)

    def _start_episode_fetch(self, collections: list) -> None:
        """**增量**同步集级观看记录（F20）。

        与旧版的区别（一句话）：**该拉哪几部由 `sync_candidates()` 决定，
        不再由"动态页显示多少条"决定**。旧版按收藏时间从新到旧拉、凑够
        `ep_timeline_count` 条就停，导致排在后面的上百部（尤其"看过"的番）
        永远拉不到 —— 实测 149 部看过番只有 4 部留下了逐集记录。

        `ep_timeline_count` 现在**只管显示**（首屏条数 + 加载更多的页大小），
        与拉取无关；`<= 0` 时跳过同步（用户关闭了集级记录，见 applyEpisodeCount）。

        排序：仍按收藏修改时间倒序，这样**分批落库时最新的记录先出现**，
        用户在首次全量的十几秒里能立刻看到最近的内容。
        截断（MAX_SUBJECTS）**必须在过滤之后**，否则排在尾部、需要同步的
        条目会被永久饿死（每次刷新都从头截同一批）。
        """
        try:
            n = self._config.getint("bangumi", "ep_timeline_count", 30)
        except Exception:
            n = 30
        if n <= 0:
            log.info("集级时间线已关闭（ep_timeline_count=0），跳过同步")
            self.message.emit("集级时间线已关闭")
            return

        ordered = sorted(collections, key=_collection_time, reverse=True)
        try:
            state = self._db.load_ep_sync_state()
        except Exception as e:
            log.exception("读取同步水位失败: %s", e)
            state = {}

        cands = sync_candidates(ordered, state)
        if not cands:
            log.info("集级记录已是最新，无需同步（候选 %s 部）", len(collections))
            self.message.emit("集级观看记录已是最新")
            return

        # 本地关联：一次建映射，别在循环里逐部查库（160 次 SELECT）
        try:
            local_map = {s.bangumi_id: s.id for s in self._db.list_subjects()
                         if s.bangumi_id}
        except Exception as e:
            log.exception("读取本地条目失败（本地关联将为空）: %s", e)
            local_map = {}

        subjects = [
            {
                "bangumi_id": int(_field(c, "bangumi_id") or 0),
                "title": _field(c, "name_cn") or _field(c, "name") or "",
                "local_id": local_map.get(int(_field(c, "bangumi_id") or 0), 0),
                # 单集时间戳的兜底：集级接口的 `updated_at` 历史上出现过恒为 0
                # （字段存在但无值，见 _fetch_with_session 的说明），此时用该
                # 动漫**收藏的最后修改时间**兜底，否则整批记录都会因为"没有
                # 时间"被丢弃。用收藏时间而不是"本次拉取时刻"：同一部的每集
                # 拿到同一个时间（视觉上仍按动漫聚拢），且那是用户真的看完
                # 那部的日子。
                "collection_time": _collection_time(c),
                # 水位口径的原始值（**不是** collection_time，见 sync_key）
                "collection_updated_at": _field(c, "collection_updated_at") or "",
                "ep_status": int(_field(c, "ep_status") or 0),
            }
            for c, _reason in cands
            if _field(c, "bangumi_id")
        ]
        if not subjects:
            log.info("没有可用于同步集级记录的条目")
            return

        planned = len(subjects)
        trimmed = planned > _EpisodeWorker.MAX_SUBJECTS
        subjects = subjects[:_EpisodeWorker.MAX_SUBJECTS]
        dist: dict[str, int] = {}
        for _c, reason in cands:
            dist[reason] = dist.get(reason, 0) + 1
        log.info("开始同步集级记录：需同步 %s 部（%s）%s",
                 planned,
                 " ".join(f"{k} {v}" for k, v in sorted(dist.items())),
                 f"，本次先做 {len(subjects)} 部" if trimmed else "")
        self.message.emit(f"正在同步集级记录（{len(subjects)} 部）…")
        self._set_running(True)

        self._ep_worker = _EpisodeWorker(self._api, subjects)
        self._ep_worker.progress.connect(self._on_ep_progress)
        self._ep_worker.synced.connect(self._on_ep_synced)
        self._ep_worker.finished_eps.connect(self._on_ep_finished)
        self._ep_worker.finished.connect(self._ep_worker.deleteLater)
        self._ep_worker.start()

    def _on_ep_progress(self, done: int, total: int) -> None:
        """进度是**部数**。全量同步的每一步都是"又搞定了一部"，
        报部数才对得上用户的直观感受（旧版报条数是因为当时按条数早停）。"""
        # 只在整数百分比变化时报一次，避免刷屏
        if total and (done == total or done % max(1, total // 10) == 0):
            self.message.emit(f"正在同步集级记录… {done}/{total} 部")

    @Slot(object)
    def _on_ep_synced(self, batch: list) -> None:
        """收到**一批**单部结果就落库（不必等全部拉完）。

        这里是与旧版最大的行为差别：旧版整表覆盖写在最后一次性完成，
        首次全量的十几秒里动态页一直是旧的。现在每批 10 部就写一次。

        三条分支（护栏的实现在这里，不在 worker）：
        - `ok=False`             → 不写、不记水位，下次刷新自动重试
        - `raw_count>0, rows=0`  → 服务端给了记录但过滤后一条不剩（历史故障：
                                   `updated_at` 恒为 0 → 整批被弃 → 页面空白）。
                                   **不写、不记水位**，下次重试。
        - 其余（含"成功且确实 0 条"）→ 写库 + 记水位，那是终态。
        """
        written = 0
        for r in batch or []:
            try:
                if not r.get("ok"):
                    continue
                if r.get("raw_count") and not r.get("rows"):
                    log.warning(
                        "条目 %s 返回 %s 条原始记录但没有可用的逐集记录，"
                        "本次不写库、不记水位（下次重试）",
                        r.get("bangumi_id"), r.get("raw_count"))
                    continue
                self._db.replace_subject_watched_episodes(
                    int(r["bangumi_id"]),
                    r.get("rows") or [],
                    r.get("collection_updated_at") or "",
                    int(r.get("ep_status") or 0),
                )
                written += 1
            except Exception:
                # 一部写库失败不该中断其余条目；也不该杀掉槽函数
                log.exception("写入条目 %s 的集级记录失败", r.get("bangumi_id"))
        if written:
            self._schedule_ui_refresh()

    def _schedule_ui_refresh(self) -> None:
        """把"写库完成 → 通知界面"合并到最多 1 次/秒。

        首次全量会写十几批，每批都 `reloadWatchedEpisodes()` 会让 QML 反复
        重建列表；节流后用户看到的仍是"持续长出新记录"，只是不抖。
        """
        if self._ui_refresh_pending:
            return
        self._ui_refresh_pending = True
        QTimer.singleShot(1000, self._flush_ui_refresh)

    def _flush_ui_refresh(self) -> None:
        self._ui_refresh_pending = False
        self._refresh_ep_view()

    def _refresh_ep_view(self) -> None:
        if self._ep_done_hook is not None:
            try:
                self._ep_done_hook()
            except Exception:
                log.exception("刷新集级观看记录失败")

    def _on_ep_finished(self, summary: dict, error: str) -> None:
        """同步收尾。数据已在 `_on_ep_synced` 里逐批落库，这里只做汇总与提示。"""
        self._set_running(False)

        summary = summary or {}
        synced = int(summary.get("synced") or 0)
        rows = int(summary.get("rows") or 0)
        failed = int(summary.get("failed") or 0)
        skipped = int(summary.get("skipped") or 0)

        if summary and not synced and failed:
            # 全军覆没才算失败（此时一条都没写进去）
            log.warning("集级记录同步失败: %s", error)
            self.failed.emit(error)
            return

        # 结束时强制刷一次，保证节流窗口内最后那批也上屏
        self._ui_refresh_pending = False
        self._refresh_ep_view()

        log.info("集级记录同步收尾：成功 %s 部 / %s 条，跳过 %s 部，失败 %s 部",
                 synced, rows, skipped, failed)
        parts = [f"成功 {synced} 部 / {rows} 条"]
        if skipped:
            parts.append(f"跳过 {skipped} 部")
        if failed:
            parts.append(f"失败 {failed} 部")
        self.message.emit("集级观看记录已同步（" + "，".join(parts) + "）")

    def _refresh_library(self) -> None:
        if self._done_hook is not None:
            try:
                self._done_hook()
            except Exception:
                log.exception("刷新收藏数据缓存失败")

    @Slot(str, result="QVariantMap")
    def resolveUsername(self, token: str = "") -> dict:
        """用指定 Token 解析当前账号的 username（供设置页「检测」按钮调用）。

        参数 `token`：要用于探测的 Token。传空串则用桥接层持有的 `api`
        （即已保存的配置）。传值时会**临时构造一个客户端**，不污染主 `api`，
        也不写盘 —— 用户可以放心地点「检测」而不会误改保存的配置。

        返回：
            { "ok": bool, "username": str, "nickname": str,
              "id": int, "message": str }

        为什么要这个接口：`/v0/users/{username}` 要的是 username 而不是
        昵称，但用户在设置页看到的、能填的往往是昵称。给一个"一键检测"
        的入口，可以把正确值直接回填，避免 404 之后一头雾水。
        """
        api = self._api
        token = (token or "").strip()
        if token:
            # 用输入框里的 Token 临时探测（不落盘、不动主 api）
            api = BangumiClient(
                token=token,
                api_base=getattr(self._api, "api_base", "https://api.bgm.tv"),
                proxy="",       # 代理沿用已保存配置即可，这里不重复读配置
                timeout=15.0,
            )
        try:
            me = api.get_me()
        except Exception as e:
            log.warning("检测用户名失败: %s", e)
            return {"ok": False, "username": "", "nickname": "", "id": 0,
                    "message": f"请求失败：{e}"}
        if not me:
            return {"ok": False, "username": "", "nickname": "", "id": 0,
                    "message": "Token 无效或已过期，无法获取用户信息"}
        username = (me.get("username") or "").strip()
        nickname = me.get("nickname") or ""
        if not username:
            return {"ok": False, "username": "", "nickname": nickname, "id": 0,
                    "message": "Token 对应账号没有 username"}
        return {
            "ok": True,
            "username": username,
            "nickname": nickname,
            "id": int(me.get("id") or 0),
            # 明确对比 username 与昵称，消除"该填哪个"的困惑
            "message": f"解析成功：用户 ID = {username}"
                       + (f"（{nickname} 是昵称，不能填在这里）"
                          if nickname and nickname != username else ""),
        }

    # ---------- 配置变更 ----------
    @Slot()
    def applyEpisodeCount(self) -> None:
        """配置保存后调用：`ep_timeline_count` 变了，通知界面重算显示。

        **这里既不拉取、也不裁剪数据表** —— 与旧版最大的差别：

        - **不拉取**：该配置现在只管"动态页显示多少条"，与"要同步哪几部"
          彻底解耦（同步范围由 `sync_candidates()` 决定）。旧版在这里重新
          拉取，于是**保存任意设置**（哪怕改的是主题色）都会触发一轮
          上百个请求的全量同步。
        - **不裁剪**：表里要保留**完整历史**。调小显示条数只是少显示几条，
          而不是把数据删掉 —— 删了再调大又得重新联网拉一遍。
          （被截掉的条数会由 QML 的「加载更多」提示体现。）

        QML 侧 `epLimit()` 每次都现读这个值并重新 slice，所以这里只要
        通知界面重算即可。
        """
        try:
            n = self._config.getint("bangumi", "ep_timeline_count", 30)
        except Exception:
            n = 30
        log.info("动态页显示条数改为 %s 条（同步范围不受影响，数据保留）", n)
        self._refresh_ep_view()
        if n <= 0:
            self.message.emit("集级时间线已关闭（下次刷新起不再同步）")

    @Slot(result=bool)
    def epSyncNeeded(self) -> bool:
        """是否从未同步过集级记录（供动态页决定要不要提示"首次同步"）。

        "有收藏缓存但一条水位都没有"= 首次运行这套增量逻辑。
        """
        try:
            if self._db.count_ep_sync_state() > 0:
                return False
            return bool(self._db.load_inprogress_cache())
        except Exception as e:
            log.exception("判断是否需要首次同步失败: %s", e)
            return False

    @Slot()
    def resyncAll(self) -> None:
        """清空同步水位并重跑一次（"重建完整观看记录"的手动入口）。

        什么时候需要：怀疑某部记录缺了、或从旧版本升级上来想强制重建。
        清水位会让所有条目被判成"首次"，因此下一次刷新会全量重拉一遍
        （与首次安装同量级，约十几秒）。
        """
        if self._running:
            log.info("正在拉取，忽略重建请求")
            self.message.emit("正在同步中，请稍后再试")
            return
        try:
            self._db.clear_ep_sync_state()
            log.info("已清空集级记录同步水位，下次刷新将全量重拉")
        except Exception as e:
            log.exception("清空同步水位失败: %s", e)
            self.message.emit(f"重建失败：{e}")
            return
        self.message.emit("已重置同步记录，开始全量重新同步…")
        self.refresh()

    # ---------- 清理 ----------
    @Slot()
    def cancel(self) -> None:
        """退出时等待未完成的拉取（不强杀线程，只等它自然结束）。

        **为什么每个都要 try 住 RuntimeError**：两个 worker 都连了
        `finished → deleteLater`，线程结束后 C++ 对象已被销毁，此时再访问
        `isRunning()` 会抛 `RuntimeError: Internal C++ object ... already deleted`。
        线程既然已经结束，本来也无需等待 —— 忽略即可。
        早期版本没接住这个异常，退出时必打一条"等待拉取结束失败"的
        误导性警告（看着像线程没退干净，其实早就结束了）。
        """
        for w, label in ((self._worker, "收藏列表"), (self._ep_worker, "集级记录")):
            if w is None:
                continue
            try:
                if w.isRunning():
                    log.info("等待%s拉取线程结束…", label)
                    w.wait(3000)
            except RuntimeError:
                # C++ 对象已随 deleteLater 销毁 = 线程早已结束
                pass
