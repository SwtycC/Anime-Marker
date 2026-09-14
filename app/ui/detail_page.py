"""详情页：左侧大封面 + 条目信息，右侧集数列表。"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from app.core.database import Database, Episode, Subject
from app.ui.widgets import EmptyState, EpisodeRow, HLine, _load_cover

log = logging.getLogger(__name__)


class DetailPage(QWidget):
    """条目详情 + 集数列表。"""

    back_clicked = Signal()
    play_episode = Signal(int)      # episode_id
    toggle_watched = Signal(int)    # episode_id
    rematch_clicked = Signal(int)   # subject_id

    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self._subject_id: Optional[int] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        # 顶部：返回 + 重新匹配
        top_row = QHBoxLayout()
        back_btn = QPushButton("← 返回", self)
        back_btn.clicked.connect(self.back_clicked.emit)
        top_row.addWidget(back_btn)
        top_row.addStretch(1)

        self.rematch_btn = QPushButton("重新匹配", self)
        self.rematch_btn.clicked.connect(self._on_rematch)
        top_row.addWidget(self.rematch_btn)
        outer.addLayout(top_row)

        # 中部：左封面 + 右信息
        body = QHBoxLayout()
        body.setSpacing(24)

        self.cover_label = QLabel(self)
        self.cover_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.cover_label.setFixedWidth(280)
        body.addWidget(self.cover_label)

        info_col = QVBoxLayout()
        info_col.setSpacing(8)

        self.title_label = QLabel(self)
        self.title_label.setProperty("role", "title")
        self.title_label.setWordWrap(True)
        info_col.addWidget(self.title_label)

        self.name_label = QLabel(self)
        self.name_label.setProperty("role", "subtitle")
        self.name_label.setWordWrap(True)
        info_col.addWidget(self.name_label)

        self.meta_label = QLabel(self)
        self.meta_label.setProperty("role", "hint")
        info_col.addWidget(self.meta_label)

        info_col.addWidget(HLine(self))

        # 集数列表（滚动）
        self.ep_scroll = QScrollArea(self)
        self.ep_scroll.setWidgetResizable(True)
        self.ep_scroll.setFrameShape(QScrollArea.NoFrame)
        self.ep_container = QWidget()
        self.ep_layout = QVBoxLayout(self.ep_container)
        self.ep_layout.setContentsMargins(0, 0, 0, 0)
        self.ep_layout.setSpacing(0)
        self.ep_scroll.setWidget(self.ep_container)
        info_col.addWidget(self.ep_scroll, 1)

        body.addLayout(info_col, 1)
        outer.addLayout(body, 1)

    # ---------- 数据 ----------
    def show_subject(self, subject_id: int) -> None:
        self._subject_id = subject_id
        subj = self.db.get_subject(subject_id)
        if subj is None:
            return
        self._render_subject(subj)
        self._render_episodes(self.db.list_episodes(subject_id))

    def _render_subject(self, s: Subject) -> None:
        self.title_label.setText(s.name_cn or s.name)
        self.name_label.setText(s.name if s.name_cn else "")
        meta: list[str] = []
        if s.total_eps:
            meta.append(f"共 {s.total_eps} 集")
        if s.folder_path:
            meta.append(s.folder_path)
        if s.match_state == "pending":
            meta.append("匹配待确认")
        self.meta_label.setText(" · ".join(meta))
        self.cover_label.setPixmap(_load_cover(s.cover_path, 280))

    def _render_episodes(self, episodes: list[Episode]) -> None:
        while self.ep_layout.count():
            item = self.ep_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not episodes:
            self.ep_layout.addWidget(EmptyState("没有集数", "请检查媒体库文件命名"))
            return
        for ep in episodes:
            row = EpisodeRow(
                episode_id=ep.id,
                ep_index=ep.ep_index,
                title=ep.title or "",
                watched=ep.watched,
            )
            row.play_clicked.connect(self.play_episode.emit)
            self.ep_layout.addWidget(row)
        self.ep_layout.addStretch(1)

    def _on_rematch(self) -> None:
        if self._subject_id is not None:
            self.rematch_clicked.emit(self._subject_id)
