"""紧凑列表：Fusion + 浅色 QPalette + compact.qss（高密度、小字号）。"""

from PySide6.QtWidgets import QApplication

from app.ui_styles._palettes import light_palette
from app.ui_styles.base import UIStyle, load_qss


class CompactStyle(UIStyle):
    key = "compact"
    name = "紧凑列表"
    description = "高密度、小字号、列表式；适合大媒体库与键盘流"

    def apply(self, app: QApplication) -> None:
        app.setStyle("Fusion")
        app.setPalette(light_palette())
        app.setStyleSheet(load_qss("compact.qss"))
