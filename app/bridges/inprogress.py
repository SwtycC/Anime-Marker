"""Bangumi 收藏列表桥接（阶段 7 · F18 的 QML 版本）。

职责：从 Bangumi 拉取「当前用户 · **看过 + 在看**」收藏，写入
`inprogress_cache`，然后通知 `LibraryBridge` 刷新（QML 侧读 `library.inProgress`）。

> 两类都拉，但两个消费方口径不同：
> **收藏页只读「看过」**（`library.inProgress` → `collect_type=2`），
> **动态页的候选是两类之和**（逐集记录主要产生于追番期间）。

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
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from app.core.bangumi_api import (
    COLLECT_TYPE_DOING, COLLECT_TYPE_DONE, BangumiAuthError, BangumiClient,
    BangumiError,
)
from app.core.config import Config
from app.core.database import Database

log = logging.getLogger(__name__)

# **每类**的拉取上限（看过 / 在看 各算各的）。实测「看过」149 条、
# 「在看」11 条，600 对个人用户足够，同时避免异常账号把内存打爆。
MAX_ITEMS = 600


def _collection_time(item: dict) -> str:
    """取条目的「Bangumi 收藏修改时间」（ISO 串），缺失时回落到缓存写入时间。

    两个键的来历见 `Database.replace_inprogress_cache`：
    `collection_updated_at` 逐条不同（真正决定"最近看的是哪几部"），
    `updated_at` 是本次拉取时刻（整批相同，只能当兜底）。

    只用于**抓取端的排序**（`_start_episode_fetch`：从最新看过的部开始拉，
    才可能用最少请求凑够 N 条）。裁剪端（`_trim_only` /
    `trim_watched_episodes`）按 `watched_at` 条数口径裁 —— 两者不必同键，
    因为抓取是**整表覆盖写**，裁剪只影响"拉取完成前临时显示什么"。
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
            #     username: '933287'   ← API 要的是这个（可能也是数字）
            #     nickname: 'swtyc'    ← 界面上显示的名字，填它必 404
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
                    log.warning(
                        "Token 无法解析 username，改用配置值：%r"
                        "（注意：这里要填 username 而非昵称，否则会 404）",
                        username)

            if not username:
                self.finished_items.emit(
                    [], "无法确定 Bangumi 用户名，请检查 Token 是否有效")
                return

            # **拉「看过」+「在看」两类**（都是动画）。
            #
            # 为什么连「在看」一起拉：动态页要显示"第几集什么时候看的"，
            # 而**逐集标记恰恰多发生在追番期间** —— 只收看完的番会把最新的
            # 记录整个过滤掉（实测：用户最近四条 ep 记录全属于在看的新番，
            # 动态页因此停在几天前，被当成"数据没拉到"反馈过两次）。
            #
            # 注意两个"看过"不是一回事：这里筛的是**整部番**的收藏状态，
            # 单集状态（`/collections/{sid}/episodes` 里每集的 type=2）
            # 由第二阶段另行筛选。
            #
            # 两类都写进 `inprogress_cache`（表里本就有 collect_type 列），
            # 但**收藏页只读 type=2**（见 LibraryBridge._load_inprogress），
            # 那个页面的「看过」语义不受影响。
            raw = self.api.iter_user_collections(
                username,
                collect_type=COLLECT_TYPE_DONE,
                max_items=self.max_items,
            )
            items = [self._to_cache_item(r, COLLECT_TYPE_DONE) for r in raw]
            log.info("已拉取「看过」收藏 %s 条", len(items))

            # 「在看」失败不算整体失败：拿到看过列表也能正常显示收藏页，
            # 只是动态页少了一批候选。所以这里单独吞掉异常（AuthError 也吞，
            # 因为看过那次调用已经能暴露 Token 问题）。
            try:
                raw_doing = self.api.iter_user_collections(
                    username,
                    collect_type=COLLECT_TYPE_DOING,
                    max_items=self.max_items,
                )
                doing = [self._to_cache_item(r, COLLECT_TYPE_DOING)
                         for r in raw_doing]
                log.info("已拉取「在看」收藏 %s 条", len(doing))
                items.extend(doing)
            except Exception as e:
                log.warning("拉取「在看」收藏失败（不影响看过列表）：%s", e)

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
    def _to_cache_item(raw: dict, collect_type: int = COLLECT_TYPE_DONE) -> dict:
        """把 Bangumi collection 条目转成 `replace_inprogress_cache` 的入参。

        `collect_type` 是**调用方请求的那一类**（2=看过 / 3=在看），用作
        `type` 字段缺失时的兜底：以"我们请求了什么"为准，而不是赌响应里
        带没带 `type` —— 万一服务端漏字段，默认值会把「在看」误标成
        「看过」，直接污染收藏页（那页只读 type=2）。

        Bangumi 的返回结构（/v0/users/{u}/collections）：
            { "subject_id": 123, "subject": { "name": ..., "name_cn": ...,
              "images": {...}, "eps": 12 }, "type": 2, "ep_status": 11,
              "updated_at": "2026-09-11T09:35:04+08:00" }
        注意**没有** total_eps 顶层字段，集数在 `subject.eps`。

        `updated_at` 是**该收藏的最后修改时间**（ISO 串，逐条不同），
        必须原样带到 `collection_updated_at` 列 —— 它决定「最近 N 部」
        选哪几部。早期版本把它丢掉了，排序只能退回"本次拉取时刻"
        （整批相同），「最近 N 部」实际变成「按名字排序的前 N 部」。
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

        return {
            "bangumi_id": raw.get("subject_id") or subject.get("id") or 0,
            "name": subject.get("name") or "",
            "name_cn": subject.get("name_cn") or "",
            "cover_url": cover,
            "ep_status": ep_status,
            "total_eps": eps,
            # 响应里的 type 优先，缺失时用"调用方请求的那一类"（见 docstring）
            "collect_type": int(raw.get("type") or collect_type),
            # 收藏的最后修改时间（ISO 串）——「最近 N 部」的排序依据
            "collection_updated_at": raw.get("updated_at") or "",
        }


class _EpisodeWorker(QThread):
    """并发拉取集级观看记录，**凑够目标条数即停**（F20）。

    为什么需要并发：集级接口是**每部动漫一次请求**
    （`/v0/users/-/collections/{sid}/episodes`），串行拉 30 部要好几秒。
    用线程池 8 并发，30 部约 1~3 秒。

    **为什么是"凑够即停"而不是"固定拉 N 部"**：设置项只管"动态页显示多少条"，
    拉几部是**手段**不是目的。一部能产出几条完全取决于用户标记了几集
    （实测平均 8.6 条/部，但剧场版可能是 0 条），所以事先算不准；
    按"拉到够为止"办，M=40 时通常 5 部就够 —— 比固定拉 40 部省约 87% 请求。
    代价是请求数随数据分布浮动，因此 `progress` 报的是**条数**进度
    （(已凑够条数, 目标条数)）而不是部数进度。

    分批提交（而非一次性把所有部丢进线程池）是早停的前提：
    只提交当前这批，拉完才知道够不够。

    为什么不复用主 `_FetchWorker`：它负责"拉收藏列表"，这是"再拉每部的
    集级明细"，是两阶段任务。拆开还有一个好处 —— 第一阶段失败（列表拿不到）
    时不必进入第二阶段，省掉必然失败的请求。
    """

    finished_eps = Signal(object, str)    # (list[dict], error)
    progress = Signal(int, int)           # (已凑够条数, 目标条数)

    # 每批提交的部数由目标条数反推（见 _batch_size）。
    # 实测每部约 8.6 条标记记录，但**分布很散**（剧场版 1 条、长篇 25 条），
    # 所以除数取 6 而不是 8.6 —— 留约 40% 余量，让"一轮凑够"成为常态。
    # 上下限的含义：
    #   下限 5  —— 批次太小，目标大时要多轮往返（每轮都要等最慢那个请求）
    #   上限 20 —— 批次太大，早停粒度粗，末批多拉的记录会被截掉（白拉）
    # 踩坑：① 最初写死 10，实测"目标 40 条"第一批就拉回 10 部（≈86 条），
    # 多打的请求全被截掉 —— 明明 5 部就够；② 改成"目标 ÷ 8"后，
    # 目标 40 时首批 5 部只回 38 条（差 2 条），又得多开一轮，
    # 结果仍是 10 次请求 —— 按平均值卡得太死，反而更亏。
    @staticmethod
    def _batch_size(target: int) -> int:
        return max(5, min(20, (target + 5) // 6))

    def _next_batch(self, fetched: int, got: int, remaining: int) -> int:
        """决定下一批拉几部 —— 首轮用静态估计，之后按**实测产出密度**自校准。

        为什么必须自校准：每部产出多少条差异极大（实测同一批里 17 部只回
        78 条 = 4.6 条/部，而全库平均是 8.6）。静态估计在这种"低产"区
        会一轮一轮地估多，实测目标 100 条时白拉 34 部（本该 22 部）。

        `fetched` / `got` 都传**累计值**（密度用整体口径，比单批更稳）；
        密度为 0 时（这批全是没逐集标记的条目）按 0.5 兜底，避免除零，
        并让批次停在上限附近继续往后找有记录的条目。
        """
        if fetched <= 0:
            return self._batch_size(self.target)
        density = got / fetched
        return max(3, min(20, int(remaining / max(density, 0.5)) + 1))
    # 兜底上限：若最近若干部都是 0 条（剧场版、只标整体状态的番），
    # "凑够即停"可能一路拉到底，这里限死，避免为一个页面打几百次请求。
    MAX_SUBJECTS = 200

    def __init__(
        self,
        api: BangumiClient,
        subjects: list[dict],             # [{bangumi_id, title, local_id}]
        subject_updated_at: Optional[dict[int, str]] = None,
        target: int = 30,                 # 目标条数（动态页的显示上限）
        workers: int = 8,
    ) -> None:
        super().__init__()
        self.api = api
        self.subjects = subjects[:self.MAX_SUBJECTS]
        self.workers = max(1, min(workers, 16))
        self.target = max(1, int(target))
        # 动漫级收藏的 updated_at（ISO 串）—— 单集时间戳缺失时的兜底，
        # 见 _fetch_with_session 的说明。
        self.subject_updated_at = subject_updated_at or {}
        # 实际发了几部的请求（早停后才知道，供上层在状态栏里说明）
        self.fetched_subjects = 0

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        items: list[dict] = []
        planned = len(self.subjects)      # 本次最多会碰几部
        errors = 0
        no_time = 0

        # ---- 每个线程一个独立 Session ----
        #
        # **踩坑（性能）**：`requests.Session` **不是线程安全的**。
        # 多线程共享同一个 Session 时，内部连接池会成为瓶颈，
        # 实测 30 个请求用 8 并发要 **44 秒**（而独立 Session 只要 2 秒）。
        # 原因是共享 Session 的 `HTTPAdapter` 在多线程下会争用同一批连接，
        # 叠加本项目配置的 3 次重试与指数退避，耗时被放大 20 倍以上。
        #
        # 这里为每个线程建一个"精简版" Session：只复制认证与代理配置，
        # **不带重试适配器**（请求失败由外层统一计入 errors，不必逐条重试）。
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

        def fetch_one(s: dict) -> list[dict]:
            bid = s["bangumi_id"]
            try:
                rows = self._fetch_with_session(session_for_thread(), bid)
            except Exception as e:
                # 单个条目失败不影响整体（网络抖动很常见）
                log.warning("拉取 %s 集级记录失败: %s", bid, e)
                raise
            # 兜底时间：优先用单集自己的时间戳，缺失时退回该动漫收藏级
            # 的 updated_at（同一部动漫的每集时间都相同，视觉上仍按动漫聚拢）
            collection_time = self.subject_updated_at.get(int(bid), "")
            out = []
            for r in rows:
                # 只保留"看过"（type=2）。type 枚举与收藏一致。
                if (r.get("type") or 0) != 2:
                    continue
                ep = r.get("episode") or {}
                watched_at = _iso_from_epoch(r.get("updated_at")) or collection_time
                if not watched_at:
                    nonlocal no_time
                    no_time += 1
                    continue
                out.append({
                    "bangumi_ep_id": int(ep.get("id") or 0),
                    "bangumi_id": int(bid),
                    "subject_id": int(s.get("local_id") or 0),
                    "subject_name": s.get("title") or "",
                    "ep_index": float(ep.get("sort") or ep.get("ep") or 0),
                    "ep_name": ep.get("name_cn") or ep.get("name") or "",
                    "watched_at": watched_at,
                })
            return out

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                idx = 0
                # 逐批提交，凑够目标条数就停（见类文档）。
                # 批次大小每轮重算：首轮静态估计，之后按实测密度自校准。
                while idx < planned and len(items) < self.target:
                    size = self._next_batch(
                        idx, len(items), self.target - len(items))
                    batch = self.subjects[idx:idx + size]
                    idx += len(batch)
                    futures = [pool.submit(fetch_one, s) for s in batch]
                    for fut in as_completed(futures):
                        try:
                            items.extend(fut.result())
                        except Exception:
                            errors += 1
                    self.fetched_subjects = idx
                    self.progress.emit(min(len(items), self.target), self.target)

            # 截到目标条数：最后一批是**整批**拉完的，凑够后批内多出来的
            # 记录要丢掉 —— 保证"页面显示的就是设置的条数"，也免得
            # 动态页再为这几条弹「另有 X 条未显示」。
            items.sort(key=lambda it: it.get("watched_at") or "", reverse=True)
            dropped = max(0, len(items) - self.target)
            items = items[:self.target]

            msg = ""
            if errors and errors == self.fetched_subjects:
                msg = "全部条目的集级记录都拉取失败（可能是网络或 Token 权限）"
            elif errors:
                msg = f"{errors}/{self.fetched_subjects} 个条目拉取失败（其余成功）"
            if dropped:
                log.info("已凑够 %s 条，末批多拉的 %s 条丢弃", self.target, dropped)
            if self.fetched_subjects >= self.MAX_SUBJECTS and len(items) < self.target:
                # 正常不该发生（除非大量条目只标了整体状态、没有逐集标记）
                log.warning("拉满 %s 部仍只凑到 %s/%s 条，已停止",
                            self.MAX_SUBJECTS, len(items), self.target)
            if no_time:
                # 拿不到时间的记录会被丢弃（时间线必须有时间才排得动）。
                # 正常不该出现（有收藏级 updated_at 兜底），出现即说明
                # 收藏缓存里的 updated_at 也是空的，值得记一笔。
                log.warning("有 %s 条集级记录缺少时间，已跳过", no_time)
            log.info("集级记录拉取完成：%s 条（实际请求 %s 部）",
                     len(items), self.fetched_subjects)
            self.finished_eps.emit(items, msg)
        except Exception:
            log.exception("集级观看记录拉取异常")
            self.finished_eps.emit([], "拉取集级记录失败（详见日志）")

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

    def _start_episode_fetch(self, collections: list[dict]) -> None:
        """拉取集级观看记录，**凑够「集级记录条数」即停**（F20）。

        配置 `bangumi.ep_timeline_count` 现在只管"动态页显示多少条"（M）。
        **拉几部由 `_EpisodeWorker` 按需决定**：按收藏修改时间从新到旧逐批拉，
        累计到 M 条就停。理由见 `_EpisodeWorker` 的类文档 —— 一部产出几条
        取决于用户标记了几集，事先算不准，平均 8.6 条/部（M=40 约 5 部即够）。

        M <= 0 时**跳过**这一步（用户想省流量时可用）。

        排序依据：收藏接口给的 `updated_at`（已存进
        `inprogress_cache.collection_updated_at`）就是该收藏的最后修改时间，
        按它倒序即可，无需额外请求。
        """
        try:
            target = self._config.getint("bangumi", "ep_timeline_count", 30)
        except Exception:
            target = 30
        if target <= 0:
            log.info("集级时间线已关闭（ep_timeline_count=0），跳过")
            self.message.emit("集级时间线已关闭")
            return

        # collections 已按收藏修改时间倒序（load_inprogress_cache 的顺序），
        # 这里再显式排一次，避免上游顺序变化导致静默失效。
        # **不预截断部数** —— 拉几部由 worker 按"凑够 M 条"决定，
        # 预截断会让"设置 40 条"在平均 8.6 条/部时只够 344 条里的一小截。
        ordered = sorted(collections, key=_collection_time, reverse=True)

        subjects = [
            {
                "bangumi_id": int(c.get("bangumi_id") or 0),
                "title": c.get("name_cn") or c.get("name") or "",
                "local_id": self._db.find_subject_by_bangumi_id(
                    int(c.get("bangumi_id") or 0)) or 0,
            }
            for c in ordered
            if c.get("bangumi_id")
        ]
        if not subjects:
            log.info("没有可用于拉取集级记录的条目")
            return

        log.info("开始拉取集级观看记录：目标 %s 条（候选 %s 部，按需拉取）",
                 target, len(subjects))
        self.message.emit(f"正在拉取集级观看记录（目标 {target} 条）…")
        self._set_running(True)

        # 单集时间戳的兜底：集级接口的 `updated_at` 历史上出现过恒为 0
        # （字段存在但无值，见 _fetch_with_session 的说明），此时用该动漫
        # **收藏的最后修改时间**兜底，否则整批记录都会因为"没有时间"被丢弃
        # —— 早于本修复的版本就是因此出现「改了 N 之后一条都不显示」。
        #
        # 用收藏时间而不是"本次拉取时刻"：同一部的每集都会拿到同一个时间
        # （视觉上仍按动漫聚拢），且这个时间是用户真的看完那部的日子。
        # 早期用拉取时刻，导致动态页所有兜底记录都显示成"刚刚"。
        fallback = {
            int(c.get("bangumi_id") or 0): _collection_time(c)
            for c in ordered
            if c.get("bangumi_id")
        }
        self._ep_worker = _EpisodeWorker(
            self._api, subjects, fallback, target=target)
        self._ep_worker.progress.connect(self._on_ep_progress)
        self._ep_worker.finished_eps.connect(self._on_ep_finished)
        self._ep_worker.finished.connect(self._ep_worker.deleteLater)
        self._ep_worker.start()

    def _on_ep_progress(self, done: int, total: int) -> None:
        """进度是**条数**（不是部数）：拉几部由数据决定，条数才是用户关心的。"""
        # 只在整数百分比变化时报一次，避免刷屏
        if total and (done == total or done % max(1, total // 10) == 0):
            self.message.emit(f"拉取集级记录… {done}/{total} 条")

    def _on_ep_finished(self, items: list, error: str) -> None:
        self._set_running(False)

        if error and not items:
            log.warning("集级记录拉取失败: %s", error)
            self.failed.emit(error)
            return

        try:
            self._db.replace_watched_episodes(items)
        except Exception as e:
            log.exception("写入集级记录失败: %s", e)
            self.failed.emit(f"写入集级记录失败：{e}")
            return

        # 实际请求了几部 —— 抓取范围现在是自动的，报出来用户才知道
        # "设了 40 条为什么只请求了 5 次"（不是故障，是凑够就停）
        try:
            fetched = int(self._ep_worker.fetched_subjects) if self._ep_worker else 0
        except RuntimeError:
            fetched = 0        # C++ 对象已被 deleteLater 销毁（线程早已结束）

        log.info("集级观看记录已更新：%s 条（来自 %s 部）", len(items), fetched)
        if self._ep_done_hook is not None:
            try:
                self._ep_done_hook()
            except Exception:
                log.exception("刷新集级观看记录失败")

        # 文案用"请求 N 部"而不是"N 部"：截取后真正贡献这 N 条的部数
        # 通常更少（末批多拉的会被丢掉），说"请求"才不引起歧义
        if error:
            # 部分失败：数据已写入，只提示
            self.message.emit(
                f"集级记录部分完成（{len(items)} 条，请求 {fetched} 部）：{error}")
        else:
            self.message.emit(
                f"集级观看记录已更新（{len(items)} 条，请求 {fetched} 部）")

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
        """配置保存后调用：按新的「集级记录条数」（M 条）重建集级记录。

        **为什么需要它**：`watched_episodes` 表是**整体替换**的。
        用户改了 M 之后，表里的旧数据既可能**过多**（改小后仍显示原来的条数），
        也可能**过少/为空**（改大后不够显示）。

        只裁剪是不够的 —— 早期实现按"最近 N 部"裁，遇到刚更新收藏但还没
        逐集标记的条目，裁剪后表会变成空的，用户看到空白会以为功能坏了。
        因此这里**直接重新拉取**（若已有收藏缓存），一次到位。

        M=0 时清空并跳过拉取（等于关闭该功能）。
        没有收藏缓存时不拉（避免无谓请求），只清空并提示先刷新。
        """
        try:
            n = self._config.getint("bangumi", "ep_timeline_count", 30)
        except Exception:
            n = 30

        # N=0：关闭功能，清空即可
        if n <= 0:
            try:
                self._db.clear_watched_episodes()
                log.info("集级时间线已关闭，清空已有记录")
            except Exception as e:
                log.exception("清空集级记录失败: %s", e)
                return
            self._after_change()
            self.message.emit("集级时间线已关闭")
            return

        # 没有收藏缓存 → 先裁剪（清掉越界数据）并提示用户刷新
        try:
            rows = self._db.load_inprogress_cache()
        except Exception as e:
            log.exception("读取收藏缓存失败: %s", e)
            return

        if not rows:
            try:
                self._db.clear_watched_episodes()
            except Exception:
                pass
            self._after_change()
            self.message.emit("尚无收藏缓存，请先点「刷新」拉取")
            return

        # 有缓存 → 直接按新的 M 重新拉取（覆盖式写入，天然处理多与少）
        if self._running:
            log.info("拉取进行中，配置变更将在本次完成后生效")
            # 先裁剪，避免本次拉取完成前界面仍显示越界数据
            self._trim_only(n)
            return

        log.info("集级记录条数改为 %s 条，开始重新拉取", n)
        self._trim_only(n)                 # 先清掉越界的，界面立刻正确
        self._start_episode_fetch(
            [self._cache_row_to_dict(r) for r in rows])

    @staticmethod
    def _cache_row_to_dict(row) -> dict:
        """InProgressItem → `_start_episode_fetch` 需要的 dict 形态。"""
        return {
            "bangumi_id": int(row.bangumi_id or 0),
            "name": row.name or "",
            "name_cn": row.name_cn or "",
            "updated_at": row.updated_at or "",
            "collection_updated_at": getattr(row, "collection_updated_at", None) or "",
        }

    def _trim_only(self, n: int) -> None:
        """把表裁到最近 n 条（不联网），让界面立刻不再显示越界数据。

        **按"条数"裁而不是按"部数"裁**：抓取范围已改成"凑够 N 条即停"，
        部数与 N 不再有一一对应关系，只有条数口径是稳定的。
        早先按部数裁时，裁剪保留的名单与重新拉取的名单一旦对不上，
        就等于在拉取前先删掉本次要用的数据（实测表现：改完 N 后一条都不显示）。
        """
        try:
            removed = self._db.trim_watched_episodes(n)
        except Exception as e:
            log.exception("裁剪集级记录失败: %s", e)
            return
        if removed:
            log.info("已裁剪 %s 条越界的集级记录", removed)
        self._after_change()

    def _after_change(self) -> None:
        """通知界面刷新（配置变更后立即生效）。"""
        if self._ep_done_hook is not None:
            try:
                self._ep_done_hook()
            except Exception:
                log.exception("刷新集级观看记录失败")

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
