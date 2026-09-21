"""PotPlayer 进度监控器。

- QTimer 定时轮询 **PotPlayer 原生接口**（WM_USER 消息）拿播放位置
- 解析进度 → 落库 → 达阈值自动标记（**先写本地，再推 Bangumi**）
- 内存标志位 + 数据库 watched 字段双重防重复

> **进度来源变更（2026-09，实测）**：早期读的是 PotPlayer **窗口标题**里的时间对，
> 但该版本**根本不会把播放时间放进标题**（`F5 → 基本 → 消息 → 在屏幕上显示播放信息`
> 只影响画面上的 OSD ✗），于是恒解析失败 —— `watch_log` 恒为空、`progress` 恒为 0、
> 「看完自动标记」从未生效过。现改用 PotPlayer 暴露的 WM_USER 消息接口：
> 不需要任何 PotPlayer 设置、毫秒级精确。标题解析保留为兜底（换播放器/版本时可能用上）。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

import win32con
import win32gui

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.database import Database, Episode
from app.utils.title_parser import parse_progress

log = logging.getLogger(__name__)

# PotPlayer 的播放信息接口：`SendMessage(hwnd, WM_USER, <常量>, 0)`
#
# 实测（PotPlayer64，2026-09）：
#   0x5002 → 总时长（ms），0x5004 → 当前位置（ms），与画面上显示的时间一致 ✓
#   0x5001（播放状态）在该版本**恒返回 0，不可用** ✗ —— 因此判定"是否在播放"
#   改用"位置有没有推进"（见 _tick），顺带能防住"把进度条拖到结尾被误判为看完"。
#
# 参考：https://deepwiki.com/kavinthangavel/media-player-scrobbler-for-simkl/2.3-media-player-integrations
PPM_GET_TOTAL_TIME_MS = 0x5002
PPM_GET_PLAYBACK_TIME_MS = 0x5004


def find_potplayer_hwnd() -> Optional[int]:
    """枚举桌面窗口，找到 PotPlayer 主窗口句柄。"""
    result: list[int] = []

    def _enum(hwnd: int, _) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if "PotPlayer" in cls or "PotPlayer" in title:
                result.append(hwnd)
        return True

    win32gui.EnumWindows(_enum, None)
    return result[0] if result else None


def get_window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def query_playback(hwnd: int) -> Optional[tuple[int, int]]:
    """问 PotPlayer 要 (当前位置ms, 总时长ms)；接口不可用时返回 None。

    走 WM_USER 消息（见文件顶部常量说明），**不需要任何 PotPlayer 设置**。
    返回 0 / 负数一律视为"这个版本不支持/还没开始播"，由调用方回退标题解析。
    """
    try:
        total = win32gui.SendMessage(hwnd, win32con.WM_USER,
                                     PPM_GET_TOTAL_TIME_MS, 0)
        pos = win32gui.SendMessage(hwnd, win32con.WM_USER,
                                   PPM_GET_PLAYBACK_TIME_MS, 0)
    except Exception as e:      # pragma: no cover - 防御性
        log.debug("查询 PotPlayer 播放位置失败: %s", e)
        return None
    if total and pos is not None and int(total) > 0 and int(pos) >= 0:
        return int(pos), int(total)
    return None


class ProgressMonitor(QObject):
    """QTimer 驱动的进度监控器。"""

    progress_changed = Signal(int, float)   # episode_id, progress
    watched = Signal(int)                   # episode_id
    error = Signal(str)

    # 连续多少次读不到进度才提示一次（按默认 3s 轮询 ≈ 15 秒）
    MISS_HINT_AFTER = 5

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        poll_interval: int = 3,
        trigger_threshold: float = 0.95,
        title_regex: str = "",
        auto_upload: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.api = api
        # 看完是否**立即**同步到 Bangumi（配置 `bangumi.auto_upload`）。
        # 关掉只影响"立刻传"这一步：本地记录照写，之后可用「动态 → 上传」补传。
        self.auto_upload = bool(auto_upload)
        self.threshold = trigger_threshold
        self.title_regex = title_regex or None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, poll_interval) * 1000)
        self._timer.timeout.connect(self._tick)
        self._episode: Optional[Episode] = None
        self._triggered: set[int] = set()
        # 上一轮读到的播放位置（ms）—— 用来判断"位置有没有在推进"
        self._last_pos: Optional[int] = None
        # 连续读不到进度的次数（用于"静默失败"时给一次提示）
        self._misses = 0

    def start(self, episode: Episode) -> None:
        self._episode = episode
        self._triggered.discard(episode.id)
        self._last_pos = None
        self._misses = 0
        self._timer.start()
        log.info("开始监控 episode_id=%s file=%s", episode.id, episode.file_path)

    def stop(self) -> None:
        self._timer.stop()
        self._episode = None
        log.info("停止监控")

    # ---------- 内部 ----------
    def _tick(self) -> None:
        ep = self._episode
        if ep is None:
            self.stop()
            return
        hwnd = find_potplayer_hwnd()
        if hwnd is None:
            log.info("PotPlayer 窗口消失，停止监控")
            self.stop()
            return

        progress, advancing = self._read_progress(hwnd)
        if progress is None:
            self._note_miss()
            return  # 两个来源都没拿到，等下次轮询
        self._misses = 0

        try:
            self.db.update_progress(ep.id, progress)
        except Exception as e:
            log.warning("进度落库失败: %s", e)

        self.progress_changed.emit(ep.id, progress)

        # 阈值判定：**必须"位置在推进"**才算看完。
        #
        # 防的是"**暂停在 98% 一直不动**"被算成看完（人没看完就离开时最常见）；
        # 注意它拦不住"手动把进度条拖到结尾"—— 拖动同样是一次位置推进，而且
        # 那本就是个明确动作（旧实现同样会算）。真要区分得看"单次跳变幅度"，
        # 目前不做。
        if (progress >= self.threshold and advancing
                and ep.id not in self._triggered and not ep.watched):
            self._trigger_watched(ep)

    def _read_progress(self, hwnd: int) -> tuple[Optional[float], bool]:
        """读一次进度 → (progress 或 None, 位置是否在推进)。

        两级来源：
          ① PotPlayer 原生接口（首选，毫秒级）—— 能判断"位置在推进"：暂停不动、
             拖进度条只会跳一次，因此只有**真的在往下播**才会满足推进条件；
          ② 窗口标题里的时间对（兜底）—— 拿不到位置，退化为不做推进判定。
        """
        got = query_playback(hwnd)
        if got is not None:
            pos, total = got
            advancing = self._last_pos is not None and pos > self._last_pos
            self._last_pos = pos
            return pos / total, advancing
        return parse_progress(get_window_title(hwnd), self.title_regex), True

    def _note_miss(self) -> None:
        """连续读不到进度时**提示一次**。

        **为什么必须提示**：这两个来源的失败完全不报错（早期连日志都没有），
        实测排查时只能靠"数据库里 progress 恒为 0"倒推 ✗ —— 用户则会以为
        是自己没设置对。提示一次即可，不刷屏。
        """
        self._misses += 1
        if self._misses == self.MISS_HINT_AFTER:
            log.warning("连续 %s 次读不到播放进度（原生接口与标题都失败）",
                        self._misses)
            self.error.emit(
                "读不到 PotPlayer 的播放进度，自动标记暂不可用"
                "（可在条目详情页手动标记看过）")

    def _trigger_watched(self, ep: Episode) -> None:
        """看完一集：**先写本地，再推 Bangumi**（顺序不能反）。

        **踩坑（顺序反了会怎样）**：早期是"先 POST Bangumi，成功才写本地" ——
        一次网络抖动就等于"这集没看过" ✗；而本地记录正是「上传」功能的数据源，
        丢了就得重看一遍才能补回来。
        现在两者解耦：**本地记录 = "我看过"，Bangumi 标记 = "同步成功"** ——
        同步失败只提示，事后用动态页/在看页的「上传」补齐即可。
        """
        # ---- 1. 本地先落袋（不依赖网络，也不依赖有没有匹配到 Bangumi）----
        try:
            self.db.mark_watched(ep.id)
        except Exception as e:
            log.exception("写本地观看记录失败 episode_id=%s: %s", ep.id, e)
            return
        self._triggered.add(ep.id)
        self.watched.emit(ep.id)
        log.info("已记录本地看过 episode_id=%s（第 %s 集）", ep.id, ep.ep_index)

        # ---- 2. 再同步到 Bangumi（失败不影响本地记录）----
        if not ep.bangumi_ep_id:
            # 实测本机 1446 集里有 455 集属于这种情况（扫描时没拿到集数元数据），
            # 它们没法同步；「上传」会把这批跳过项一并列出来告诉用户
            log.warning("episode_id=%s 无 bangumi_ep_id，跳过同步（本地已记录）", ep.id)
            return
        # **必须换成 Bangumi 条目 ID**：`Episode.subject_id` 是本地
        # `subjects.id`（外键），而 `mark_episode_watched` 要的是 Bangumi 的
        # `subject_id` —— 两者毫无关系（实测本机 72 个条目里没有一个相等：
        # 本地 id=2 对应 bangumi_id=302189）。早期直接传 `ep.subject_id`，
        # 于是每次自动标记都 POST 到"另一个条目"上，必然 400/404 失败，
        # 表现为「看完自动标记」从来没成功过（`episodes.watched` 全 0、
        # watch_log 为空），而不是网络问题。
        subject = self.db.get_subject(ep.subject_id)
        if subject is None or not subject.bangumi_id:
            log.warning("episode_id=%s 的条目未匹配 Bangumi（本地 id=%s），跳过同步",
                        ep.id, ep.subject_id)
            return
        if not self.auto_upload:
            # 关掉自动上传：只留本地记录，等用户在小窗里勾选上传
            log.info("自动上传已关闭，episode_id=%s 仅记录本地（可在动态页「上传」补传）", ep.id)
            return
        try:
            self.api.mark_episode_watched(subject.bangumi_id, ep.bangumi_ep_id)
        except BangumiError as e:
            log.warning("同步到 Bangumi 失败（本地记录已保留，可用「上传」补齐）: %s", e)
            self.error.emit(f"Bangumi 标记失败（本地已记录，可用「上传」补齐）：{e}")
            return
        log.info("已同步到 Bangumi：episode_id=%s（条目 %s 第 %s 集）",
                 ep.id, subject.bangumi_id, ep.ep_index)
        # 让这条目的集级记录下次刷新时重新同步，否则刚标的这一集要等
        # 收藏行变化或 30 天超期兜底才会出现在动态页（见 sync_candidates）
        try:
            self.db.clear_ep_sync_state_for(int(subject.bangumi_id))
        except Exception as e:
            log.warning("让条目 %s 的集级记录重新同步失败（不影响标记）: %s",
                        subject.bangumi_id, e)
