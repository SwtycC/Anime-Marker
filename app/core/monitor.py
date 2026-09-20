"""PotPlayer 进度监控器。

- QTimer 定时轮询窗口标题
- 解析进度 → 落库 → 达阈值自动标记 Bangumi
- 内存标志位 + 数据库 watched 字段双重防重复
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

import win32gui

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.database import Database, Episode
from app.utils.title_parser import parse_progress

log = logging.getLogger(__name__)


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


class ProgressMonitor(QObject):
    """QTimer 驱动的进度监控器。"""

    progress_changed = Signal(int, float)   # episode_id, progress
    watched = Signal(int)                   # episode_id
    error = Signal(str)

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        poll_interval: int = 3,
        trigger_threshold: float = 0.95,
        title_regex: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.api = api
        self.threshold = trigger_threshold
        self.title_regex = title_regex or None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, poll_interval) * 1000)
        self._timer.timeout.connect(self._tick)
        self._episode: Optional[Episode] = None
        self._triggered: set[int] = set()

    def start(self, episode: Episode) -> None:
        self._episode = episode
        self._triggered.discard(episode.id)
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

        title = get_window_title(hwnd)
        progress = parse_progress(title, self.title_regex)
        if progress is None:
            return  # 标题未包含时间，等下次轮询

        try:
            self.db.update_progress(ep.id, progress)
        except Exception as e:
            log.warning("进度落库失败: %s", e)

        self.progress_changed.emit(ep.id, progress)

        if progress >= self.threshold and ep.id not in self._triggered and not ep.watched:
            self._trigger_watched(ep)

    def _trigger_watched(self, ep: Episode) -> None:
        if not ep.bangumi_ep_id:
            log.warning("episode_id=%s 无 bangumi_ep_id，跳过自动标记", ep.id)
            self._triggered.add(ep.id)
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
            log.warning("episode_id=%s 的条目未匹配 Bangumi（本地 id=%s），"
                        "跳过自动标记", ep.id, ep.subject_id)
            self._triggered.add(ep.id)
            return
        try:
            self.api.mark_episode_watched(subject.bangumi_id, ep.bangumi_ep_id)
            self.db.mark_watched(ep.id)
            # 让这条目的集级记录下次刷新时重新同步，否则刚标的这一集要等
            # 收藏行变化或 30 天超期兜底才会出现在动态页（见 sync_candidates）
            try:
                self.db.clear_ep_sync_state_for(int(subject.bangumi_id))
            except Exception as e:
                log.warning("让条目 %s 的集级记录重新同步失败（不影响标记）: %s",
                            subject.bangumi_id, e)
            self._triggered.add(ep.id)
            self.watched.emit(ep.id)
            log.info("已自动标记 episode_id=%s（Bangumi 条目 %s 第 %s 集）",
                     ep.id, subject.bangumi_id, ep.ep_index)
        except BangumiError as e:
            log.warning("标记失败，下次轮询重试: %s", e)
            self.error.emit(f"Bangumi 标记失败：{e}")
