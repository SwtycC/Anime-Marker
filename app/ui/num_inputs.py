"""数值输入控件。

设计目标：输入框与加减按钮之间要有明显间距（对齐「媒体库根目录」那行的
QLineEdit + 独立按钮布局），而不是 QSpinBox 内嵌子控件那种紧贴效果。

因此不用 QSpinBox，改为 QLineEdit + 两个独立 QPushButton 的水平布局：
    [ 输入框                  ]  [−] [+]        ← 间距由 layout spacing 控制
同时天然不响应滚轮（QLineEdit 不绑定滚轮）。
"""

from __future__ import annotations

from typing import Optional, Union

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QHBoxLayout, QLineEdit, QPushButton, QWidget,
)

# 输入框与按钮之间的间距（对应「媒体库根目录」那行的视觉间距）
FIELD_SPACING = 10
BUTTON_SIZE = 30          # 加减按钮正方形边长


class NumberField(QWidget):
    """数值输入：QLineEdit + −/+ 独立按钮。

    支持整数与浮点（decimals > 0 时）。
    """

    value_changed = Signal(float)

    def __init__(
        self,
        value: float = 0,
        minimum: float = 0,
        maximum: float = 100,
        step: float = 1,
        decimals: int = 0,
        suffix: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._step = step
        self._decimals = decimals
        self._suffix = suffix

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(FIELD_SPACING)   # ← 关键：输入框与按钮之间的距离

        self.edit = QLineEdit(self)
        self.edit.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.edit, 1)

        self.minus_btn = self._make_button("\u2212")   # − U+2212 减号
        self.minus_btn.clicked.connect(lambda: self._nudge(-1))
        layout.addWidget(self.minus_btn)

        self.plus_btn = self._make_button("+")
        self.plus_btn.clicked.connect(lambda: self._nudge(1))
        layout.addWidget(self.plus_btn)

        self.edit.editingFinished.connect(self._commit_text)
        self.setValue(value)

    # ---------- 内部 ----------
    def _make_button(self, text: str) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setObjectName("stepButton")          # QSS 统一控制外观
        btn.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)           # 不抢输入框焦点
        return btn

    def _format(self, v: float) -> str:
        text = f"{v:.{self._decimals}f}" if self._decimals else f"{v:g}"
        return f"{text}{self._suffix}"

    def _parse(self, text: str) -> Optional[float]:
        raw = text.strip()
        if self._suffix and raw.endswith(self._suffix):
            raw = raw[: -len(self._suffix)].strip()
        try:
            return float(raw)
        except ValueError:
            return None

    def _clamp(self, v: float) -> float:
        v = max(self._min, min(self._max, v))
        return round(v, self._decimals) if self._decimals else round(v)

    def _nudge(self, direction: int) -> None:
        self.setValue(float(self.value()) + direction * self._step)
        self.value_changed.emit(float(self.value()))

    def _commit_text(self) -> None:
        parsed = self._parse(self.edit.text())
        if parsed is None:
            # 输入非法：还原为当前值
            self.edit.setText(self._format(float(self.value())))
            return
        self.setValue(parsed)
        self.value_changed.emit(float(self.value()))

    # ---------- 公共 API（与 QSpinBox 兼容的子集）----------
    def value(self) -> Union[int, float]:
        parsed = self._parse(self.edit.text())
        if parsed is None:
            return self._min
        v = self._clamp(parsed)
        return v if self._decimals else int(v)

    def setValue(self, v: float) -> None:  # noqa: N802
        v = self._clamp(float(v))
        self.edit.setText(self._format(v))
        self.edit.setCursorPosition(0)       # 文本左对齐显示

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802
        self._min, self._max = minimum, maximum

    def setSingleStep(self, step: float) -> None:  # noqa: N802
        self._step = step

    def setDecimals(self, n: int) -> None:  # noqa: N802
        self._decimals = n

    def setSuffix(self, suffix: str) -> None:  # noqa: N802
        self._suffix = suffix

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self.edit.setEnabled(enabled)
        self.minus_btn.setEnabled(enabled)
        self.plus_btn.setEnabled(enabled)


class _NoWheelMixin:
    """把滚轮事件忽略，交给父级滚动区处理。"""

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()


class NoWheelComboBox(_NoWheelMixin, QComboBox):
    """下拉框：滚轮不切换选项（需点击展开后再选）。"""


class WheelGuard(QObject):
    """给已有控件装「滚轮不响应」守卫（事件过滤器方式）。"""

    def __init__(self, target: QObject) -> None:
        super().__init__(target)
        self.target = target
        target.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Wheel and isinstance(
            obj, (QAbstractSpinBox, QComboBox)
        ):
            return True
        return super().eventFilter(obj, event)


def disable_wheel(*widgets: QObject) -> list[WheelGuard]:
    """批量禁用滚轮响应，返回守卫列表（需保留引用防 GC）。"""
    guards: list[WheelGuard] = []
    for w in widgets:
        if isinstance(w, (QAbstractSpinBox, QComboBox)):
            guards.append(WheelGuard(w))
    return guards
