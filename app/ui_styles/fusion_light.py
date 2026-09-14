"""浅色现代：Fusion + 浅色 QPalette + fusion_light.qss。"""

from PySide6.QtWidgets import QApplication

from app.ui_styles._palettes import light_palette
from app.ui_styles.base import UIStyle, load_qss


class FusionLightStyle(UIStyle):
    key = "fusion_light"
    name = "浅色现代"
    description = "白底 + 黑字 + 1px 细线；强调色 #2563EB"

    def apply(self, app: QApplication) -> None:
        app.setStyle("Fusion")
        app.setPalette(light_palette())
        app.setStyleSheet(load_qss("fusion_light.qss"))
