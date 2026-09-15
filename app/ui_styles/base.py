"""UIStyle 抽象基类 + QSS 加载工具。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from app.utils.checkbox_icons import ensure_check_icons, ensure_spin_icons
from app.utils.paths import resource_path

# QSS 中的图标占位符 → 运行时生成的图标键名
_PLACEHOLDER_MAP = {
    # 复选框对勾
    "@CHECK_LIGHT@": "light",
    "@CHECK_DARK@": "dark",
    # 数值框加减号
    "@PLUS_LIGHT@": "plus_light",
    "@PLUS_DARK@": "plus_dark",
    "@MINUS_LIGHT@": "minus_light",
    "@MINUS_DARK@": "minus_dark",
}


def load_qss(name: str) -> str:
    """加载 resources/qss/<name>，并替换图标占位符为实际本地路径。

    占位符机制：QSS 里写 image: url("@PLUS_LIGHT@")，
    这里替换成系统临时目录下的纯 ASCII 路径（规避中文路径在 QSS 中失效）。
    """
    p = resource_path(f"qss/{name}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""

    icons = {**ensure_check_icons(), **ensure_spin_icons()}
    for placeholder, key in _PLACEHOLDER_MAP.items():
        if placeholder in text and key in icons:
            text = text.replace(placeholder, icons[key])
    return text


class UIStyle:
    """UI 风格抽象。子类提供 key / name / description，并实现 apply()。"""

    key: str = ""
    name: str = ""
    description: str = ""

    def apply(self, app: QApplication) -> None:
        raise NotImplementedError
