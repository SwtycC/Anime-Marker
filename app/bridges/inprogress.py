"""Bangumi 收藏列表桥接（阶段 7 · F18 的 QML 版本）。

职责：从 Bangumi 拉取「当前用户 · **看过**」收藏，写入 `inprogress_cache`，
然后通知 `LibraryBridge` 刷新（QML 侧读 `library.inProgress`）。

> **命名说明**：表名、Property 名都还叫 `inprogress*`（沿用 F18 的旧实现），
> 但**语义已改为「看过」**。之所以不改名，是因为要动 `inprogress_cache` 表、
> `library.inProgress`、`InProgressPage` 一整条链路，收益仅是"名字更好看"，
> 而风险是漏改某处导致静默失效。这里用注释显式记录，避免后人误判。

设计要点：
1. **网络走 QThread**：拉取可能耗时数秒（分页 + 代理），不能阻塞 UI。
2. **离线降级**：拉到数据就整体替换缓存；失败则保留旧缓存，
   只把错误通过 `failed` 信号告诉状态栏 —— 断网时页面仍有内容可看。
3. **用户名解析**：优先用 Token 调 `/v0/me` 拿 `username`
   （注意**不是昵称**，详见 `resolveUsername()` 的说明）。
4. **本地关联**：只按 `bangumi_id` 查本地条目，不做模糊匹配 ——
   关联结果用于「跳详情」按钮，宁可没有也不要指错。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from app.core.bangumi_api import (
    COLLECT_TYPE_DONE, BangumiAuthError, BangumiClient, BangumiError,
)
from app.core.config import Config
from app.core.database import Database

log = logging.getLogger(__name__)

# 拉取上限。实测「看过」149 条、「在看」11 条，
# 600 对个人用户足够，同时避免异常账号把内存打爆。
MAX_ITEMS = 600


class _FetchWorker(QThread):
    """后台拉取收藏列表。"""

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
            # ---- 解析 username（不是昵称！）----
            #
            # 踩坑记录：Bangumi 的 `/v0/users/{username}` 路径参数要的是
            # **`username` 字段**，而不是界面上显示的**昵称**（`nickname`）。
            # 用昵称去请求会返回 **404「用户不存在」**，且错误信息极具误导性
            # （看起来像"接口挂了"或"用户没数据"）。
            #
            # 实测（GET /v0/me）：
            #     username: '933287'   ← API 要的是这个（可能也是数字）
            #     nickname: 'swtyc'    ← 界面上显示的名字，填它必 404
            #
            # 因此策略是：**只要 Token 可用就优先自动解析**，配置里那一栏
            # 仅作兜底（Token 无效时的离线场景）。这样用户填了昵称也不会出错。
            username = ""
            me = self.api.get_me()
            if me:
                username = (me.get("username") or "").strip()
                if username:
                    log.info("已从 Token 解析 username：%s（昵称 %s）",
                             username, me.get("nickname") or "-")

            if not username:
                # Token 解析失败 → 用配置里的值兜底
                username = (self.username or "").strip()
                if username:
                    log.warning(
                        "Token 无法解析 username，改用配置值：%r"
                        "（注意：这里要填 username 而非昵称，否则会 404）",
                        username)

            if not username:
                self.finished_items.emit(
                    [], "无法确定 Bangumi 用户名，请检查 Token 是否有效")
                return

            # **只看「看过」**（type=2）。不拉「在看」的原因：
            # 本软件的场景是"回顾已看完的番"，在看列表由 Bangumi 自己维护。
            # 实测本账号 看过=149 条、在看=11 条。
            raw = self.api.iter_user_collections(
                username,
                collect_type=COLLECT_TYPE_DONE,
                max_items=self.max_items,
            )
            items = [self._to_cache_item(r) for r in raw]
            log.info("已拉取「看过」收藏 %s 条", len(items))
            self.finished_items.emit(items, "")
        except BangumiAuthError as e:
            log.warning("拉取收藏列表权限不足: %s", e)
            self.finished_items.emit([], f"Token 权限不足：{e}")
        except BangumiError as e:
            # 404 在这里几乎总是"用户名填成了昵称"，给出可操作的提示
            msg = str(e)
            if "404" in msg:
                log.warning("拉取收藏列表 404（通常是 username 填成了昵称）: %s", e)
                self.finished_items.emit(
                    [], "用户不存在：请确认「用户 ID」填的是 username（数字 ID）"
                        "而非昵称，或清空该栏让程序自动从 Token 解析")
                return
            log.warning("拉取收藏列表失败: %s", e)
            self.finished_items.emit([], msg)
        except Exception:
            log.exception("拉取收藏列表异常")
            self.finished_items.emit([], "拉取失败（详见日志）")

    @staticmethod
    def _to_cache_item(raw: dict) -> dict:
        """把 Bangumi collection 条目转成 `replace_inprogress_cache` 的入参。

        Bangumi 的返回结构（/v0/users/{u}/collections）：
            { "subject_id": 123, "subject": { "name": ..., "name_cn": ...,
              "images": {...}, "eps": 12 }, "type": 2, "ep_status": 11 }
        注意**没有** total_eps 顶层字段，集数在 `subject.eps`。
        """
        subject = raw.get("subject") or {}
        images = subject.get("images") or {}
        # 封面优先 large，其次 common（与旧版一致）
        cover = images.get("large") or images.get("common") or images.get("medium") or ""

        # 总集数兜底链：subject.eps → total_episodes → 已看集数。
        # 实测「碧蓝之海 第三季」的 eps 为 0（Bangumi 数据缺失），
        # 此时用 ep_status 兜底，至少不会显示成"共 0 集"。
        eps = subject.get("eps") or subject.get("total_episodes") or 0
        ep_status = raw.get("ep_status") or 0
        if not eps and ep_status:
            eps = ep_status

        return {
            "bangumi_id": raw.get("subject_id") or subject.get("id") or 0,
            "name": subject.get("name") or "",
            "name_cn": subject.get("name_cn") or "",
            "cover_url": cover,
            "ep_status": ep_status,
            "total_eps": eps,
            "collect_type": raw.get("type") or COLLECT_TYPE_DONE,
        }


class InProgressBridge(QObject):
    """Bangumi「看过」收藏数据源（负责拉取与落库，展示交给 LibraryBridge）。"""

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
        """拉取「动画 · 看过」并写入缓存。重复调用会被忽略。"""
        if self._running:
            log.info("收藏列表拉取中，忽略本次请求")
            return

        username = (self._config.get("bangumi", "username", "") or "").strip()
        self._set_running(True)
        self.started.emit()
        self.message.emit("正在拉取 Bangumi 看过列表…")

        self._worker = _FetchWorker(self._api, username, MAX_ITEMS)
        self._worker.finished_items.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_finished(self, items: list, error: str) -> None:
        self._set_running(False)

        if error:
            # 离线降级：保留旧缓存，只报错
            log.warning("收藏列表拉取失败，保留旧缓存: %s", error)
            self.failed.emit(error)
            return

        if not items:
            log.info("「看过」收藏为空")
            self._db.replace_inprogress_cache([])
            self._refresh_library()
            self.finished.emit(0)
            self.message.emit("Bangumi 上没有「看过」的动漫")
            return

        try:
            self._db.replace_inprogress_cache(items)
        except Exception as e:
            log.exception("写入在看缓存失败: %s", e)
            self.failed.emit(f"写入缓存失败：{e}")
            return

        log.info("「看过」列表已更新：%s 条", len(items))
        self._refresh_library()
        self.finished.emit(len(items))
        self.message.emit(f"Bangumi 看过列表已更新（{len(items)} 部）")

    def _refresh_library(self) -> None:
        if self._done_hook is not None:
            try:
                self._done_hook()
            except Exception:
                log.exception("刷新收藏数据缓存失败")

    @Slot(str, result="QVariantMap")
    def resolveUsername(self, token: str = "") -> dict:
        """用指定 Token 解析当前账号的 username（供设置页「检测」按钮调用）。

        参数 `token`：要用于探测的 Token。传空串则用桥接层持有的 `api`
        （即已保存的配置）。传值时会**临时构造一个客户端**，不污染主 `api`，
        也不写盘 —— 用户可以放心地点「检测」而不会误改保存的配置。

        返回：
            { "ok": bool, "username": str, "nickname": str,
              "id": int, "message": str }

        为什么要这个接口：`/v0/users/{username}` 要的是 username 而不是
        昵称，但用户在设置页看到的、能填的往往是昵称。给一个"一键检测"
        的入口，可以把正确值直接回填，避免 404 之后一头雾水。
        """
        api = self._api
        token = (token or "").strip()
        if token:
            # 用输入框里的 Token 临时探测（不落盘、不动主 api）
            api = BangumiClient(
                token=token,
                api_base=getattr(self._api, "api_base", "https://api.bgm.tv"),
                proxy="",       # 代理沿用已保存配置即可，这里不重复读配置
                timeout=15.0,
            )
        try:
            me = api.get_me()
        except Exception as e:
            log.warning("检测用户名失败: %s", e)
            return {"ok": False, "username": "", "nickname": "", "id": 0,
                    "message": f"请求失败：{e}"}
        if not me:
            return {"ok": False, "username": "", "nickname": "", "id": 0,
                    "message": "Token 无效或已过期，无法获取用户信息"}
        username = (me.get("username") or "").strip()
        nickname = me.get("nickname") or ""
        if not username:
            return {"ok": False, "username": "", "nickname": nickname, "id": 0,
                    "message": "Token 对应账号没有 username"}
        return {
            "ok": True,
            "username": username,
            "nickname": nickname,
            "id": int(me.get("id") or 0),
            # 明确对比 username 与昵称，消除"该填哪个"的困惑
            "message": f"解析成功：用户 ID = {username}"
                       + (f"（{nickname} 是昵称，不能填在这里）"
                          if nickname and nickname != username else ""),
        }

    # ---------- 清理 ----------
    @Slot()
    def cancel(self) -> None:
        """退出时中断拉取（不强杀线程，只置位让它自然结束）。"""
        if self._worker is not None and self._worker.isRunning():
            log.info("等待在看列表拉取线程结束…")
            self._worker.wait(3000)
