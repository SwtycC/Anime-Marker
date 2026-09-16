"""配置读取/写入（config.ini）。

首次打开时若文件不存在，按 DEFAULTS 生成；
后续打开时补齐缺失的 section / option，便于版本升级。
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from app.utils.paths import config_path

DEFAULTS: dict[str, dict[str, str]] = {
    "general": {
        "library_path": "",
        "player_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        "ls_path": r"C:\Program Files\Lossless Scaling\LosslessScaling.exe",
    },
    "bangumi": {
        "token": "",
        "username": "",
        "api_base": "https://api.bgm.tv",
        "proxy": "",
        "user_agent": "AnimeMarker/1.0 (https://github.com/yourname/anime-marker)",
        "inprogress_cache_ttl": "300",
    },
    "qbittorrent": {
        "host": "127.0.0.1",
        "port": "8080",
        "username": "admin",
        "password": "",
        "category": "Bangumi",
        "save_path": "",
        "webui_url": "",
    },
    "rss": {
        "poll_interval": "30",
        "rule": "new_only",
        "auto_download": "false",
        "poll_on_start": "true",
    },
    "scanner": {
        # 季数识别模式：cn=第X季/第X部/S1/Season 1；all=额外启用罗马数字
        "season_patterns": "cn",
        # 多季展示：flat=平铺（默认）；grouped=按系列聚合
        "season_display": "flat",
        # 自动匹配的最低分与最小差距（调高更保守）
        "accept_score": "60",
        "accept_gap": "20",
    },
    "launcher": {
        "enable_ls": "true",
        "ls_shortcut": "ctrl+alt+l",
        "ls_start_delay": "5",
        "player_start_delay": "1",
    },
    "monitor": {
        "poll_interval": "3",
        "trigger_threshold": "0.95",
        "title_regex": "",
    },
    "ui": {
        "style": "fusion_dark",
        "poster_width": "200",
    },
}


class Config:
    """INI 配置封装。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_path()
        self._parser = configparser.ConfigParser()
        if not self.path.exists():
            self._init_defaults()
        else:
            self._parser.read(self.path, encoding="utf-8")
            self._ensure_sections()

    # ---------- 初始化 ----------
    def _init_defaults(self) -> None:
        for section, kv in DEFAULTS.items():
            self._parser[section] = dict(kv)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.save()

    def _ensure_sections(self) -> None:
        changed = False
        for section, kv in DEFAULTS.items():
            if not self._parser.has_section(section):
                self._parser.add_section(section)
                changed = True
            for k, v in kv.items():
                if not self._parser.has_option(section, k):
                    self._parser.set(section, k, v)
                    changed = True
        if changed:
            self.save()

    # ---------- 通用读写 ----------
    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._parser.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback: int = 0) -> int:
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback: float = 0.0) -> float:
        return self._parser.getfloat(section, key, fallback=fallback)

    def getbool(self, section: str, key: str, fallback: bool = False) -> bool:
        return self._parser.getboolean(section, key, fallback=fallback)

    def set(self, section: str, key: str, value: Any) -> None:
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, key, str(value))

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            self._parser.write(f)

    # ---------- 便捷访问 ----------
    @property
    def library_paths(self) -> list[Path]:
        raw = self.get("general", "library_path", "")
        return [Path(p.strip()) for p in raw.split(";") if p.strip()]

    @property
    def bangumi_token(self) -> str:
        return self.get("bangumi", "token", "")

    @property
    def poll_interval(self) -> int:
        return self.getint("monitor", "poll_interval", 3)

    @property
    def trigger_threshold(self) -> float:
        return self.getfloat("monitor", "trigger_threshold", 0.95)
