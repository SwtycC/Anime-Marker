"""扫描桥接：把 ScanWorker（QThread）的进度与结果转给 QML。

关键点：**QML 不能直接持有 QThread**。
ScanWorker 是 QThread 子类，其信号跨线程发射；这里用一层 QObject 包装，
把信号原样转发并追加 QML 友好的聚合字段（百分比、文本摘要）。

用法（QML）：
    scannerBridge.start()
    Connections { target: scannerBridge
        function onProgressChanged(cur, total) { ... }
        function onLogMessage(msg) { ... }
        function onFinished(count) { model.reload() }
    }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.core.bangumi_api import BangumiClient
from app.core.config import Config
from app.core.database import Database
from app.core.scanner import ScanWorker

log = logging.getLogger(__name__)

# 季数识别与多季展示的**固定方案**（原先由设置页的两个分段选择器决定，
# 现已移除界面、写死在此）。
#
# 放模块级常量而不是散在调用处：qml_app 也要用同一个值（它要据此决定
# 海报墙的展示模式），两处必须一致 —— 写死两份字面量迟早会漂移。
SEASON_MODE = "cn"        # 识别「第X季 / S1 / Season 1」，不含罗马数字
SEASON_DISPLAY = "flat"   # 多季平铺（不聚合为系列卡片）


class ScannerBridge(QObject):
    """扫描控制器。"""

    # ---- 状态（QML 绑定用）----
    runningChanged = Signal()
    progressChanged = Signal(int, int)      # current, total
    logMessage = Signal(str)
    finished = Signal(int, int)             # matched, pending
    failed = Signal(str)
    # 本次扫描的结论文案变了（见 resultSummary）
    resultSummaryChanged = Signal()
    # 「添加动漫」完成：携带新入库条目的 subject_id（0 = 未入库，如待确认）
    #
    # 为什么要单独一个信号：这条路径由用户从弹窗发起、预期是"加完直接
    # 进详情页"，需要知道**具体是哪一条**；而通用的 finished(matched,
    # pending) 只给数量，QML 无从得知该跳去哪一条。
    subjectAdded = Signal(int)
    # 某条目**因网络/接口失败而未匹配上**：携带 (名称, 原因摘要)。
    #
    # 界面据此弹一条**黄色提示**说明原因（用户明确要求），而不是让用户
    # 在一长串 urllib3 重试日志里自己找线索。见 ScanWorker.match_failed。
    matchFailed = Signal(str, str)

    def __init__(
        self,
        db: Database,
        config: Config,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._api: Optional[BangumiClient] = None
        self._worker: Optional[ScanWorker] = None
        self._running = False
        self._current = 0
        self._total = 0
        # 本次扫描的统计
        self._matched = 0
        self._pending = 0
        # 扫描完成后由 QmlApp 注入的回调（用于刷新 LibraryBridge）
        self._on_finished_hook = None
        # 本次扫描是否为「单条目重扫」（决定完成后要不要发起在看/动态同步）
        self._single_scan = False
        # 本次是否为「添加动漫」（弹窗选目录发起）：
        # 完成后把新条目 id 经 subjectAdded 回传，供 QML 直接进详情页
        self._adding = False
        # 本次「添加动漫」新入库的 subject_id（0 = 未入库）
        self._added_subject_id = 0
        # 本次「添加动漫」是否勾选了匹配（决定播报要不要提"未匹配"）
        self._last_match_requested = True
        # 本次「详情页重扫」的目标条目**本来就关联着 Bangumi** 吗
        # （决定"没重新匹配"要不要报成未匹配，见 _make_summary）
        self._target_linked = False
        # 本次扫描的结论文案（空串 = 还没扫过）
        self._result_summary = ""

    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    def set_finished_hook(self, hook) -> None:
        """扫描结束时调用（QmlApp 用来让 LibraryBridge 刷新）。"""
        self._on_finished_hook = hook

    # ---------- 状态属性 ----------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    @Property(int, notify=progressChanged)
    def current(self) -> int:
        return self._current

    @Property(int, notify=progressChanged)
    def total(self) -> int:
        return self._total

    @Property(float, notify=progressChanged)
    def percent(self) -> float:
        """进度百分比 0~100，QML 进度条直接绑这个。"""
        if self._total <= 0:
            return 0.0
        return min(100.0, self._current * 100.0 / self._total)

    @Property(bool, notify=runningChanged)
    def singleScan(self) -> bool:
        """本次扫描是否为**单个目录**（「添加动漫」或详情页重扫）。

        QML 据此换用更贴合的说法 —— 全量扫描报"匹配 N 个，待确认 M 个"
        是自然的（扫了一整个库），但只加了一个目录时这么报就很怪，
        用户要的是"这一部到底加上没有"。
        """
        return self._single_scan

    @Property(bool, notify=runningChanged)
    def lastMatchRequested(self) -> bool:
        """本次「添加动漫」是否勾选了"匹配 Bangumi"。

        用来决定要不要提"未匹配"：用户**主动选择不匹配**时，说"未匹配 N"
        是废话（他没要求匹配），只报"已加入"即可。见 Main.qml 的播报逻辑。
        """
        return self._last_match_requested

    @Property(str, notify=resultSummaryChanged)
    def resultSummary(self) -> str:
        """本次扫描的**结论文案**（顶部提示条显示的那一句）。

        **为什么文案由后端给、而不是 QML 按数量拼**：单条目扫描的结论
        **推不出来**。`matched` 只数"本次新匹配上了几条"，而详情页重扫一条
        **已经关联 Bangumi** 的条目时，扫描会走 manual 记录保护 —— 跳过重新
        匹配、只回填集数（见 `ScanWorker._process`），数量因此恒为
        0；界面据此报"扫描完成：未匹配"，可那条目明明是匹配着的。
        同理「添加动漫」没勾选匹配时也不该提"未匹配"（用户压根没要求匹配）
        —— 这正是 `lastMatchRequested` 当初想解决的问题，只是 QML 侧一直没接。

        全量扫描仍然用"匹配 N 个，待确认 M 个"（扫一整个库，用计数才自然）。
        """
        return self._result_summary

    def _make_summary(self, was_adding: bool) -> str:
        """算出结论文案。

        `was_adding` 由调用方传：`_on_ok` 里 `_adding` 会在取完新条目 id 后
        就被置回 False（那是给下一次扫描复位用的），算文案时读不到它了。
        """
        if not self._single_scan:
            return ("扫描完成：匹配 %d 个，待确认 %d 个"
                    % (self._matched, self._pending))
        if was_adding:
            if self._matched:
                return "扫描完成：已匹配"
            if not self._added_subject_id:
                # 空目录 / 没有视频：说"未匹配"会让人以为匹配坏了
                return "扫描完成：没有找到可入库的视频"
            if not self._last_match_requested:
                # 用户主动没勾「匹配 Bangumi」：只报"加上了"，
                # 提"未匹配"是废话（他没要求匹配）
                return "扫描完成：已加入"
            return "扫描完成：未匹配"
        # ---- 详情页重扫 ----
        # 目标条目**本来就关联着** Bangumi 时，本次没重新匹配也是"已匹配"：
        # 重扫的用处是刷新集数，不是重新认领作品。
        return ("扫描完成：已匹配"
                if (self._matched or self._target_linked)
                else "扫描完成：未匹配")

    # ---------- 动作 ----------
    @Slot(result=str)
    def validate(self) -> str:
        """启动前校验，返回错误文本（空串 = 可开始）。

        单独抽出来是为了让 QML 能先弹确认框再真正开扫 —— Slot 不能
        同步返回"要不要继续"，故分成两步。
        """
        if self._running:
            return "扫描正在进行中"
        if self._api is None:
            return "Bangumi API 未初始化"
        paths = self._config.library_paths
        if not paths:
            return "请先配置媒体库根目录"
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            return "以下路径不存在：\n" + "\n".join(missing)
        return ""

    @Slot()
    def start(self) -> None:
        """开始扫描（异步）。调用前请先用 validate() 检查。"""
        if self._running:
            log.info("扫描已在运行，忽略本次请求")
            return
        err = self.validate()
        if err:
            self.failed.emit(err)
            return

        self._api = self._api or BangumiClient()
        self._matched = 0
        self._pending = 0
        self._current = 0
        self._total = 0

        worker = ScanWorker(
            self._config.library_paths,
            self._api,
            self._db,
            # 这两项**已从界面移除，固定写死**（不再读配置）：
            #   season_mode    = "cn"    → 识别「第X季 / S1 / Season 1」
            #   season_display = "flat"  → 多季平铺展示
            # 依据：两组选项实际是"一次选定就再也不改"的偏好，放在设置页
            # 只增加决策负担；而 `all`（额外识别罗马数字）会显著抬高误匹配
            # 风险，「聚合」也会让海报墙与"按季追番"的直觉不符。
            # 配置键仍保留在 DEFAULTS 里，仅为兼容旧 config.ini（见那里说明）。
            season_mode=SEASON_MODE,
            season_display=SEASON_DISPLAY,
            accept_score=self._config.getint("scanner", "accept_score", 60),
            accept_gap=self._config.getint("scanner", "accept_gap", 20),
            # 「集数按顺序对应」开关：与 accept_score 一样在**每次启动扫描**
            # 时现读 —— 设置页保存后下一次扫描即生效，无需重启。
            align_by_order=self._config.getbool(
                "scanner", "ep_align_order", True),
            # 「附加内容显示」开关（同上，启动扫描时现读）：关闭后 SP/OVA/
            # NCOP… 完全不进集数列表（见 ScanWorker.extra_show 的说明）
            extra_show=self._config.getbool(
                "scanner", "extra_show", True),
            # 「附加内容独立编号」开关（同上，启动扫描时现读）
            extra_numbering=self._config.getbool(
                "scanner", "extra_numbering", True),
        )
        worker.progress_changed.connect(self._on_progress)
        worker.item_matched.connect(self._on_matched)
        worker.log_message.connect(self._on_log)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_failed)
        worker.match_failed.connect(self._on_match_failed)
        worker.item_pending.connect(self._on_pending)

        self._worker = worker
        # 全量扫描：允许完成回调发起「在看/动态」同步（见 _on_ok）
        self._single_scan = False
        self._target_linked = False     # 见 _make_summary（全量用不到，清掉免残留）
        self._set_running(True)
        worker.start()
        log.info("扫描已启动，媒体库：%s", self._config.library_paths)

    @Slot(int)
    def startSubject(self, subject_id: int) -> None:
        """单个条目重新扫描（详情页「添加」按钮）。

        **为什么需要它**：全量扫描要把整个媒体库跑一遍（每个条目都要
        发 Bangumi 请求），只为了刷新一部番的集数/标题/别名太浪费。
        这里只处理该条目自己的目录。

        与 `start()` 共用同一套 worker 与信号：进度、日志、完成回调、
        finished 通知全都照旧 —— 界面无需为单扫写第二套处理逻辑。

        **媒体库路径照常传下去，但候选来源仍由 `only_folder` 决定**
        （`_collect_candidates` 一给 `only_folder` 就只从目标目录下探）。
        那条路径另有用途：单扫要**复算祖先上下文**才能和全扫算出同一套
        关键词（见 `ScanWorker._ancestor_state`）—— 早先这里刻意传 `[]`，
        结果同一条目"重扫"与"全扫"得到不同关键词、甚至被写下不同的
        `series_name`。
        用户改过媒体库路径、旧条目已不在库下时，复算自然失败并退回旧行为。
        """
        if self._running:
            log.info("扫描进行中，忽略单条目扫描请求")
            self.logMessage.emit("扫描正在进行中，请稍候")
            return
        if self._api is None:
            self.failed.emit("Bangumi API 未初始化")
            return

        subj = self._db.get_subject(subject_id)
        if subj is None:
            self.failed.emit("条目不存在")
            return
        folder = (subj.folder_path or "").strip()
        if not folder:
            # ---- 没有记录目录 → **按 RSS 的规则推算一次**----
            #
            # 这里复用同一套推算（`RssMatcher.plan_save_path`，纯计算、
            # 不创建目录）：算出来的目录**真实存在**才继续扫，
            # 不存在就还是报错（这时确实没东西可变，报错是对的）。
            folder = self._guess_folder(subj) or ""
        if not folder or not Path(folder).is_dir():
            self.failed.emit("该条目还没有目录（先下载一次，或手动指定目录）")
            return

        self._api = self._api or BangumiClient()
        self._matched = 0
        self._pending = 0
        self._current = 0
        self._total = 0

        worker = ScanWorker(
            # **媒体库路径要传**（见方法说明）：它**不参与挑选候选**
            # （`only_folder` 一给，`_collect_candidates` 就只从目标目录
            # 下探，压根不看这里），只用来**复算祖先上下文**
            # —— 单扫算关键词时要知道"从媒体库根走到这里都经过了哪些层"。
            # 早先这里传的是 `[]`，于是复算无从下手、退回旧行为：
            # 关键词退化成半截罗马音（匹配转人工）、series_name 被写成
            # 中间的容器目录名（条目"被丢出系列"）。传了之后，目标目录
            # **不在**任何媒体库路径下时依旧走那条旧兜底路径，不会更差。
            self._config.library_paths,
            self._api,
            self._db,
            season_mode=SEASON_MODE,
            season_display=SEASON_DISPLAY,
            accept_score=self._config.getint("scanner", "accept_score", 60),
            accept_gap=self._config.getint("scanner", "accept_gap", 20),
            align_by_order=self._config.getbool(
                "scanner", "ep_align_order", True),
            only_folder=Path(folder),
            extra_show=self._config.getbool(
                "scanner", "extra_show", True),
            extra_numbering=self._config.getbool(
                "scanner", "extra_numbering", True),
        )
        worker.progress_changed.connect(self._on_progress)
        worker.item_matched.connect(self._on_matched)
        worker.log_message.connect(self._on_log)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_failed)
        worker.match_failed.connect(self._on_match_failed)
        worker.item_pending.connect(self._on_pending)

        self._worker = worker
        # 标记为单条目扫描：完成回调据此决定**不发起**「在看/动态」同步
        # （见 _on_ok 的说明）。
        self._single_scan = True
        # 目标条目**本来就关联着** Bangumi 吗 —— 重扫这种条目时扫描会走
        # manual 记录保护、**跳过重新匹配**（数量恒为 0），结论文案不能
        # 据此报"未匹配"（见 _make_summary）。
        self._target_linked = bool(subj.bangumi_id)
        self._set_running(True)
        worker.start()
        log.info("单条目扫描已启动：subject_id=%s（%s）", subject_id, folder)

    def _guess_folder(self, subj) -> str:
        """该条目没记目录时，按 **RSS 的同一套规则**推算它"应该在哪"。

        只是**借规则算个路径**（`RssMatcher.plan_save_path` 是纯计算、
        不创建目录、不落库），算出来可能与实际不同 —— 所以调用方必须
        再判一次 `is_dir()`，只有真实存在才拿去扫描。

        **为什么复用而不再写一套**：路径规则（媒体库根 / 系列名 / 季子目录、
        以及"系列名靠条目名兜底推断"）散在两处必然漂移，很快就会出现
        "下载器说下到 A、扫描器去扫 B"。
        """
        try:
            from app.core.database import RssSource
            from app.core.rss_matcher import RssMatcher

            # 造一个"临时订阅"只为借用它的推算入口：
            # save_subject_id 指向本条 → plan_save_path 就会按它算。
            fake = RssSource(id=0, name="", url="",
                             save_subject_id=int(subj.id),
                             local_subject_id=int(subj.id))
            matcher = RssMatcher(self._db, None, self._config)
            path, _note, _exists = matcher.plan_save_path(fake)
            log.info("条目 #%s 没有目录记录，按规则推算为：%s", subj.id, path)
            return path or ""
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("推算条目 #%s 的目录失败：%s", getattr(subj, "id", "?"), e)
            return ""

    @Slot(str, bool, result="QVariantMap")
    def addFolder(self, folder: str, match: bool = True) -> dict:
        """「添加动漫」：把一个**单个动漫目录**加入库。

        与 `startSubject` 的区别：那个是对**已入库**条目重扫；这里是从零
        把用户手选的一个目录走一遍"匹配 → 入库 → 填集数"。

        参数：
            folder —— 用户选择的目录（或单个视频文件）
            match  —— 是否做 Bangumi 匹配。
                      **没填 Token 时 QML 传 False**：此时不做网络匹配，
                      只把目录里的视频作为本地条目入库。用户明确要求
                      "没填 Token 就不匹配"。

        **返回结构化结果**（原本只返回一个错误字符串，后来发现"已存在"
        是第三种情况：既不是错误、也不该继续扫描，而是**提示 + 跳转**，
        和"失败"的处理完全不同，字符串表达不了，于是改成 QVariantMap）：

            { "ok": true }                         —— 已开始扫描
            { "ok": false, "exists": true,
              "subjectId": <int>, "name": <str> }  —— 该目录已在库中
            { "ok": false, "message": <str> }      —— 其它失败（直接展示原因）
        """
        if self._running:
            return {"ok": False, "message": "扫描正在进行中，请稍候"}

        raw = (folder or "").strip()
        if not raw:
            return {"ok": False, "message": "请先选择动漫文件夹"}
        # 先剥掉可能的首尾引号（用户可能从别处粘贴 `"F:\动漫\某某"` 这种带
        # 引号的路径），再交给 Path 规整（`F:/x` 与 `F:\x` 都认）。
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1].strip()
        target = Path(raw)
        if not target.exists():
            return {"ok": False, "message": f"目录不存在：{target}"}
        # 允许直接选单个视频文件（剧场版单文件的情形）
        if not target.is_dir() and not target.is_file():
            return {"ok": False, "message": f"不是有效的目录或文件：{target}"}

        # ---- 「已存在的动漫」检查（在扫描之前）----
        #
        # 判定用 `folder_path` 精确匹配：与入库时的写法一致（`upsert_*`
        # 都是直接存 `str(cand.folder_path)`），而 candidate 的 folder_path
        # 对"单个动漫目录"就等于所选目录本身，因此能对上。
        #
        # 注意单文件的情形：那种条目的 folder_path 记的是**父目录**（见
        # scanner 的 `if folder.is_file()` 分支），所以这里也按父目录查，
        # 保持一致。
        lookup = target.parent if target.is_file() else target
        try:
            existing = self._db.find_subject_by_folder(str(lookup))
        except Exception as e:                      # pragma: no cover - 防御性
            log.warning("查重失败 %s: %s", lookup, e)
            existing = None
        if existing is not None:
            name = existing.name_cn or existing.name or str(lookup)
            log.info("该目录已在库中：%s（subject_id=%s）", name, existing.id)
            return {
                "ok": False,
                "exists": True,
                "subjectId": int(existing.id),
                "name": name,
            }

        if match and self._api is None:
            return {"ok": False, "message": "Bangumi API 未初始化"}

        # **拒绝"一选就是整个媒体库/"的情况**（用户诉求）：
        # 选到 `F:/动漫` 这类**装着一堆动漫的父目录**时，若直接把它当一个
        # 条目扫描，会把旗下所有番混成一条 —— 完全违背"添加单个动漫"的用意。
        #
        # 判据：该目录下**直接**含视频则为"单个动漫目录"（正常）；
        # 若它自己不含视频、但**直接子目录**里有多个含视频的目录，说明选的是
        # 父级容器，应提示用户往下选一层，而不是把整片库塞进一条。
        #
        # 注意这里只**拒绝而非自动展开**：自动展开会一次入库几十部番，
        # 用户无从预期、也无法逐条确认；让他明确选一个更符合"单个添加"。
        if target.is_dir():
            from app.core.scanner import _videos_in
            if not _videos_in(target, recursive=False):
                subs = []
                try:
                    for sub in sorted(target.iterdir()):
                        if sub.is_dir() and _videos_in(sub, recursive=True):
                            subs.append(sub)
                except OSError as e:
                    return {"ok": False, "message": f"读取目录失败：{e}"}
                if len(subs) > 1:
                    return {"ok": False,
                            "message": f"这个目录下有 {len(subs)} 个动漫文件夹，"
                                       f"请选择其中一个再添加"}

        self._api = self._api or BangumiClient()
        self._matched = 0
        self._pending = 0
        self._current = 0
        self._total = 0

        worker = ScanWorker(
            # 媒体库路径：**只用于复算祖先上下文**（关键词 / series_name
            # 都靠它，见 ScanWorker._ancestor_state），不参与挑候选
            # （`only_folder` 一给就只从所选目录下探）。
            #
            # 早先传 `[]`，于是加进来的目录若**名字本身不是作品名** ——
            # 典型是发布组目录「[DMG&VCB-Studio] Seishun Buta Yarou wa …」——
            # 关键词会退化成被 MAX_LATIN_RUN 截断的半截罗马音，多部作品
            # 同分、前二名差距 0，直接转人工（与"重新扫描"那次是同一个坑）。
            # 所选目录不在媒体库里（"添加"本来就允许任意目录）时复算失败，
            # 自动退回旧行为。
            self._config.library_paths,
            self._api,
            self._db,
            season_mode=SEASON_MODE,
            season_display=SEASON_DISPLAY,
            accept_score=self._config.getint("scanner", "accept_score", 60),
            accept_gap=self._config.getint("scanner", "accept_gap", 20),
            align_by_order=self._config.getbool(
                "scanner", "ep_align_order", True),
            only_folder=target,
            extra_show=self._config.getbool(
                "scanner", "extra_show", True),
            extra_numbering=self._config.getbool(
                "scanner", "extra_numbering", True),
            # 不做匹配时直接跳过网络搜索（见 ScanWorker.no_match 的说明）
            no_match=not match,
        )
        worker.progress_changed.connect(self._on_progress)
        worker.item_matched.connect(self._on_matched)
        worker.log_message.connect(self._on_log)
        worker.finished_ok.connect(self._on_ok)
        worker.failed.connect(self._on_failed)
        worker.match_failed.connect(self._on_match_failed)
        worker.item_pending.connect(self._on_pending)

        self._worker = worker
        self._single_scan = True          # 单条添加不触发「在看/动态」同步
        self._adding = True
        self._added_subject_id = 0
        # 记下用户有没有勾选匹配 —— 播报"未匹配"的前提取决于它
        # （用户主动选择不匹配时，报"未匹配"是废话）。见 _make_summary。
        #
        # **这里原先被紧接着的一行 `= True` 覆盖掉了**：于是
        # `lastMatchRequested` 恒为真、"没勾匹配就不提未匹配"这条从未生效
        # —— 而 QML 侧也一直没接这个属性，两边一起错着看不出来。
        self._last_match_requested = bool(match)
        # 「添加动漫」不是重扫：目标条目是**新加**的，无所谓"本来就关联"
        self._target_linked = False
        self._set_running(True)
        worker.start()
        log.info("添加动漫已启动：%s（匹配=%s）", target, match)
        return {"ok": True}

    @Slot()
    def cancel(self) -> None:
        """中止扫描（用户主动）。"""
        w = self._worker
        if w is not None and w.isRunning():
            w.terminate()
            w.wait(2000)
            log.warning("扫描已被用户中止")
            self.logMessage.emit("扫描已中止")
        self._set_running(False)
        # 文案要换掉：本次是中止，不是"完成"（否则提示条会把**上一次**扫描的
        # 结论再弹一遍 —— `resultSummary` 是常驻属性，不设就留着旧值）
        self._result_summary = "扫描已中止"
        self.resultSummaryChanged.emit()
        self.finished.emit(self._matched, self._pending)

    # ---------- 内部 ----------
    def _set_running(self, value: bool) -> None:
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    def _on_progress(self, current: int, total: int) -> None:
        self._current, self._total = current, total
        self.progressChanged.emit(current, total)

    def _on_matched(self, subject_id: int, name: str) -> None:
        self._matched += 1
        log.debug("已匹配 subject_id=%s name=%s", subject_id, name)
        self.logMessage.emit(f"✓ 已匹配：{name}")

    def _on_log(self, msg: str) -> None:
        # **不要在这里靠文本匹配计数**（踩坑）：原先写的是
        # `if "待手动确认" in msg: self._pending += 1` —— 把展示用的日志
        # 文案当成了协议。后来网络失败那条日志改成了"⚠ 联网匹配失败"
        # （文案不同），计数便静默漏掉那类条目：界面报"待确认 0"，
        # 而库里确实多了一条 pending。现在改由 `_on_pending` 计数。
        self.logMessage.emit(msg)

    def _on_pending(self, name: str, reason: str) -> None:
        """条目被写成待确认（pending）→ 计数（与日志文案解耦）。"""
        self._pending += 1
        log.debug("待确认条目：%s（%s）", name, reason)

    def _on_ok(self) -> None:
        self._set_running(False)
        log.info("扫描完成：匹配 %s，待确认 %s", self._matched, self._pending)
        # 下面要读 `_adding`，但它在本方法里会被置回 False（那是给下一次
        # 扫描复位的），先留一份下来算结论文案（见 _make_summary）。
        was_adding = self._adding
        # 扫完的**汇总文案不再走 logMessage**。
        #
        # 原先这里 emit 一句"扫描完成：匹配 N 个，
        # 待确认 M 个"，而 QML 侧 `onLogMessage` 会把每条日志都弹成提示条、
        # `onFinished` 里又弹了同样一句 —— 于是**同一个结果闪两遍**
        # （前一条被后一条覆盖，看起来像抖了一下）。汇总属于"最终结果"，
        # 只应由 finished 统一播报；logMessage 留给过程性日志。
        if self._on_finished_hook is not None:
            try:
                self._on_finished_hook()
            except Exception as e:
                log.exception("扫描完成回调失败: %s", e)

        # 「添加动漫」：把新入库的条目 id 回传 QML（进详情页用）。
        #
        # 必须在 finished 之前发：QML 那边 onSubjectAdded 里会切到详情页，
        # 而 onFinished 会刷新海报墙 —— 顺序反了会出现"先刷新列表、后跳页"
        # 的闪烁。
        #
        # 取**最后一个**（正常情况下只有一个；万一用户选的目录被 _walk
        # 拆出了多条，取最后一条至少保证进的是其中一个，而不是 0）。
        if self._adding:
            ids = self._worker.created_subject_ids if self._worker else []
            self._added_subject_id = ids[-1] if ids else 0
            self._adding = False
            if self._added_subject_id:
                log.info("添加动漫完成：subject_id=%s", self._added_subject_id)
            else:
                log.warning("添加动漫未产生条目（可能是空目录）")
                self.logMessage.emit("该目录下没有找到可入库的视频")
            self.subjectAdded.emit(self._added_subject_id)

        # 结论文案要在 `finished` **之前**算好并置位：QML 的
        # `onFinished` 里直接读 `scanner.resultSummary` 播报。
        self._result_summary = self._make_summary(was_adding)
        self.resultSummaryChanged.emit()
        log.info("扫描结论：%s", self._result_summary)
        self.finished.emit(self._matched, self._pending)

    def _on_failed(self, msg: str) -> None:
        self._set_running(False)
        log.error("扫描失败：%s", msg)
        self.failed.emit(msg)

    def _on_match_failed(self, name: str, reason: str) -> None:
        """某条目因网络原因未匹配上 → 原样转发给 QML 弹黄色提示。"""
        log.warning("条目联网匹配失败：%s（%s）", name, reason)
        self.matchFailed.emit(name, reason)
