"""在看列表服务（F18）。

职责：拉取 Bangumi 上 type=3（在看）的动画收藏，做本地关联与缓存降级。

流程（对应 §8.3）：
1. 缓存未过期（< inprogress_cache_ttl）→ 直接用 inprogress_cache
2. 否则调 API → 写缓存 → 渲染
3. API 失败 → 回退旧缓存 + 标记 offline=True，由 UI 提示「离线数据」
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.core.bangumi_api import (
    BangumiAuthError,
    BangumiClient,
    BangumiError,
    COLLECT_TYPE_DOING,
    SUBJECT_TYPE_ANIME,
)
from app.core.config import Config
from app.core.database import Database, InProgressItem

log = logging.getLogger(__name__)


@dataclass
class InProgressView:
    """在看列表拉取结果（含降级标记）。"""

    items: list[InProgressItem] = field(default_factory=list)
    offline: bool = False           # True = 用的是旧缓存
    error: str = ""                 # 非空时 UI 顶部提示
    username: str = ""


class InProgressService:
    """在看列表加载器。"""

    def __init__(self, db: Database, api: BangumiClient, config: Config) -> None:
        self.db = db
        self.api = api
        self.config = config
        self._username: Optional[str] = None

    # ---------- 用户名解析 ----------
    def resolve_username(self, force: bool = False) -> str:
        """优先配置项；为空则用 Token 调 /v0/me 解析并缓存到内存。"""
        if self._username and not force:
            return self._username

        configured = self.config.get("bangumi", "username", "").strip()
        if configured:
            self._username = configured
            return configured

        me = self.api.get_me()
        if me:
            self._username = str(me.get("username") or me.get("id") or "")
            log.info("由 Token 解析出 Bangumi 用户名：%s", self._username)
        else:
            self._username = ""
        return self._username

    # ---------- 加载 ----------
    @property
    def cache_ttl(self) -> int:
        return self.config.getint("bangumi", "inprogress_cache_ttl", 300)

    def load(self, force: bool = False) -> InProgressView:
        """加载在看列表。force=True 绕过缓存。"""
        username = self.resolve_username()

        if not username:
            return self._fallback(
                "未配置 Bangumi 用户名，且无法从 Token 解析。"
                "请到「设置」填写 Bangumi 用户名（Token 权限不足时也需手填）。"
            )
        log.info("在看列表加载：username=%s force=%s", username, force)

        if not force:
            age = self.db.inprogress_cache_age()
            if age is not None and age < self.cache_ttl:
                items = self._attach_local(self.db.load_inprogress_cache())
                return InProgressView(items=items, offline=False, username=username)

        try:
            raw = self.api.iter_user_collections(
                username,
                subject_type=SUBJECT_TYPE_ANIME,
                collect_type=COLLECT_TYPE_DOING,
            )
        except BangumiAuthError as e:
            return self._fallback(
                f"拉取失败（{e}）。请确认用户名是否正确、Token 是否有效。",
                username=username,
            )
        except BangumiError as e:
            return self._fallback(f"拉取失败：{e}", username=username)

        log.info("在看列表 API 返回 %s 条原始记录", len(raw))

        normalized = [self._normalize(r) for r in raw]
        normalized = [n for n in normalized if n.get("bangumi_id")]
        self.db.replace_inprogress_cache(normalized)
        items = self._attach_local(self.db.load_inprogress_cache())
        log.info("在看列表已更新：%s 条", len(items))
        return InProgressView(items=items, offline=False, username=username)

    # ---------- 内部 ----------
    def _fallback(self, error: str, username: str = "") -> InProgressView:
        """API 不可用时回退旧缓存。"""
        items = self._attach_local(self.db.load_inprogress_cache())
        offline = bool(items)
        if offline:
            error = f"{error}（当前显示本地缓存的离线数据）"
        log.warning("在看列表降级：%s", error)
        return InProgressView(items=items, offline=offline, error=error, username=username)

    @staticmethod
    def _normalize(record: dict) -> dict:
        """Bangumi collection 记录 → inprogress_cache 字段。"""
        subj = record.get("subject") or {}
        images = subj.get("images") or {}
        return {
            "bangumi_id": subj.get("id") or record.get("subject_id"),
            "name": subj.get("name", ""),
            "name_cn": subj.get("name_cn", ""),
            "cover_url": images.get("large") or images.get("common") or images.get("medium") or "",
            "ep_status": record.get("ep_status") or 0,
            "total_eps": subj.get("eps") or subj.get("total_episodes") or 0,
            "collect_type": COLLECT_TYPE_DOING,
        }

    def _attach_local(self, items: list[InProgressItem]) -> list[InProgressItem]:
        """填充 local_subject_id：命中本地库则可用于点击播放。"""
        if not items:
            return items
        for it in items:
            it.local_subject_id = self.db.find_subject_by_bangumi_id(it.bangumi_id)
        return items
