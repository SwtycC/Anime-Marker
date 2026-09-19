# Anime-Marker

Windows 桌面端本地动漫管理 + Bangumi 进度同步 + PotPlayer 播放联动工具。

用 Python + PySide6（QML 界面）写成，把自己硬盘上的动画和 Bangumi 上的观看记录对起来：
**自动匹配条目 → 一键播放（可联动插帧）→ 看完自动标记 → 在应用里回看观看动态**。

---

## 功能特性

**媒体库与海报墙**

- 扫描本地目录（支持多个根目录，分号分隔），自动提取标题、季数、集数
- 调 Bangumi 搜索接口逐个匹配，按匹配度打分；低分条目进「待确认」，可手动指定或重新匹配
- 海报墙支持**平铺 / 按系列聚合**两种展示；封面下载后本地缓存，断网也能出图
- 条目详情页：集列表、已看进度、播放、手动标记看过、重新匹配

**播放联动**

- 用 PotPlayer 打开（外部进程，不内置播放器）
- 可联动 Lossless Scaling（小黄鸭）自动开插帧
- 读取 PotPlayer 窗口标题解析播放进度，到阈值（默认 95%）自动标记该集「看过」

**Bangumi 集成**

- 收藏页：浏览「看过」的动画，与本地区目关联
- 动态页：**逐集观看时间线** —— 本地播放记录 + Bangumi 逐集标记混排，按天分组（今天 / 昨天 / 本周 / 上周 / 本月 / 上月 / 月份）
- 支持「本地 + Bangumi」与「仅本地」两种视图；点行进详情，返回回到来源页

**RSS 订阅与自动下载**

- 管理 RSS 源（Mikan / dmhy 等标准 RSS 2.0 / Atom），按规则筛选
- 三层查重后推送 qBittorrent 下载（本程序不实现 BT 协议，只调 qB 的 Web UI API）

**其他**

- 亮色 / 暗色主题 + 可换主题色
- 全离线可用：收藏、集级记录、封面都有本地缓存
- 网络失败有分类提示（区分「服务端故障」与「本地链路/代理问题」，见下方常见问题）

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11（依赖 `win32gui` 读播放器窗口标题）|
| Python | **3.10 或更高**（用到 `X \| None` 等新语法）|
| PotPlayer | 播放功能需要（[下载](https://potplayer.daum.net/)）|
| Lossless Scaling | 可选，用于自动开插帧 |
| qBittorrent | 可选，订阅自动下载需要（需开启 Web UI）|
| Bangumi 账号 | 可选，但「收藏 / 动态」需要 Access Token |

## 安装与运行

```cmd
pip install -r requirements.txt
python main.py
```

## 首次配置

在「设置」页填写以下内容后点击「保存并扫描」：

| 配置项 | 说明 |
| --- | --- |
| **Bangumi Token** | 获取地址：**<https://next.bgm.tv/demo/access-token>**（请勾选「读取收藏」权限，否则收藏列表无法拉取） |
| Bangumi 用户名 | 收藏列表需要；留空则尝试用 Token 解析 |
| 媒体库根目录 | 本地动漫所在目录，多个用英文分号 `;` 分隔 |
| PotPlayer 路径 | `PotPlayerMini64.exe` |
| 小黄鸭路径 | `LosslessScaling.exe`（可选，用于自动开启插帧） |
| qBittorrent | 订阅自动下载需要；需在 qBittorrent 中开启 Web UI |
| 代理 | 可选。国内网络访问 `bgm.tv` 可能需要，见「常见问题」 |

> PotPlayer 需在「选项 → 播放 → 窗口标题」中加入播放时间/总时长字段（如 `%playtime% / %totaltime%`），否则无法自动识别播放进度。

## 截图

**海报墙** —— 封面本地缓存，断网也能出图

![海报墙](docs/screenshots/poster-wall.png)

**动态** —— 本地播放记录与 Bangumi 逐集标记混排的时间线

![动态](docs/screenshots/timeline.png)

**条目详情** —— 集列表、已看进度、播放与重新匹配

![详情页](docs/screenshots/detail.png)

**设置** —— Token / 路径 / 阈值 / 代理 / 主题

![设置](docs/screenshots/settings.png)

---

## 常见问题

### 动态页最新一条停在几天前，但 Bangumi 网页上还有更新的记录

**多数情况下这是预期行为，不是故障。** 动态页有条既定口径：

1. 只收录**动画**（`subject_type=2`）—— 特摄、日剧等三次元条目不会出现；
2. 显示条数有上限（「设置 → 动态显示条数」，默认 30），超出部分不显示；
3. 逐集记录是**按需拉取**的：从最近看过的番往后逐批拉，**凑够显示条数就停**（实测平均 8.6 条/部，所以设 40 条通常只请求 5~7 部）。抓取部数因此随数据浮动，不是 bug；
4. 候选 = 整部番状态为「看过」**或**「在看」的动画，单集状态为「看过」。追番期间产生的逐集记录**会**被收录（早期版本只收「看过」的番，导致最新记录整批丢失，已修）。

先试：把「动态显示条数」调大 → 点「刷新」。若仍不对，看状态栏的具体报错（下两条）。

### 状态栏报「Bangumi 服务端故障（连续返回 502）」

**是 Bangumi 后端的问题，不是你或本程序的问题。** 判据：502 响应带 `CF-RAY` 头（说明请求已穿过 Cloudflare 到达源站），响应体是源站 nginx 的默认错误页。此时本地网络、代理、Token 都正常，**稍后重试**即可（同期的公开接口 `/v0/subjects/{id}` 通常还是好的，可用来对照）。

### 状态栏报「连接超时 / 连接被重置（域名疑似被阻断）」

**本地到 `bgm.tv` 的链路被阻断**，典型表现是 DNS 污染（解析出 Facebook / Dropbox 等无关 IP，且每次查询都不一样）+ SNI 阻断（用真实 IP 直连时 TLS 握手被 RST）。

处理：**在「设置 → 代理」填写代理**（如 `http://127.0.0.1:7890`）。两点提醒：

- **换节点通常没用** —— 如果代理客户端的分流规则把 `bgm.tv` 判给了「直连」（它是国内域名，常被国内规则集收录），流量压根没进隧道。请确认规则里 `bgm.tv` 走代理，或临时切全局模式验证；
- 提示里会写明**当前有没有走代理**（例如「已走代理 127.0.0.1:7897，请确认它没把 bgm.tv 分流成直连」），照着排查即可。

### 封面不显示，控制台刷 `HTTP/2 protocol error`

封面图由 Qt 自己的网络栈下载（**走系统代理**，与上面「设置 → 代理」那条是两套），切换节点或重启代理客户端时，正在下载的连接会被掐断。**切一下页面或重启应用**即可恢复；不影响列表与动态数据。

### 动态页某一行点了没反应

该条目**不在本地媒体库里**（未入库）。只有已入库的条目能打开详情页；未入库的行光标是箭头，点击会提示「未入库，无法打开详情」。想让这类条目可点，先在媒体库里放入对应文件并扫描匹配。

### PotPlayer 播放进度识别不到

检查 PotPlayer 标题格式：**选项 → 播放 → 窗口标题**里必须包含播放时间与总时长（如 `%playtime% / %totaltime%`）。仍不行可在「设置 → 高级 → 标题正则」自定义解析规则。

### 扫描很慢 / 想控制请求量

扫描会对每个候选条目调 Bangumi 接口（搜索 + 拉集数）。条目多时请求数会上去，程序已内置 429 / 5xx 退避重试（指数退避 3 次）与本地缓存（收藏、集级记录、封面），重复扫描不会重复请求同一资源。

---

## 目录结构

```
main.py                 入口（run_qml.py 为兼容转发）
app/qml/                QML 界面（页面、组件、Theme 单例）
app/bridges/            QML ↔ Python 桥接层（Property / Signal / Slot）
app/core/               业务逻辑（scanner / matcher / bangumi_api / monitor /
                        launcher / database / config / rss_* / qbittorrent_api）
app/utils/              工具（paths / logger / cover_cache / title_parser / bgm_log）
resources/icons/        图标
data/                   运行时数据（SQLite 库、封面缓存、日志）—— 自动生成，不入库
```

## 打包（可选）

```cmd
pip install -r requirements-dev.txt
pyinstaller anime_marker.spec --noconfirm --clean
```

产物在 `dist/AnimeMarker/`（单目录形式，含 Qt 运行库与许可证文件）。

---

## 第三方许可

本项目以 **MIT** 发布（见 [LICENSE](LICENSE)）。运行时依赖及其许可证见 **[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)**，其中 **PySide6（LGPLv3）** 对分发有额外要求，打包时已把许可证文件一并放进产物目录。

## 声明

- 本程序与 Bangumi 官方无关，仅通过其公开 API 读取数据；**不写入**任何收藏状态。
- API 调用遵守 Bangumi 的 [User-Agent 约定](https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md)（见 `app/__init__.py` 的 `USER_AGENT`）。
- 请自行确保所管理媒体内容的来源合法。

## 参与

`main` 分支受保护，不接受直接推送。欢迎 fork 后提 Pull Request。
