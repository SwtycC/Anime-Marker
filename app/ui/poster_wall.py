"""海报墙：FlowLayout 流式网格 + 统一尺寸卡片。

- 窗口变窄时卡片自动排到下一行，只出现垂直滚动条，无水平滚动
- 卡片尺寸统一：宽 = poster_width，高 = 宽 × 1.4 + 文本区
- 两种展示模式（`scanner.season_display`）：
  - flat（默认）：每个条目一张卡片，多季并排平铺
  - grouped：同一系列的多个条目合并为一张卡片，显示汇总进度
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from app.core.database import Database, Subject
from app.ui.widgets import EmptyState, FlowLayout, PosterCard
from app.ui_layout import POSTER_MARGIN, POSTER_SPACING

log = logging.getLogger(__name__)

SPACING = POSTER_SPACING
MARGIN = POSTER_MARGIN

class PosterWallPage(QWidget):
    """海报墙页面。"""

    subject_clicked = Signal(int)

    def __init__(
        self,
        db: Database,
        poster_width: int = 200,
        display_mode: str = "flat",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.poster_width = poster_width
        self.display_mode = display_mode
        self._cards: list[PosterCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("contentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll)

        # 两种内容容器，按数据有无切换：
        # - self.container：海报网格（FlowLayout）
        # - self.empty_host：空状态（整体居中，避免文字贴在左上角）
        self.stack = QStackedWidget()
        self.scroll.setWidget(self.stack)

        self.container = QWidget()
        self.flow = FlowLayout(
            self.container, margin=MARGIN, hspacing=SPACING, vspacing=SPACING
        )
        self.stack.addWidget(self.container)

        self.empty_host = QWidget()
        empty_layout = QVBoxLayout(self.empty_host)
        empty_layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        self.empty_state = EmptyState(
            "还没有条目", "请到「设置」中配置媒体库并扫描"
        )
        # EmptyState 内部已含上下 stretch，这里让它撑满即可实现整体居中
        empty_layout.addWidget(self.empty_state)
        self.stack.addWidget(self.empty_host)

    # ---------- 刷新 ----------
    def reload(self) -> None:
        """从数据库刷新海报。"""
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()

        subjects = self.db.list_subjects()
        if not subjects:
            # 空状态占满整个滚动区并居中（不放进 FlowLayout，否则会贴左上角）
            self.stack.setCurrentWidget(self.empty_host)
            return
        self.stack.setCurrentWidget(self.container)

        if self.display_mode == "grouped":
            for card in self._build_grouped_cards(subjects):
                self.flow.addWidget(card)
                self._cards.append(card)
        else:
            for s in subjects:
                card = self._make_card(s)
                self.flow.addWidget(card)
                self._cards.append(card)

    # ---------- flat 模式 ----------
    def _make_card(self, s: Subject) -> PosterCard:
        meta_parts: list[str] = []
        if s.total_eps:
            meta_parts.append(f"共 {s.total_eps} 集")
        if s.match_state == "pending":
            meta_parts.append("待匹配")
        elif s.match_state == "manual":
            meta_parts.append("手动指定")
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

    # ---------- grouped 模式 ----------
    def _build_grouped_cards(self, subjects: list[Subject]) -> list[PosterCard]:
        """按 series_name 聚合：同系列合并为一张卡片。

        卡片标题 = 系列名；副标题 = 部数与总进度；
        点击后回到第一个子条目（详情页可再切换）。
        """
        groups: "OrderedDict[str, list[Subject]]" = OrderedDict()
        for s in subjects:
            key = (s.series_name or "").strip()
            groups.setdefault(key, []).append(s)

        cards: list[PosterCard] = []
        for series, items in groups.items():
            if not series or len(items) == 1:
                # 无系列名或只有一部 → 平铺展示
                cards.append(self._make_card(items[0]))
                continue
            cards.append(self._make_series_card(series, items))
        return cards

    def _make_series_card(self, series: str, items: list[Subject]) -> PosterCard:
        """系列聚合卡片。"""
        # 进度汇总：已看集数 / 总集数
        watched_total = 0
        eps_total = 0
        for s in items:
            eps = self.db.list_episodes(s.id)
            eps_total += len(eps) or (s.total_eps or 0)
            watched_total += sum(1 for e in eps if e.watched)

        # 副标题：部数 + 进度
        parts = [f"{len(items)} 部"]
        if eps_total:
            parts.append(f"已看 {watched_total}/{eps_total}")
        if any(s.match_state == "pending" for s in items):
            parts.append("含待匹配")

        # 封面取第一部（通常是最早一季）
        cover = next((s.cover_path for s in items if s.cover_path), "")

        card = PosterCard(
            subject_id=items[0].id,          # 点击进入第一部
            title=series,
            meta=" · ".join(parts),
            cover_path=cover,
            poster_width=self.poster_width,
        )
        card.clicked.connect(self.subject_clicked.emit)
        return card
