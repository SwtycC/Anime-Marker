"""Anime Marker 应用包。

项目代码组织：
- app.qml        视图层（QML 界面，见 §5.12）
- app.bridges    QML ↔ Python 桥接层（library / scanner / settings / player）
- app.core       业务逻辑（scanner / matcher / bangumi_api / monitor /
                 launcher / database / config / rss_* / qbittorrent_api）
- app.utils      工具（paths / logger / cover_cache / title_parser / bgm_log …）

入口：`python main.py`（`run_qml.py` 为兼容转发）。

历史说明：早期是 QWidget 界面（`app/ui/` + `app/ui_styles/`），
现已全量迁移到 QML，旧实现归档在项目根 `_legacy/`（已加入 .gitignore）。
"""

__version__ = "1.0.0"
