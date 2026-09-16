"""主窗口：左侧导航 + QStackedWidget 多页。

参考 pythonguis-examples 中 Mozzarella Ashbadger 的切页模式。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database, Episode
from app.core.inprogress import InProgressService
from app.core.launcher import LauncherError, PlayerLauncher
from app.core.monitor import ProgressMonitor
from app.core.qbittorrent_api import QbClient
from app.core.rss_service import RssService
from app.core.scanner import ScanWorker
from app.ui.detail_page import DetailPage
from app.ui.inprogress_page import InProgressPage
from app.ui.match_dialog import MatchDialog
from app.ui.poster_wall import PosterWallPage
from app.ui.settings_page import SettingsPage
from app.ui.status_bar import AppStatusBar
from app.ui.subscription_page import SubscriptionPage
from app.ui.timeline_page import TimelinePage
from app.ui.widgets import nav_icon
from app.ui_layout import (
    NAV_BOTTOM_MARGIN, NAV_BUTTON_SIZE, NAV_CONTENT_GUTTER, NAV_ICON_SIZE,
    POSTER_COLUMNS, POSTER_MARGIN, POSTER_SPACING,
)

log = logging.getLogger(__name__)




class MainWindow(QMainWindow):
    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Anime Marker")
        self.setMinimumSize(900, 560)
        self._fit_to_screen()

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

        # F18/F19 服务
        self.qb = self._build_qb()
        self.inprogress_service = InProgressService(self.db, self.api, self.config)
        self.rss_service = RssService(self.db, self.config, self.api, self.qb, parent=self)
        self.rss_service.progress.connect(
            lambda msg: self.status_bar.set_message(msg, 3000)
        )
        self.rss_service.poll_finished.connect(self._on_rss_poll_finished)

        # ---- UI：内容区铺满 + 底部悬浮胶囊导航（overlay）----
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)   # 零边距：内容真正占满整个中央区
        root.setSpacing(0)

        # 页面栈：不再为导航预留布局空间，导航以 overlay 浮于其上
        self.stack = QStackedWidget(central)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # 悬浮胶囊导航（overlay，resizeEvent 中保持水平居中、不贴底）
        self.nav_pill = QFrame(central)
        self.nav_pill.setObjectName("navPill")
        pill_layout = QHBoxLayout(self.nav_pill)
        pill_layout.setContentsMargins(10, 6, 10, 6)
        pill_layout.setSpacing(8)

        self.btn_wall = self._make_nav_button("grid", "海报墙", 0)
        self.btn_wall.setChecked(True)
        pill_layout.addWidget(self.btn_wall)

        self.btn_inprogress = self._make_nav_button("play", "在看", 1)
        pill_layout.addWidget(self.btn_inprogress)

        self.btn_timeline = self._make_nav_button("clock", "动态", 2)
        pill_layout.addWidget(self.btn_timeline)

        self.btn_subscription = self._make_nav_button("rss", "订阅", 3)
        pill_layout.addWidget(self.btn_subscription)

        self.btn_settings = self._make_nav_button("gear", "设置", 4)
        pill_layout.addWidget(self.btn_settings)

        self._reposition_nav()

        poster_width = config.getint("ui", "poster_width", 200)
        self.wall_page = PosterWallPage(
            self.db,
            poster_width=poster_width,
            display_mode=config.get("scanner", "season_display", "flat"),
            parent=self,
        )
        self.detail_page = DetailPage(self.db, parent=self)
        self.inprogress_page = InProgressPage(self.inprogress_service, parent=self)
        self.timeline_page = TimelinePage(self.db, parent=self)
        self.subscription_page = SubscriptionPage(
            self.db, self.config, self.rss_service, self.qb, parent=self
        )
        self.settings_page = SettingsPage(self.config, parent=self)

        # 海报墙 / 详情 共用一层子栈
        self.browse_stack = QStackedWidget(self)
        self.browse_stack.addWidget(self.wall_page)
        self.browse_stack.addWidget(self.detail_page)

        self.stack.addWidget(self.browse_stack)       # index 0
        self.stack.addWidget(self.inprogress_page)    # index 1
        self.stack.addWidget(self.timeline_page)      # index 2
        self.stack.addWidget(self.subscription_page)  # index 3
        self.stack.addWidget(self.settings_page)      # index 4

        # 每个页面在内容层自行留出底部间距：
        # 内容可滚到胶囊下方，但最后一项不会停在胶囊遮挡位置
        self._apply_content_gutter()

        # 信号
        self.wall_page.subject_clicked.connect(self._open_detail)
        self.inprogress_page.subject_clicked.connect(self._open_detail_from_page)
        self.inprogress_page.goto_settings.connect(lambda: self._switch(4))
        self.timeline_page.subject_clicked.connect(self._open_detail_from_timeline)
        self.subscription_page.status_message.connect(
            lambda msg: self.status_bar.set_message(msg, 5000)
        )
        self.detail_page.back_clicked.connect(self._back_to_wall)
        self.detail_page.play_episode.connect(self._play_episode)
        self.detail_page.rematch_clicked.connect(self._rematch_subject)
        self.settings_page.scan_requested.connect(self._start_scan)
        self.settings_page.save_requested.connect(self._rebuild_services)

        # 状态栏（版本号 | 进度条 + 日志）
        self.status_bar = AppStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.set_message("就绪")

        # 初次加载
        self.wall_page.reload()

        # RSS 定时轮询（按配置 poll_on_start 决定是否立即抓一次）
        self.rss_service.start()

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

    def _build_qb(self) -> QbClient:
        """按配置构建 qBittorrent 客户端（连接失败在实际使用时才会暴露）。"""
        return QbClient(
            host=self.config.get("qbittorrent", "host", "127.0.0.1"),
            port=self.config.getint("qbittorrent", "port", 8080),
            username=self.config.get("qbittorrent", "username", "admin"),
            password=self.config.get("qbittorrent", "password", ""),
            category=self.config.get("qbittorrent", "category", "Bangumi"),
            save_path=self.config.get("qbittorrent", "save_path", ""),
            webui_url=self.config.get("qbittorrent", "webui_url", ""),
        )

    def _rebuild_services(self) -> None:
        """设置保存后重建依赖配置的服务。"""
        self.api = self._build_api()
        self.monitor.api = self.api
        self.qb = self._build_qb()
        self.inprogress_service.api = self.api
        self.inprogress_service._username = None  # 强制重新解析用户名
        self.rss_service.api = self.api
        self.rss_service.qb = self.qb
        self.subscription_page.qb = self.qb
        self.subscription_page.refresh_qb_status(silent=True)
        self.subscription_page.reload()

    # ---------- 导航 ----------
    def _switch(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.btn_wall.setChecked(index == 0)
        self.btn_inprogress.setChecked(index == 1)
        self.btn_timeline.setChecked(index == 2)
        self.btn_subscription.setChecked(index == 3)
        self.btn_settings.setChecked(index == 4)
        if index == 0:
            self.wall_page.reload()
        elif index == 1:
            self.inprogress_page.reload()
        elif index == 2:
            self.timeline_page.reload()
        elif index == 3:
            self.subscription_page.reload()
            # 进入订阅页时才检测 qBittorrent（静默：失败只更新状态条，不弹窗）
            self.subscription_page.refresh_qb_status(silent=True)

    def _open_detail(self, subject_id: int) -> None:
        self.detail_page.show_subject(subject_id)
        self.browse_stack.setCurrentWidget(self.detail_page)

    def _open_detail_from_timeline(self, subject_id: int) -> None:
        """从动态页跳转：先切回海报墙栈，再打开详情。"""
        self._switch(0)
        self._open_detail(subject_id)

    def _open_detail_from_page(self, subject_id: int) -> None:
        """从在看页跳转：先切回海报墙栈，再打开详情。"""
        self._switch(0)
        self._open_detail(subject_id)

    def _on_rss_poll_finished(self, summary) -> None:
        self.status_bar.set_message(
            f"轮询完成：新 {summary.new_entries} / 已推送 {summary.pushed}"
            f" / 待确认 {summary.pending} / 跳过 {summary.skipped}",
            8000,
        )

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
        self.status_bar.set_message(f"正在监控：{ep.title}")

    def _find_episode(self, episode_id: int) -> Optional[Episode]:
        for s in self.db.list_subjects():
            for e in self.db.list_episodes(s.id):
                if e.id == episode_id:
                    return e
        return None

    # ---------- 监控 ----------
    def _on_episode_watched(self, episode_id: int) -> None:
        self.status_bar.set_message(f"已自动标记看过：episode_id={episode_id}", 5000)
        if self.detail_page._subject_id is not None:
            self.detail_page.show_subject(self.detail_page._subject_id)

    def _on_monitor_error(self, msg: str) -> None:
        self.status_bar.set_message(msg, 5000)

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

        self._scanner = ScanWorker(
            paths, self.api, self.db,
            season_mode=self.config.get("scanner", "season_patterns", "cn"),
            season_display=self.config.get("scanner", "season_display", "flat"),
            accept_score=self.config.getint("scanner", "accept_score", 60),
            accept_gap=self.config.getint("scanner", "accept_gap", 20),
        )
        self._scanner.progress_changed.connect(self._on_scan_progress)
        self._scanner.item_matched.connect(self._on_scan_matched)
        self._scanner.log_message.connect(self._on_scan_log)
        self._scanner.finished_ok.connect(self._on_scan_finished)
        self._scanner.failed.connect(self._on_scan_failed)
        self._scanner.start()
        self.status_bar.start_progress(0, 100)
        self.status_bar.set_message("开始扫描…")

    # ---------- 扫描进度回调 ----------
    def _on_scan_progress(self, current: int, total: int) -> None:
        self.status_bar.set_progress(current, total)

    def _on_scan_matched(self, subject_id: int, name: str) -> None:
        # 匹配成功只更新日志文字，不打断进度条
        self.status_bar.set_message(f"✓ 已匹配：{name}")

    def _on_scan_log(self, msg: str) -> None:
        self.status_bar.set_message(msg)

    def _on_scan_finished(self) -> None:
        self.status_bar.stop_progress()
        self.status_bar.set_message("扫描完成", 5000)
        self.wall_page.reload()

    def _on_scan_failed(self, msg: str) -> None:
        self.status_bar.stop_progress()
        QMessageBox.critical(self, "扫描失败", msg)
        self.status_bar.set_message("扫描失败", 5000)

    # ---------- 重新匹配（F11） ----------
    def _rematch_subject(self, subject_id: int) -> None:
        subj = self.db.get_subject(subject_id)
        if subj is None:
            QMessageBox.warning(self, "重新匹配", "条目不存在")
            return

        dialog = MatchDialog(self.db, self.api, subj, parent=self)
        if dialog.exec() == MatchDialog.Accepted:
            self.status_bar.set_message(
                f"已手动匹配：{subj.folder_path}", 5000
            )
            self.detail_page.show_subject(subject_id)
            self.wall_page.reload()

    def _fit_to_screen(self) -> None:
        """初始宽度 = 刚好放下 5 列海报卡片；高度取屏幕 85%。

        用户仍可自由拖拽边框缩放（FlowLayout 会自动重排列数）。
        若按 5 列算出的宽度超出屏幕，则回退到屏幕 85%，避免窗口比屏幕还宽。
        """
        screen = QApplication.primaryScreen()

        # 5 列卡片所需宽度：5×卡片宽 + 4×列间距 + 左右边距 + 滚动条
        cols = POSTER_COLUMNS
        card_w = self.config.getint("ui", "poster_width", 200)
        content_w = (
            cols * card_w
            + (cols - 1) * POSTER_SPACING
            + 2 * POSTER_MARGIN
        )
        scrollbar_w = 14
        target_w = content_w + scrollbar_w

        if screen is None:
            self.resize(target_w, 700)
            return

        avail = screen.availableGeometry()
        # 超出屏幕时按屏幕 85% 兜底
        if target_w > int(avail.width() * 0.92):
            target_w = int(avail.width() * 0.85)

        h = min(860, int(avail.height() * 0.85))
        self.resize(max(900, target_w), max(560, h))
        # 居中显示
        self.move(
            avail.x() + (avail.width() - self.width()) // 2,
            avail.y() + (avail.height() - self.height()) // 2,
        )

    # ---------- 悬浮导航 ----------
    def _make_nav_button(self, kind: str, tooltip: str, index: int) -> QPushButton:
        """圆形图标导航按钮（外边界为完整圆形，由 QSS border-radius 保证）。"""
        btn = QPushButton(self.nav_pill)
        btn.setObjectName("navButton")
        btn.setCheckable(True)
        btn.setIcon(nav_icon(kind))
        btn.setIconSize(QSize(NAV_ICON_SIZE, NAV_ICON_SIZE))
        btn.setFixedSize(NAV_BUTTON_SIZE, NAV_BUTTON_SIZE)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self._switch(index))
        return btn

    def _reposition_nav(self) -> None:
        """胶囊水平居中，距底边 NAV_BOTTOM_MARGIN，浮于内容之上。"""
        self.nav_pill.adjustSize()
        w, h = self.nav_pill.width(), self.nav_pill.height()
        c = self.centralWidget()
        x = max(0, (c.width() - w) // 2)
        y = max(0, c.height() - h - NAV_BOTTOM_MARGIN)
        self.nav_pill.move(x, y)
        self.nav_pill.raise_()
        # 胶囊浮于上层，实色背景保证按钮可读
        self.nav_pill.setAttribute(Qt.WA_StyledBackground, True)

    def _apply_content_gutter(self) -> None:
        """给每个页面的滚动内容加底部内边距，避免最后一项被胶囊遮挡。

        做法：修改各页面「滚动容器内部」的 layout 底部边距，
        而不是缩小外层内容区——这样胶囊才是真正的 overlay。
        """
        pages = [
            self.wall_page, self.detail_page, self.inprogress_page,
            self.timeline_page, self.subscription_page, self.settings_page,
        ]
        for page in pages:
            self._pad_page_bottom(page)

    @staticmethod
    def _pad_page_bottom(page: QWidget) -> None:
        """递归找到页面里的滚动宿主 layout，追加底部内边距。"""
        from PySide6.QtWidgets import QScrollArea

        scroll = page.findChild(QScrollArea)
        if scroll is None:
            # 无滚动区的页面（如详情页左侧）直接给页面自身 padding
            layout = page.layout()
            if layout is not None:
                m = layout.contentsMargins()
                layout.setContentsMargins(
                    m.left(), m.top(), m.right(),
                    m.bottom() + NAV_CONTENT_GUTTER,
                )
            return

        container = scroll.widget()
        if container is None or container.layout() is None:
            return
        m = container.layout().contentsMargins()
        container.layout().setContentsMargins(
            m.left(), m.top(), m.right(), m.bottom() + NAV_CONTENT_GUTTER
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_nav()

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
        self.rss_service.stop()
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.terminate()
            self._scanner.wait(2000)
        self.db.close()
        super().closeEvent(event)
