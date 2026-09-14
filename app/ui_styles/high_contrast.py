"""高对比度：Fusion + 高对比 QPalette + high_contrast.qss。"""

from PySide6.QtWidgets import QApplication

from app.ui_styles._palettes import high_contrast_palette
from app.ui_styles.base import UIStyle, load_qss


class HighContrastStyle(UIStyle):
    key = "high_contrast"
    name = "高对比度"
    description = "纯黑白对比 + 2px 边框，无障碍友好"

    def apply(self, app: QApplication) -> None:
        app.setStyle("Fusion")
        app.setPalette(high_contrast_palette())
        app.setStyleSheet(load_qss("high_contrast.qss"))
