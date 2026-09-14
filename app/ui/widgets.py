"""通用组件：海报卡片、集数行、空状态、分隔线、FlowLayout。

设计基线（§5.8.0）：极简线性、1px 边框、无阴影、颜色走 QSS 选择器，
组件代码不写死颜色或圆角。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QLayoutItem,
    QVBoxLayout, QWidget,
)

from app.utils.paths import resource_path

PLACEHOLDER_COVER = resource_path("icons/placeholder_cover.png")


def _load_cover(path: str | Path, width: int, height: int = 0) -> QPixmap:
    """加载封面；文件不存在回退占位图；等比缩放（不变形）。

    - height > 0：缩放到 (width, height) 内并保持比例（用于卡片固定封面区）
    - height = 0：按宽度等比缩放（用于详情页大封面）
    """
    p = Path(path) if path else PLACEHOLDER_COVER
    if not p.exists():
        p = PLACEHOLDER_COVER
    pix = QPixmap(str(p))
    if pix.isNull():
        pix = QPixmap(str(PLACEHOLDER_COVER))
    if pix.isNull():
        return pix
    if height > 0:
        return pix.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if width > 0:
        return pix.scaledToWidth(width, Qt.SmoothTransformation)
    return pix


class FlowLayout(QLayout):
    """流式布局：子 widget 按行排列，宽度不足时自动换到下一行。

    参考 Qt 官方 Flow Layout 示例自实现。配合 QScrollArea.setWidgetResizable(True)
    使用时，只出现垂直滚动条，窗口变窄自动换行，无水平滚动条。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        hspacing: int = 16,
        vspacing: int = 16,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._hspace = hspacing
        self._vspace = vspacing
        if margin >= 0:
            self.setContentsMargins(margin, margin, margin, margin)

    # ---- QLayout 必需接口 ----
    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def horizontalSpacing(self) -> int:
        return self._hspace

    def verticalSpacing(self) -> int:
        return self._vspace

    # ---- 核心：排列 ----
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            space_x = self.horizontalSpacing()
            space_y = self.verticalSpacing()
            next_x = x + hint.width() + space_x
            # 当前行放不下且不是行首 → 换行
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + hint.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + m.bottom()


class PosterCard(QFrame):
    """海报卡片：统一固定尺寸；封面等比缩放居中；标题单行省略。

    宽度 = poster_width；封面区高 = 宽 × 1.4；文本区固定 56px。
    """

    clicked = Signal(int)

    TEXT_AREA_HEIGHT = 56
    COVER_RATIO = 1.4

    def __init__(
        self,
        subject_id: int,
        title: str,
        meta: str = "",
        cover_path: str = "",
        poster_width: int = 200,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.subject_id = subject_id
        self.poster_width = poster_width
        self.cover_height = int(poster_width * self.COVER_RATIO)
        self.setObjectName("posterCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(poster_width, self.cover_height + self.TEXT_AREA_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.cover_label = QLabel(self)
        self.cover_label.setObjectName("cover")
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setFixedSize(poster_width, self.cover_height)
        self.set_cover(cover_path)
        layout.addWidget(self.cover_label)

        text_wrap = QVBoxLayout()
        text_wrap.setContentsMargins(10, 6, 10, 6)
        text_wrap.setSpacing(2)

        self._full_title = title
        self.title_label = QLabel(self)
        self.title_label.setProperty("role", "title")
        self.title_label.setToolTip(title)  # 省略时悬停显示全名
        text_wrap.addWidget(self.title_label)

        self.meta_label = QLabel(meta, self)
        self.meta_label.setProperty("role", "meta")
        text_wrap.addWidget(self.meta_label)

        layout.addLayout(text_wrap)
        self._elide_title()

    def set_cover(self, path: str) -> None:
        self.cover_label.setPixmap(
            _load_cover(path, self.poster_width, self.cover_height)
        )

    def _elide_title(self) -> None:
        available = self.poster_width - 20  # 左右 margin 各 10
        elided = self.title_label.fontMetrics().elidedText(
            self._full_title, Qt.ElideRight, available
        )
        self.title_label.setText(elided)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.subject_id)
        super().mousePressEvent(event)


class EpisodeRow(QFrame):
    """集数行：序号 + 标题 + 已看勾选。双击发出 play_clicked。"""

    play_clicked = Signal(int)
    toggle_clicked = Signal(int)

    def __init__(
        self,
        episode_id: int,
        ep_index: float,
        title: str,
        watched: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.episode_id = episode_id
        self.setObjectName("episodeRow")
        self.setProperty("watched", "true" if watched else "false")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        idx_label = QLabel(f"{ep_index:g}", self)
        idx_label.setProperty("role", "meta")
        idx_label.setMinimumWidth(36)
        layout.addWidget(idx_label)

        self.title_label = QLabel(title, self)
        layout.addWidget(self.title_label, 1)

        self.check_label = QLabel("✓" if watched else "", self)
        self.check_label.setProperty("role", "check")
        layout.addWidget(self.check_label)

    def set_watched(self, watched: bool) -> None:
        self.setProperty("watched", "true" if watched else "false")
        self.check_label.setText("✓" if watched else "")
        # 触发 QSS 重绘
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.play_clicked.emit(self.episode_id)
        super().mouseDoubleClickEvent(event)


class TimelineRow(QFrame):
    """动态行：时间 + 动漫名 + 第 X 集 · 集标题。点击跳转条目详情。"""

    clicked = Signal(int)

    def __init__(
        self,
        subject_id: int,
        time_text: str,
        subject_name: str,
        ep_index: float,
        ep_title: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.subject_id = subject_id
        self.setObjectName("timelineRow")
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        time_label = QLabel(time_text, self)
        time_label.setProperty("role", "meta")
        time_label.setMinimumWidth(110)
        layout.addWidget(time_label)

        name_label = QLabel(subject_name, self)
        name_label.setProperty("role", "title")
        layout.addWidget(name_label)

        ep_text = f"第 {ep_index:g} 集"
        if ep_title:
            ep_text += f" · {ep_title}"
        ep_label = QLabel(ep_text, self)
        ep_label.setProperty("role", "meta")
        layout.addWidget(ep_label, 1)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.subject_id)
        super().mousePressEvent(event)


class EmptyState(QWidget):
    """空状态：一行提示 + 可选副提示。"""

    def __init__(
        self,
        message: str,
        hint: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)
        layout.addStretch(1)

        title = QLabel(message, self)
        title.setProperty("role", "subtitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        if hint:
            hint_label = QLabel(hint, self)
            hint_label.setProperty("role", "hint")
            hint_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(hint_label)

        layout.addStretch(1)


class HLine(QFrame):
    """1px 水平分隔线。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Plain)
        self.setFixedHeight(1)
