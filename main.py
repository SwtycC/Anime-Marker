"""Anime Marker 程序入口（QML 界面）。

启动顺序：
1. 配置日志（必须在所有 logging.getLogger() 之前）
2. QApplication（QML 也需要 QApplication，不是 QGuiApplication）
3. 加载配置
4. 读取主题配置并注入 Theme 单例
5. 创建 QQmlApplicationEngine，注册 QML 导入路径与桥接层
6. 加载 Main.qml

> 历史说明：早期版本是 QWidget 界面（`app/ui/`），现已全量迁移到 QML
> （`app/qml/`），旧实现归档在 `_legacy/`。`run_qml.py` 保留为兼容入口，
> 与 `main.py` 行为一致。
"""

from __future__ import annotations

import os
import sys


def _force_basic_controls_style() -> None:
    """强制 QtQuick Controls 使用 Basic 风格，而非系统原生风格。

    **必须在导入 QmlApp（进而导入 QtQuick.Controls 相关模块）之前设置**，
    否则 `QQuickStyle.setStyle()` 会因控件已实例化而无效。

    背景：Windows 上的默认风格是 `windows`（原生），该风格**不支持**
    自定义 `background` / `contentItem`。表现为：
    - `AppScrollBar` 的透明轨道被忽略，仍画出原生浅灰滚动条
    - 深色界面右侧出现一条 10px 宽的白边（实测 `#f3f3f3`）
    - 控制台会打印 "The current style does not support customization"

    改为 Basic 后所有控件均可自由定制，且外观在各平台一致。
    """
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")


# 顺序关键：先设风格，再导入应用模块
_force_basic_controls_style()

from app.core.config import Config  # noqa: E402
from app.qml_app import QmlApp  # noqa: E402
from app.utils.logger import setup_logging  # noqa: E402
from app.utils.paths import app_data_dir  # noqa: E402


def main() -> int:
    # 1. 日志（必须在任何 logging.getLogger() 之前）
    setup_logging(app_data_dir() / "logs")

    # 2. 配置（首次运行会按 DEFAULTS 生成 config.ini）
    config = Config()

    # 3. 启动 QML 应用
    app = QmlApp(config)
    try:
        return app.run(sys.argv)
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
