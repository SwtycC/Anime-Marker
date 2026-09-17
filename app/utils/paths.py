"""统一路径管理（开发态 vs 打包态）。

- 用户数据（配置/库/封面/日志）：
    开发态放项目根 data/，打包后放 %APPDATA%\\AnimeMarker\\
- 资源（图标/QSS）：
    开发态放项目根 resources/，打包后从 sys._MEIPASS 读取

代码中所有 I/O 路径必须经本模块，禁止 Path(__file__).parent 拼接。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "AnimeMarker"


def is_frozen() -> bool:
    """是否在 PyInstaller 打包后运行。"""
    return getattr(sys, "frozen", False)


def project_root() -> Path:
    """项目根目录（开发态 = main.py 所在目录；打包态 = exe 所在目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def app_data_dir() -> Path:
    """用户数据目录。

    - 开发态：<project_root>/data
    - 打包态：%APPDATA%/AnimeMarker
    """
    if is_frozen():
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        base = project_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    """用户配置文件路径。"""
    if is_frozen():
        return app_data_dir() / "config.ini"
    # 开发态：项目根的 config.ini
    return project_root() / "config.ini"


def database_path() -> Path:
    return app_data_dir() / "anime.db"


def covers_dir() -> Path:
    p = app_data_dir() / "covers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def thumbs_dir() -> Path:
    p = app_data_dir() / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = app_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_path(rel: str) -> Path:
    """资源文件路径（图标/QSS）。

    - 开发态：<project_root>/resources/<rel>
    - 打包态：<sys._MEIPASS>/resources/<rel>
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", os.path.abspath(".")))
    else:
        base = project_root()
    return base / "resources" / rel


def qml_dir() -> Path:
    """QML 界面文件目录。

    - 开发态：<project_root>/app/qml
    - 打包态：<sys._MEIPASS>/qml（见 anime_marker.spec 的 datas 映射）

    注意：QML 引擎加载**目录**（而非单个文件）才能让 qmldir 生效，
    因此这里返回目录本身，由调用方用 addImportPath() 注册。
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", os.path.abspath(".")))
        return base / "qml"
    return project_root() / "app" / "qml"
