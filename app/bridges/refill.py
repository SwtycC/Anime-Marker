"""集标题自动补全的调度器。

补全本身（查哪些条目缺标题、怎么回填）在 `core/ep_refill.py`，这里只
负责回答另一个问题：**什么时候跑**。

时机（每一个都对应一种"刚刚失败 / 刚刚恢复"的场景）：

  ① **启动补偿那一趟结束后立刻**（`RssBridge.scanPendingOnStart` 的回调，
     见 `kick_after_startup`）：上次退出前没补上的条目，这一次启动就能
     补上。与「下载完自动扫描」共用同一个启动时机 —— 补偿那趟若刚发起
     一次扫描，补全会看到 `scanner.running` 而自动让路；
  ② **每次扫描结束后 2 分钟**：下载完自动扫描刚拉失败时（代理没开 /
     Token 过期），立刻重试没有意义；给用户一点时间去开代理；
  ③ **配置保存后 1 秒**：用户很可能刚换了 Token 或代理，这是最可能
     恢复成功的时刻，应当**立刻**重试；
  ④ **网络失败后按 2/5/10/20/30 分钟退避**：软件一直开着、稍后代理
     恢复，也能自愈。成功后重置退避；**鉴权失败不自动重试**（Token
     不改，重试多少次都是 401），等第③条来唤醒。

**扫描进行中直接跳过**（15 秒后再看）：两边都会写同一个条目的
`episodes`，没必要制造并发；而且扫描自己就会拉到集数表，等它跑完
再补更划算。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from app.core.bangumi_api import BangumiClient
from app.core.database import Database
from app.core.ep_refill import (
    ERR_AUTH,
    ERR_NETWORK,
    EpisodeRefillWorker,
)
from app.utils.qt_thread import retire_worker

log = logging.getLogger(__name__)


class EpisodeRefillBridge(QObject):
    """自动补全的定时与线程管理（不暴露给 QML，由 QmlApp 驱动）。"""

    runningChanged = Signal()
    #: 一批补全结束：(回填条数, 涉及条目数)。供日志/状态栏使用。
    finished = Signal(int, int)

    #: 启动补偿回调的兜底延迟：正常情况下 `kick_after_startup` 在补偿那趟
    #: 结束（约 2 秒后）就触发；这里只防"回调没注入 / 补偿方法抛异常"时
    #: 补全永远不跑。**取较早者**，所以它不会拖慢正常路径。
    STARTUP_FALLBACK_MS = 60_000
    #: 扫描结束后再等一会儿 —— 扫描自己的请求可能刚失败，立刻重试没意义
    AFTER_SCAN_DELAY_MS = 2 * 60_000
    #: 配置保存（换 Token / 代理）后尽快重试
    AFTER_CONFIG_DELAY_MS = 1_000
    #: 扫描器正忙时的复查间隔
    BUSY_RETRY_MS = 15_000
    #: 网络失败后的退避序列（最后一档封顶）
    RETRY_DELAYS_MS = (2 * 60_000, 5 * 60_000, 10 * 60_000,
                       20 * 60_000, 30 * 60_000)

    def __init__(
        self,
        db: Database,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._api: Optional[BangumiClient] = None
        self._worker: Optional[EpisodeRefillWorker] = None
        self._running = False
        #: 连续网络失败的次数（决定退避档位；成功一批就归零）
        self._retry_index = 0
        #: 「扫描是否正在进行」的查询回调（QmlApp 注入）
        self._scan_busy = None
        #: 补完之后的通知（QmlApp 注入，让已打开的详情页重取集数）
        self._on_finished_hook = None

        # 单次触发：每次跑完由 _on_done 决定要不要再排下一次
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run)

    # ---------- 注入 ----------
    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    def set_scan_busy_hook(self, hook) -> None:
        """注入"扫描是否在跑"的查询回调（扫描期间不补，见模块说明）。"""
        self._scan_busy = hook

    def set_finished_hook(self, hook) -> None:
        """一批补全结束（且确实补到了行）时回调。"""
        self._on_finished_hook = hook

    # ---------- 状态 ----------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    def _set_running(self, value: bool) -> None:
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    # ---------- 调度 ----------
    def start(self) -> None:
        """挂上兜底计时器（QmlApp 在界面加载完成后调用）。

        真正的首次检查由「启动补偿」那趟的结束回调触发
        （`kick_after_startup`），这里只防回调没生效。
        """
        self._schedule(self.STARTUP_FALLBACK_MS)
        log.info("集标题自动补全已就绪（随启动补偿触发；兜底 %s 秒）",
                 self.STARTUP_FALLBACK_MS // 1000)

    @Slot()
    def kick_after_startup(self) -> None:
        """启动补偿这一趟结束后调用（QmlApp 接 `RssBridge` 的补偿回调）。

        延迟取 0：补偿那趟已经决定了"要不要补扫"，补全现在就该看一眼。
        若它刚发起扫描，`_run` 会发现扫描器在忙并让路（15 秒后再看）。
        """
        self._schedule(0)

    @Slot()
    def kick_after_scan(self) -> None:
        """扫描结束后调用（QmlApp 接 `scanner.finished`）。

        `scanner.finished` 带 `(matched, pending)` 两个参数，这里用
        `@Slot()` 声明成无参槽 —— Qt 允许槽的参数比信号少，多余的会被
        丢掉；不声明的话 PySide 会把两个参数原样传进来而报 TypeError。
        """
        self._schedule(self.AFTER_SCAN_DELAY_MS)

    @Slot()
    def kick_after_config(self) -> None:
        """配置保存后调用：Token / 代理可能刚改，立刻重试。"""
        # replace=True：此时应当覆盖掉「网络失败退避 30 分钟」那个计时器
        self._schedule(self.AFTER_CONFIG_DELAY_MS, replace=True)

    def _schedule(self, delay_ms: int, *, replace: bool = False) -> None:
        """排一次检查；已有更早的计时器时不往后推。

        为什么"不往后推"：`_run` 失败后可能会排一个 30 分钟的退避，
        而扫描结束的信号随时会再来一次 2 分钟。若每次都无条件覆盖，
        退避会变成"每 2 分钟一次"；反过来若忽略更早的，又会让"扫描
        刚结束"的时机白等。取**较早者**即可。
        """
        delay = max(0, int(delay_ms))
        if not replace and self._timer.isActive():
            left = self._timer.remainingTime()
            if 0 <= left <= delay:
                return
        self._timer.start(delay)

    def _run(self) -> None:
        if self._api is None:
            return
        if self._worker is not None and self._worker.isRunning():
            # 计时器在上一批还没收尾时响了（`done` 与 `finished` 之间有个
            # 时间差，`_on_done` 里排的退避也可能撞上这个窗口）。
            # **必须重新排**，不能直接 return —— 否则这次触发就丢了。
            self._schedule(self.BUSY_RETRY_MS, replace=True)
            return
        if self._scan_busy is not None:
            try:
                busy = bool(self._scan_busy())
            except Exception:       # pragma: no cover - 防御性
                busy = False
            if busy:
                # 扫描结束时会再 kick 一次，这里只是兜底
                self._schedule(self.BUSY_RETRY_MS, replace=True)
                return

        try:
            subject_ids = self._db.list_subjects_missing_ep_meta()
        except Exception as e:
            log.warning("读取「待补集标题」条目失败：%s", e)
            return
        if not subject_ids:
            self._retry_index = 0
            # 用 info（不是 debug）：启动补偿那趟后面紧跟这一行，"什么都没做"
            # 也要能一眼看到 —— 否则用户会怀疑补全根本没跑（与 RssBridge
            # 那句"没有需要补扫的记录"同一考虑）。
            log.info("集标题补全：没有需要补的条目")
            return

        worker = EpisodeRefillWorker(self._db, self._api, subject_ids)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        # 引用必须留到 finished（见 utils/qt_thread.py 的说明）
        worker.finished.connect(
            lambda: retire_worker(self, "_worker", worker))
        self._worker = worker
        self._set_running(True)
        log.info("集标题补全开始：%s 部待检查", len(subject_ids))
        worker.start()

    # ---------- 回调 ----------
    def _on_progress(self, done: int, total: int) -> None:
        log.debug("集标题补全进度：%s/%s", done, total)

    def _on_done(self, filled: int, subjects: int, error: str) -> None:
        self._set_running(False)
        if filled:
            log.info("集标题补全完成：回填 %s 条（涉及 %s 部）", filled, subjects)
            self.finished.emit(filled, subjects)
            if self._on_finished_hook is not None:
                try:
                    self._on_finished_hook()
                except Exception as e:      # pragma: no cover - 防御性
                    log.warning("集标题补全后的刷新回调失败：%s", e)
        else:
            log.info("集标题补全：本次没有可回填的集标题")

        if error == ERR_NETWORK:
            delay = self.RETRY_DELAYS_MS[
                min(self._retry_index, len(self.RETRY_DELAYS_MS) - 1)]
            self._retry_index += 1
            log.info("集标题补全遇到网络错误，%s 秒后重试",
                     delay // 1000)
            self._schedule(delay, replace=True)
        elif error == ERR_AUTH:
            # Token 失效：自动重试没有意义，等「配置保存」来唤醒
            log.warning("集标题补全暂停：Bangumi Token 过期或无效"
                        "（改好 Token 后会自动重试）")
        else:
            self._retry_index = 0

    # ---------- 退出 ----------
    def wait_worker(self, ms: int = 7000) -> None:
        """退出时停掉计时器并等待补全线程（QmlApp.shutdown 调用）。

        停止只在**两部之间**生效，所以默认等 7 秒（比客户端单次超时略长，
        与 LibraryBridge.waitRankWorker 同一口径）。超时也不强杀：worker
        可能正持有数据库锁，`terminate()` 会把锁留死。
        """
        self._timer.stop()
        w = self._worker
        if w is not None and w.isRunning():
            log.info("等待集标题补全线程结束…")
            w.requestInterruption()
            w.wait(ms)
