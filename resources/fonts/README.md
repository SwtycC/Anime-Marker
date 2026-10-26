# 内置字体

本目录存放随程序分发的字体（只读）。用户上传的字体不在这里，而是在用户
数据目录（开发态 `data/fonts`，打包态 `%APPDATA%/AnimeMarker/fonts`），
见 `app/utils/paths.py` 的 `fonts_dir()`。

| 目录 | 字体 | 下拉菜单显示名 |
| --- | --- | --- |
| `LxgwWenkaiGB-Lite/` | LXGW WenKai GB Lite（霞鹜文楷 GB 轻便版） | 霞鹜文楷 GB 轻便版 |

## 许可证

霞鹜文楷（LXGW WenKai）以 **SIL Open Font License 1.1** 授权，
许可证全文在仓库根目录的 `licenses/OFL-1.1-LxgwWenKai.txt`，
并随发行包一起分发（`anime_marker.spec` 的 `licenses` 目录）。

上游：<https://github.com/lxgw/LxgwWenKai>

## 打包

`anime_marker.spec` 里有 `('resources/fonts', 'resources/fonts')` 这一条。
新增内置字体时把文件放进本目录即可；代码侧在
`app/bridges/fonts.py` 的 `BUILTIN_FONT_REL` / `BUILTIN_FONT_LABEL` 登记
（目前只登记了一款）。
