"""配置桥接：把 config.ini 的读写暴露给 QML。

设计要点：
1. **批量读取**：QML 侧一次 `getAll()` 拿回整份配置（dict），
   避免为每个字段写一个 Slot（几十个字段会很难维护）。
2. **批量写入**：QML 侧组装好 dict 后一次 `saveAll(map)`，
   只写白名单内的键，防止 QML 侧误写脏数据。
3. **文件对话框**：QML 没有原生目录/文件选择器，必须由 Python 提供
   （`pickDirectory()` / `pickFile()`）。这两个是**模态阻塞**调用，
   Qt 会自行处理事件循环，QML 侧同步拿返回值即可。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QWidget

from app.core.config import Config

log = logging.getLogger(__name__)

# 允许 QML 读写的键（点号分隔：section.key）—— 白名单，防止误写
ALLOWED_KEYS = {
    # Bangumi
    "bangumi.token", "bangumi.username", "bangumi.api_base", "bangumi.proxy",
    "bangumi.ep_timeline_count", "bangumi.auto_upload",
    # 路径
    "general.library_path", "general.player_path", "general.ls_path",
    # 启动器
    "launcher.enable_ls", "launcher.ls_shortcut", "launcher.ls_start_delay",
    "launcher.player_start_delay", "launcher.fullscreen_shortcut",
    "launcher.fullscreen_settle",
    # 监控
    "monitor.poll_interval", "monitor.trigger_threshold", "monitor.title_regex",
    # 扫描与匹配
    "scanner.season_patterns", "scanner.season_display",
    "scanner.accept_score", "scanner.accept_gap",
    # qBittorrent
    "qbittorrent.host", "qbittorrent.port", "qbittorrent.username",
    "qbittorrent.password", "qbittorrent.category", "qbittorrent.save_path",
    "qbittorrent.webui_url",
    # RSS
    "rss.poll_interval", "rss.rule", "rss.auto_download", "rss.poll_on_start",
    # 界面
    "ui.theme_mode", "ui.accent_color", "ui.poster_width",
}

# 读取时的默认值（config.ini 缺项时兜底，与 config.DEFAULTS 保持一致）
FALLBACKS: dict[str, str] = {
    "bangumi.token": "",
    "bangumi.username": "",
    "bangumi.api_base": "https://api.bgm.tv",
    "bangumi.proxy": "",
    "bangumi.ep_timeline_count": "30",
    "bangumi.auto_upload": "true",
    "general.library_path": "",
    "general.player_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    "general.ls_path": r"C:\Program Files\Lossless Scaling\LosslessScaling.exe",
    "launcher.enable_ls": "true",
    "launcher.ls_shortcut": "ctrl+alt+p",
    "launcher.fullscreen_shortcut": "alt+enter",
    "launcher.fullscreen_settle": "3.0",
    "monitor.poll_interval": "3",
    "monitor.trigger_threshold": "0.95",
    "scanner.season_patterns": "cn",
    "scanner.season_display": "flat",
    "scanner.accept_score": "60",
    "scanner.accept_gap": "30",
    "qbittorrent.host": "127.0.0.1",
    "qbittorrent.port": "8080",
    "qbittorrent.username": "admin",
    "qbittorrent.password": "",
    "qbittorrent.category": "Bangumi",
    "qbittorrent.save_path": "",
    "qbittorrent.webui_url": "",
    "rss.poll_interval": "30",
    "rss.rule": "new_only",
    "rss.auto_download": "false",
    "rss.poll_on_start": "true",
    "ui.theme_mode": "light",
    "ui.accent_color": "#2F6FEB",
    "ui.poster_width": "200",
}


def _to_ini_text(value: Any) -> str:
    """把 QML 传来的值转成 ini 文本。

    **关键**：QML 的 number 一律是 JS number，到了 Python 侧是 float。
    若直接 `str(9.0)` 会写出 `"9.0"`，之后 `ConfigParser.getint()`
    解析 `"9.0"` 会抛 ValueError（只认十进制整数），导致读取配置崩掉。
    因此这里对「整数值的浮点数」做归一：
        9.0    → "9"
        0.95   → "0.95"
        True   → "true"
        0      → "0"
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 整数值的浮点（含 9.000000001 这类误差）按整数写
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        # 去掉尾部多余的 0：0.9500 → 0.95
        return f"{value:g}"
    return str(value)


class SettingsBridge(QObject):
    """配置读写 + 文件选择。"""

    # 保存完成（QmlApp 监听后重建依赖配置的服务）
    saved = Signal()
    # 请求「保存并扫描」
    scanRequested = Signal()

    def __init__(
        self,
        config: Config,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._dialog_parent: Optional[QWidget] = None

    def set_dialog_parent(self, widget: QWidget) -> None:
        """设置文件对话框的父窗口（由 QmlApp 注入主窗口）。"""
        self._dialog_parent = widget

    # ---------- 读 ----------
    @Slot(result="QVariantMap")
    def getAll(self) -> dict:
        """一次性返回所有可配置项（点号键）。"""
        out: dict[str, str] = {}
        for key in ALLOWED_KEYS:
            section, _, name = key.partition(".")
            fallback = FALLBACKS.get(key, "")
            try:
                out[key] = self._config.get(section, name, fallback)
            except Exception as e:  # pragma: no cover - 防御性
                log.warning("读取配置 %s 失败：%s", key, e)
                out[key] = fallback
        return out

    # ---------- 写 ----------
    @Slot("QVariantMap", result=bool)
    def saveAll(self, values: dict) -> bool:
        """批量写入白名单内的键。返回是否成功。"""
        if not isinstance(values, dict):
            log.warning("saveAll 收到非字典参数：%r", type(values))
            return False

        written = 0
        skipped: list[str] = []
        for key, value in values.items():
            if key not in ALLOWED_KEYS:
                skipped.append(key)
                continue
            section, _, name = key.partition(".")
            self._config.set(section, name, _to_ini_text(value))
            written += 1

        if skipped:
            log.warning("saveAll 忽略未授权键：%s", skipped)
        try:
            self._config.save()
        except Exception as e:
            log.exception("配置保存失败：%s", e)
            return False

        log.info("配置已保存（%s 项，忽略 %s 项）", written, len(skipped))
        self.saved.emit()
        return True

    @Slot(result=bool)
    def requestScan(self) -> bool:
        """QML 点「保存并扫描」时调用，交由 QmlApp 触发扫描。"""
        self.scanRequested.emit()
        return True

    # ---------- 文件选择 ----------
    @Slot(str, result=str)
    def pickDirectory(self, start_dir: str = "") -> str:
        """选择目录（媒体库根目录）。取消返回空串。"""
        path = QFileDialog.getExistingDirectory(
            self._dialog_parent, "选择目录", start_dir or ""
        )
        return path or ""

    @Slot(str, str, result=str)
    def pickFile(self, title: str = "", start_dir: str = "") -> str:
        """选择可执行文件（PotPlayer / 小黄鸭）。取消返回空串。"""
        path, _ = QFileDialog.getOpenFileName(
            self._dialog_parent,
            title or "选择文件",
            start_dir or "",
            "可执行文件 (*.exe);;所有文件 (*)",
        )
        return path or ""

    # ---------- 便捷访问（供 QmlApp 使用）----------
    def value(self, key: str, fallback: str = "") -> str:
        section, _, name = key.partition(".")
        return self._config.get(section, name, fallback or FALLBACKS.get(key, ""))

    def intValue(self, key: str, fallback: int = 0) -> int:
        section, _, name = key.partition(".")
        return self._config.getint(section, name, fallback)

    def boolValue(self, key: str, fallback: bool = False) -> bool:
        section, _, name = key.partition(".")
        return self._config.getbool(section, name, fallback)
