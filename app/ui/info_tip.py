"""信息提示组件：圆形感叹号按钮 + 点击弹出的说明窗口（InfoTip）。

交互：点击 (i) 按钮 → 弹出小窗口，内含说明文字（可选中复制）+ 「复制」/「打开链接」按钮。

设计要点：
1. 弹窗用 Qt.Popup 标志：点击窗口外任意处自动关闭，符合弹窗习惯。
2. 文字用只读 QTextEdit，支持框选与 Ctrl+C。
3. 颜色取自 QPalette，自动适配 fusion_dark / fusion_light，不写死 hex。
4. 图标保持「线性描边」风格：1.5px 圆环 + 感叹号，与导航图标一致。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import (
    QCursor, QDesktopServices, QGuiApplication, QMouseEvent, QPainter,
    QPalette, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

ICON_SIZE = 18          # 图标直径
POPUP_WIDTH = 340       # 弹窗宽度
POPUP_PADDING = 14      # 弹窗内边距
TEXT_MAX_HEIGHT = 220   # 文字区最大高度（超出则内部滚动）


def _palette_colors() -> tuple[str, str, str]:
    """返回 (文字色, 次要文字色, 边框色) 的 hex 字符串。"""
    pal = QApplication.palette()
    text = pal.color(QPalette.WindowText).name()
    hint = pal.color(QPalette.PlaceholderText).name()
    border = pal.color(QPalette.Mid).name()
    return text, hint, border


class InfoIcon(QWidget):
    """圆形感叹号按钮：悬停放大，可点击。"""

    def __init__(self, size: int = ICON_SIZE, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.base_size = size
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 平移到控件中心，后续以 (0,0) 为圆心绘制
        p.translate(self.width() / 2, self.height() / 2)

        pal = QApplication.palette()
        # 悬停时线条加深为正常文字色，未悬停用次要文字色（仅变线，不变填充）
        color = (
            pal.color(QPalette.WindowText) if self._hover
            else pal.color(QPalette.PlaceholderText)
        )
        # 圆环半径：留出 1px 描边与抗锯齿余量，避免圆被裁切
        r = self.base_size / 2 - 1.5

        # 线性圆环（1.5px，与基线图标一致）
        # 注意：必须用 QRectF 且以 (-r, -r) 为左上角，才是「圆心在原点」的正圆；
        # 写成 drawEllipse(0, 0, w, h) 会变成以原点为左上角、偏到右下方的椭圆。
        pen = QPen(color)
        pen.setWidthF(1.5)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(-r, -r, r * 2, r * 2))

        # 感叹号：竖线 + 圆点（整体位于圆环内部，比例协调）
        p.drawLine(QPointF(0, -r * 0.45), QPointF(0, r * 0.08))
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        dot_r = max(1.0, r * 0.14)
        p.drawEllipse(QPointF(0, r * 0.42), dot_r, dot_r)
        p.end()


class InfoPopup(QFrame):
    """说明弹窗：文字（可选中复制）+ 复制/打开链接按钮。"""

    def __init__(
        self,
        text: str = "",
        link: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # Popup：点击外部自动关闭
        self.setWindowFlags(Qt.Popup)
        self.setFixedWidth(POPUP_WIDTH)

        text_color, hint_color, border_color = _palette_colors()
        self.setStyleSheet(
            f"QFrame {{ background: palette(window);"
            f" border: 1px solid {border_color}; border-radius: 6px; }}"
            f" QTextEdit {{ background: transparent; color: {text_color};"
            f" border: none; padding: 0; }}"
            f" QLabel {{ color: {hint_color}; background: transparent; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(POPUP_PADDING, POPUP_PADDING,
                                 POPUP_PADDING, POPUP_PADDING)
        outer.setSpacing(10)

        # 文字区：可框选 + Ctrl+C
        self.text_edit = QTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setFrameShape(QFrame.NoFrame)
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text_edit.setPlainText(text)
        outer.addWidget(self.text_edit)
        self._fit_height()

        # 底部按钮行：左「关闭」 右「打开链接」
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self.close_btn = QPushButton("关闭", self)
        self.close_btn.clicked.connect(self.hide)
        btn_row.addWidget(self.close_btn)

        self.open_btn = QPushButton("打开链接", self)
        self.open_btn.clicked.connect(self._on_open)
        self.open_btn.setVisible(bool(link))
        btn_row.addWidget(self.open_btn)

        outer.addLayout(btn_row)

        self._link = link

    # ---------- 尺寸 ----------
    def _fit_height(self) -> None:
        """按内容高度收缩文字区（上限 TEXT_MAX_HEIGHT，超出则滚动）。"""
        doc = self.text_edit.document()
        doc.setTextWidth(self.text_edit.width() or (POPUP_WIDTH - POPUP_PADDING * 2))
        # +8 补偿行距与边框，避免末行被截断
        content_h = int(doc.size().height()) + 8
        self.text_edit.setFixedHeight(min(content_h, TEXT_MAX_HEIGHT))
        self.adjustSize()

    def set_text(self, text: str) -> None:
        self.text_edit.setPlainText(text)
        self._fit_height()

    def set_link(self, url: str) -> None:
        self._link = url
        self.open_btn.setVisible(bool(url))

    # ---------- 动作 ----------
    def _on_open(self) -> None:
        if self._link:
            QDesktopServices.openUrl(QUrl(self._link))

    # ---------- 关闭时清理状态 ----------
    def hideEvent(self, event) -> None:  # noqa: N802
        """弹窗关闭后清除残留的 hover / 焦点，避免导航按钮样式异常加深。

        原因：Qt.Popup 关闭时 Qt 只归还焦点，不会重算鼠标悬停，
        导致被鼠标"划过"的控件（如底部导航按钮）保持 hover 样式，
        视觉上像选中态加深。
        """
        super().hideEvent(event)
        # 清空焦点，防止某个按钮保持 focus 描边
        fw = QApplication.focusWidget()
        if fw is not None and fw is not self:
            fw.clearFocus()
        self._clear_hover()

    @staticmethod
    def _clear_hover() -> None:
        """让鼠标下方的控件重新计算 hover 状态。

        做法：补发一个 MouseMove 事件，让 QSS 重新匹配 :hover，
        从而清除弹窗关闭后残留的深色样式。
        """
        if QApplication.instance() is None:
            return
        w = QApplication.widgetAt(QCursor.pos())
        if w is None:
            return
        local = w.mapFromGlobal(QCursor.pos())
        pos = QPointF(local)
        ev = QMouseEvent(
            QEvent.MouseMove, pos, pos,
            Qt.NoButton, Qt.NoButton, Qt.NoModifier,
        )
        QApplication.sendEvent(w, ev)
        w.update()


class InfoTip(QWidget):
    """感叹号按钮 + 点击弹出说明窗口。

    用法：
        tip = InfoTip("提示文字", link="https://...")
        layout.addWidget(tip)
    """

    def __init__(
        self,
        text: str = "",
        link: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(ICON_SIZE, ICON_SIZE)

        self.icon = InfoIcon(ICON_SIZE, self)
        self.popup = InfoPopup(text, link, self.window())

    # ---------- 事件 ----------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._toggle_popup()
        super().mousePressEvent(event)

    def _toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            return
        self._show_popup()

    def _show_popup(self) -> None:
        # 弹窗挂在顶层窗口下，保证不被滚动区裁剪
        host = self.window() or self
        if self.popup.parent() is not host:
            self.popup.setParent(host)
        self.popup.adjustSize()

        # 定位：按钮下方左对齐，超出屏幕则回收
        anchor = self.mapToGlobal(self.rect().bottomLeft())
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()

        x = anchor.x()
        y = anchor.y() + 6
        w, h = self.popup.width(), self.popup.height()
        if x + w > avail.right():
            x = max(avail.left(), avail.right() - w)
        if y + h > avail.bottom():
            y = self.mapToGlobal(self.rect().topLeft()).y() - h - 6

        self.popup.move(x, y)
        self.popup.show()

    # ---------- 公共 API ----------
    def set_text(self, text: str) -> None:
        self.popup.set_text(text)

    def set_link(self, url: str) -> None:
        self.popup.set_link(url)
