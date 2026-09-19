# 第三方许可证

Anime Marker 本身以 **MIT** 发布（见 [LICENSE](LICENSE)）。本文件列出**运行时依赖**及其
许可证，以及分发二进制时需要履行的义务。各许可证原文在 [`licenses/`](licenses/) 目录。

## 运行时依赖

| 依赖 | 开发环境版本 | 用途 | 许可证 |
| --- | --- | --- | --- |
| [PySide6](https://pypi.org/project/PySide6/) · shiboken6 | 6.9.2 | QML 界面与 Qt 运行时 | **LGPL-3.0**（另有 GPL / 商业许可）|
| [requests](https://pypi.org/project/requests/) | 2.32.5 | 所有 HTTP 请求（Bangumi / qBittorrent / RSS）| Apache-2.0 |
| urllib3 | 2.5.0 | requests 的传输层 | MIT |
| certifi | 2025.11.12 | CA 根证书包 | MPL-2.0 |
| charset-normalizer | 3.4.4 | 响应编码探测 | MIT |
| idna | 3.11 | 国际化域名 | BSD-3-Clause |
| [Pillow](https://pypi.org/project/Pillow/) | 12.0.0 | 封面图处理 | HPND（MIT-CMU 类）|
| [pywin32](https://pypi.org/project/pywin32/) | 311 | 读取播放器窗口标题 | PSF-2.0 |
| [qbittorrent-api](https://pypi.org/project/qbittorrent-api/) | 2026.8.1 | 订阅自动下载 | MIT |
| [PyAutoGUI](https://pypi.org/project/PyAutoGUI/) | 可选 | 模拟快捷键开插帧 | BSD-3-Clause |
| [keyboard](https://pypi.org/project/keyboard/) | 可选 | 全局热键 | MIT |

> 版本号是开发环境的实测值；`requirements.txt` 里写的是最低版本要求。

## 分发时要做什么

1. **随包附带** `LICENSE`、本文件与整个 `licenses/` 目录 —— 已写进
   [`anime_marker.spec`](anime_marker.spec) 的 `datas`，PyInstaller 产物 `dist/AnimeMarker/`
   里就带着它们。
2. **LGPL-3.0（PySide6）的额外义务** —— 这是唯一有实质约束的一项：
   - 提供 LGPL-3.0 全文（`licenses/LGPL-3.0.txt` ✓）；
   - **说明 Qt 是动态链接使用的**：本项目打包为 PyInstaller **单目录**形式（`COLLECT`），
     Qt / PySide6 以独立 DLL 随包提供，用户可以直接替换成自己构建的版本 ——
     这天然满足 LGPL「允许用户用修改后的库替换」的要求；
   - **不要修改 Qt / PySide6 的源码**；若修改了，修改部分需按 LGPL 公开。
3. **MIT / BSD-3 / Apache-2.0 / MPL-2.0 / PSF** 类：保留版权声明与许可证原文即可
   —— `licenses/` 已备齐（每个依赖各一份，含各自的版权行；来源见该目录的 README）。
4. 不要改动依赖库自带的版权头与许可证文件。

> 只发布源码（不发 exe）时，第 1、3、4 条同样适用，第 2 条中"允许替换库"的部分不涉及。
> 若将来改为**静态链接 Qt 或闭源分发**，需要单独评估 —— 要么遵守 LGPL（开源修改部分、
> 保证可替换），要么购买 Qt 商业许可。
