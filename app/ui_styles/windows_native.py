"""Windows 原生风格：跟随系统控件外观，不附加 QSS。"""

from PySide6.QtWidgets import QApplication

from app.ui_styles.base import UIStyle


class WindowsNativeStyle(UIStyle):
    key = "windows_native"
    name = "Windows 原生"
    description = "跟随系统控件外观，不附加 QSS；窗口内自绘部分仍遵守 §5.8.0 基线"

    def apply(self, app: QApplication) -> None:
        app.setStyle("windowsvista")
        # 不设置 palette / stylesheet，让系统主题生效
