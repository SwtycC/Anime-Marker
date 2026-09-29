"""扫描桥接：把 ScanWorker（QThread）的进度与结果转给 QML。

关键点：**QML 不能直接持有 QThread**。
ScanWorker 是 QThread 子类，其信号跨线程发射；这里用一层 QObject 包装，
把信号原样转发并追加 QML 友好的聚合字段（百分比、文本摘要）。

用法（QML）：
    scannerBridge.start()
    Connections { target: scannerBridge
        function onProgressChanged(cur, total) { ... }
        function onLogMessage(msg) { ... }
        function onFinished(count) { model.reload() }
    }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database
from app.core.scanner import ScanWorker

log = logging.getLogger(__name__)

# 季数识别与多季展示的**固定方案**（原先由设置页的两个分段选择器决定，
# 现已移除界面、写死在此）。
#
# 放模块级常量而不是散在调用处：qml_app 也要用同一个值（它要据此决定
# 海报墙的展示模式），两处必须一致 —— 写死两份字面量迟早会漂移。
SEASON_MODE = "cn"        # 识别「第X季 / S1 / Season 1」，不含罗马数字
SEASON_DISPLAY = "flat"   # 多季平铺（不聚合为系列卡片）


class ScannerBridge(QObject):
    """扫描控制器。"""

    # ---- 状态（QML 绑定用）----
    runningChanged = Signal()
    progressChanged = Signal(int, int)      # current, total
    logMessage = Signal(str)
    finished = Signal(int, int)             # matched, pending
    failed = Signal(str)

    def __init__(
        self,
        db: Database,
        config: Config,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._api: Optional[BangumiClient] = None
        self._worker: Optional[ScanWorker] = None
        self._running = False
        self._current = 0
        self._total = 0
        # 本次扫描的统计
        self._matched = 0
        self._pending = 0
        # 扫描完成后由 QmlApp 注入的回调（用于刷新 LibraryBridge）
        self._on_finished_hook = None
        # 本次扫描是否为「单条目重扫」（决定完成后要不要发起在看/动态同步）
        self._single_scan = False

    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    def set_finished_hook(self, hook) -> None:
        """扫描结束时调用（QmlApp 用来让 LibraryBridge 刷新）。"""
        self._on_finished_hook = hook

    # ---------- 状态属性 ----------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(int, notify=progressChanged)
    def current(self) -> int:
        return self._current

    @Property(int, notify=progressChanged)
    def total(self) -> int:
        return self._total

    @Property(float, notify=progressChanged)
    def percent(self) -> float:
        """进度百分比 0~100，QML 进度条直接绑这个。"""
        if self._total <= 0:
            return 0.0
        return min(100.0, self._current * 100.0 / self._total)

    # ---------- 动作 ----------
    @Slot(result=str)
    def validate(self) -> str:
        """启动前校验，返回错误文本（空串 = 可开始）。

        单独抽出来是为了让 QML 能先弹确认框再真正开扫 —— Slot 不能
        同步返回"要不要继续"，故分成两步。
        """
        if self._running:
            return "扫描正在进行中"
        if self._api is None:
            return "Bangumi API 未初始化"
        paths = self._config.library_paths
        if not paths:
            return "请先配置媒体库根目录"
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            return "以下路径不存在：\n" + "\n".join(missing)
        return ""

    @Slot()
    def start(self) -> None:
        """开始扫描（异步）。调用前请先用 validate() 检查。"""
        if self._running:
            log.info("扫描已在运行，忽略本次请求")
            return
        err = self.validate()
        if err:
            self.failed.emit(err)
            return

        self._api = self._api or BangumiClient()
        self._matched = 0
        self._pending = 0
        self._current = 0
        self._total = 0

        worker = ScanWorker(
            self._config.library_paths,
            self._api,
            self._db,
            # 这两项**已从界面移除，固定写死**（不再读配置）：
            #   season_mode    = "cn"    → 识别「第X季 / S1 / Season 1」
            #   season_display = "flat"  → 多季平铺展示
            # 依据：两组选项实际是"一次选定就再也不改"的偏好，放在设置页
            # 只增加决策负担；而 `all`（额外识别罗马数字）会显著抬高误匹配
            # 风险，「聚合」也会让海报墙与"按季追番"的直觉不符。
            # 配置键仍保留在 DEFAULTS 里，仅为兼容旧 config.ini（见那里说明）。
            season_mode=SEASON_MODE,
            season_display=SEASON_DISPLAY,
            accept_score=self._config.getint("scanner", "accept_score", 60),
            accept_gap=self._config.getint("scanner", "accept_gap", 20),
            # 「集数按顺序对应」开关：与 accept_score 一样在**每次启动扫描**
            # 时现读 —— 设置页保存后下一次扫描即生效，无需重启。
            align_by_order=self._config.getbool(
                "scanner", "ep_align_order", True),
            # 「附加内容显示」开关（同上，启动扫描时现读）：关闭后 SP/OVA/
            # NCOP… 完全不进集数列表（见 ScanWorker.extra_show 的说明）
            extra_show=self._config.getbool(
                "scanner", "extra_show", True),
            # 「附加内容独立编号」开关（同上，启动扫描时现读）
            extra_numbering=self._config.getbool(
                "scanner", "extra_numbering", True),
        )
        worker.progress_changed.connect(self._on_progress)
        worker.item_matched.connect(self._on_matched)
        worker.log_message.connect(self._on_log)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_failed)

        self._worker = worker
        # 全量扫描：允许完成回调发起「在看/动态」同步（见 _on_ok）
        self._single_scan = False
        self._set_running(True)
        worker.start()
        log.info("扫描已启动，媒体库：%s", self._config.library_paths)

    @Slot(int)
    def startSubject(self, subject_id: int) -> None:
        """单个条目重新扫描（详情页「添加」按钮）。

        **为什么需要它**：全量扫描要把整个媒体库跑一遍（每个条目都要
        发 Bangumi 请求），只为了刷新一部番的集数/标题/别名太浪费。
        这里只处理该条目自己的目录。

        与 `start()` 共用同一套 worker 与信号：进度、日志、完成回调、
        finished 通知全都照旧 —— 界面无需为单扫写第二套处理逻辑。

        注意 `library_paths` 传空列表：单扫不看媒体库配置（用户可能已
        改过路径，而这条旧条目仍应能刷新），候选来源由 `only_folder` 决定。
        """
        if self._running:
            log.info("扫描进行中，忽略单条目扫描请求")
            self.logMessage.emit("扫描正在进行中，请稍候")
            return
        if self._api is None:
            self.failed.emit("Bangumi API 未初始化")
            return

        subj = self._db.get_subject(subject_id)
        if subj is None:
            self.failed.emit("条目不存在")
            return
        folder = (subj.folder_path or "").strip()
        if not folder:
            self.failed.emit("该条目没有记录目录路径，无法重新扫描")
            return

        self._api = self._api or BangumiClient()
        self._matched = 0
        self._pending = 0
        self._current = 0
        self._total = 0

        worker = ScanWorker(
            [],                       # 不用媒体库路径（见方法说明）
            self._api,
            self._db,
            season_mode=SEASON_MODE,
            season_display=SEASON_DISPLAY,
            accept_score=self._config.getint("scanner", "accept_score", 60),
            accept_gap=self._config.getint("scanner", "accept_gap", 20),
            align_by_order=self._config.getbool(
                "scanner", "ep_align_order", True),
            only_folder=Path(folder),
            extra_show=self._config.getbool(
                "scanner", "extra_show", True),
            extra_numbering=self._config.getbool(
                "scanner", "extra_numbering", True),
        )
        worker.progress_changed.connect(self._on_progress)
        worker.item_matched.connect(self._on_matched)
        worker.log_message.connect(self._on_log)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_failed)

        self._worker = worker
        # 标记为单条目扫描：完成回调据此决定**不发起**「在看/动态」同步
        # （见 _on_ok 的说明）。
        self._single_scan = True
        self._set_running(True)
        worker.start()
        log.info("单条目扫描已启动：subject_id=%s（%s）", subject_id, folder)

    @Slot()
    def cancel(self) -> None:
        """中止扫描（用户主动）。"""
        w = self._worker
        if w is not None and w.isRunning():
            w.terminate()
            w.wait(2000)
            log.warning("扫描已被用户中止")
            self.logMessage.emit("扫描已中止")
        self._set_running(False)
        self.finished.emit(self._matched, self._pending)

    # ---------- 内部 ----------
    def _set_running(self, value: bool) -> None:
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    def _on_progress(self, current: int, total: int) -> None:
        self._current, self._total = current, total
        self.progressChanged.emit(current, total)

    def _on_matched(self, subject_id: int, name: str) -> None:
        self._matched += 1
        log.debug("已匹配 subject_id=%s name=%s", subject_id, name)
        self.logMessage.emit(f"✓ 已匹配：{name}")

    def _on_log(self, msg: str) -> None:
        # pending 条目的日志形如「  待手动确认：原因」，据此计数
        if "待手动确认" in msg:
            self._pending += 1
        self.logMessage.emit(msg)

    def _on_ok(self) -> None:
        self._set_running(False)
        log.info("扫描完成：匹配 %s，待确认 %s", self._matched, self._pending)
        self.logMessage.emit(
            f"扫描完成：匹配 {self._matched} 个，待确认 {self._pending} 个"
        )
        if self._on_finished_hook is not None:
            try:
                self._on_finished_hook()
            except Exception as e:
                log.exception("扫描完成回调失败: %s", e)
        self.finished.emit(self._matched, self._pending)

    def _on_failed(self, msg: str) -> None:
        self._set_running(False)
        log.error("扫描失败：%s", msg)
        self.failed.emit(msg)
