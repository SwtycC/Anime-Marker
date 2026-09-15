"""在看页（F18）：Bangumi type=3 收藏的单行卡片列表。

- 网络拉取在 QThread 执行，完成后回主线程渲染
- 封面后台下载，下完后自动重绘
- 点击卡片：本地已有 → 跳详情页；未入库 → 提示并引导扫描
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from app.core.inprogress import InProgressService, InProgressView
from app.ui.widgets import EmptyState, InProgressCard
from app.utils.cover_cache import cover_path_for, download as download_cover

log = logging.getLogger(__name__)


class _LoadWorker(QThread):
    """后台拉取在看列表（含缓存判定与降级）。"""

    loaded = Signal(object)  # InProgressView

    def __init__(self, service: InProgressService, force: bool) -> None:
        super().__init__()
        self.service = service
        self.force = force

    def run(self) -> None:
        # 任何异常都必须上报，否则页面会永远空白
        try:
            view = self.service.load(force=self.force)
        except Exception as e:
            log.exception("在看列表加载异常")
            from app.core.inprogress import InProgressView
            view = InProgressView(
                items=[], offline=False, error=f"加载异常：{e}"
            )
        self.loaded.emit(view)


class _CoverWorker(QThread):
    """后台补齐缺失的封面。"""

    done = Signal()

    def __init__(self, items: list) -> None:
        super().__init__()
        self.targets = [
            (it.bangumi_id, it.cover_url) for it in items if it.cover_url
        ]

    def run(self) -> None:
        changed = False
        for bid, url in self.targets:
            if cover_path_for(bid, url).exists():
                continue
            try:
                download_cover(bid, url)
                changed = True
            except Exception as e:
                log.warning("在看封面下载失败 bangumi_id=%s: %s", bid, e)
        if changed:
            self.done.emit()


class InProgressPage(QWidget):
    """在看列表页。"""

    subject_clicked = Signal(int)   # local_subject_id（已入库时才发出）
    goto_settings = Signal()

    def __init__(
        self,
        service: InProgressService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._loader: Optional[_LoadWorker] = None
        self._cover_worker: Optional[_CoverWorker] = None

        # 外层零边距：滚动条才能贴住窗口右边缘（同海报墙）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部固定区（标题 + 刷新按钮 + 提示条）：包一层容器给回 24px 边距
        header = QWidget(self)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 24, 24, 12)
        header_layout.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("在看", header)
        title.setProperty("role", "title")
        top.addWidget(title)
        top.addStretch(1)

        self.refresh_btn = QPushButton("刷新", header)
        self.refresh_btn.setToolTip("强制重新拉取 Bangumi 在看列表")
        self.refresh_btn.clicked.connect(lambda: self.reload(force=True))
        top.addWidget(self.refresh_btn)
        header_layout.addLayout(top)

        self.alert_bar = QLabel(header)
        self.alert_bar.setObjectName("alertBar")
        self.alert_bar.setWordWrap(True)
        self.alert_bar.hide()
        header_layout.addWidget(self.alert_bar)

        outer.addWidget(header)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("contentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll, 1)

        # 24px 边距移到滚动内容里，滚动条仍贴边
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(24, 0, 24, 8)
        self.list_layout.setSpacing(12)
        self.scroll.setWidget(self.container)

    # ---------- 加载 ----------
    def reload(self, force: bool = False) -> None:
        if self._loader is not None and self._loader.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self._loader = _LoadWorker(self.service, force)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.start()

    def _on_loaded(self, view: InProgressView) -> None:
        self.refresh_btn.setEnabled(True)
        if view.error:
            self.alert_bar.setText(view.error)
            self.alert_bar.show()
        else:
            self.alert_bar.hide()
        self._render(view)
        # 后台补封面，完成后重绘
        self._cover_worker = _CoverWorker(view.items)
        self._cover_worker.done.connect(lambda: self._render(view))
        self._cover_worker.start()

    # ---------- 渲染 ----------
    def _render(self, view: InProgressView) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not view.items:
            self.list_layout.addWidget(
                EmptyState(
                    "没有在看记录",
                    "在 Bangumi 上将动画标记为「在看」后会显示在这里",
                )
            )
            self.list_layout.addStretch(1)
            return

        for it in view.items:
            if it.ep_status and it.ep_status > 0:
                total = f" / 共 {it.total_eps} 集" if it.total_eps else ""
                progress_text = f"看到第 {it.ep_status} 集{total}"
            else:
                progress_text = "尚未开始"

            cover = ""
            if it.cover_url:
                p = cover_path_for(it.bangumi_id, it.cover_url)
                if p.exists():
                    cover = str(p)

            card = InProgressCard(
                bangumi_id=it.bangumi_id,
                title=it.name_cn or it.name,
                progress_text=progress_text,
                local_subject_id=it.local_subject_id or 0,
                cover_path=cover,
            )
            card.clicked.connect(self._on_card_clicked)
            self.list_layout.addWidget(card)
        self.list_layout.addStretch(1)

    def _on_card_clicked(self, _bangumi_id: int, local_subject_id: int) -> None:
        if local_subject_id:
            self.subject_clicked.emit(local_subject_id)
            return
        box = QMessageBox(self)
        box.setWindowTitle("未入库")
        box.setText(
            "本地未找到该动漫。\n\n"
            "请先到「设置」确认媒体库路径后执行「保存并扫描」。"
        )
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
        self.goto_settings.emit()
