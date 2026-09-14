"""UIStyle 抽象基类 + QSS 加载工具。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from app.utils.paths import resource_path


def load_qss(name: str) -> str:
    """加载 resources/qss/<name>。文件不存在返回空串。"""
    p = resource_path(f"qss/{name}")
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


class UIStyle:
    """UI 风格抽象。子类提供 key / name / description，并实现 apply()。"""

    key: str = ""
    name: str = ""
    description: str = ""

    def apply(self, app: QApplication) -> None:
        raise NotImplementedError
