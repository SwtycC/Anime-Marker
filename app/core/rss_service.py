"""RSS 订阅轮询编排（F19）。

- QThread 后台执行，通过信号向 UI 汇报（同 §5.1 扫描模式）
- 抓取 → 解析 → 判新 → 下发 qBittorrent → 落库
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.qbittorrent_api import QbClient, QbError
from app.core.rss_feed import RssError, fetch_and_parse
from app.core.rss_matcher import RssMatcher

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PUSHED = "pushed"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class PollSummary:
    """单次轮询汇总（UI 展示用）。"""

    total_entries: int = 0
    new_entries: int = 0
    pushed: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    #: 被"必须包含/不包含"规则筛掉的数量（v12）。
    #: 与 `skipped` 分开计数：那个是"判新判定不用下"，这个是"用户明确不想要" ——
    #: 混在一起会让用户以为规则没生效（看到"跳过 200"却不知道是自己的规则拦的）。
    filtered: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class PollWorker(QThread):
    """后台轮询所有启用的订阅源。"""

    progress = Signal(str)                    # 文本进度
    source_done = Signal(int, str)            # source_id, 摘要文本
    finished_summary = Signal(object)         # PollSummary

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        qb: Optional[QbClient],
        only_source_id: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.db = db
        self.config = config
        self.api = api
        self.qb = qb
        self.only_source_id = only_source_id
        #: 本轮是否已确认过 qBittorrent 就绪（含"尝试启动过"）。
        #: 一轮里只为**第一次**要下发的条目做探测/启动，避免每条都问一遍。
        self._qb_ready = False

    def run(self) -> None:
        summary = PollSummary()
        sources = self.db.list_rss_sources(only_enabled=True)
        if self.only_source_id is not None:
            sources = [s for s in sources if s.id == self.only_source_id]

        matcher = RssMatcher(self.db, self.qb, self.config)
        proxy = self.config.get("bangumi", "proxy", "")
        ua = self.config.get("bangumi", "user_agent", "AnimeMarker/1.0")
        auto = self.config.getbool("rss", "auto_download", False)

        if not sources:
            log.info("RSS 轮询：没有启用的订阅源，跳过")
        for src in sources:
            self.progress.emit(f"轮询：{src.name or src.url}")
            # 每条订阅单独打一行日志（含订阅名与地址）—— 多订阅时
            # 光看"轮询异常"不知道是哪一个
            log.info("RSS 轮询开始：#%s %s（%s）rule=%s qb=%s",
                     src.id, src.name or "(无名)", src.url, src.rule,
                     "已配置" if self.qb is not None else "未配置")
            try:
                self._poll_one(src, matcher, proxy, ua, auto, summary)
                self.db.update_rss_source(src.id, last_poll_at=_now_iso(), last_error="")
                self.source_done.emit(src.id, "完成")
            except (RssError, QbError) as e:
                msg = str(e)
                summary.errors.append(f"{src.name or src.url}：{msg}")
                self.db.update_rss_source(src.id, last_poll_at=_now_iso(), last_error=msg)
                self.source_done.emit(src.id, f"失败：{msg}")
                log.warning("订阅源轮询失败 %s: %s", src.url, e)
            except Exception as e:
                summary.errors.append(f"{src.name or src.url}：{e}")
                self.db.update_rss_source(src.id, last_error=str(e))
                self.source_done.emit(src.id, f"异常：{e}")
                log.exception("订阅源轮询异常 %s", src.url)

        self.finished_summary.emit(summary)

    # ---------- 单源 ----------
    def _poll_one(
        self,
        src: RssSource,
        matcher: RssMatcher,
        proxy: str,
        ua: str,
        auto: bool,
        summary: PollSummary,
    ) -> None:
        entries = fetch_and_parse(src.url, proxy=proxy, user_agent=ua)
        summary.total_entries += len(entries)

        # 新条目在前（RSS 通常倒序），逐条判定
        for entry in entries:
            # ---- 标题过滤（v12）：**放在判新之前** ----
            # 被"必须包含/不包含"筛掉的内容，连判新都不必做 —— 先过滤
            # 能省下两层查重的开销，也让"过滤掉多少条"这个统计是干净的
            # （不会混进"跳过"里）。
            filtered = matcher.title_filtered(entry.title or "", src)
            if filtered:
                summary.filtered += 1
                # 只记前几条明细：一个订阅被过滤 200 条时刷 200 行日志没意义
                if summary.filtered <= 5:
                    log.info("按规则过滤：《%s》—— %s",
                             (entry.title or "")[:60], filtered)
                continue

            result = matcher.judge(entry, src)

            if not result.is_new:
                summary.skipped += 1
                continue
            summary.new_entries += 1

            tags = [src.name or "AnimeMarker"]
            if result.subject_id:
                tags.append(f"bgm:{result.subject_id}")

            # **取下载链接必须走 `entry`**（踩坑，实测报错）：
            # `download_url` 是 `FeedEntry` 上的属性，`JudgeResult` 没有 ——
            # 原先三处都写成 `result.download_url`，于是**每次轮询都在
            # 第一条新集上抛 AttributeError**：
            #     AttributeError: 'JudgeResult' object has no attribute
            #     'download_url'
            # 整轮轮询直接中断（异常被 run() 兜住、记成"轮询异常"），
            # 表现为"点了立即检查、没有任何下载记录、日志一条红字"。
            # `JudgeResult.entry` 就是判定的那个条目，链接从它取。
            download_url = entry.download_url

            # ---- 保存路径（v12：下载到指定条目的目录）----
            # 每条各自算（不同订阅可能指向不同条目）；算不出就是空串，
            # 表示不干预、用 qBittorrent 自己的全局保存路径。
            save_path = matcher.resolve_save_path(src) or None

            if not result.should_download:
                self.db.add_download_record(
                    source_id=src.id,
                    ep_index=result.ep_index,
                    torrent_title=entry.title,
                    magnet=download_url,
                    subject_id=result.subject_id,
                    status=STATUS_PENDING,
                )
                summary.pending += 1
                continue

            # 允许下载但 qBittorrent 不可用 → 降级为待确认
            if self.qb is None:
                self.db.add_download_record(
                    source_id=src.id,
                    ep_index=result.ep_index,
                    torrent_title=entry.title,
                    magnet=download_url,
                    subject_id=result.subject_id,
                    status=STATUS_PENDING,
                )
                summary.pending += 1
                summary.errors.append("未配置 qBittorrent，新集已入库为待确认")
                continue

            # ---- 下发前确保 qBittorrent 在跑（v14）----
            #
            # 实测诉求："在其退出但需要时打开"。qBittorrent 的 GUI 与
            # Web UI 是同一个进程，用户从托盘退出后 Web UI 一起没了，
            # 这里连不上就会把新集全落成「待确认」（虽然不丢，但要手动点）。
            #
            # **只在"确实要下发"的路径上调用**（上面两个 continue 已排除
            # 不下载的情形）：没必要为了"仅记录"而启动外部程序。
            # `ensure_running` 内部会先探测，已在运行就直接返回，开销很小；
            # 一轮里对每条都调也不会重复 Popen（内部有 `_start_attempted`）。
            note = ""
            if not self._qb_ready:
                try:
                    note = self.qb.ensure_running()
                except Exception as e:      # pragma: no cover - 防御性
                    log.warning("按需启动 qBittorrent 异常：%s", e)
                self._qb_ready = True       # 一轮只探测/启动一次
                if note:
                    log.info("qBittorrent 状态：%s", note)
                    summary.errors.append(note)

            record_id = self.db.add_download_record(
                source_id=src.id,
                ep_index=result.ep_index,
                torrent_title=entry.title,
                magnet=download_url,
                subject_id=result.subject_id,
                status=STATUS_PENDING,
            )
            try:
                self.qb.add_entry(
                    magnet=entry.magnet,
                    torrent_url=entry.torrent_url,
                    tags=tags,
                    # 指定了条目目录就下到那儿；否则 None（用 qB 全局设置）
                    save_path=save_path,
                )
                self.db.update_download_status(record_id, STATUS_PUSHED)
                summary.pushed += 1
            except QbError as e:
                # **失败原因要落库**（v11 的 last_error），否则界面只显示
                # 「失败 N」计数，用户无从知道是连接失败还是磁力链无效。
                reason = _explain_qb_error(e, magnet=entry.magnet,
                                           torrent_url=entry.torrent_url)
                self.db.update_download_status(record_id, STATUS_FAILED,
                                               last_error=reason)
                summary.failed += 1
                # 日志里带上**完整标题**（不是截断 40 字的版本）—— 排查时
                # 要知道具体是哪一集，截断的标题可能撞名（如多部同季）
                log.warning("下发失败 ep=%s 《%s》：%s",
                            result.ep_index, entry.title, reason)
                summary.errors.append(f"下发失败：{entry.title[:40]}（{reason}）")

    # ---------- 手动确认下发（「待确认」条目） ----------
    def push_pending(self, record_id: int) -> tuple[bool, str]:
        """把一条 pending 记录推送到 qBittorrent（由 UI 线程调用）。"""
        if self.qb is None:
            return False, "未配置 qBittorrent"
        # 手动点「下发」时同样按需拉起（用户很可能是刚开机、qB 还没启动）
        try:
            self.qb.ensure_running()
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("按需启动 qBittorrent 异常：%s", e)
        records = {r.id: r for r in self.db.list_downloads()}
        rec = records.get(record_id)
        if rec is None:
            return False, "记录不存在"
        if not rec.magnet:
            return False, "该记录没有可用的下载链接"
        try:
            tags = ["AnimeMarker"]
            if rec.subject_id:
                tags.append(f"bgm:{rec.subject_id}")
            if rec.magnet.startswith("magnet:"):
                self.qb.add(rec.magnet, tags=tags)
            else:
                self.qb.add_torrent_file(rec.magnet, tags=tags)
            self.db.update_download_status(record_id, STATUS_PUSHED)
            return True, "已推送"
        except QbError as e:
            self.db.update_download_status(record_id, STATUS_FAILED)
            return False, str(e)


class RssService(QObject):
    """定时轮询调度器：QTimer 每 poll_interval 分钟触发一次。"""

    progress = Signal(str)
    poll_finished = Signal(object)   # PollSummary

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        qb: Optional[QbClient],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.config = config
        self.api = api
        self.qb = qb
        self._worker: Optional[PollWorker] = None
        #: 所有"已 start()、但线程尚未真正结束"的 worker。
        #:
        #: **为什么除了 `_worker` 还要这个集合**（踩坑，实测退出时报
        #: `QThread: Destroyed while thread '' is still running`）：
        #: `_worker` 是"当前这一个"的引用，而它会在 `finished` 信号里
        #: 被置 None（那是为了不误判"还在跑"）。可是 `finished` 发出的
        #: 时机**早于**对象真正销毁（`deleteLater` 是排队投递的），
        #: 于是 `stop()` 时 `_worker` 已是 None —— 既 wait 不到它、
        #: 也 terminate 不了，程序退出时那个线程还在收尾，Qt 就报警告。
        #: 用一个集合保留"还没真正结束"的对象，退出时才能逐个等待。
        self._live_workers: list["PollWorker"] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.poll())

    def start(self) -> None:
        """按配置启动定时轮询（设置页「自动轮询」开关控制）。

        **开关同时管"启动时查一次"与"之后定时查"**（实测要求把这个做成
        开关）。原先它对应 `poll_on_start`，只管前者：关掉后启动时安静了，
        但 30 分钟后定时器照常触发 —— 用户看到"我明明关了自动，它自己
        下起来了"，与预期相反。所以这里把定时器的启动也纳入同一个开关。

        关闭时不启动定时器，只在你点「立即检查」时抓一次（`poll()` 不受
        本开关影响，那是手动动作）。
        """
        enabled = self.config.getbool("rss", "poll_on_start", True)
        minutes = max(1, self.config.getint("rss", "poll_interval", 30))
        if not enabled:
            self._timer.stop()
            log.info("自动轮询已关闭（仅手动「立即检查」会抓取）")
            return
        self._timer.setInterval(minutes * 60 * 1000)
        self._timer.start()
        log.info("RSS 轮询已启动，间隔 %s 分钟", minutes)
        self.poll()

    def set_qb(self, qb: Optional[QbClient]) -> None:
        """替换 qBittorrent 客户端（设置页改完地址/密码后由 QmlApp 调用）。"""
        self.qb = qb

    def apply_config(self) -> None:
        """设置保存后重新下发轮询间隔与开关（同 ProgressMonitor.apply_config 的坑）。

        **三件事都要重算**，不能只改间隔：
          ① 开关**打开** → 起定时器（若之前是关的，必须重新 start，
             否则用户"打开开关"后要重启程序才生效）；
          ② 开关**关闭** → 停定时器（否则它还在后台悄悄轮询）；
          ③ 开关开着 → 顺带更新间隔。

        `_timer.setInterval` 对**运行中**的 QTimer 同样有效，无需重启定时器。
        """
        enabled = self.config.getbool("rss", "poll_on_start", True)
        minutes = max(1, self.config.getint("rss", "poll_interval", 30))
        if not enabled:
            if self._timer.isActive():
                self._timer.stop()
            log.info("自动轮询已关闭（立即生效）")
            return
        self._timer.setInterval(minutes * 60 * 1000)
        if not self._timer.isActive():
            self._timer.start()
            log.info("自动轮询已开启（立即生效），间隔 %s 分钟", minutes)
        else:
            log.info("RSS 轮询间隔已更新为 %s 分钟", minutes)

    def stop(self) -> None:
        """停掉定时器，并**等待所有在跑的轮询线程结束**（退出时调用）。

        **为什么要遍历 `_live_workers` 而不只看 `_worker`**（踩坑，实测
        退出时报 `QThread: Destroyed while thread '' is still running`）：
        `_worker` 在 `finished` 信号里就被置 None 了，可那时对象还没被
        Qt 真正销毁、线程也可能仍在收尾 —— 只看它就会**漏掉**这个正在
        结束的线程，于是 `wait()` 没做，程序退出时 Qt 析构该线程并报警告。
        集合里保留的是"还没真正结束"的对象，逐个 wait 才彻底。

        不用 `terminate()` 常规路径：轮询只是发 HTTP 请求与写数据库，
        让它自然跑完（最多等 2 秒）比强杀安全 —— 强杀可能留下半条
        下载记录。只有 `wait` 超时（比如 xbittorrent 连不上卡在超时）
        才兜底 `terminate`，避免退出无限等待。
        """
        self._timer.stop()
        for w in list(self._live_workers):
            try:
                if w.isRunning():
                    if not w.wait(2000):
                        log.warning("轮询线程未在 2 秒内结束，强制终止")
                        w.terminate()
                        w.wait(500)
            except RuntimeError:
                pass                    # C++ 对象已被回收，无需再等
        self._live_workers.clear()
        self._worker = None

    def is_running(self) -> bool:
        """是否有轮询在跑。

        **为什么要 try/except**（关键，实测报
        `Internal C++ object (PollWorker) already deleted`）：
        `deleteLater` 销毁的是 **C++ 对象**，而 Python 侧的包装对象
        还在（`self._worker` 未及时置空时）。此时访问它的任何方法
        都会抛 `RuntimeError`，于是「立即检查」点了没反应、日志刷满
        堆栈 —— 这正是实测反馈"点击立即检查，不会进入检查"的原因。
        把"对象已销毁"一律当作"没在跑"处理（语义上也确实如此）。
        """
        w = self._worker
        if w is None:
            return False
        try:
            return bool(w.isRunning())
        except RuntimeError:
            # C++ 对象已销毁 = 这一轮早已结束
            self._worker = None
            return False

    def poll(self, only_source_id: Optional[int] = None) -> None:
        """触发一次轮询（异步）。

        **生命周期：`start()` 时进 `_live_workers`，线程真正结束时移除。**
        """
        # 踩坑记录（三轮才修对，值得留着）：

        # ① 初版 `self._worker = PollWorker(...)` 裸赋值。上一次还在跑时
        #    被覆盖 → 旧对象被 GC，而线程还在跑 → Qt 报
        #    `QThread: Destroyed while thread '' is still running`。
        # ② 于是加了"覆盖前给旧对象挂 `deleteLater`"——**这反而更糟**：
        #    对象销毁后 `self._worker` 仍指向它，下一次 `is_running()`
        #    访问已销毁的 C++ 对象，抛 `RuntimeError: Internal C++ object
        #    (PollWorker) already deleted`，表现为「立即检查」点了完全
        #    没反应、日志刷满堆栈（实测反馈）。
        # ③ 改成"在 QThread `finished` 里置 None + deleteLater"，那个
        #    RuntimeError 没了，但**退出时**又出现 `Destroyed while
        #    thread is still running` —— 因为 `finished` 早于对象真正
        #    销毁，置 None 之后 `stop()` 就**找不到它去 wait** 了
        #    （实测反馈）。所以才有 `_live_workers` 这个集合。

        # 不用同步 `wait()`：poll 从 UI 线程调用，`wait()` 会把界面卡住到
        # 整轮轮询结束（几秒到几十秒）。等待只发生在 `stop()`（退出时）。
        
        if self.is_running():
            log.info("上一次轮询尚未结束，跳过本次触发")
            return

        w = PollWorker(self.db, self.config, self.api, self.qb,
                       only_source_id)
        self._worker = w
        # 进集合：这样即使 `_worker` 后来被置 None，`stop()` 仍能等到它。
        self._live_workers.append(w)
        w.progress.connect(self.progress.emit)
        w.finished_summary.connect(self._on_finished)
        # ---- 收尾挂在 QThread 的 `finished` 上 ----
        #
        # **为什么不用 `finished_summary`**：它在 `run()` **内部**末尾
        # emit，此时 `run()` 还没返回、线程仍在运行 —— 若在那里清
        # `self._worker`，紧接着的一次 `poll()` 会看到"空闲"而新建并
        # 覆盖引用，旧线程随 GC 析构 → 回到最初的 `Destroyed` 崩溃。
        # `finished` 是 Qt 在**线程真正结束之后**才发出的，安全。
        # 它也覆盖 `run()` 抛异常的路径（那种情况不发 `finished_summary`）。
        w.finished.connect(lambda: self._clear_worker(w))
        w.start()

    def _clear_worker(self, w: "PollWorker") -> None:
        """worker 收尾：移出集合 + 置空引用 + 交还 Qt 回收。

        幂等（`finished` 只发一次，但这样写更稳）。判 `w is self._worker`
        只是为了避免误清"后来新建的那一个"（`deleteLater` 与 `finished`
        都是排队投递的，时序可能与下一次 `poll()` 交错）。

        顺序：**先断引用再 deleteLater**。反过来的话，`deleteLater`
        销毁 C++ 对象后 Python 引用还在，任何 `is_running()` 都会抛
        `RuntimeError`（正是第 ② 版的症状）。
        """
        try:
            self._live_workers.remove(w)
        except ValueError:
            pass                        # 已被 stop() 清空，属正常
        if self._worker is not w:
            return
        self._worker = None
        try:
            w.deleteLater()
        except RuntimeError:            # pragma: no cover - 已被回收
            pass

    def _on_finished(self, summary: PollSummary) -> None:
        # 注意：**这里不清理 worker** —— 详见 poll() 里的说明。
        # `finished_summary` 发出时线程还在跑，在此置空会让紧接着的
        # `poll()` 误判为"空闲"而覆盖引用。
        log.info(
            "轮询完成：条目 %s / 新 %s / 已推送 %s / 待确认 %s / 跳过 %s / "
            "过滤 %s / 失败 %s",
            summary.total_entries, summary.new_entries, summary.pushed,
            summary.pending, summary.skipped, summary.filtered,
            summary.failed,
        )
        # **失败原因汇总进日志**（实测需求："增加日志判断为什么失败"）。
        # 逐条 WARNING 已经打过（见 _poll_one），这里再给一句总览，
        # 便于直接搜 "轮询失败原因" 定位。
        if summary.errors:
            log.warning("轮询失败原因汇总：%s",
                        _summarize_errors(summary.errors, limit=5))
        self.poll_finished.emit(summary)

    def push_pending(self, record_id: int) -> tuple[bool, str]:
        """手动下发「待确认」条目（同步，用于 UI 按钮）。"""
        worker = PollWorker(self.db, self.config, self.api, self.qb)
        return worker.push_pending(record_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _explain_qb_error(exc: Exception, magnet: str = "",
                      torrent_url: str = "") -> str:
    """把 qBittorrent 的异常翻译成**能照着做**的一句话。

    按症状分类（与 `bangumi_api.describe_connection_error` 同一套思路）：
    连接类 / 认证类 / 种子链接类 / 其余原样返回。
    """
    text = str(exc)
    low = text.lower()

    # ① 没有可下发的链接：这是**数据问题**，不是连接问题。
    #    实测 comicat 这类站点的 RSS 有时只有页面链接（既没磁力也没
    #    .torrent），判定为"新集"却无从下发。
    if "没有可用的磁力链或种子链接" in text:
        return ("该条目既没有磁力链也没有种子链接（RSS 里只有网页链接），"
                "无法自动下载，请手动打开订阅页查看")
    if not magnet and not torrent_url:
        return "该条目没有可用的下载链接（RSS 未提供磁力链或 .torrent）"

    # ② 连接类：qBittorrent 没开 / 地址端口不对 / Web UI 没启用
    if ("connection" in low or "refused" in low or "timed out" in low
            or "max retries" in low or "connect" in low):
        return ("连不上 qBittorrent —— 请确认它正在运行、"
                "且「选项 → Web UI」已启用，地址端口与设置页一致")

    # ③ 认证类：用户名/密码错，或未授权（Web UI 的「对本地主机免密」关闭）
    if any(k in low for k in ("login", "unauthor", "forbidden", "403", "401",
                              "credential", "password")):
        return "qBittorrent 登录失败 —— 请核对设置页的用户名与密码"

    # ④ 种子本身的问题（qB 返回 415/Failed to add 之类）
    if "torrent file" in low or "failed to add" in low or "invalid" in low:
        return f"qBittorrent 拒绝该种子（可能链接已失效）：{text}"

    # ⑤ 其余：原样返回，至少让日志/界面有原文可查
    return text


def _summarize_errors(errors: list[str], limit: int = 3) -> str:
    """把多条错误合成一句短摘要（供状态栏/日志）。

    去重后取前几条 —— 10 条相同的"连不上 qBittorrent"没必要重复说十遍。
    """
    seen: list[str] = []
    for e in errors:
        if e and e not in seen:
            seen.append(e)
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    head = "；".join(seen)
    if len(errors) > len(seen):
        head += f"（共 {len(errors)} 条错误）"
    return head
