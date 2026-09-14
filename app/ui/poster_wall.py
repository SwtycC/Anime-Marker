"""海报墙：FlowLayout 流式网格 + 统一尺寸卡片。

- 窗口变窄时卡片自动排到下一行，只出现垂直滚动条，无水平滚动
- 卡片尺寸统一：宽 = poster_width，高 = 宽 × 1.4 + 文本区
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.core.database import Database, Subject
from app.ui.widgets import EmptyState, FlowLayout, PosterCard

log = logging.getLogger(__name__)

SPACING = 16
MARGIN = 24


class PosterWallPage(QWidget):
    """海报墙页面。"""

    subject_clicked = Signal(int)

    def __init__(
        self,
        db: Database,
        poster_width: int = 200,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.poster_width = poster_width
        self._cards: list[PosterCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)

        self.container = QWidget()
        self.flow = FlowLayout(
            self.container, margin=MARGIN, hspacing=SPACING, vspacing=SPACING
        )
        self.scroll.setWidget(self.container)

    def reload(self) -> None:
        """从数据库刷新海报。"""
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()

        subjects = self.db.list_subjects()
        if not subjects:
            empty = EmptyState("还没有条目", "请到「设置」中配置媒体库并扫描")
            empty.setMinimumSize(480, 320)
            self.flow.addWidget(empty)
            return

        for s in subjects:
            card = self._make_card(s)
            self.flow.addWidget(card)
            self._cards.append(card)

    def _make_card(self, s: Subject) -> PosterCard:
        meta_parts: list[str] = []
        if s.total_eps:
            meta_parts.append(f"共 {s.total_eps} 集")
        if s.match_state == "pending":
            meta_parts.append("待匹配")
        meta = " · ".join(meta_parts)

        card = PosterCard(
            subject_id=s.id,
            title=s.name_cn or s.name,
            meta=meta,
            cover_path=s.cover_path or "",
            poster_width=self.poster_width,
        )
        card.clicked.connect(self.subject_clicked.emit)
        return card
