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
- 条目详情页：集列表、已看进度、播放、重新匹配

**播放联动**

- 用 PotPlayer 打开（外部进程，不内置播放器）
- 可联动 Lossless Scaling（小黄鸭）自动开插帧
- 通过 PotPlayer 原生接口读播放位置（不依赖窗口标题），到阈值（默认 95%）自动记录并同步

**Bangumi 集成**

- 在看页：列出正在追的番（整部番状态为「在看」），与本地区目关联；
  点行进详情、看「下一集」、把本地记录「上传」到 Bangumi
- 动态页：**逐集观看时间线** —— 本地播放记录（tag `本地`）与 Bangumi 逐集标记（tag `bgm`）混排，
  同一集两边都有时合并成一行、两个 tag 同时显示；按天分组；覆盖**全部收藏状态**，向下滚动可加载更多
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
| 系统 | Windows 10 / 11（依赖 `win32gui` 读播放器窗口与播放位置）|
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

### 关于「看完自动标记」

**不需要任何 PotPlayer 设置** —— 程序通过 PotPlayer 的原生接口直接读播放位置
（`WM_USER` 消息，毫秒级），到阈值（默认 95%）就记录：

1. **先写本地**：这一集立刻进入「动态」页，来源标记为 `本地`；
2. **再同步 Bangumi**：成功则该行同时带上 `bgm` 标记（两个 tag = 已同步）；
   失败不影响本地记录，之后可以在「动态」页点「上传」补传。

> 早期版本读的是 PotPlayer **窗口标题**里的播放时间，但实测（2026-09）该版本
> 并不会把时间放进标题（`F5 → 基本 → 消息 → 在屏幕上显示播放信息` 只影响画面上的
> OSD ✗），于是自动标记从未生效 —— 现已改用原生接口。

**「设置 → Bangumi → 自动上传」**控制第 2 步：

- **开（默认）**：看完立刻同步，动态页马上出现 `bgm` 标记；
- **关**：只记本地（来源 tag 只有「本地」），什么时候上传由你用
  「动态 → 上传」小窗决定 —— **关掉不会丢记录**，只是延迟同步。

可选的显示设置（与自动标记无关，纯粹看着方便）：`F5 → 基本 → 消息` →
勾选「在屏幕上显示播放信息」→「播放时间」选「播放时间 / 全部时间」，
画面上会出现 `00:12:34 / 00:24:00 (53%)` 的叠加信息。

### 把本地观看记录上传到 Bangumi

**动态页右上角「上传」**（在「仅本地」左边）→ 弹出小窗，列出**本地看过、
Bangumi 还没标**的动漫，勾选后批量补传（也有「全选」）：

- **幂等**：只传差集，重复点不会重复提交；上次失败的那些再点一次也只补它们；
- 本地看过、但**缺少 Bangumi 集号**的集无法补传（扫描时没拿到集数元数据），
  小窗里会逐条标出"另有 N 集缺 Bangumi 集号，会跳过"；
- 传完会自动让这些条目的集级记录重新同步一次，动态页里对应的行立刻会多出
  `bgm` 标记（原来是只有「本地」tag）。

### 让小黄鸭（Lossless Scaling）自动开插帧

用本程序播放时联动补帧，下面四条要**同时**满足，缺一条就会出现"小黄鸭起来了但不补帧"：

**1. 把小黄鸭的配置绑到 PotPlayer**（最关键、最容易漏）

小黄鸭左侧 `+` 新建配置（或选中现有配置点铅笔编辑）→ 在 **「筛选」** 里点「浏览」→ **选 `PotPlayerMini64.exe`** → 保存。

> 小黄鸭是**按"跑起来的是哪个 exe"匹配配置**的（不是窗口标题、也不是视频文件）。「筛选」留空或指错，PotPlayer 就永远只会用「默认」那条配置。
> **验证**：播放时看小黄鸭窗口底部状态栏的 `配置:` 一行 —— 显示你那条配置的名字 = 生效；显示 `"默认"` = 没匹配上。

**2. 该配置里把「帧生成」打开**

「帧生成 → 类型」不能是「关」（新建的配置常常是关的）；只补帧的话「缩放」可以保持关。
「捕获 → 捕获 API」保持 `DXGI`，「光标 → 限制光标」建议开。

**3. 快捷键两边一致**

小黄鸭「设置 → 缩放快捷键」的值，要和应用「设置 → 插帧快捷键」相同。

> ⚠️ **不要用 `Ctrl+Alt+L`** —— 那是 **QQ 的「锁定 QQ」全局快捷键**：会被 QQ 抢注（小黄鸭也设不上），而本程序照发就会**把 QQ 锁掉**。
> 不想依赖快捷键的话，可以在新建配置时打开 **「自动缩放」**，该程序运行时小黄鸭会自动应用配置。

**4. PotPlayer 用 D3D11 类视频渲染器**

`F5 → 视频 → 视频渲染器`。小黄鸭靠 DXGI 抓画面，其他渲染器可能抓不到。

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

更新中

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

- 本程序与 Bangumi 官方无关，通过其公开 API 读写你自己的观看记录：
  **只会把「你看完的集」标记为看过**（看完自动同步，或用「上传」补传），
  不修改收藏状态、评分、吐槽等任何其他内容。
- API 调用遵守 Bangumi 的 [User-Agent 约定](https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md)（见 `app/__init__.py` 的 `USER_AGENT`）。
- 请自行确保所管理媒体内容的来源合法。

## 参与

`main` 分支受保护，不接受直接推送。欢迎 fork 后提 Pull Request。
