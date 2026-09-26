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
# 季数识别 / 多季展示已写死（界面移除，见 scanner 里常量的说明）
from app.bridges.scanner import SEASON_DISPLAY
from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database
from app.utils.logger import setup_logging
from app.utils.paths import app_data_dir, qml_dir, resource_path

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
        self.library_bridge = LibraryBridge(self.db, self.api)
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
        self.library_bridge.set_api(self.api)
        # 海报墙展示模式：固定方案（见 SEASON_DISPLAY 的说明）。
        # 原先这里读 `scanner.season_display`，但界面已移除该选项，
        # 配置里可能是用户很久以前设过的旧值 —— 继续读会让"界面上
        # 已经没有了、行为却还跟着旧值走"，所以直接取常量。
        self.library_bridge.set_display_mode(SEASON_DISPLAY)
        # 「集级记录条数」可能被改小 —— 立即裁剪多余的旧记录，
        # 否则界面仍会显示上一次拉取的更多部内容（见 applyEpisodeCount）
        self.inprogress_bridge.applyEpisodeCount()
        # 监控参数（自动上传 / 轮询间隔 / 阈值）**必须显式同步**：
        # ProgressMonitor 是长生命周期对象、参数在构造时读入，
        # 不重新下发的话要重启程序才生效（见 PlayerBridge.apply_config）
        self.player_bridge.apply_config()
        log.info("配置已应用，服务已重建")

    def _on_scan_finished(self) -> None:
        """扫描完成的收尾：刷新海报墙，并**顺带同步「在看」与「动态」**。

        **为什么要在这里带上后两者**：它们的数据源是 Bangumi 的收藏接口
        （见 InProgressBridge），与本地扫描没有直接关系 —— 但用户在"新装
        / 重扫"之后想要的是一份**完整可用**的界面，而不是"海报墙满了、
        在看页和动态页还是空的、得再手动点两次刷新"。

        扫描本来就是"批量联网"的重操作，把它俩挂上只多一个请求批次
        （收藏列表 1 次 + 集级记录增量几十次），不会让等待体感变长；
        反过来，用户少点两次、也少一次"为什么这里是空的"的困惑。

        顺序：**先刷新海报墙**（纯本地、瞬间完成），再发起网络同步 ——
        这样扫描一结束界面立刻有内容，在看页随后自行填充。

        **未配置 Token 时跳过网络同步**：在看/动态的数据全部来自 Bangumi
        的收藏接口，没有 Token 必然 401（`get_me` 拿不到 username 就报
        "无法确定 Bangumi 用户名"）。扫到那一步只是白跑一趟、并在状态栏
        留下一条看不懂的报错 —— 用户此时该看到的是"去设置里填 Token"，
        而不是一条网络失败。海报墙不受影响（纯本地）。
        """
        self.library_bridge.reload()

        token = (self.config.bangumi_token or "").strip()
        if not token:
            log.info("未配置 Bangumi Token，跳过「在看 / 动态」同步"
                     "（海报墙已刷新；如需同步请在设置里填写 Token）")
            return
        try:
            self.inprogress_bridge.refresh()
            log.info("扫描完成，已顺带发起「在看 / 动态」同步")
        except Exception as e:
            # 同步失败不影响扫描结果本身（海报墙已刷新），只记警告
            log.warning("扫描后自动同步「在看 / 动态」失败（可手动刷新）: %s", e)

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
        # 图标目录的基地址（NavIcon 的 grid / rss / gear 用，拼上
        # `<name>.svg` 即为完整地址）。
        #
        # 为什么由 Python 注入：QML 的导入根是 app/，而图标在项目根的
        # resources/ 下；相对路径在开发态 / 打包态结构不同，不可靠。
        # 末尾保留斜杠便于 QML 侧直接拼接；转成 file:// URL 是因为
        # QML 的 Image.source 不认 Windows 反斜杠。
        icons_base = QUrl.fromLocalFile(
            str(resource_path("icons")) + "/").toString()
        ctx.setContextProperty("iconsBaseUrl", icons_base)
        log.info("图标目录：%s", icons_base)

        # 海报圆角遮罩（PosterCard 用它把封面裁成四角圆角）。
        #
        # 为什么需要它：纯 QML 裁不了圆角（`clip` 只支持轴对齐矩形），
        # MultiEffect 的 maskSource 又要求 source 真实渲染，几轮尝试后
        # 只有"预制遮罩图"这条路可靠 —— 这正是 Qt Quick Controls 内部
        # 也在用的做法（见 resources/poster_mask.png 的生成脚本说明）。
        # 同样由 Python 注入：相对路径在开发态/打包态结构不同。
        mask_url = QUrl.fromLocalFile(
            str(resource_path("poster_mask.png"))).toString()
        ctx.setContextProperty("posterMaskUrl", mask_url)
        log.info("海报遮罩：%s", mask_url)

        # 启动主题：**必须在 engine.load() 之前注入**。
        #
        # Theme 单例在创建时（Main.qml 实例化的那一瞬间，早于首帧渲染）就会
        # 读这个属性并初始化，因此首帧就是配置里的主题色。
        # 早期只在界面加载完之后才调 _apply_saved_theme()，结果是首帧先按
        # Theme.qml 的默认色（蓝 #2F6FEB）画出来、下一帧才跳成配置色 ——
        # 肉眼就是"打开软件的一瞬间搜索按钮是蓝的，然后才变主题色"（实测反馈）。
        is_dark, accent = self._saved_theme()
        ctx.setContextProperty("themeStartup", {"dark": is_dark, "accent": accent})

        # **启动尺寸也要在 load 之前注入**（踩坑：启动瞬间下半部分黑边）。
        #
        # 症状：软件打开的一瞬间，窗口下半截是黑的，随后才正常。
        # 原因：`Main.qml` 里写死的初始尺寸是 1120×720，而真实尺寸由
        # 下面的 `_fit_window_to_columns()` 事后用 `setProperty()` 改大
        # （到 860 高）。时序是：
        #     ① QML 加载完成 → 窗口**立即以 720 高可见**并渲染首帧
        #     ② Python 把高度改成 860
        #     ③ 新长出来的 140px 还没来得及绘制 → 那块显示为黑底
        # 即"黑边"出现在**下方**，正是被拉高的那一段。
        #
        # 修法：把尺寸算好、随主题一起注入，让 QML 首次渲染就用最终尺寸
        # （`_fit_window_to_columns` 仍保留，用于 QML 起来后兜底复核）。
        ctx.setContextProperty("windowStartup", self._compute_window_size())

        self.theme_bridge = ThemeBridge(self.config)
        ctx.setContextProperty("themeBridge", self.theme_bridge)

        # 桥接层：QML 侧直接用 library / scanner 这两个名字
        self.scanner_bridge.set_api(self.api)
        # 海报墙展示模式：固定方案（同上）
        self.library_bridge.set_display_mode(SEASON_DISPLAY)
        # 扫描完成后：刷新海报墙 + 顺带同步「在看」与「动态」
        self.scanner_bridge.set_finished_hook(self._on_scan_finished)
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
    def _compute_window_size(self) -> dict:
        """算出启动尺寸与位置（纯函数，不改任何界面状态）。

        返回 `{"width", "height", "x", "y"}`，供两处使用：
          ① `run()` 里注入给 QML 的 `windowStartup` —— **首帧就用正确尺寸**，
             避免"先按 QML 里写死的 720 高显示、再被改大"导致的下半截黑边；
          ② `_fit_window_to_columns()` —— QML 起来后复核一次（兜底：
             万一注入没生效，这里仍能把窗口纠正到算出来的尺寸）。

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
            width = max(900, target_w)
            height = max(560, target_h)
            return {
                "width": width,
                "height": height,
                "x": avail.x() + (avail.width() - width) // 2,
                "y": avail.y() + (avail.height() - height) // 2,
                "cols": cols,
            }
        # 拿不到屏幕信息（极罕见）：给一组安全值，居中留给窗口管理器
        return {
            "width": max(900, content_w),
            "height": 720,
            "x": -1,
            "y": -1,
            "cols": cols,
        }

    def _fit_window_to_columns(self, window) -> None:
        """把窗口纠正到 `_compute_window_size()` 算出的尺寸（兜底复核）。

        **为什么首帧注入之后还要做一次**：QML 侧读 `windowStartup` 属于
        "约定"，一旦那段绑定被改动或写错，窗口尺寸就会静默回到 QML 里
        写死的值。这里在加载完成后复核一次，代价是两个 setProperty。
        （与 `_apply_saved_theme()` 的关系同理：都是"启动注入 + 事后复核"。）
        """
        size = self._compute_window_size()
        window.setProperty("width", size["width"])
        window.setProperty("height", size["height"])
        if size["x"] >= 0 and size["y"] >= 0:
            window.setProperty("x", size["x"])
            window.setProperty("y", size["y"])
        log.info("窗口尺寸已适配 %s 列：%sx%s",
                 size["cols"], size["width"], size["height"])

    # ---------- 主题 ----------
    def _saved_theme(self) -> tuple[bool, str]:
        """读取配置里的主题（亮暗 + 主题色），非法值回退默认。"""
        mode = self.config.get(CFG_SECTION, CFG_THEME_MODE, DEFAULT_THEME_MODE)
        accent = self.config.get(CFG_SECTION, CFG_ACCENT, DEFAULT_ACCENT)
        if not _is_hex_color(accent):
            log.warning("配置中的主题色非法，回退默认：%r", accent)
            accent = DEFAULT_ACCENT
        return mode == "dark", accent

    def _apply_saved_theme(self) -> None:
        """把配置里的主题注入 QML 的 Theme 单例。

        实现方式：调用 Main.qml 暴露的 `applyTheme(dark, accent)` 函数 ——
        比在 Python 里反射访问 QML 单例更稳定，也便于以后扩展更多主题项。

        注意：**首帧的主题色不靠这里**（那时已经渲染完了），而是在 load 之前
        通过 `themeStartup` 上下文属性注入，由 Theme 单例在创建时读取
        （见 `run()` 里的说明）。这里保留一次调用做复核：万一 QML 侧的启动
        注入被改动或失效，界面仍会被纠正到配置的主题上。
        """
        if self.window is None:
            return
        is_dark, accent = self._saved_theme()
        try:
            self.window.applyTheme(is_dark, accent)  # type: ignore[attr-defined]
            log.info("已应用主题：mode=%s accent=%s",
                     "dark" if is_dark else "light", accent)
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
            self.library_bridge.waitTagWorker()
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("等待标签拉取结束失败：%s", e)
        # 后台的「看完同步到 Bangumi」也要等：它的收尾（写回 watched_episodes）
        # 在主线程，不等就永远不会执行（见 ProgressMonitor.wait_pending_sync）
        try:
            self.player_bridge.wait_pending_sync()
        except Exception as e:  # pragma: no cover - 防御性
            log.warning("等待后台同步结束失败：%s", e)
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
