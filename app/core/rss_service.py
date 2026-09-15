"""RSS 订阅轮询编排（F19）。

- QThread 后台执行，通过信号向 UI 汇报（同 §5.1 扫描模式）
- 抓取 → 解析 → 判新 → 下发 qBittorrent → 落库
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.qbittorrent_api import QbClient, QbError
from app.core.rss_feed import RssError, fetch_and_parse
from app.core.rss_matcher import RssMatcher

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PUSHED = "pushed"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class PollSummary:
    """单次轮询汇总（UI 展示用）。"""

    total_entries: int = 0
    new_entries: int = 0
    pushed: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class PollWorker(QThread):
    """后台轮询所有启用的订阅源。"""

    progress = Signal(str)                    # 文本进度
    source_done = Signal(int, str)            # source_id, 摘要文本
    finished_summary = Signal(object)         # PollSummary

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        qb: Optional[QbClient],
        only_source_id: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.db = db
        self.config = config
        self.api = api
        self.qb = qb
        self.only_source_id = only_source_id

    def run(self) -> None:
        summary = PollSummary()
        sources = self.db.list_rss_sources(only_enabled=True)
        if self.only_source_id is not None:
            sources = [s for s in sources if s.id == self.only_source_id]

        matcher = RssMatcher(self.db, self.qb, self.config)
        proxy = self.config.get("bangumi", "proxy", "")
        ua = self.config.get("bangumi", "user_agent", "AnimeMarker/1.0")
        auto = self.config.getbool("rss", "auto_download", False)

        for src in sources:
            self.progress.emit(f"轮询：{src.name or src.url}")
            try:
                self._poll_one(src, matcher, proxy, ua, auto, summary)
                self.db.update_rss_source(src.id, last_poll_at=_now_iso(), last_error="")
                self.source_done.emit(src.id, "完成")
            except (RssError, QbError) as e:
                msg = str(e)
                summary.errors.append(f"{src.name or src.url}：{msg}")
                self.db.update_rss_source(src.id, last_poll_at=_now_iso(), last_error=msg)
                self.source_done.emit(src.id, f"失败：{msg}")
                log.warning("订阅源轮询失败 %s: %s", src.url, e)
            except Exception as e:
                summary.errors.append(f"{src.name or src.url}：{e}")
                self.db.update_rss_source(src.id, last_error=str(e))
                self.source_done.emit(src.id, f"异常：{e}")
                log.exception("订阅源轮询异常 %s", src.url)

        self.finished_summary.emit(summary)

    # ---------- 单源 ----------
    def _poll_one(
        self,
        src: RssSource,
        matcher: RssMatcher,
        proxy: str,
        ua: str,
        auto: bool,
        summary: PollSummary,
    ) -> None:
        entries = fetch_and_parse(src.url, proxy=proxy, user_agent=ua)
        summary.total_entries += len(entries)

        # 新条目在前（RSS 通常倒序），逐条判定
        for entry in entries:
            result = matcher.judge(entry, src)

            if not result.is_new:
                summary.skipped += 1
                continue
            summary.new_entries += 1

            tags = [src.name or "AnimeMarker"]
            if result.subject_id:
                tags.append(f"bgm:{result.subject_id}")

            if not result.should_download:
                self.db.add_download_record(
                    source_id=src.id,
                    ep_index=result.ep_index,
                    torrent_title=entry.title,
                    magnet=result.download_url,
                    subject_id=result.subject_id,
                    status=STATUS_PENDING,
                )
                summary.pending += 1
                continue

            # 允许下载但 qBittorrent 不可用 → 降级为待确认
            if self.qb is None:
                self.db.add_download_record(
                    source_id=src.id,
                    ep_index=result.ep_index,
                    torrent_title=entry.title,
                    magnet=result.download_url,
                    subject_id=result.subject_id,
                    status=STATUS_PENDING,
                )
                summary.pending += 1
                summary.errors.append("未配置 qBittorrent，新集已入库为待确认")
                continue

            record_id = self.db.add_download_record(
                source_id=src.id,
                ep_index=result.ep_index,
                torrent_title=entry.title,
                magnet=result.download_url,
                subject_id=result.subject_id,
                status=STATUS_PENDING,
            )
            try:
                self.qb.add_entry(
                    magnet=entry.magnet,
                    torrent_url=entry.torrent_url,
                    tags=tags,
                )
                self.db.update_download_status(record_id, STATUS_PUSHED)
                summary.pushed += 1
            except QbError as e:
                self.db.update_download_status(record_id, STATUS_FAILED)
                summary.failed += 1
                summary.errors.append(f"下发失败：{entry.title[:40]}（{e}）")

    # ---------- 手动确认下发（「待确认」条目） ----------
    def push_pending(self, record_id: int) -> tuple[bool, str]:
        """把一条 pending 记录推送到 qBittorrent（由 UI 线程调用）。"""
        if self.qb is None:
            return False, "未配置 qBittorrent"
        records = {r.id: r for r in self.db.list_downloads()}
        rec = records.get(record_id)
        if rec is None:
            return False, "记录不存在"
        if not rec.magnet:
            return False, "该记录没有可用的下载链接"
        try:
            tags = ["AnimeMarker"]
            if rec.subject_id:
                tags.append(f"bgm:{rec.subject_id}")
            if rec.magnet.startswith("magnet:"):
                self.qb.add(rec.magnet, tags=tags)
            else:
                self.qb.add_torrent_file(rec.magnet, tags=tags)
            self.db.update_download_status(record_id, STATUS_PUSHED)
            return True, "已推送"
        except QbError as e:
            self.db.update_download_status(record_id, STATUS_FAILED)
            return False, str(e)


class RssService(QObject):
    """定时轮询调度器：QTimer 每 poll_interval 分钟触发一次。"""

    progress = Signal(str)
    poll_finished = Signal(object)   # PollSummary

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        qb: Optional[QbClient],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.config = config
        self.api = api
        self.qb = qb
        self._worker: Optional[PollWorker] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.poll())

    def start(self) -> None:
        minutes = max(1, self.config.getint("rss", "poll_interval", 30))
        self._timer.setInterval(minutes * 60 * 1000)
        self._timer.start()
        log.info("RSS 轮询已启动，间隔 %s 分钟", minutes)
        if self.config.getbool("rss", "poll_on_start", True):
            self.poll()

    def stop(self) -> None:
        self._timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def poll(self, only_source_id: Optional[int] = None) -> None:
        """触发一次轮询（异步）。"""
        if self.is_running():
            log.info("上一次轮询尚未结束，跳过本次触发")
            return
        self._worker = PollWorker(self.db, self.config, self.api, self.qb, only_source_id)
        self._worker.progress.connect(self.progress.emit)
        self._worker.finished_summary.connect(self._on_finished)
        self._worker.start()

    def _on_finished(self, summary: PollSummary) -> None:
        log.info(
            "轮询完成：条目 %s / 新 %s / 已推送 %s / 待确认 %s / 跳过 %s / 失败 %s",
            summary.total_entries, summary.new_entries, summary.pushed,
            summary.pending, summary.skipped, summary.failed,
        )
        self.poll_finished.emit(summary)

    def push_pending(self, record_id: int) -> tuple[bool, str]:
        """手动下发「待确认」条目（同步，用于 UI 按钮）。"""
        worker = PollWorker(self.db, self.config, self.api, self.qb)
        return worker.push_pending(record_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
