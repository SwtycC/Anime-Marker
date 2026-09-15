"""动态页：按时间倒序展示已看记录（F17）。

数据来源：episodes.watched_at（自动/手动标记已看的时间）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from app.core.database import Database
from app.ui.widgets import EmptyState, TimelineRow

log = logging.getLogger(__name__)

MAX_ENTRIES = 200


def _fmt_time(iso: str) -> str:
    """ISO8601 → 友好显示：今天 HH:MM / 昨天 HH:MM / MM-DD HH:MM / YYYY-MM-DD。"""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("今天 %H:%M")
    if dt.date() == (now - timedelta(days=1)).date():
        return dt.strftime("昨天 %H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


class TimelinePage(QWidget):
    """观看动态时间线。"""

    subject_clicked = Signal(int)

    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db

        # 外层零边距：滚动条贴住窗口右边缘（同海报墙）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 标题固定在滚动区外
        header = QWidget(self)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 24, 24, 12)
        header_layout.setSpacing(0)

        title = QLabel("动态", header)
        title.setProperty("role", "title")
        header_layout.addWidget(title)
        outer.addWidget(header)

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
        self.list_layout.setSpacing(0)
        self.scroll.setWidget(self.container)

    def reload(self) -> None:
        """从数据库刷新时间线。"""
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        entries = self.db.list_watched_timeline(limit=MAX_ENTRIES)
        if not entries:
            self.list_layout.addWidget(
                EmptyState("还没有观看记录", "看过的集数会按时间显示在这里")
            )
            self.list_layout.addStretch(1)
            return

        for e in entries:
            row = TimelineRow(
                subject_id=e.subject_id,
                time_text=_fmt_time(e.watched_at),
                subject_name=e.subject_name,
                ep_index=e.ep_index,
                ep_title=e.ep_title,
            )
            row.clicked.connect(self.subject_clicked.emit)
            self.list_layout.addWidget(row)
        self.list_layout.addStretch(1)
