"""QML 桥接层（§5.12.6）。

每个 bridge 负责把一类后端能力翻译成 QML 可调用的形式：

| 文件 | 职责 |
| --- | --- |
| `library.py`  | 媒体库查询（条目 / 集数 / 时间线 / 在看 / 统计） |
| `scanner.py`  | 媒体库扫描（QThread 信号转发） |
| `settings.py` | 配置读写（Token / 路径 / 阈值 / 下载规则）+ 文件对话框 |
| `player.py`   | 播放集数 + PotPlayer 进度监控 + 自动标记看过 |
| `match.py`    | 手动匹配（搜索候选 + 打分排序 + 应用） |
| `inprogress.py` | 在看列表（Bangumi 拉取 + 离线缓存降级） |
| `rss.py`      | RSS 订阅源管理 + 下载记录查询 |

约定：
- 对外只用 `@Slot` / `@Property` / `Signal`，不暴露 ORM 对象
- 返回值一律是 QML 能理解的基本类型（dict / list / str / int / float / bool）
- 耗时操作走 QThread，用 Signal 回主线程
"""

from app.bridges.inprogress import InProgressBridge
from app.bridges.library import LibraryBridge, as_file_url
from app.bridges.match import MatchBridge
from app.bridges.player import PlayerBridge
from app.bridges.rss import RssBridge
from app.bridges.scanner import ScannerBridge
from app.bridges.settings import SettingsBridge

__all__ = [
    "LibraryBridge",
    "ScannerBridge",
    "SettingsBridge",
    "PlayerBridge",
    "MatchBridge",
    "InProgressBridge",
    "RssBridge",
    "as_file_url",
]
