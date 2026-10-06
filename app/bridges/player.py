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
    #: 整部看完 → 自动标记「看过」（参数 subject_id, 名称）。
    #: 转发自 ProgressMonitor.subject_completed，qml_app 连到
    #: LibraryBridge.reload() 刷新海报墙/在看页的状态标签。
    subjectCompleted = Signal(int, str)

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
        # 集名（episodes.title）。**不是动漫名** —— 那个见 _playing_subject_name。
        self._playing_title = ""
        # 动漫名（subjects.name_cn / name）。播放时查一次并缓存，
        # 供状态栏显示；状态栏每次进度变化都读它，不能每次查库。
        self._playing_subject_name = ""
        # 集号显示文本（"第 3 集" / "SP01"），同样是播放时算好缓存。
        self._playing_episode_label = ""
        self._progress = 0.0

        self._monitor = ProgressMonitor(
            db=db,
            api=api,
            poll_interval=config.getint("monitor", "poll_interval", 3),
            trigger_threshold=config.getfloat("monitor", "trigger_threshold", 0.95),
            title_regex=config.get("monitor", "title_regex", ""),
            auto_upload=config.getbool("bangumi", "auto_upload", True),
            auto_complete=config.getbool(
                "bangumi", "auto_complete_watched", True),
            parent=self,
        )
        self._monitor.progress_changed.connect(self._on_progress)
        self._monitor.watched.connect(self._on_watched)
        self._monitor.error.connect(self.failed)
        # 「自动完结」结果 → 状态栏提示 + 转发给 qml_app（刷状态标签）
        self._monitor.subject_completed.connect(self._on_subject_completed)

    def set_api(self, api: BangumiClient) -> None:
        """设置保存后重建 API 时调用。"""
        self._api = api
        self._monitor.api = api

    def apply_config(self) -> None:
        """设置保存后，把**监控相关配置**同步给运行中的 monitor（无需重启）。

        **为什么必须显式同步**：`ProgressMonitor` 只在 `__init__` 里建一次，
        监控参数（自动上传 / 轮询间隔 / 阈值 / 标题正则）原先都是构造时
        一次性读入的，改完配置不重启程序不生效。表现就是"勾了自动上传，
        看完这集仍只写本地，得手动刷新才补传上 Bangumi"。

        `player_path` / `ls_path` 等启动参数不需要同步 —— `playEpisode()`
        每次播放都现读配置（见该方法内构造 `PlayerLauncher` 的地方）。
        """
        self._monitor.apply_config(
            poll_interval=self._config.getint("monitor", "poll_interval", 3),
            trigger_threshold=self._config.getfloat(
                "monitor", "trigger_threshold", 0.95),
            title_regex=self._config.get("monitor", "title_regex", ""),
            auto_upload=self._config.getbool("bangumi", "auto_upload", True),
            auto_complete=self._config.getbool(
                "bangumi", "auto_complete_watched", True),
        )

    # ---------- 状态属性 ----------
    @Property(int, notify=playingChanged)
    def playingEpisodeId(self) -> int:
        return self._playing_episode_id

    @Property(str, notify=playingChanged)
    def playingTitle(self) -> str:
        """当前播放的**集名**（如「思考的芦苇」）。

        注：这是**集**的标题，不是动漫名 —— 动漫名见 `playingSubjectName`。
        """
        return self._playing_title

    @Property(str, notify=playingChanged)
    def playingSubjectName(self) -> str:
        """当前播放所属的**动漫名**（优先中文名）。

        **为什么单独一个属性**（"状态写清楚什么动漫播放中"）：
        状态栏原来只有 `播放中 93%`，看不出是哪部；而 `playingTitle`
        存的其实是**集名**（`episodes.title`），拿它当动漫名是错的。
        动漫名只能由 `episodes.subject_id → subjects` 查出来，
        在 `playEpisode` 时取一次缓存住（状态栏每次进度变化都要读它，
        不能每次都查库 —— 见那里的说明）。
        """
        return self._playing_subject_name

    @Property(str, notify=playingChanged)
    def playingEpisodeLabel(self) -> str:
        """当前播放集的**集号显示文本**：正片是「第 N 集」，附加内容是 `SP01` 等。

        规则与详情页左列一致（见 library._episode_to_dict 的 `label`）：
        附加内容用 `ep_label`（`SP01`/`OVA02`/`NCOP3`…），正片用集号；
        集号是整数时去掉小数点（`3.0` → `第 3 集`），带小数（`12.5`）
        原样显示 —— 那些是未识别为附加内容、被顺延编号的文件。
        """
        if not self._playing_episode_label:
            return ""
        return self._playing_episode_label

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
            fullscreen_shortcut=self._config.get(
                "launcher", "fullscreen_shortcut", "alt+enter"),
            fullscreen_settle=self._config.getfloat(
                "launcher", "fullscreen_settle", 3.0),
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
        # 动漫名与集号标签在这里**算好缓存**（状态栏要不断读它们，
        # 不能每次都查库 / 走格式化）。
        self._playing_subject_name = self._resolve_subject_name(ep.subject_id)
        self._playing_episode_label = self._format_episode_label(ep)
        self._progress = 0.0
        self.playingChanged.emit()
        self._monitor.start(ep)
        self.message.emit(
            f"正在播放：{self._playing_subject_name} "
            f"{self._playing_episode_label} {self._playing_title}".strip())
        log.info("开始播放 episode_id=%s file=%s", ep.id, ep.file_path)
        return True

    @Slot()
    def stop(self) -> None:
        """停止监控（不改播放器状态，仅停止轮询）。"""
        self._monitor.stop()
        self._playing_episode_id = 0
        self._playing_title = ""
        self._playing_subject_name = ""
        self._playing_episode_label = ""
        self.playingChanged.emit()

    def wait_pending_sync(self, ms: int = 3000) -> None:
        """退出时等待后台的 Bangumi 同步线程（见 ProgressMonitor 同名方法）。"""
        self._monitor.wait_pending_sync(ms)

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
            return
        # ---- 「自动完结」检查（v15）----
        # 手动勾也可能是最后一集（比如用户补勾漏看的集）——
        # 与播放自动标记走同一个入口（见 ProgressMonitor.maybe_complete_subject）。
        # 查 episode 是 O(条目数) 的遍历（见 _find_episode 说明），
        # 只在手动点击时发生一次，可接受。
        ep = self._find_episode(episode_id)
        if ep is not None:
            self._monitor.maybe_complete_subject(ep.subject_id)

    # ---------- 内部 ----------
    def _resolve_subject_name(self, subject_id: int) -> str:
        """查该集所属动漫的显示名（中文名优先，退回原名）。

        查不到（条目刚被删等）返回空串 —— 状态栏会退化成只显示集信息，
        不该因此报错或显示 "None"。
        """
        try:
            s = self._db.get_subject(int(subject_id))
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("读取条目名失败 subject_id=%s: %s", subject_id, e)
            return ""
        if s is None:
            return ""
        return (s.name_cn or s.name or "").strip()

    @staticmethod
    def _format_episode_label(ep: Episode) -> str:
        """集号显示文本：附加内容用 `ep_label`，正片用「第 N 集」。

        与详情页左列的规则保持一致（同一个文件在两处不该显示成不同的编号）。
        集号去掉无意义的小数点（`3.0` → `第 3 集`），带小数的原样保留
        （`12.5` 这种是"没识别成附加内容、被顺延编号"的文件）。
        """
        label = (getattr(ep, "ep_label", "") or "").strip()
        if label:
            return label
        idx = ep.ep_index
        if idx is None:
            return ""
        try:
            f = float(idx)
        except (TypeError, ValueError):
            return ""
        if f <= 0:
            return ""
        return "第 %s 集" % (int(f) if f == int(f) else f)

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

    def _on_subject_completed(self, subject_id: int, name: str) -> None:
        """整部看完已自动标记「看过」→ 状态栏提示 + 转发信号。

        提示是必要的（自动动作必须让用户知道它发生了，否则表现为
        "状态自己变了"）；转发给 qml_app 连 LibraryBridge.reload()，
        让海报墙/在看页的状态标签立刻更新。
        """
        self.subjectCompleted.emit(subject_id, name)
        self.message.emit("《%s》每集都已看过，已自动标记为「看过」" % name)
