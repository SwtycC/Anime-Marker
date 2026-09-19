"""Anime Marker 应用包。

项目代码组织：
- app.qml        视图层（QML 界面，见 §5.12）
- app.bridges    QML ↔ Python 桥接层（library / scanner / settings / player）
- app.core       业务逻辑（scanner / matcher / bangumi_api / monitor /
                 launcher / database / config / rss_* / qbittorrent_api）
- app.utils      工具（paths / logger / cover_cache / title_parser / bgm_log …）

入口：`python main.py`（`run_qml.py` 为兼容转发）。

历史说明：早期是 QWidget 界面（`app/ui/` + `app/ui_styles/`）。
"""

__version__ = "1.0.0"

# Bangumi API 要求的 User-Agent。
#
# 官方规定（https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md）：
#   - 非浏览器的调用方必须带「**开发者个人 ID** + 应用名」；
#   - 开源项目请附项目主页；分发的应用请带版本号；
#   - **明确点名禁止** `Bangumi/1.0` 这种"应用名 + 版本号"的写法，
#     以及各种请求库的默认 UA（可能被直接禁用）。
# 形状：`<开发者ID>/<应用名>/<版本> (<项目主页>)`
#
# 早期我们发的是 `AnimeMarker/1.0 (https://github.com/yourname/anime-marker)`：
# 既没有开发者 ID（就是被点名的那种），项目主页还是 `yourname` 占位符（404）。
# 现在统一从这里取，避免三处（config 默认值 / BangumiClient 默认值 / 配置面板兜底）
# 各写一份又漂移。
USER_AGENT = (
    f"SwtycC/Anime-Marker/{__version__} "
    "(https://github.com/SwtycC/Anime-Marker)"
)
