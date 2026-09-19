# licenses/

本目录存放**运行时依赖**的许可证与版权声明，随发行包一起分发
（`anime_marker.spec` 已把整个目录打进 `dist/AnimeMarker/`）。

| 文件 | 对应依赖 | 许可证 | 来源 |
| --- | --- | --- | --- |
| `LGPL-3.0.txt` | PySide6、shiboken6 | LGPL-3.0 | gnu.org 官方原文 |
| `Apache-2.0.txt` | requests | Apache-2.0 | apache.org 官方原文 |
| `MIT-urllib3.txt` | urllib3 | MIT | 该包 wheel 内附带的原文 |
| `MIT-charset-normalizer.txt` | charset-normalizer | MIT | 同上 |
| `MIT-qbittorrent-api.txt` | qbittorrent-api | MIT | 同上 |
| `MIT-keyboard.txt` | keyboard | MIT | 上游仓库 `LICENSE` |
| `BSD-3-Clause-idna.txt` | idna | BSD-3-Clause | 该包 wheel 内附带的原文 |
| `BSD-3-Clause-pyautogui.txt` | PyAutoGUI | BSD-3-Clause | 上游仓库 `LICENSE.txt` |
| `HPND-Pillow.txt` | Pillow | HPND（MIT-CMU 类）| 该包 wheel 内附带的原文 |
| `MPL-2.0-certifi.txt` | certifi | MPL-2.0 | 同上（该文件是 **MPL 通知块 + 全文链接**，不是全文）|
| `PSF-2.0-pywin32.txt` | pywin32 | PSF-2.0 | SPDX 许可证数据（pywin32 的 wheel 未附带全文）|

两点说明：

- **为什么每个依赖各留一份原文**：MIT / BSD 类要求分发二进制时保留**该软件自己的版权声明**，
  共用一份通用文本会丢掉版权行。所以本目录是"一包一份"。
- **certifi 那份只有通知块**：MPL-2.0 允许以"告知如何获取全文"的方式履行，文件里带了链接；
  想更保守可再从 <https://www.mozilla.org/media/MPL/2.0/index.815ca599c9df.txt> 取全文一并附上。

## 一个需要留意的细节（pywin32）

pywin32 自身是 PSF-2.0，但它**自带一个 LGPL 的子包 `adodbapi`**
（`site-packages/adodbapi/license.txt` 是 LGPL 全文）。本项目只用 `win32gui` / `win32con`，
PyInstaller 按导入关系打包，**adodbapi 不会被收进产物**；如果将来有人改成 `import adodbapi`，
记得把那份 LGPL 文本也补进本目录。
