"""在看列表桥接（阶段 7 · F18 的 QML 版本）。

职责：从 Bangumi 拉取「当前用户 · 在看」收藏，写入 `inprogress_cache`，
然后通知 `LibraryBridge` 刷新（QML 侧读 `library.inProgress`）。

设计要点：
1. **网络走 QThread**：拉取可能耗时数秒（分页 + 代理），不能阻塞 UI。
2. **离线降级**：拉到数据就整体替换缓存；失败则保留旧缓存，
   只把错误通过 `failed` 信号告诉状态栏 —— 断网时页面仍有内容可看。
3. **用户名解析**：优先用配置里的 `bangumi.username`；为空时用 Token
   调 `/v0/me` 反解（与旧版 F18 行为一致）。
4. **本地关联**：只按 `bangumi_id` 查本地条目，不做模糊匹配 ——
   关联结果用于「跳详情」按钮，宁可没有也不要指错。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from app.core.bangumi_api import BangumiClient, BangumiAuthError, BangumiError
from app.core.config import Config
from app.core.database import Database

log = logging.getLogger(__name__)

# 拉取上限（保护：在看列表通常几十条，500 足够）
MAX_ITEMS = 500


class _FetchWorker(QThread):
    """后台拉取在看列表。"""

    finished_items = Signal(object, str)   # (list[dict], error_message)

    def __init__(
        self,
        api: BangumiClient,
        username: str,
        max_items: int = MAX_ITEMS,
    ) -> None:
        super().__init__()
        self.api = api
        self.username = username
        self.max_items = max_items

    def run(self) -> None:
        try:
            # 未配置用户名 → 用 Token 反解
            username = self.username
            if not username:
                me = self.api.get_me()
                if not me:
                    self.finished_items.emit(
                        [], "无法解析 Bangumi 用户名（请检查 Token 是否有效，"
                            "或在设置里填写用户名）")
                    return
                username = me.get("username") or ""
                if not username:
                    self.finished_items.emit([], "Token 对应账号没有用户名")
                    return

            raw = self.api.iter_user_collections(
                username, max_items=self.max_items)
            items = [self._to_cache_item(r) for r in raw]
            self.finished_items.emit(items, "")
        except BangumiAuthError as e:
            log.warning("拉取在看列表权限不足: %s", e)
            self.finished_items.emit([], f"Token 权限不足：{e}")
        except BangumiError as e:
            log.warning("拉取在看列表失败: %s", e)
            self.finished_items.emit([], str(e))
        except Exception:
            log.exception("拉取在看列表异常")
            self.finished_items.emit([], "拉取失败（详见日志）")

    @staticmethod
    def _to_cache_item(raw: dict) -> dict:
        """把 Bangumi collection 条目转成 `replace_inprogress_cache` 的入参。

        Bangumi 的返回结构（/v0/users/{u}/collections）：
            { "subject_id": 123, "subject": { "name": ..., "name_cn": ...,
              "images": {...}, "eps": 12 }, "type": 3, "ep_status": 5 }
        注意**没有** total_eps 顶层字段，集数在 `subject.eps`。
        """
        subject = raw.get("subject") or {}
        images = subject.get("images") or {}
        # 封面优先 large，其次 common（与旧版一致）
        cover = images.get("large") or images.get("common") or images.get("medium") or ""
        return {
            "bangumi_id": raw.get("subject_id") or subject.get("id") or 0,
            "name": subject.get("name") or "",
            "name_cn": subject.get("name_cn") or "",
            "cover_url": cover,
            "ep_status": raw.get("ep_status") or 0,
            # 总集数：优先 subject.eps，回退 total_episodes（剧场版等）
            "total_eps": subject.get("eps") or subject.get("total_episodes") or 0,
            "collect_type": raw.get("type") or 3,
        }


class InProgressBridge(QObject):
    """在看列表数据源（负责拉取与落库，展示交给 LibraryBridge）。"""

    # 状态通知
    runningChanged = Signal()
    started = Signal()
    finished = Signal(int)        # 拉到的条数
    failed = Signal(str)          # 错误文案
    message = Signal(str)         # 中间进度文案

    def __init__(
        self,
        db: Database,
        config: Config,
        api: BangumiClient,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._api = api
        self._running = False
        self._worker: Optional[_FetchWorker] = None
        # 拉取成功后由 QmlApp 接上，用来刷新 QML 的 library.inProgress
        self._done_hook = None

    # ---------- 配置 ----------
    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    def set_done_hook(self, hook) -> None:
        """设置拉取成功后的回调（通常是 library.reloadInProgress）。"""
        self._done_hook = hook

    # ---------- 状态 ----------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        return self._running

    def _set_running(self, value: bool) -> None:
        if self._running != value:
            self._running = value
            self.runningChanged.emit()

    # ---------- 拉取 ----------
    @Slot()
    def refresh(self) -> None:
        """拉取「动画 · 在看」并写入缓存。重复调用会被忽略。"""
        if self._running:
            log.info("在看列表拉取中，忽略本次请求")
            return

        username = (self._config.get("bangumi", "username", "") or "").strip()
        self._set_running(True)
        self.started.emit()
        self.message.emit("正在拉取在看列表…")

        self._worker = _FetchWorker(self._api, username, MAX_ITEMS)
        self._worker.finished_items.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_finished(self, items: list, error: str) -> None:
        self._set_running(False)

        if error:
            # 离线降级：保留旧缓存，只报错
            log.warning("在看列表拉取失败，保留旧缓存: %s", error)
            self.failed.emit(error)
            return

        if not items:
            log.info("在看列表为空")
            self._db.replace_inprogress_cache([])
            self._refresh_library()
            self.finished.emit(0)
            self.message.emit("在看列表为空")
            return

        try:
            self._db.replace_inprogress_cache(items)
        except Exception as e:
            log.exception("写入在看缓存失败: %s", e)
            self.failed.emit(f"写入缓存失败：{e}")
            return

        log.info("在看列表已更新：%s 条", len(items))
        self._refresh_library()
        self.finished.emit(len(items))
        self.message.emit(f"在看列表已更新（{len(items)} 条）")

    def _refresh_library(self) -> None:
        if self._done_hook is not None:
            try:
                self._done_hook()
            except Exception:
                log.exception("刷新海报墙在看数据失败")

    # ---------- 清理 ----------
    @Slot()
    def cancel(self) -> None:
        """退出时中断拉取（不强杀线程，只置位让它自然结束）。"""
        if self._worker is not None and self._worker.isRunning():
            log.info("等待在看列表拉取线程结束…")
            self._worker.wait(3000)
