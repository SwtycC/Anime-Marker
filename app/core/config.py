"""配置读取/写入（config.ini）。

首次打开时若文件不存在，按 DEFAULTS 生成；
后续打开时补齐缺失的 section / option，便于版本升级。
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from app import USER_AGENT
from app.utils.paths import config_path

DEFAULTS: dict[str, dict[str, str]] = {
    "general": {
        "library_path": "",
        "player_path": r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        "ls_path": r"C:\Program Files\Lossless Scaling\LosslessScaling.exe",
    },
    "bangumi": {
        "token": "",
        "username": "",
        "api_base": "https://api.bgm.tv",
        "proxy": "",
        "user_agent": USER_AGENT,   # 合规 UA，见 app/__init__.py
        "inprogress_cache_ttl": "300",
        # 「动态」页的**首屏显示条数**（F20）。
        #
        # 只影响显示：动态页先渲染最近 N 条，向下滚动或点「加载更多」每次
        # 追加一页（一页 = max(50, N) 条）。
        #
        # **与同步范围无关**：逐集记录是"全量同步一次 + 之后增量更新"
        # （该重拉哪几部由"水位数 vs 当前收藏状态"决定，见 sync_candidates），
        # 所以改这个值既不会多发请求，也不会删掉已同步的数据。
        # 早期它兼作抓取范围（"凑够 N 条即停"），代价是排在后面的上百部番
        # 永远拉不到 —— 已废弃。
        # 0 = 关闭：动态页不再显示 Bangumi 逐集记录（本地播放记录照常显示，
        # 已同步的数据保留，调回非 0 立刻恢复显示）。
        # 设置页用步进 ±5 的加减按钮调整。
        "ep_timeline_count": "30",
        # 看完一集后**立即同步到 Bangumi**（F21）。
        #   开（默认）：本地记一笔 → 立刻 POST 标记该集看过，动态页马上显示 bgm 标记；
        #   关：只写本地（来源 tag 只有「本地」），什么时候上传由你在
        #       「动态 → 上传」小窗里决定（差集与幂等由那边保证）。
        # 两种情况下**本地记录都会先落袋** —— 关掉自动上传不会丢记录。
        "auto_upload": "true",
    },
    "qbittorrent": {
        "host": "127.0.0.1",
        "port": "8080",
        "username": "admin",
        "password": "",
        "category": "Bangumi",
        "save_path": "",
        "webui_url": "",
        # qBittorrent 主程序路径。
        #
        # **为什么需要它**（实测诉求："在其退出但需要时打开"）：
        # qBittorrent 的 GUI 与 Web UI 是**同一个进程** —— 用户从托盘
        # 「退出」后 Web UI 一起消失，本程序就再也连不上，新集只能落成
        # 「待确认」。有了路径，就能在下发前**按需把它拉起来**
        # （见 QbClient.ensure_running）。
        #
        # 留空 = 不自动启动（保持旧行为，只在下发失败时给出提示）。
        "exe_path": "",
        # 连不上时是否自动启动 qBittorrent。
        # 默认关：启动外部程序属于"有副作用"的操作，让用户显式打开
        # （与 launcher 的「播放前自动启动小黄鸭」同一个取舍）。
        "auto_start": "false",
    },
    "rss": {
        "poll_interval": "30",
        "rule": "new_only",
        "auto_download": "false",
        "poll_on_start": "true",
    },
    "scanner": {
        # 自动匹配的最低分与最小差距（调高更保守）
        "accept_score": "60",
        "accept_gap": "20",
        # 官方序号与本地编号**完全对不上**、但集数数量一致时，按排序位置
        # 一一配对并改用官方序号（scanner._align_by_order）。
        #   开（默认）：解决「Re:零 第三季」这类跨篇章连续编号
        #     （官方 51~58 vs 本地 [01]~[08]）的对应问题；
        #   关：不做顺序配对，对不上号的集保持本地编号、标题用文件名。
        # 顺序配对本质是"数量一致下的猜测"，没有按号匹配可靠 —— 命中时
        # 详情页会弹黄色提示；介意误配可关。
        "ep_align_order": "true",
        # 附加内容（SP / OVA / NCOP / 特典 / 予告…）**是否入库并显示**。
        #
        # 这个开关在最外层：关掉后这些文件在集数列表里**完全消失**，
        # 播放记录 / 进度统计也不再考虑它们 —— 适合"只想看正片"的用户。
        #   开（默认）：识别出的附加内容照常入库、排在正片之后；
        #   关：整条附加内容流程短路（连下面的 extra_numbering 也不必看）。
        # 因此设置页里「附加内容独立编号」在它关闭时是**置灰的** ——
        # 外层都关了，里层的编号方式自然无从谈起。
        "extra_show": "true",
        # 附加内容（SP / OVA / NCOP / 特典 / 予告…）的编号方式。
        #
        # 背景（实测）：这些内容既不是正片、Bangumi 的 `/v0/episodes` 也
        # **不返回**它们（`type=1` 查询返回 0 条，`total` 也不含），所以
        # 标题拿不到官方数据、只能用文件名。但至少该给它们**正确的显示
        # 标签**：早期它们完全不被识别，落进兜底桶被顺延成「13.5 集」
        # 「14 集」这种假集号，与正片混在同一序列里（实测截图反馈）。
        #   开（默认）：左列显示**文件名里的原文**（`SP01`/`OVA01`/`NCOP3`/
        #     `WEB予告 #02`…，前导零保留），排序值排在正片之后；
        #   关：沿用旧行为（在正片之后顺延 13.5 / 14 / 14.5…）。
        "extra_numbering": "true",
    },
    "launcher": {
        "enable_ls": "true",
        # **必须与小黄鸭「设置 → 缩放快捷键」里的值一致**，否则按了也没反应。
        # 别用 ctrl+alt+l —— 那是 QQ 的「锁定 QQ」全局快捷键，会被 QQ
        # 抢先注册（小黄鸭也设不上），实测触发过一次 QQ 自我锁定。
        # 小黄鸭各版本默认不同，以你机器上的设置为准。
        "ls_shortcut": "ctrl+alt+p",
        # PotPlayer 的「全屏/还原」快捷键（默认 alt+enter）。
        #
        # **为什么需要全屏**（实测）：小黄鸭的 WGC 捕获按"捕获那一刻的窗口
        # 尺寸"锁定输出尺寸。窗口化/最大化时都抓不对 —— 窗口化会画面缩小
        # + 四周黑边，最大化则**帧数极不稳定甚至只有几帧**。
        # 实测只有**真全屏**（覆盖任务栏）才能拿到正常帧数。
        # 若你在 PotPlayer 里改过全屏快捷键，这里要填成一致的值。
        "fullscreen_shortcut": "alt+enter",
        # 全屏后、发插帧键前的额外等待（秒）。PotPlayer 达到全屏尺寸后可能
        # 仍在切换渲染模式，太早发插帧键会让小黄鸭的捕获失效 ——
        # 实测症状："全屏后有帧数，一开始播放就没了"（因为渲染模式切换
        # 让捕获失效）。**实测 1.0s 不够、3.0s 稳定**，故取 3.0 为默认。
        # 更快的机器可调小、更慢的调大（设置页「全屏后等待」）。
        "fullscreen_settle": "3.0",
        "ls_start_delay": "5",
        "player_start_delay": "1",
    },
    "monitor": {
        "poll_interval": "3",
        # 看完一集的判定阈值（播放进度达到多少算看完，0~1）。
        "trigger_threshold": "0.90",
        "title_regex": "",
    },
    "ui": {
        "poster_width": "200",
        # 初始窗口恰好容纳的列数（QmlApp._fit_window_to_columns 据此反推窗口宽度）
        "poster_columns": "5",
        # QML 界面主题（§5.12）：
        #   theme_mode   = light / dark
        #   accent_color = 主题色（#RRGGBB），设置页可切换
        "theme_mode": "light",
        "accent_color": "#2F6FEB",
    },
}


class Config:
    """INI 配置封装。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_path()
        self._parser = configparser.ConfigParser()
        if not self.path.exists():
            self._init_defaults()
        else:
            self._parser.read(self.path, encoding="utf-8")
            self._ensure_sections()

    # ---------- 初始化 ----------
    def _init_defaults(self) -> None:
        for section, kv in DEFAULTS.items():
            self._parser[section] = dict(kv)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.save()

    def _ensure_sections(self) -> None:
        changed = False
        for section, kv in DEFAULTS.items():
            if not self._parser.has_section(section):
                self._parser.add_section(section)
                changed = True
            for k, v in kv.items():
                if not self._parser.has_option(section, k):
                    self._parser.set(section, k, v)
                    changed = True
        if changed:
            self.save()

    # ---------- 通用读写 ----------
    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._parser.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback: int = 0) -> int:
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback: float = 0.0) -> float:
        return self._parser.getfloat(section, key, fallback=fallback)

    def getbool(self, section: str, key: str, fallback: bool = False) -> bool:
        return self._parser.getboolean(section, key, fallback=fallback)

    def set(self, section: str, key: str, value: Any) -> None:
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, key, str(value))

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            self._parser.write(f)

    # ---------- 便捷访问 ----------
    @property
    def library_paths(self) -> list[Path]:
        raw = self.get("general", "library_path", "")
        return [Path(p.strip()) for p in raw.split(";") if p.strip()]

    @property
    def bangumi_token(self) -> str:
        return self.get("bangumi", "token", "")

    @property
    def poll_interval(self) -> int:
        return self.getint("monitor", "poll_interval", 3)

    @property
    def trigger_threshold(self) -> float:
        return self.getfloat("monitor", "trigger_threshold", 0.90)
