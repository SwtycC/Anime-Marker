"""深色现代（默认）：Fusion + 深色 QPalette + fusion_dark.qss。"""

from PySide6.QtWidgets import QApplication

from app.ui_styles._palettes import dark_palette
from app.ui_styles.base import UIStyle, load_qss


class FusionDarkStyle(UIStyle):
    key = "fusion_dark"
    name = "深色现代"
    description = "默认风格：深灰底 + 白字 + 1px 细线；强调色纯白"

    def apply(self, app: QApplication) -> None:
        app.setStyle("Fusion")
        app.setPalette(dark_palette())
        app.setStyleSheet(load_qss("fusion_dark.qss"))
