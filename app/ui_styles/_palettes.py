"""共享 QPalette 工厂。

遵循 §5.8.0 极简线性基线：仅黑白灰 + 单一强调色。
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette


def light_palette() -> QPalette:
    """浅色：白底 #FFFFFF / #FAFAFA，文字 #111111 / #666666，强调 #2563EB。"""
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#FAFAFA"))
    p.setColor(QPalette.Base, QColor("#FFFFFF"))
    p.setColor(QPalette.AlternateBase, QColor("#FAFAFA"))
    p.setColor(QPalette.WindowText, QColor("#111111"))
    p.setColor(QPalette.Text, QColor("#111111"))
    p.setColor(QPalette.PlaceholderText, QColor("#999999"))
    p.setColor(QPalette.Button, QColor("#FFFFFF"))
    p.setColor(QPalette.ButtonText, QColor("#111111"))
    p.setColor(QPalette.Highlight, QColor("#2563EB"))
    p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    p.setColor(QPalette.Mid, QColor("#E5E5E5"))
    p.setColor(QPalette.Dark, QColor("#999999"))
    p.setColor(QPalette.Light, QColor("#FFFFFF"))
    p.setColor(QPalette.ToolTipBase, QColor("#FFFFFF"))
    p.setColor(QPalette.ToolTipText, QColor("#111111"))
    p.setColor(QPalette.Link, QColor("#2563EB"))
    return p


def dark_palette() -> QPalette:
    """深色：深灰底 #111111，文字 #FAFAFA，强调纯白。"""
    p = QPalette()
    p.setColor(QPalette.Window, QColor("#111111"))
    p.setColor(QPalette.Base, QColor("#1A1A1A"))
    p.setColor(QPalette.AlternateBase, QColor("#222222"))
    p.setColor(QPalette.WindowText, QColor("#FAFAFA"))
    p.setColor(QPalette.Text, QColor("#FAFAFA"))
    p.setColor(QPalette.PlaceholderText, QColor("#666666"))
    p.setColor(QPalette.Button, QColor("#1A1A1A"))
    p.setColor(QPalette.ButtonText, QColor("#FAFAFA"))
    p.setColor(QPalette.Highlight, QColor("#FFFFFF"))
    p.setColor(QPalette.HighlightedText, QColor("#000000"))
    p.setColor(QPalette.Mid, QColor("#2A2A2A"))
    p.setColor(QPalette.Dark, QColor("#000000"))
    p.setColor(QPalette.Light, QColor("#333333"))
    p.setColor(QPalette.ToolTipBase, QColor("#1A1A1A"))
    p.setColor(QPalette.ToolTipText, QColor("#FAFAFA"))
    p.setColor(QPalette.Link, QColor("#FFFFFF"))
    return p
