"""UI 风格注册表。

使用：
    from app.ui_styles import apply_style
    apply_style(app, "fusion_dark")
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from app.ui_styles.base import UIStyle
from app.ui_styles.fusion_dark import FusionDarkStyle
from app.ui_styles.fusion_light import FusionLightStyle

DEFAULT_STYLE_KEY = "fusion_dark"

REGISTRY: dict[str, UIStyle] = {
    s.key: s
    for s in [
        FusionDarkStyle(),
        FusionLightStyle(),
    ]
}


def apply_style(app: QApplication, key: str) -> None:
    """应用指定风格；key 非法时回退到默认。"""
    style = REGISTRY.get(key) or REGISTRY[DEFAULT_STYLE_KEY]
    style.apply(app)
    app.setProperty("ui_style_key", key)


__all__ = ["UIStyle", "REGISTRY", "apply_style", "DEFAULT_STYLE_KEY"]
