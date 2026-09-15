# Anime-Marker

Windows 桌面端本地动漫管理 + Bangumi 进度同步 + PotPlayer 播放联动工具。

## 快速开始

```cmd
pip install -r requirements.txt
python main.py
```

## 首次配置

在「设置」页填写以下内容后点击「保存并扫描」：

| 配置项 | 说明 |
| --- | --- |
| **Bangumi Token** | 获取地址：**<https://next.bgm.tv/demo/access-token>**（请勾选「读取收藏」权限，否则「在看」列表无法拉取） |
| Bangumi 用户名 | 「在看」列表需要；留空则尝试用 Token 解析 |
| 媒体库根目录 | 本地动漫所在目录，多个用英文分号 `;` 分隔 |
| PotPlayer 路径 | `PotPlayerMini64.exe` |
| 小黄鸭路径 | `LosslessScaling.exe`（可选，用于自动开启插帧） |
| qBittorrent | 订阅自动下载需要；需在 qBittorrent 中开启 Web UI |

> PotPlayer 需在「选项 → 播放 → 窗口标题」中加入播放时间/总时长字段（如 `%playtime% / %totaltime%`），否则无法自动识别播放进度。

