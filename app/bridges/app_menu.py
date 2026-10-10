"""状态栏「更多」菜单的桥接层。

菜单里五件事：检查更新 / 打开日志目录 / 帮助 / 关于 / 反馈问题。
本类负责**能落到 Python 或系统那头的部分**（打开目录、打开链接、
检查更新）；「关于」是个纯展示小窗，由 QML 自己管，不走这里。

为什么单独一个桥接而不是塞进 `SettingsBridge`：那几个动作没有一个
跟"配置"有关，塞进去会让那个类既管配置又管外链。这里只做"点一下就走"
的动作，没有状态要维护 —— 唯一的"状态"是检查更新的结果。

**检查更新的两条路径，反馈口径不同**（见 `checkUpdate` / `startInitialCheck`）：
  自动（**每天第一次**开软件后延迟一次）：**只有"查到新版"才有动静**，
    且只是点亮菜单里的小红点 —— 不弹窗、不动状态栏。每次开软件都冒一句
    话太吵。同一天再开软件**不发这次请求**（记账见 update_check 的
    `auto_check_due`），想查点「检查更新」。
  手动（用户点了）：**必须有回应**，否则用户不知道点没点上，
    且**任何时候都能查**，不受上面那条"一天一次"的限制。

**小红点一旦亮起就跨进程亮着**：查到的版本号落在 config 里，开软件时直接
还原（见 `load_known_newer`）—— 不然"今天已经查过，所以这次不查"会连带
把红点也弄丢，用户当天再开一次软件就以为没更新了。
"""

from __future__ import annotations

import logging
import platform

from PySide6.QtCore import (
    Property, QObject, QThread, QTimer, QUrl, QUrlQuery, Signal, Slot,
)
from PySide6.QtGui import QDesktopServices

from app import __version__
from app.core.config import Config
from app.core.update_check import (
    ISSUES_NEW, REPO_PAGE, UpdateInfo, auto_check_due, fetch_latest,
    load_known_newer, mark_auto_checked, remember_latest,
)
from app.utils.paths import logs_dir, resource_path

log = logging.getLogger(__name__)

#: 启动后隔多久做那次静默检查（毫秒）。
#
# 为什么要等：启动瞬间要加载界面、跑一次媒体库扫描、RSS 还可能立刻抓一轮
# —— 这会儿再插一个网络请求只会让开机更慢。8 秒后各项都落了地再问。
INITIAL_DELAY_MS = 8000


class _UpdateWorker(QThread):
    """后台问一次 GitHub（见 update_check.fetch_latest）。"""

    done = Signal(object)          # UpdateInfo

    def __init__(self, proxy: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: 设置页「代理」里填的那个（空串 = 走系统代理）。**在 _start 里
        #: 现读配置**，所以设置页改完不用重启，下一次检查就用新的
        #: （同 scanner 的 accept_score 那套：每个 worker 启动时现读）。
        self._proxy = proxy

    def run(self) -> None:
        # fetch_latest 自己吞掉所有异常（见模块说明），这里不必再兜
        self.done.emit(fetch_latest(proxy=self._proxy))


class AppMenuBridge(QObject):
    """状态栏「更多」菜单的动作端。"""

    hasUpdateChanged = Signal()
    latestTagChanged = Signal()
    checkingChanged = Signal()
    #: 给状态栏的一句话（检查更新的手动结果）
    statusMessage = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # 两件事都要读它：自动检查"今天的份"的记账、以及红点的还原/落盘
        # （见 update_check 里那段说明）。
        self._config = config
        self._worker: _UpdateWorker | None = None
        # 上次运行查到的新版本号 → **开软件就先把红点还原出来**，
        # 不等今天的检查（今天多半不查了，见 startInitialCheck）。
        # 直接赋值、不发信号：这会儿 QML 还没绑上来，发也没人听。
        stored = load_known_newer(config)
        self._has_update = bool(stored)
        self._latest_tag = stored
        self._checking = False
        # 这一次请求回来时该按"手动"处理吗（见 _start 的并发说明）
        self._pending_manual = False
        # 启动时那次静默检查的定时器（只在界面加载后由 startInitialCheck 启动）
        self._initial_timer: QTimer | None = None

    # ---------- 状态（QML 绑定用）----------
    @Property(bool, notify=hasUpdateChanged)
    def hasUpdate(self) -> bool:
        """是否有比本地新的版本 —— 菜单项右侧那个小红点读它。

        **亮着就一直亮**：值落盘，重开软件时由 __init__ 还原（见模块说明）。
        """
        return self._has_update

    @Property(str, notify=latestTagChanged)
    def latestTag(self) -> str:
        """远端最新版本号（没有 / 未知时为空串）—— 菜单项右侧显示它。"""
        return self._latest_tag

    @Property(bool, notify=checkingChanged)
    def checking(self) -> bool:
        """是否正在检查（菜单项据此显示"检查更新…"并禁用点击）。"""
        return self._checking

    @Property(str, constant=True)
    def version(self) -> str:
        """本地版本号（「关于」小窗用；与状态栏那份同源）。"""
        return __version__

    # ---------- 打开动作 ----------
    @Slot(result=bool)
    def openLogsDir(self) -> bool:
        """打开日志目录。

        **为什么值得单列一项**：日志在 `data/logs/`，普通用户根本找不到；
        出问题时让他"把日志发我"是常态，一键打开省掉一轮来回。
        """
        path = logs_dir()
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        log.info("打开日志目录：%s（%s）", path, "成功" if ok else "失败")
        if not ok:
            self.statusMessage.emit(f"打不开目录：{path}")
        return bool(ok)

    @Slot(result=bool)
    def openHelp(self) -> bool:
        """用系统默认程序打开随包的帮助文档。

        **走系统浏览器而不是应用内渲染**：帮助是手写的 HTML，里面有表格
        和排版；Qt 的 `Text.MarkdownText` 不支持表格，应用内渲染会散架。
        而链接里放的是 `file://` 本地文件（随包发布、版本锁死），
        **不会联网** —— 也不会像"指向线上文档"那样，让旧版软件点开看到
        新版的说明。
        """
        path = resource_path("help.html")
        if not path.exists():
            log.warning("帮助文档不存在：%s", path)
            self.statusMessage.emit("帮助文档缺失（打包时漏了 resources/help.html？）")
            return False
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        log.info("打开帮助文档：%s（%s）", path, "成功" if ok else "失败")
        return bool(ok)

    @Slot(result=bool)
    def openRepo(self) -> bool:
        """打开项目主页（「关于」小窗里的仓库链接）。"""
        return bool(QDesktopServices.openUrl(QUrl(REPO_PAGE)))

    @Slot(result=bool)
    def openIssues(self) -> bool:
        """打开「新建 issue」页面，并**预填版本号与运行环境**。

        预填不是锦上添花：反馈里最常见的就是"你什么版本、什么系统"，
        让用户自己填一半人会漏。这里把已知的都带上，他只要写现象。
        """
        body = (
            "### 问题描述\n\n（请描述遇到了什么）\n\n"
            "### 复现步骤\n\n1. \n2. \n\n"
            "### 期望行为\n\n\n\n"
            "### 运行环境\n\n"
            f"- Anime Marker v{__version__}\n"
            f"- {platform.system()} {platform.release()}（{platform.version()}）\n"
        )
        url = QUrl(ISSUES_NEW)
        # **用 QUrlQuery 而不是手拼 `?body=...`**：正文里有换行、中文、
        # `#`/`&` 这些字符，手拼会拼出一个非法 URL（`#` 之后会被当成
        # 锚点截断）。QUrlQuery 负责百分号编码。
        query = QUrlQuery()
        query.addQueryItem("body", body)
        url.setQuery(query)
        ok = QDesktopServices.openUrl(url)
        log.info("打开反馈页（%s）", "成功" if ok else "失败")
        return bool(ok)

    # ---------- 检查更新 ----------
    @Slot()
    def startInitialCheck(self) -> None:
        """启动后延迟一次**静默**检查（由 QmlApp 在界面加载完成后调用）。

        **今天已经查过就整个不挂定时器**（而不是挂上去再空转）：连那 8 秒
        的等待都省掉，日志里也留不下"其实什么也没做"的痕迹。
        """
        if not auto_check_due(self._config):
            log.info("今天已自动检查过更新，跳过（可手动「检查更新」）")
            return
        if self._initial_timer is None:
            self._initial_timer = QTimer(self)
            self._initial_timer.setSingleShot(True)
            self._initial_timer.timeout.connect(self._check_silently)
        self._initial_timer.start(INITIAL_DELAY_MS)

    @Slot()
    def checkUpdate(self) -> None:
        """用户点了「检查更新」——**一定要给回应**（见模块说明）。

        有新版时**直接打开发布页**：菜单行本来就显示着远端版本号和小红点，
        用户是在"已经看到有新版"的前提下点的这一下，再弹一个"要打开吗"
        纯属多一次点击。
        """
        self._start(manual=True)

    def _check_silently(self) -> None:
        # **先记账、再发请求**：这次请求发出去了就算"今天的份"
        # （失败也不重试的理由见 update_check 里那段说明）。
        #
        # 放在这里而不是 `_on_checked` 里：万一请求还没回来用户就关了软件，
        # 今天也算查过 —— 否则反复开关软件会反复发请求，正是要避免的。
        mark_auto_checked(self._config)
        self._start(manual=False)

    def _start(self, manual: bool) -> None:
        if self._worker is not None and self._worker.isRunning():
            # 自动那次还没回来、用户又点了手动：不要叠第二个请求，
            # 但手动的"一定要有回应"不能破 —— 记下来，回来时一并处理。
            if manual:
                self._pending_manual = True
            return
        self._pending_manual = bool(manual)
        self._set_checking(True)
        # 代理**每次现读**（与 Bangumi 客户端同一个键）：设置页改完不必重启，
        # 下一次检查就走新的。留空则空串 → fetch_latest 不指定代理、走系统代理。
        self._worker = _UpdateWorker(
            self._config.get("bangumi", "proxy", ""))
        self._worker.done.connect(self._on_checked)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_checked(self, info: UpdateInfo) -> None:
        self._worker = None
        manual, self._pending_manual = self._pending_manual, False
        self._set_checking(False)

        # 小红点 / 版本号：两种路径都要更新（自动那次的全部意义就在这里）
        #
        # 两种"确定"的结果都顺手落盘，好让红点跨进程亮着（见 load_known_newer）。
        # **"failed" / "no_release" 一律不动记账**：查失败不是"没有新版"，
        # 把上次查到的结果抹掉，就成了"断网一次红点就没了"。
        if info.state == "newer":
            remember_latest(self._config, info.tag)
            if self._latest_tag != info.tag:
                self._latest_tag = info.tag
                self.latestTagChanged.emit()
            if not self._has_update:
                self._has_update = True
                self.hasUpdateChanged.emit()
        elif info.state == "latest":
            remember_latest(self._config)
            if self._has_update:
                self._has_update = False
                self.hasUpdateChanged.emit()
            if self._latest_tag:
                self._latest_tag = ""
                self.latestTagChanged.emit()

        if not manual:
            return                       # 自动那次：有新版也只点红点，不出声

        log.info("手动检查更新：%s", info)
        if info.state == "newer":
            self.statusMessage.emit(
                f"发现新版本 {info.tag}（当前 v{__version__}），已打开发布页")
            QDesktopServices.openUrl(QUrl(info.url))
        elif info.state == "latest":
            self.statusMessage.emit(f"已是最新版本（v{__version__}）")
        elif info.state == "no_release":
            self.statusMessage.emit("仓库还没有发布过版本，暂时无从比对")
        else:
            self.statusMessage.emit(f"检查更新失败：{info.error or '未知原因'}")

    def _set_checking(self, value: bool) -> None:
        if self._checking != value:
            self._checking = value
            self.checkingChanged.emit()

    # ---------- 收尾 ----------
    def waitWorker(self, ms: int = 3000) -> None:
        """退出时等待进行中的检查线程（QmlApp.shutdown 调用）。"""
        if self._initial_timer is not None:
            self._initial_timer.stop()
        w = self._worker
        if w is not None and w.isRunning():
            log.info("等待检查更新线程结束…")
            w.wait(ms)
