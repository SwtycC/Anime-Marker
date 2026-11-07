"""配置读取/写入（config.ini）。

首次打开时若文件不存在，按 DEFAULTS 生成；
后续打开时：

1. **先跑版本迁移**（`core/config_migrations.py`）—— 处理"键改名 /
   挪位置 / 废弃"这类结构变化，旧值搬进新键、旧键删掉、并留一份备份；
2. **再补齐缺失的 section / option** —— 处理"新增键"。

顺序不能反：迁移要在补默认值之前，否则新键已被填成默认值，
迁移函数就分不清"用户没填"和"用户填了默认值"，无法判断该不该搬。
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from app import USER_AGENT
from app.core import config_migrations
from app.utils.paths import config_path

DEFAULTS: dict[str, dict[str, str]] = {
    # 配置自身的元信息（不由设置页读写）。
    #
    # `config_version` = 这份配置的**结构最后迁移到的版本**，迁移框架
    # 据此判断该跑哪些步骤（见 core/config_migrations.py）。它**不等于**
    # "最后打开它的软件版本"：只发版、没有结构变化时不会更新。
    # 全新安装由 `_init_defaults` 按当前版本写入，不需要在这里写死。
    #
    # **放在第一个**：写出的 config.ini 以它开头，打开文件一眼能看到
    # 这份结构属于哪个版本。`save()` 会按这个顺序重排节。
    "meta": {
        "config_version": "",
    },
    "general": {
        "library_path": "",
        # **路径类一律给空串填。
        "ls_path": "",
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
        # 「自动完结」（v15）：
        #   一部动漫的**每一集**都标为看过后，自动把该动漫也标记为
        #   「看过」（collect_type=2）；填了 Token 时同步到 Bangumi。
        # 开关只控制"要不要自动改状态"，不管集级记录 —— 那是 auto_upload。
        "auto_complete_watched": "true",
    },
    "library": {
        # 海报墙「按排名」排序的数据**多久自动刷新一次**（天）。
        #
        # 排名 = Bangumi 站内名次（`rating.rank`），会随收藏人数小幅浮动。
        # 这类变化对"我要看哪部番"几乎没有影响，7 天足够；库特别大、
        # 或特别在意榜位的人可以调小。
        #
        # **0 = 关闭自动刷新**（仍可在海报墙「排序」面板里手动点）。
        # 设置页有对应输入框（见 SettingsPage「排名刷新间隔」）。
        "rank_refresh_days": "7",
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
        "auto_download": "false",
        # 「自动轮询」总开关（设置页「自动轮询」，位于「轮询间隔」上方）：
        #   开（默认）—— 启动时立即检查一次，之后每 `poll_interval` 分钟检查；
        #   关        —— 不启动定时器，只在你点「立即检查」时抓一次。
        "poll_on_start": "true",
    },
    "scanner": {
        # 自动匹配的最低分与最小差距（调高更保守）
        "accept_score": "60",
        "accept_gap": "20",
        # 官方序号与本地编号**完全对不上**、但集数数量一致时，按排序位置
        # 一一配对并改用官方序号（scanner._align_by_order）。
        #   开（默认）：解决跨篇章连续编号
        #     （如：官方 51~58 vs 本地 [01]~[08]）的对应问题；
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
        #   开（默认）：左列显示**文件名里的原文**（`SP01`/`OVA01`/`NCOP3`/
        #     `WEB予告 #02`…，前导零保留），排序值排在正片之后；
        #   关：沿用旧行为（在正片之后顺延 13.5 / 14 / 14.5…）。
        "extra_numbering": "true",
    },
    # 播放器。
    #
    # 路径与**该播放器自己的**全屏快捷键放在一起：全屏键由播放器定义
    # （PotPlayer 是 alt+enter），配错的表现是"按了没反应 → 插帧一直不开"。
    #
    # 目前只支持 PotPlayer（进度读取绑死了它的接口），所以只有一组值；
    # 将来支持多播放器时再加"档案名"这一层，现在不预留。
    #
    # 首次使用时由设置页「浏览…」选定，空串会被
    # `PlayerLauncher._check_exists` 拦成「路径未配置」。
    "player": {
        "path": "",
        "fullscreen": "",
    },
    "launcher": {
        "enable_ls": "true",
        # **必须与小黄鸭「设置 → 缩放快捷键」里的值一致**，否则按了也没反应。
        # 初始空串：没配小黄鸭时不往系统里发任何热键。
        "ls_shortcut": "",
        # 全屏后、发插帧键前的额外等待（秒）。PotPlayer 达到全屏尺寸后可能
        # 仍在切换渲染模式，太早发插帧键会让小黄鸭的捕获失效 
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
    "update": {
        # 下面两笔账**都由程序自己写，不用手改**（见 update_check 里那段说明）。
        #
        # 「今天」那次自动检查的记账日期（`YYYY-MM-DD`，本地时区）。
        # 启动时那次静默检查只在它与今天不相等时才发出，发完立刻写成今天
        # —— 也就是"每天只在第一次开软件时自动查一次"。想手动查不受影响，
        # 状态栏「更多 → 检查更新」随时可点。
        # 删掉它 = 下次开软件重新自动查一次。
        "last_auto_check": "",
        # 上次查到**确实比本地新**的版本号（`v1.2.3`）。
        #
        # 为什么要落盘：小红点得"有更新就一直亮着"。今天已经查过、或者这
        # 次断网查失败时，程序靠这笔记账把红点直接还原出来，不然同一天重开
        # 一次软件点就没了（见 update_check.load_known_newer）。
        # 装上新版后本地版本一涨，同一个 tag 自然不再算"新"，红点自己灭。
        "latest_tag": "",
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
        # 界面字体族名（QFontDatabase 返回的 family）。
        #
        # **空串 = 系统默认**（Windows 上是「微软雅黑 UI」之类）。
        # 内置「霞鹜文楷 GB 轻便版」的族名由 FontBridge 在运行时查出，
        # 这里不写死 —— 字体文件的内部族名若变化（换版本）也不会失配。
        # 用户上传的字体存族名而不是文件路径：Qt 的 font.family 只认族名。
        "font_family": "",
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
            # ① 结构迁移（改名/搬迁/废弃）必须先于 ② 补默认值，
            #    见模块 docstring
            if config_migrations.run(self):
                self.save()
            self._ensure_sections()

    # ---------- 初始化 ----------
    def _init_defaults(self) -> None:
        for section, kv in DEFAULTS.items():
            self._parser[section] = dict(kv)
        # 全新配置直接记成当前版本：没有旧结构要迁，也就不需要备份
        config_migrations.mark_current(self)
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

    # `has_option` / `remove_option` 是给 config_migrations 用的 ——
    # 迁移函数只该调 Config 的方法，不该直接摸 `self._parser`（实现细节）。
    def has_option(self, section: str, key: str) -> bool:
        return self._parser.has_option(section, key)

    def has_section(self, section: str) -> bool:
        return self._parser.has_section(section)

    def remove_option(self, section: str, key: str) -> bool:
        """删掉一项；不存在返回 False（**不抛异常**，迁移里可放心调）。"""
        if not self._parser.has_option(section, key):
            return False
        return self._parser.remove_option(section, key)

    def save(self) -> None:
        self._reorder_sections()
        with self.path.open("w", encoding="utf-8") as f:
            self._parser.write(f)

    def _reorder_sections(self) -> None:
        """按 `DEFAULTS` 的键顺序重排节，让文件打开时层次稳定。

        **为什么需要**：`configparser` 把节存在 dict 里，`set` / 新增节
        一律**追加到末尾**。因此 `meta` 即便是 `DEFAULTS` 的第一项，
        老用户盘上那份也会因为它"早已存在"而留在文件末尾（`[meta]` 在
        最下面是第 74 行那种观感）。这里在每次写盘前统一按 `DEFAULTS`
        的顺序摆好，新旧配置就都从 `[meta]` 开始了。

        不在 `DEFAULTS` 里的节（用户手加的、或早期版本遗留的）保持原有
        相对顺序，统一排在后面 —— 不能丢，丢了等于吃掉用户的配置。
        """
        known = [s for s in DEFAULTS if self._parser.has_section(s)]
        extra = [s for s in self._parser.sections() if s not in DEFAULTS]
        order = known + extra
        if order == self._parser.sections():
            return                  # 已经是期望顺序，省掉一次重建

        # ConfigParser 没有"移动节"的接口，只能按目标顺序重建一份
        old = self._parser
        new = configparser.ConfigParser()
        # 保留大小写选项（与 old 一致）与非严格解析行为
        new.optionxform = old.optionxform
        new.defaults().clear()
        for section in order:
            new.add_section(section)
            for key, value in old.items(section, raw=True):
                new.set(section, key, value)
        self._parser = new

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
