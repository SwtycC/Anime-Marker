"""Anime Marker 程序入口。

启动顺序：
1. 配置日志（必须在所有 logging.getLogger() 之前）
2. 计算路径（开发态 vs 打包态）
3. 加载配置
4. 应用 UI 风格
5. 创建并启动主窗口
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.core.config import Config
from app.ui.main_window import MainWindow
from app.ui_styles import apply_style
from app.utils.logger import setup_logging
from app.utils.paths import app_data_dir


def main() -> int:
    # 1. 日志
    setup_logging(app_data_dir() / "logs")

    # 2. QApplication（必须在使用任何 Qt widget 之前）
    app = QApplication(sys.argv)
    app.setApplicationName("Anime Marker")
    app.setOrganizationName("AnimeMarker")
    app.setApplicationDisplayName("Anime Marker")

    # 3. 配置
    config = Config()
    style_key = config.get("ui", "style", fallback="fusion_dark")
    apply_style(app, style_key)

    # 4. 主窗口
    window = MainWindow(config)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
