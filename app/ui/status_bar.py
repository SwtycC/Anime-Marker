"""自定义状态栏：版本号 | 进度条 + 日志。

布局：
    [ Anime Marker v1.0.0 ] │ [ ▓▓▓░░░░ 3/10 ] 正在扫描：无职转生 第二季

- 左侧：版本号（常驻，不随消息变化）
- 分隔符：1px 竖线
- 中部：进度条（仅在耗时任务进行中显示，完成后自动隐藏）
- 右侧：日志/状态文字

对外接口保持简单：
    bar.set_message(text, timeout_ms=0)   # 显示状态文字
    bar.start_progress(cur, total)        # 显示并更新进度
    bar.stop_progress()                   # 隐藏进度条
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QStatusBar, QWidget,
)

from app import __version__

log = logging.getLogger(__name__)

# 进度条尺寸与外观（颜色由 QSS 的 #statusProgress 控制）
PROGRESS_WIDTH = 140
PROGRESS_HEIGHT = 6


class VersionLabel(QLabel):
    """版本号标签。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(f"Anime Marker v{__version__}", parent)
        self.setObjectName("statusVersion")
        self.setToolTip(f"Anime Marker v{__version__}")


class Separator(QFrame):
    """1px 竖线分隔符。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusSeparator")
        self.setFrameShape(QFrame.VLine)
        self.setFrameShadow(QFrame.Plain)
        self.setFixedWidth(1)


class AppStatusBar(QStatusBar):
    """版本号 + 进度条 + 日志的自定义状态栏。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("appStatusBar")
        # 避免 Qt 默认的临时消息挤掉我们的常驻控件
        self.setSizeGripEnabled(False)

        # ---- 版本号 ----
        self.version_label = VersionLabel(self)
        self.addWidget(self.version_label)

        # ---- 分隔符 ----
        self.addWidget(Separator(self))

        # ---- 进度条（默认隐藏）----
        self.progress = QProgressBar(self)
        self.progress.setObjectName("statusProgress")
        self.progress.setFixedSize(PROGRESS_WIDTH, PROGRESS_HEIGHT)
        self.progress.setTextVisible(False)     # 数字由右侧日志文字承担
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.addWidget(self.progress)

        # ---- 日志文字（占满剩余空间）----
        self.message_label = QLabel(self)
        self.message_label.setObjectName("statusMessage")
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.addWidget(self.message_label, 1)

        # 临时消息的超时计时器
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(lambda: self.set_message(""))

        self._idle_text = "就绪"
        self.set_message(self._idle_text)

    # ---------- 日志 ----------
    def set_message(self, text: str, timeout_ms: int = 0) -> None:
        """显示状态文字。

        timeout_ms > 0 时，到时自动清空（若期间没有新消息）。
        """
        self.message_label.setText(text or "")
        self._clear_timer.stop()
        if timeout_ms > 0 and text:
            self._clear_timer.start(timeout_ms)

    def set_idle_text(self, text: str) -> None:
        """设置空闲时的默认文字。"""
        self._idle_text = text
        if not self.message_label.text():
            self.set_message(text)

    # ---------- 进度 ----------
    def start_progress(self, current: int = 0, total: int = 100) -> None:
        """显示进度条并设置进度。"""
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            # 总数未知：用忙碌指示
            self.progress.setRange(0, 0)
        self.progress.show()

    def set_progress(self, current: int, total: int) -> None:
        """更新进度（未显示时自动显示）。"""
        if self.progress.isHidden():
            self.start_progress(current, total)
            return
        if total > 0:
            if self.progress.maximum() != total:
                self.progress.setRange(0, total)
            self.progress.setValue(current)

    def stop_progress(self, hide: bool = True) -> None:
        """结束进度（默认隐藏进度条）。"""
        if hide:
            self.progress.hide()
        else:
            # 保留但填满，用于「刚完成」的视觉反馈
            self.progress.setValue(self.progress.maximum())

    @property
    def progress_visible(self) -> bool:
        return self.progress.isVisible()

    # ---------- 兼容旧接口 ----------
    def showMessage(self, message: str, timeout: int = 0) -> None:  # noqa: N802
        """覆盖 QStatusBar.showMessage，转发到自定义标签。

        这样既有的 self.statusBar().showMessage(...) 调用无需全部改写。
        """
        self.set_message(message, timeout)
