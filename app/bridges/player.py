"""播放与进度监控桥接。

职责：
1. 按 episode_id 播放本地视频（含可选的小黄鸭插帧，走 `core/launcher.py`）
2. 播放期间用 `core/monitor.py` 轮询 PotPlayer 窗口标题，解析进度
3. 进度达阈值时自动标记 Bangumi「看过」，并通知 QML 刷新

**QML 不能直接持有 QTimer / win32gui 资源**，因此这里做一层包装：
- `playEpisode()` 是同步 Slot（启动进程很快，不阻塞）
- 进度通过 `progressChanged` 信号回传，QML 绑定进度条
- `watchedChanged` 通知详情页重绘集数列表

与旧版 `main_window._play_episode` 的行为保持一致：
找不到集数 → 发 failed；启动器失败 → 发 failed；成功则启动 monitor。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database, Episode
from app.core.launcher import LauncherError, PlayerLauncher
from app.core.monitor import ProgressMonitor

log = logging.getLogger(__name__)


class PlayerBridge(QObject):
    """播放 + 监控控制器。"""

    # ---- 状态 ----
    playingChanged = Signal()
    progressChanged = Signal(int, float)      # episode_id, progress(0~1)
    watched = Signal(int)                     # episode_id（自动标记成功）
    message = Signal(str)                     # 状态栏文字
    failed = Signal(str)                      # 播放失败

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._api = api

        self._playing_episode_id = 0
        self._playing_title = ""
        self._progress = 0.0

        self._monitor = ProgressMonitor(
            db=db,
            api=api,
            poll_interval=config.getint("monitor", "poll_interval", 3),
            trigger_threshold=config.getfloat("monitor", "trigger_threshold", 0.95),
            title_regex=config.get("monitor", "title_regex", ""),
            auto_upload=config.getbool("bangumi", "auto_upload", True),
            parent=self,
        )
        self._monitor.progress_changed.connect(self._on_progress)
        self._monitor.watched.connect(self._on_watched)
        self._monitor.error.connect(self.failed)

    def set_api(self, api: BangumiClient) -> None:
        """设置保存后重建 API 时调用。"""
        self._api = api
        self._monitor.api = api

    # ---------- 状态属性 ----------
    @Property(int, notify=playingChanged)
    def playingEpisodeId(self) -> int:
        return self._playing_episode_id

    @Property(str, notify=playingChanged)
    def playingTitle(self) -> str:
        return self._playing_title

    @Property(bool, notify=playingChanged)
    def playing(self) -> bool:
        return self._playing_episode_id != 0

    @Property(float, notify=progressChanged)
    def progress(self) -> float:
        """当前播放进度 0~1。"""
        return self._progress

    # ---------- 动作 ----------
    @Slot(int, result=bool)
    def playEpisode(self, episode_id: int) -> bool:
        """播放指定集数。返回是否成功启动。"""
        ep = self._find_episode(episode_id)
        if ep is None:
            self.failed.emit("未找到该集")
            return False

        launcher = PlayerLauncher(
            player_path=self._config.get("general", "player_path", ""),
            ls_path=self._config.get("general", "ls_path", ""),
            enable_ls=self._config.getbool("launcher", "enable_ls", True),
            ls_shortcut=self._config.get("launcher", "ls_shortcut", "ctrl+alt+p"),
            ls_start_delay=self._config.getfloat("launcher", "ls_start_delay", 5.0),
            player_start_delay=self._config.getfloat(
                "launcher", "player_start_delay", 1.0),
        )
        try:
            launcher.play(ep.file_path)
        except LauncherError as e:
            log.warning("播放失败 episode_id=%s: %s", episode_id, e)
            self.failed.emit(str(e))
            return False

        self._playing_episode_id = ep.id
        self._playing_title = ep.title or ""
        self._progress = 0.0
        self.playingChanged.emit()
        self._monitor.start(ep)
        self.message.emit(f"正在监控：{ep.title}")
        log.info("开始播放 episode_id=%s file=%s", ep.id, ep.file_path)
        return True

    @Slot()
    def stop(self) -> None:
        """停止监控（不改播放器状态，仅停止轮询）。"""
        self._monitor.stop()
        self._playing_episode_id = 0
        self._playing_title = ""
        self.playingChanged.emit()

    @Slot(int)
    def markWatched(self, episode_id: int) -> None:
        """手动标记某集为看过（仅本地，不推 Bangumi）。

        自动标记走 monitor；这里用于用户手动点勾的场景。
        """
        try:
            self._db.mark_watched(episode_id)
            self.watched.emit(episode_id)
            self.message.emit("已标记为看过")
        except Exception as e:
            log.exception("手动标记失败 episode_id=%s", episode_id)
            self.failed.emit(f"标记失败：{e}")

    # ---------- 内部 ----------
    def _find_episode(self, episode_id: int) -> Optional[Episode]:
        """按 episode_id 反查集数记录。

        注意：Database 没有 `get_episode(id)`，需要遍历。
        条目数通常几十、每季十几集，遍历成本可接受；
        若以后条目上万，应给 episodes 加主键查询方法。
        """
        try:
            for s in self._db.list_subjects():
                for e in self._db.list_episodes(s.id):
                    if e.id == episode_id:
                        return e
        except Exception as e:
            log.exception("查找集数失败 episode_id=%s: %s", episode_id, e)
        return None

    def _on_progress(self, episode_id: int, progress: float) -> None:
        self._progress = float(progress)
        self.progressChanged.emit(episode_id, self._progress)

    def _on_watched(self, episode_id: int) -> None:
        self.watched.emit(episode_id)
        self.message.emit(f"已自动标记看过：{self._playing_title}")
