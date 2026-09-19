"""QML 应用外壳（QmlApp）。

启动顺序：
1. 配置日志（main.py）
2. QApplication（QML 也需要 QApplication，不是 QGuiApplication）
3. 加载配置
4. 创建核心服务（Database / BangumiClient）与桥接层
5. 读取主题配置并注入 Theme 单例
6. 创建 QQmlApplicationEngine，注册导入路径与上下文对象
7. 加载 Main.qml，并按「恰好 N 列海报」反推初始窗口尺寸

入口：`python main.py`（`run_qml.py` 为兼容转发）。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

from app import USER_AGENT, __version__
from app.bridges import (
    InProgressBridge, LibraryBridge, MatchBridge, PlayerBridge, RssBridge,
    ScannerBridge, SettingsBridge,
)
from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database
from app.utils.logger import setup_logging
from app.utils.paths import app_data_dir, qml_dir

log = logging.getLogger(__name__)

# 配置文件里的节/键（§5.12）
CFG_SECTION = "ui"
CFG_THEME_MODE = "theme_mode"
CFG_ACCENT = "accent_color"

DEFAULT_THEME_MODE = "light"
DEFAULT_ACCENT = "#2F6FEB"


class ThemeBridge(QObject):
    """主题桥接：把配置里的主题设置注入 QML，并把 QML 的改动写回配置。

    为什么需要它：QML 侧 `Theme` 是单例，Python 拿不到实例句柄，
    但可以通过 `engine.rootObjects()` 拿到主窗口，再访问其 Theme 上下文 —— 
    这样做很绕；更稳的方式是把「读写主题」封装成 Slot/Property 交给 QML 调用。
    """

    def __init__(self, config: Config, window: Any = None) -> None:
        super().__init__()
        self.config = config
        self.window = window

    @Slot(result=str)
    def themeMode(self) -> str:
        return self.config.get(CFG_SECTION, CFG_THEME_MODE, DEFAULT_THEME_MODE)

    @Slot(result=str)
    def accentColor(self) -> str:
        return self.config.get(CFG_SECTION, CFG_ACCENT, DEFAULT_ACCENT)

    @Slot(str)
    def saveThemeMode(self, mode: str) -> None:
        """QML 切换模式时调用（立即写盘，主题是低频操作，无需批量保存）。"""
        mode = (mode or "").strip() or DEFAULT_THEME_MODE
        self.config.set(CFG_SECTION, CFG_THEME_MODE, mode)
        self.config.save()
        log.info("主题模式已保存：%s", mode)

    @Slot(str)
    def saveAccent(self, color_value: str) -> None:
        """QML 切换主题色时调用。"""
        color_value = (color_value or "").strip() or DEFAULT_ACCENT
        if not _is_hex_color(color_value):
            log.warning("忽略非法主题色：%r", color_value)
            return
        self.config.set(CFG_SECTION, CFG_ACCENT, color_value.upper())
        self.config.save()
        log.info("主题色已保存：%s", color_value)


def _is_hex_color(text: str) -> bool:
    """校验 #RRGGBB / #AARRGGBB，避免把脏值写进配置。"""
    if not text.startswith("#"):
        return False
    body = text[1:]
    if len(body) not in (6, 8):
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


class QmlApp:
    """QML 应用外壳：持有 engine 与窗口引用，暴露给桥接层使用。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.engine: Optional[QQmlApplicationEngine] = None
        self.window = None
        self.theme_bridge: Optional[ThemeBridge] = None

        # ---- 核心服务 ----
        self.db = Database()
        self.api = self._build_api()

        # ---- 桥接层 ----
        self.library_bridge = LibraryBridge(self.db)
        self.scanner_bridge = ScannerBridge(self.db, self.config)
        self.settings_bridge = SettingsBridge(self.config)
        self.player_bridge = PlayerBridge(self.db, self.config, self.api)
        self.match_bridge = MatchBridge(self.db, self.api)
        self.inprogress_bridge = InProgressBridge(self.db, self.config, self.api)
        # 收藏列表拉取完成后，让 QML 侧的 library.inProgress 重新取数
        self.inprogress_bridge.set_done_hook(self.library_bridge.reloadInProgress)
        # 集级观看记录（第二阶段）拉完后，刷新动态页数据源
        self.inprogress_bridge.set_episode_done_hook(
            self.library_bridge.reloadWatchedEpisodes)
        self.rss_bridge = RssBridge(self.db)

        # 配置保存后重建依赖配置的服务
        self.settings_bridge.saved.connect(self._rebuild_services)
        # 「保存并扫描」按钮
        self.settings_bridge.scanRequested.connect(self._on_scan_requested)

    # ---------- 配置变更 ----------
    def _rebuild_services(self) -> None:
        """配置保存后重建依赖配置的对象。"""
        self.api = self._build_api()
        self.scanner_bridge.set_api(self.api)
        self.player_bridge.set_api(self.api)
        self.match_bridge.set_api(self.api)
        self.inprogress_bridge.set_api(self.api)
        # 海报墙展示模式可能变了
        self.library_bridge.set_display_mode(
            self.config.get("scanner", "season_display", "flat")
        )
        # 「集级记录条数」可能被改小 —— 立即裁剪多余的旧记录，
        # 否则界面仍会显示上一次拉取的更多部内容（见 applyEpisodeCount）
        self.inprogress_bridge.applyEpisodeCount()
        log.info("配置已应用，服务已重建")

    def _on_scan_requested(self) -> None:
        """设置页点「保存并扫描」。"""
        if self.scanner_bridge.running:
            log.info("扫描进行中，忽略本次请求")
            return
        self.scanner_bridge.start()

    def _build_api(self) -> BangumiClient:
        return BangumiClient(
            token=self.config.get("bangumi", "token", ""),
            api_base=self.config.get("bangumi", "api_base", "https://api.bgm.tv"),
            proxy=self.config.get("bangumi", "proxy", ""),
            user_agent=self.config.get(
                "bangumi", "user_agent", USER_AGENT),
        )

    # ---------- 启动 ----------
    def run(self, argv: list[str]) -> int:
        app = QApplication(argv)
        app.setApplicationName("Anime Marker")
        app.setOrganizationName("AnimeMarker")
        app.setApplicationDisplayName("Anime Marker")

        self.engine = QQmlApplicationEngine()

        # 注册 QML 模块目录：让 `import AnimeMarker` 与 Theme 单例生效
        qml_root = qml_dir()
        if not qml_root.exists():
            log.error("QML 目录不存在：%s", qml_root)
            raise FileNotFoundError(f"QML 目录不存在：{qml_root}")
        self.engine.addImportPath(str(qml_root.parent))
        log.info("QML 导入路径：%s", qml_root.parent)

        # ---- 上下文对象 ----
        ctx = self.engine.rootContext()
        ctx.setContextProperty("appVersion", __version__)
        self.theme_bridge = ThemeBridge(self.config)
        ctx.setContextProperty("themeBridge", self.theme_bridge)

        # 桥接层：QML 侧直接用 library / scanner 这两个名字
        self.scanner_bridge.set_api(self.api)
        self.library_bridge.set_display_mode(
            self.config.get("scanner", "season_display", "flat")
        )
        # 扫描完成后刷新海报墙数据
        self.scanner_bridge.set_finished_hook(self.library_bridge.reload)
        ctx.setContextProperty("library", self.library_bridge)
        ctx.setContextProperty("scanner", self.scanner_bridge)
        ctx.setContextProperty("settingsBridge", self.settings_bridge)
        ctx.setContextProperty("player", self.player_bridge)
        ctx.setContextProperty("matcher", self.match_bridge)
        ctx.setContextProperty("inprogress", self.inprogress_bridge)
        ctx.setContextProperty("rss", self.rss_bridge)

        # ---- 加载主界面 ----
        main_qml = qml_root / "Main.qml"
        self.engine.load(QUrl.fromLocalFile(str(main_qml)))

        roots = self.engine.rootObjects()
        if not roots:
            log.error("QML 加载失败（rootObjects 为空）：%s", main_qml)
            return 1
        self._fit_window_to_columns(roots[0])
        self.window = roots[0]
        self.theme_bridge.window = self.window
        # 文件对话框需要 QWidget 作为父窗口；QML 的 Window 不是 QWidget，
        # 传 None 也能工作（对话框居中到屏幕），但指定父窗口体验更好。
        self.settings_bridge.set_dialog_parent(None)
        log.info("QML 界面加载完成：%s", main_qml)

        # ---- 应用持久化的主题（必须在界面加载后调用，此时 Theme 单例已就绪）----
        self._apply_saved_theme()

        return app.exec()

    # ---------- 窗口尺寸 ----------
    def _fit_window_to_columns(self, window) -> None:
        """初始窗口宽度 = 恰好放下 N 列海报卡片（与旧版 MainWindow._fit_to_screen 一致）。

        背景：QML 的 `Flow` 会按可用宽度自动决定列数。若窗口宽度只是"够放 4 列多一点"，
        就会出现「4 列 + 右侧一大片空白」的观感 —— 既浪费空间又不好看。
        这里精确反推「N 列所需宽度」，让初始状态正好铺满且不留半列空白。

        计算口径必须与 PosterWallPage 的 Flow 保持一致：
            窗口宽 = 左右 pagePadding × 2
                   + N × 卡片宽 + (N-1) × 卡片间距
                   + 滚动条宽（预留，避免出现滚动条后挤掉一列）
        """
        try:
            cols = self.config.getint("ui", "poster_columns", 5)
            card_w = self.config.getint("ui", "poster_width", 200)
        except Exception:  # pragma: no cover - 配置异常时退回默认
            cols, card_w = 5, 200

        # 与 Theme.qml 的常量对齐
        page_padding = 24
        spacing = 16
        scrollbar_w = 14          # 竖向滚动条宽度（Qt 默认约 12~14）

        content_w = (
            page_padding * 2
            + cols * card_w
            + (cols - 1) * spacing
            + scrollbar_w
        )

        # 屏幕上放不下时，退回到屏幕可用宽度的 90%
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(content_w, int(avail.width() * 0.90))
            target_h = min(860, int(avail.height() * 0.85))
        else:
            target_w, target_h = content_w, 720

        window.setProperty("width", max(900, target_w))
        window.setProperty("height", max(560, target_h))

        # 居中显示
        if screen is not None:
            avail = screen.availableGeometry()
            window.setProperty(
                "x", avail.x() + (avail.width() - window.property("width")) // 2)
            window.setProperty(
                "y", avail.y() + (avail.height() - window.property("height")) // 2)
        log.info("窗口尺寸已适配 %s 列：%sx%s", cols, target_w, target_h)

    # ---------- 主题 ----------
    def _apply_saved_theme(self) -> None:
        """把配置里的主题注入 QML 的 Theme 单例。

        实现方式：调用 Main.qml 暴露的 `applyTheme(dark, accent)` 函数 ——
        比在 Python 里反射访问 QML 单例更稳定，也便于以后扩展更多主题项。
        """
        if self.window is None:
            return
        mode = self.config.get(CFG_SECTION, CFG_THEME_MODE, DEFAULT_THEME_MODE)
        accent = self.config.get(CFG_SECTION, CFG_ACCENT, DEFAULT_ACCENT)
        if not _is_hex_color(accent):
            log.warning("配置中的主题色非法，回退默认：%r", accent)
            accent = DEFAULT_ACCENT

        is_dark = mode == "dark"
        try:
            self.window.applyTheme(is_dark, accent)  # type: ignore[attr-defined]
            log.info("已应用主题：mode=%s accent=%s", mode, accent)
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("应用主题失败（将使用默认值）：%s", e)

    # ---------- 关闭 ----------
    def shutdown(self) -> None:
        """释放资源：停止扫描、关闭数据库。"""
        try:
            if self.scanner_bridge.running:
                self.scanner_bridge.cancel()
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("停止扫描失败：%s", e)
        try:
            self.inprogress_bridge.cancel()
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("等待收藏/集级拉取结束失败：%s", e)
        try:
            self.db.close()
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("关闭数据库失败：%s", e)
        log.info("QML 应用关闭")


def main() -> int:
    # 1. 日志（必须在任何 logging.getLogger() 之前）
    setup_logging(app_data_dir() / "logs")

    # 2. 配置（首次运行会按 DEFAULTS 生成 config.ini）
    config = Config()

    # 3. 启动 QML 应用
    app = QmlApp(config)
    try:
        return app.run(sys.argv)
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
