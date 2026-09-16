"""订阅页（F19）：RSS 源管理 + 下载状态 + 待确认下发。

- 订阅源增删改 / 启用开关 / 单源或全部轮询
- qBittorrent 连通状态展示 + 「打开 Web UI」
- 待确认条目手动推送下载
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from app.core.config import Config
from app.core.database import Database
from app.core.qbittorrent_api import QbClient, QbError
from app.core.rss_matcher import (
    RULE_COMPLETE_PACK, RULE_FILL_GAP, RULE_MANUAL, RULE_NEW_ONLY,
)
from app.core.rss_service import RssService
from app.ui.num_inputs import NoWheelComboBox
from app.ui.widgets import DownloadRow, EmptyState, SourceRow

log = logging.getLogger(__name__)

RULE_LABELS = [
    ("只下新集", RULE_NEW_ONLY),
    ("补缺集", RULE_FILL_GAP),
    ("完结整包", RULE_COMPLETE_PACK),
    ("仅通知", RULE_MANUAL),
]


def _fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or ""
    return dt.strftime("%m-%d %H:%M")


class SubscriptionPage(QWidget):
    """订阅管理页。"""

    status_message = Signal(str)

    def __init__(
        self,
        db: Database,
        config: Config,
        rss_service: RssService,
        qb: Optional[QbClient],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.config = config
        self.rss_service = rss_service
        self.qb = qb

        # qBittorrent 正在下载任务数的缓存（避免频繁连接）
        self._downloading_cache: dict[str, int] = {}

        # 外层零边距：滚动条贴住窗口右边缘（同海报墙）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部固定区：标题 + 按钮 + 状态条 + 添加表单
        header = QWidget(self)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 24, 24, 12)
        header_layout.setSpacing(12)

        # ---- 标题行 ----
        top = QHBoxLayout()
        title = QLabel("订阅", header)
        title.setProperty("role", "title")
        top.addWidget(title)
        top.addStretch(1)

        self.poll_all_btn = QPushButton("轮询全部", header)
        self.poll_all_btn.clicked.connect(self._on_poll_all)
        top.addWidget(self.poll_all_btn)

        self.webui_btn = QPushButton("打开 Web UI", header)
        self.webui_btn.clicked.connect(self._open_webui)
        top.addWidget(self.webui_btn)

        self.redetect_btn = QPushButton("重新检测", header)
        self.redetect_btn.setToolTip("重新检测 qBittorrent Web UI 连通性")
        self.redetect_btn.clicked.connect(self._on_redetect)
        top.addWidget(self.redetect_btn)
        header_layout.addLayout(top)

        # ---- qB 状态条 ----
        self.qb_bar = QLabel(header)
        self.qb_bar.setObjectName("alertBar")
        self.qb_bar.setWordWrap(True)
        header_layout.addWidget(self.qb_bar)

        # ---- 添加订阅 ----
        add_row = QHBoxLayout()
        add_row.setSpacing(8)

        self.name_edit = QLineEdit(header)
        self.name_edit.setPlaceholderText("备注名（如：葬送的芙莉莲）")
        add_row.addWidget(self.name_edit, 1)

        self.url_edit = QLineEdit(header)
        self.url_edit.setPlaceholderText("RSS 链接（Mikan / dmhy …）")
        add_row.addWidget(self.url_edit, 2)

        self.rule_combo = NoWheelComboBox(header)
        for label, key in RULE_LABELS:
            self.rule_combo.addItem(label, userData=key)
        add_row.addWidget(self.rule_combo)

        add_btn = QPushButton("添加", header)
        add_btn.setProperty("role", "primary")
        add_btn.clicked.connect(self._on_add)
        add_row.addWidget(add_btn)
        header_layout.addLayout(add_row)

        outer.addWidget(header)

        # ---- 列表 ----
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("contentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll, 1)

        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(24, 0, 24, 8)
        self.list_layout.setSpacing(12)
        self.scroll.setWidget(self.container)

        # 启动时只显示静态提示，不做网络请求（避免拖慢启动）：
        # 真正检测 qBittorrent 连通性由「进入本页」或用户点「重新检测」触发
        self._show_qb_placeholder()
        self.rss_service.poll_finished.connect(lambda _s: self.reload())

    # ---------- 数据 ----------
    def reload(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sources = self.db.list_rss_sources()
        # 复用上一次检测结果（避免每次 reload 都连 qBittorrent）：
        # 仅在「进入页面」或用户点「重新检测」时才真正查询
        downloading = self._downloading_cache

        if not sources:
            self.list_layout.addWidget(
                EmptyState("还没有订阅", "在上方粘贴 RSS 链接并点击「添加」")
            )
        for src in sources:
            stats = self.db.count_downloads_by_status(src.id)
            stats_text = (
                f"已推送 {stats.get('pushed', 0) + stats.get('done', 0)}"
                f" · 下载中 {downloading.get(src.name, 0)}"
                f" · 待确认 {stats.get('pending', 0) + stats.get('failed', 0)}"
            )
            last_poll = f"最近轮询：{_fmt_time(src.last_poll_at)}" if src.last_poll_at else "未轮询"
            row = SourceRow(
                source_id=src.id,
                name=src.name,
                url=src.url,
                rule=next((lbl for lbl, k in RULE_LABELS if k == src.rule), src.rule),
                enabled=src.enabled,
                stats_text=stats_text,
                last_poll_text=last_poll,
                last_error=src.last_error or "",
            )
            row.poll_clicked.connect(self._on_poll_one)
            row.delete_clicked.connect(self._on_delete)
            row.toggled.connect(self._on_toggle)
            self.list_layout.addWidget(row)

        # ---- 待确认下载 ----
        pendings = [r for r in self.db.list_downloads() if r.status in ("pending", "failed")]
        header = QLabel(f"待确认下载（{len(pendings)}）", self.container)
        header.setProperty("role", "subtitle")
        self.list_layout.addWidget(header)

        if not pendings:
            hint = QLabel("没有待确认的条目", self.container)
            hint.setProperty("role", "hint")
            self.list_layout.addWidget(hint)
        for rec in pendings:
            drow = DownloadRow(
                record_id=rec.id,
                ep_index=rec.ep_index,
                title=rec.torrent_title,
                status=rec.status,
            )
            drow.download_clicked.connect(self._on_push)
            self.list_layout.addWidget(drow)

        self.list_layout.addStretch(1)

    def _downloading_count_by_tag(self) -> dict[str, int]:
        """按订阅名标签统计 qBittorrent 中正在下载的任务数。

        这是可选功能的探测：qBittorrent 未运行属正常情况（用户可能只用
        媒体库功能），故失败只记 DEBUG，不刷 WARNING 日志。
        """
        if self.qb is None:
            return {}
        try:
            counts: dict[str, int] = {}
            for t in self.qb.list_torrents():
                if t.state in ("pausedUP", "stoppedUP"):
                    continue
                for tag in (t.tags or "").split(","):
                    tag = tag.strip()
                    if tag and not tag.startswith("bgm:") and tag != "AnimeMarker":
                        counts[tag] = counts.get(tag, 0) + 1
            return counts
        except QbError as e:
            log.debug("读取 qBittorrent 任务失败（qB 未运行属正常）：%s", e)
            return {}

    # ---------- qB 状态 ----------
    def _show_qb_placeholder(self) -> None:
        """未检测前的静态提示（不发请求）。"""
        if self.qb is None:
            self.qb_bar.setText(
                "未配置 qBittorrent：订阅仍会判新入库，但不会自动下发。"
            )
        else:
            self.qb_bar.setText("qBittorrent 状态：未检测（点击「重新检测」）")
        self.qb_bar.show()
        self.qb_bar.setProperty("state", "idle")

    def refresh_qb_status(self, silent: bool = False) -> None:
        """检测 qBittorrent 连通性。

        silent=True 时失败只显示状态文本、不弹窗（用于进入页面时自动检测）。
        """
        if self.qb is None:
            self._show_qb_placeholder()
            return
        if not silent:
            self.qb_bar.setText("正在检测 qBittorrent…")
            QApplication.processEvents()
        try:
            version = self.qb.test_connection()
            self.qb_bar.setText(f"qBittorrent 已连接（v{version}）")
            self.qb_bar.setProperty("state", "ok")
            # 连接成功时顺带刷新下载中任务数（与检测共用一次连接）
            self._downloading_cache = self._downloading_count_by_tag()
        except QbError as e:
            self.qb_bar.setText(f"qBittorrent 未连接：{e}")
            self.qb_bar.setProperty("state", "error")
            self._downloading_cache = {}
        self.qb_bar.show()
        self.qb_bar.style().unpolish(self.qb_bar)
        self.qb_bar.style().polish(self.qb_bar)

    # ---------- 动作 ----------
    def _on_add(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "添加订阅", "请填写 RSS 链接")
            return
        name = self.name_edit.text().strip() or url
        rule = self.rule_combo.currentData()
        try:
            self.db.add_rss_source(name=name, url=url, rule=rule)
        except Exception as e:
            QMessageBox.warning(self, "添加订阅", f"添加失败：{e}")
            return
        self.name_edit.clear()
        self.url_edit.clear()
        self.reload()
        self.status_message.emit(f"已添加订阅：{name}")

    def _on_delete(self, source_id: int) -> None:
        ret = QMessageBox.question(
            self,
            "删除订阅",
            "删除该订阅及其历史记录？\n（不会删除 qBittorrent 中已存在的任务）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self.db.delete_rss_source(source_id)
        self.reload()
        self.status_message.emit("已删除订阅")

    def _on_toggle(self, source_id: int, enabled: bool) -> None:
        self.db.update_rss_source(source_id, enabled=1 if enabled else 0)

    def _on_poll_all(self) -> None:
        if self.rss_service.is_running():
            self.status_message.emit("轮询进行中，请稍候…")
            return
        self.rss_service.poll()
        self.status_message.emit("开始轮询全部订阅…")

    def _on_poll_one(self, source_id: int) -> None:
        if self.rss_service.is_running():
            self.status_message.emit("轮询进行中，请稍候…")
            return
        self.rss_service.poll(only_source_id=source_id)
        self.status_message.emit("开始轮询该订阅…")

    def _on_push(self, record_id: int) -> None:
        ok, msg = self.rss_service.push_pending(record_id)
        self.status_message.emit(msg)
        self.reload()

    def _on_redetect(self) -> None:
        """手动触发检测（用户主动点击，允许阻塞式反馈）。"""
        self.refresh_qb_status(silent=False)
        self.reload()

    def _open_webui(self) -> None:
        if self.qb is None:
            self.status_message.emit("未配置 qBittorrent")
            return
        QDesktopServices.openUrl(QUrl(self.qb.webui_url))
