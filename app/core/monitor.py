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
from datetime import datetime
from typing import Optional

from PySide6.QtCore import (Q_ARG, QMetaObject, QObject, QRunnable, Qt,
                            QThreadPool, QTimer, Signal, Slot)

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


class _SyncRunnable(QRunnable):
    """后台把「看完了一集」同步到 Bangumi，避免阻塞 UI 线程。

    **为什么必须独立成线程**（实测：程序"未响应"）：调它的地方在
    `_tick()` 的调用链上，而 `_tick` 由 QTimer 在 UI 线程驱动；
    `mark_episode_watched` 带 `Retry(total=3, connect=3)` + `timeout=10s`，
    网络不通时最坏阻塞 **30+ 秒**，期间界面完全冻结、连关播放器都卡住。
    详见 `_trigger_watched` 里那段时间线。

    **跨线程只做网络、不碰数据库**：`Database` 的所有访问都在主线程
    （见 database 的 `_cursor` 全局锁说明），worker 里调 SQL 会引入锁竞争。
    所以本类只负责 POST，结果（成功/失败）交回主线程由
    `ProgressMonitor._on_sync_finished` 处理落库。

    用 `QMetaObject.invokeMethod` 而不是直接调用回调：QRunnable 跑在池线程，
    直接改 Qt 对象属性/发信号可能跨线程访问，交给主线程的事件循环最稳。
    """

    def __init__(self, monitor: "ProgressMonitor", ep: Episode,
                 subject) -> None:
        super().__init__()
        self._monitor = monitor
        self._ep = ep
        self._subject = subject

    @Slot()
    def run(self) -> None:
        err = ""
        try:
            self._monitor.api.mark_episode_watched(
                self._subject.bangumi_id, self._ep.bangumi_ep_id)
        except BangumiError as e:
            err = str(e)
        except Exception as e:          # pragma: no cover - 防御性
            log.exception("同步到 Bangumi 异常 episode_id=%s", self._ep.id)
            err = str(e)
        # 回主线程收尾（写库 + 提示）
        QMetaObject.invokeMethod(
            self._monitor, "_on_sync_finished", Qt.QueuedConnection,
            Q_ARG(int, self._ep.id),
            Q_ARG(int, int(self._subject.bangumi_id)),
            Q_ARG(str, self._subject.name_cn or self._subject.name or ""),
            Q_ARG(str, err))


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
        # 与 Bangumi 同步的**后台**线程池。
        #
        # 用池而不是"每次新建 QThread"：看完一集就发一个请求，一部番十几集
        # 会建十几个线程；池复用少量线程即可。maxThreadCount 限制为 2 ——
        # 同步是"一集一个"的低频动作，没必要并发太多去撞 Bangumi 的限流。
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    def apply_config(
        self,
        poll_interval: Optional[int] = None,
        trigger_threshold: Optional[float] = None,
        title_regex: Optional[str] = None,
        auto_upload: Optional[bool] = None,
    ) -> None:
        """在**运行中**更新监控参数（设置页保存后调用）。

        **为什么需要它**：这些参数原先是构造时一次性读入并冻结的
        （`self.auto_upload = bool(auto_upload)`），而 `ProgressMonitor`
        在 `PlayerBridge.__init__` 里只建一次、之后一直复用。于是用户在
        设置页改了「自动上传」「轮询间隔」等，**当前这次运行不会生效**,
        必须重启程序 —— 表现是"勾了自动上传，看完还是没传，手动刷新才补上"。

        所有参数都是可选：只传要改的那几个，其余保持不动。
        `poll_interval` 会立即重设 QTimer 间隔（`setInterval` 对运行中的
        QTimer 同样有效，无需重启定时器）。
        """
        if poll_interval is not None:
            self._timer.setInterval(max(1, int(poll_interval)) * 1000)
        if trigger_threshold is not None:
            self.threshold = float(trigger_threshold)
        if title_regex is not None:
            self.title_regex = title_regex or None
        if auto_upload is not None:
            changed = self.auto_upload != bool(auto_upload)
            self.auto_upload = bool(auto_upload)
            if changed:
                log.info("自动上传已%s（立即生效）",
                         "开启" if self.auto_upload else "关闭")

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

    def wait_pending_sync(self, ms: int = 3000) -> None:
        """等待后台同步线程结束（退出时调用）。

        **为什么需要**：同步放在线程池里跑，程序退出时若不等待，正在进行的
        POST 会被硬切断；更重要的是它的**收尾回调（写库）在主线程**，
        主线程一结束就永远不会执行 —— 表现为"看完这集、Bangumi 上也标了，
        本地 `watched_episodes` 却缺这一行"（下次「上传」小窗里又冒出来）。

        给一个上限而不是无限等：网络卡住时不能让退出流程永久挂住，
        超时就放弃（那一条留待下次同步补齐，不影响已提交的标记）。
        """
        try:
            if self._pool is not None and self._pool.activeThreadCount() > 0:
                log.info("等待 %s 个后台同步线程结束…",
                         self._pool.activeThreadCount())
                self._pool.waitForDone(ms)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("等待后台同步线程失败：%s", e)

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
        #
        # **`watched` 必须现查库、不能用 `ep.watched`**（踩坑）：
        # `ep` 是 `start()` 时抓的**快照**，播放期间可能已经过期 ——
        # 用户在详情页手动点过「标记看过」、或用「上传」小窗补传过，
        # 库里已是 1 而快照仍是 0。此时超阈值会**再触发一次**：
        #   ① 本地 `mark_watched()` 覆盖掉原来的 `watched_at` 时间；
        #   ② 再 POST 一次 Bangumi（接口幂等，但白白多一次请求）。
        # 现查一次库代价极小（本地 SQLite），换掉这个竞态。
        if (progress >= self.threshold and advancing
                and ep.id not in self._triggered
                and not self._is_watched(ep.id)):
            self._trigger_watched(ep)

    def _is_watched(self, episode_id: int) -> bool:
        """现查库确认该集是否已标记看过（不用启动时的快照，见 _tick 说明）。

        查不到（集被删 / 重扫换了 id）时保守返回 True —— 宁可漏标一次
        （用户可在详情页手动补），也不要对着一条不存在的记录反复写。
        """
        try:
            row = self.db.get_episode(episode_id)
        except Exception as e:      # pragma: no cover - 防御性
            log.warning("查询 episode_id=%s 观看状态失败: %s", episode_id, e)
            return True
        return bool(row.watched) if row is not None else True

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
        # ---- 3. 网络同步：**必须放到后台线程**（见下方"致命踩坑"）----
        #
        # **致命踩坑（实测：程序"未响应"）**：这里是 `_tick()` 的调用链，
        # 而 `_tick` 由 QTimer 在 **UI 线程** 驱动。早期直接在下面同步调
        # `api.mark_episode_watched()`，该请求带 `Retry(total=3, connect=3)`
        # 且 timeout=10s —— **最坏情况在 UI 线程里阻塞 30+ 秒**：
        #
        #     16:23:07  第 1 次重试（connect timeout 10s）
        #     16:23:18  第 2 次重试
        #     16:23:30  第 3 次重试
        #     16:23:40  最终失败        ← 这 33 秒界面完全冻结
        #     16:23:42  用户关 PotPlayer 的操作也卡住 → "程序未响应"
        #
        # 网络正常时这个 POST 只要几百毫秒，所以平时看不出来；一旦
        # `api.bgm.tv` 连不上（连接超时，而非 5xx）就必然复现。
        #
        # 修法：交给线程池，UI 线程立刻返回。失败/成功仍走同一套处理
        # （提示 + 写回本地），只是时机变成"稍后"。
        self._pool.start(_SyncRunnable(self, ep, subject))

    @Slot(int, int, str, str)
    def _on_sync_finished(self, episode_id: int, bangumi_id: int,
                          subject_name: str, error: str) -> None:
        """后台同步的收尾（**在主线程执行**，见 _SyncRunnable 的说明）。

        - `error` 非空 → 后台 POST 失败：本地记录早已写好（`_trigger_watched`
          第 1 步），这里只提示用户可用「上传」补齐。
        - 成功 → 写回 `watched_episodes` + 清该条目的同步水位，
          这两步都碰数据库，因此必须留在主线程（见 _SyncRunnable 的边界说明）。
        """
        if error:
            log.warning("同步到 Bangumi 失败（本地记录已保留，可用「上传」补齐）: %s",
                        error)
            self.error.emit(f"Bangumi 标记失败（本地已记录，可用「上传」补齐）：{error}")
            return

        log.info("已同步到 Bangumi：episode_id=%s（条目 %s）", episode_id, bangumi_id)

        # ---- 把这一集写回本地「已同步」表 ----
        #
        # **为什么必须写回**（踩坑）：`watched_episodes` 是「**从 Bangumi 拉回来的**
        # 已标记集」；而这里是「**推过去**」—— 只 POST 不写回，这张表就永远缺这一行。
        # 于是 `Database.pending_uploads()` 的判据
        # （`LEFT JOIN watched_episodes ... WHERE w.bangumi_ep_id IS NULL`）
        # 会把**刚刚自动上传成功的集**继续算成"待上传" ✗ ——
        # 实测现象：日志已打印"已同步到 Bangumi"，但「上传」小窗里
        # 那一集仍然列在待上传清单里，用户以为没传上去。
        #
        # 与手动补传走同一条路（见 InProgressBridge._on_upload_finished）：
        # upsert 后动态页立刻出现 `bgm` 标记，不必等下一次集级同步。
        # `watched_at` 取**上传时刻**，与 Bangumi 网页记录的时间一致。
        ep = self.db.get_episode(episode_id)
        if ep is None:
            # 条目在同步期间被删了（重扫 / 手动删除）：无事可做
            log.info("episode_id=%s 已不存在，跳过写回（Bangumi 标记已生效）",
                     episode_id)
            return
        try:
            self.db.upsert_watched_episodes([{
                "bangumi_ep_id": int(ep.bangumi_ep_id),
                "subject_id": int(ep.subject_id),
                "bangumi_id": int(bangumi_id),
                "subject_name": subject_name,
                "ep_index": ep.ep_index or 0,
                "ep_name": ep.title or "",
                "watched_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"),
            }])
        except Exception as e:
            # 只影响"待上传清单"与界面标记，下次集级同步会补上，不影响已提交的标记
            log.warning("写回本地集级记录失败（不影响 Bangumi 标记）: %s", e)
        # 让这条目的集级记录下次刷新时重新同步，否则刚标的这一集要等
        # 收藏行变化或 30 天超期兜底才会出现在动态页（见 sync_candidates）
        try:
            self.db.clear_ep_sync_state_for(int(bangumi_id))
        except Exception as e:
            log.warning("让条目 %s 的集级记录重新同步失败（不影响标记）: %s",
                        bangumi_id, e)
