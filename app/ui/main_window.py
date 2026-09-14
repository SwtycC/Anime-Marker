"""主窗口：左侧导航 + QStackedWidget 多页。

参考 pythonguis-examples 中 Mozzarella Ashbadger 的切页模式。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database, Episode
from app.core.launcher import LauncherError, PlayerLauncher
from app.core.monitor import ProgressMonitor
from app.core.scanner import ScanWorker
from app.ui.detail_page import DetailPage
from app.ui.poster_wall import PosterWallPage
from app.ui.settings_page import SettingsPage
from app.ui.timeline_page import TimelinePage

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Anime Marker")
        self.resize(1280, 800)

        # 核心服务
        self.db = Database()
        self.api = self._build_api()
        self.monitor = ProgressMonitor(
            db=self.db,
            api=self.api,
            poll_interval=config.getint("monitor", "poll_interval", 3),
            trigger_threshold=config.getfloat("monitor", "trigger_threshold", 0.95),
            title_regex=config.get("monitor", "title_regex", ""),
            parent=self,
        )
        self.monitor.watched.connect(self._on_episode_watched)
        self.monitor.error.connect(self._on_monitor_error)
        self._scanner: Optional[ScanWorker] = None

        # ---- UI ----
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧导航
        nav = QWidget(central)
        nav.setObjectName("navRail")
        nav.setFixedWidth(200)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 16, 12, 16)
        nav_layout.setSpacing(4)

        self.btn_wall = QPushButton("海报墙", nav)
        self.btn_wall.setCheckable(True)
        self.btn_wall.setChecked(True)
        self.btn_wall.clicked.connect(lambda: self._switch(0))
        nav_layout.addWidget(self.btn_wall)

        self.btn_timeline = QPushButton("动态", nav)
        self.btn_timeline.setCheckable(True)
        self.btn_timeline.clicked.connect(lambda: self._switch(1))
        nav_layout.addWidget(self.btn_timeline)

        self.btn_settings = QPushButton("设置", nav)
        self.btn_settings.setCheckable(True)
        self.btn_settings.clicked.connect(lambda: self._switch(2))
        nav_layout.addWidget(self.btn_settings)

        nav_layout.addStretch(1)
        root.addWidget(nav)

        # 页面栈
        self.stack = QStackedWidget(central)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        poster_width = config.getint("ui", "poster_width", 200)
        self.wall_page = PosterWallPage(self.db, poster_width=poster_width, parent=self)
        self.detail_page = DetailPage(self.db, parent=self)
        self.timeline_page = TimelinePage(self.db, parent=self)
        self.settings_page = SettingsPage(self.config, parent=self)

        # 海报墙 / 详情 共用一层子栈
        self.browse_stack = QStackedWidget(self)
        self.browse_stack.addWidget(self.wall_page)
        self.browse_stack.addWidget(self.detail_page)

        self.stack.addWidget(self.browse_stack)   # index 0
        self.stack.addWidget(self.timeline_page)  # index 1
        self.stack.addWidget(self.settings_page)  # index 2

        # 信号
        self.wall_page.subject_clicked.connect(self._open_detail)
        self.timeline_page.subject_clicked.connect(self._open_detail_from_timeline)
        self.detail_page.back_clicked.connect(self._back_to_wall)
        self.detail_page.play_episode.connect(self._play_episode)
        self.detail_page.rematch_clicked.connect(self._rematch_subject)
        self.settings_page.scan_requested.connect(self._start_scan)

        # 状态栏
        self.statusBar().showMessage("就绪")

        # 初次加载
        self.wall_page.reload()

        # 鼠标后侧键（XButton1 / Back）= 详情页返回海报墙
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---------- 服务构建 ----------
    def _build_api(self) -> BangumiClient:
        return BangumiClient(
            token=self.config.get("bangumi", "token", ""),
            api_base=self.config.get("bangumi", "api_base", "https://api.bgm.tv"),
            proxy=self.config.get("bangumi", "proxy", ""),
            user_agent=self.config.get(
                "bangumi", "user_agent",
                "AnimeMarker/1.0 (https://github.com/yourname/anime-marker)",
            ),
        )

    # ---------- 导航 ----------
    def _switch(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.btn_wall.setChecked(index == 0)
        self.btn_timeline.setChecked(index == 1)
        self.btn_settings.setChecked(index == 2)
        if index == 0:
            self.wall_page.reload()
        elif index == 1:
            self.timeline_page.reload()

    def _open_detail(self, subject_id: int) -> None:
        self.detail_page.show_subject(subject_id)
        self.browse_stack.setCurrentWidget(self.detail_page)

    def _open_detail_from_timeline(self, subject_id: int) -> None:
        """从动态页跳转：先切回海报墙栈，再打开详情。"""
        self._switch(0)
        self._open_detail(subject_id)

    def _back_to_wall(self) -> None:
        self.browse_stack.setCurrentWidget(self.wall_page)
        self.wall_page.reload()

    # ---------- 播放 ----------
    def _play_episode(self, episode_id: int) -> None:
        ep = self._find_episode(episode_id)
        if ep is None:
            QMessageBox.warning(self, "播放", "未找到该集")
            return

        launcher = PlayerLauncher(
            player_path=self.config.get("general", "player_path", ""),
            ls_path=self.config.get("general", "ls_path", ""),
            enable_ls=self.config.getbool("launcher", "enable_ls", True),
            ls_shortcut=self.config.get("launcher", "ls_shortcut", "ctrl+alt+l"),
            ls_start_delay=self.config.getfloat("launcher", "ls_start_delay", 5.0),
            player_start_delay=self.config.getfloat("launcher", "player_start_delay", 1.0),
        )
        try:
            launcher.play(ep.file_path)
        except LauncherError as e:
            QMessageBox.warning(self, "播放失败", str(e))
            return
        self.monitor.start(ep)
        self.statusBar().showMessage(f"正在监控：{ep.title}")

    def _find_episode(self, episode_id: int) -> Optional[Episode]:
        for s in self.db.list_subjects():
            for e in self.db.list_episodes(s.id):
                if e.id == episode_id:
                    return e
        return None

    # ---------- 监控 ----------
    def _on_episode_watched(self, episode_id: int) -> None:
        self.statusBar().showMessage(f"已自动标记看过：episode_id={episode_id}", 5000)
        if self.detail_page._subject_id is not None:
            self.detail_page.show_subject(self.detail_page._subject_id)

    def _on_monitor_error(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 5000)

    # ---------- 扫描 ----------
    def _start_scan(self) -> None:
        if self._scanner is not None and self._scanner.isRunning():
            QMessageBox.information(self, "扫描", "扫描正在进行中")
            return
        # 保存后重建 API（token 可能更新）
        self.api = self._build_api()
        self.monitor.api = self.api

        paths = self.config.library_paths
        if not paths:
            QMessageBox.warning(self, "扫描", "请先配置媒体库根目录")
            return

        self._scanner = ScanWorker(paths, self.api, self.db)
        self._scanner.progress_changed.connect(
            lambda cur, total: self.statusBar().showMessage(f"扫描中 {cur}/{total}")
        )
        self._scanner.item_matched.connect(
            lambda sid, name: self.statusBar().showMessage(f"已匹配：{name}", 3000)
        )
        self._scanner.log_message.connect(
            lambda msg: self.statusBar().showMessage(msg, 3000)
        )
        self._scanner.finished_ok.connect(self._on_scan_finished)
        self._scanner.failed.connect(self._on_scan_failed)
        self._scanner.start()
        self.statusBar().showMessage("开始扫描…")

    def _on_scan_finished(self) -> None:
        self.statusBar().showMessage("扫描完成", 5000)
        self.wall_page.reload()

    def _on_scan_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "扫描失败", msg)
        self.statusBar().showMessage("扫描失败", 5000)

    # ---------- 重新匹配 ----------
    def _rematch_subject(self, subject_id: int) -> None:
        QMessageBox.information(
            self, "重新匹配",
            "此功能在 M6 提供（弹 Bangumi 搜索框选条目）。当前版本请删除数据库后重扫。",
        )

    # ---------- 鼠标侧键 ----------
    def eventFilter(self, obj, event) -> bool:
        """后侧键（XButton1 / Qt.BackButton）：详情页 → 海报墙。"""
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.BackButton:
            if self.browse_stack.currentWidget() is self.detail_page:
                self._back_to_wall()
                return True
        return super().eventFilter(obj, event)

    # ---------- 关闭 ----------
    def closeEvent(self, event) -> None:
        self.monitor.stop()
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.terminate()
            self._scanner.wait(2000)
        self.db.close()
        super().closeEvent(event)
