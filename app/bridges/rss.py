"""RSS 订阅桥接（阶段 7 · F19 的 QML 版本）。

职责范围（本阶段只做**管理**，不做实际轮询下载）：
1. 订阅源 CRUD：`sources` / `addSource()` / `updateSource()` / `removeSource()`
2. 下载记录查询：`downloads()` / `downloadStats()`
3. 关联 Bangumi 条目：`linkSubject()`（把订阅绑定到本地条目）

**为什么本阶段不实现轮询下载**：
下载链路需要 qBittorrent WebAPI 客户端（登录 / 添加种子 / 查询状态）与
RSS 解析器（feedparser 或 xml.etree）配合，且需要「三层判新」逻辑。
这是一块独立且较大的功能，数据库表（`rss_sources` / `download_history`）
与配置项已就绪，但为了本阶段能交付可用的界面，先只做订阅源管理 ——
用户可以先建好订阅、绑定条目，下载器在后续阶段接入。

数据表结构见 `database.py` 的 `rss_sources` / `download_history`。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.core.database import Database

log = logging.getLogger(__name__)

# 判新规则（决定"哪些集才下载"）
RULE_NEW_ONLY = "new_only"      # 只下本地没有的集
RULE_ALL = "all"                # 全部都下
RULES = (RULE_NEW_ONLY, RULE_ALL)

# 下载状态 → 中文（QML 侧展示用）
STATUS_LABELS = {
    "pending": "等待中",
    "downloading": "下载中",
    "completed": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
}


class RssBridge(QObject):
    """RSS 订阅源与下载记录。"""

    sourcesChanged = Signal()
    downloadsChanged = Signal()
    failed = Signal(str)
    message = Signal(str)

    def __init__(self, db: Database, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._sources_cache: list[dict] = []
        self._sources_dirty = True

    # ---------- 订阅源 ----------
    @Property("QVariantList", notify=sourcesChanged)
    def sources(self) -> list[dict]:
        """订阅源列表（含绑定条目名与下载统计）。"""
        if self._sources_dirty:
            self._sources_cache = self._load_sources()
            self._sources_dirty = False
        return self._sources_cache

    def _load_sources(self) -> list[dict]:
        try:
            rows = self._db.list_rss_sources()
        except Exception as e:
            log.exception("读取订阅源失败: %s", e)
            return []

        out: list[dict] = []
        for s in rows:
            # 绑定条目的展示名（用于界面上显示"→ XX动漫"）
            subject_name = ""
            local_id = 0
            if s.bangumi_id:
                try:
                    local_id = self._db.find_subject_by_bangumi_id(s.bangumi_id) or 0
                    if local_id:
                        subj = self._db.get_subject(local_id)
                        subject_name = (subj.name_cn or subj.name) if subj else ""
                except Exception:
                    pass

            # 下载统计（各状态计数）
            try:
                stats = self._db.count_downloads_by_status(s.id)
            except Exception:
                stats = {}

            out.append({
                "id": s.id,
                "name": s.name or "",
                "url": s.url or "",
                "bangumiId": int(s.bangumi_id or 0),
                "enabled": bool(s.enabled),
                "rule": s.rule or RULE_NEW_ONLY,
                "ruleLabel": "只下新集" if (s.rule or RULE_NEW_ONLY) == RULE_NEW_ONLY
                             else "全部下载",
                "lastPollAt": s.last_poll_at or "",
                "lastError": s.last_error or "",
                "createdAt": s.created_at or "",
                "localSubjectId": local_id,
                "subjectName": subject_name,
                "downloadCount": sum(stats.values()),
                "completedCount": int(stats.get("completed", 0)),
                "failedCount": int(stats.get("failed", 0)),
            })
        return out

    @Slot()
    def reload(self) -> None:
        self._sources_dirty = True
        self._downloads_dirty = True
        self.sourcesChanged.emit()
        self.downloadsChanged.emit()

    @Slot(str, str, str, result=int)
    def addSource(self, name: str, url: str, rule: str = RULE_NEW_ONLY) -> int:
        """新增订阅源，返回新 ID（失败返回 0）。"""
        name = (name or "").strip()
        url = (url or "").strip()
        if not url:
            self.failed.emit("订阅地址不能为空")
            return 0
        if not (url.startswith("http://") or url.startswith("https://")):
            self.failed.emit("订阅地址需以 http:// 或 https:// 开头")
            return 0
        if rule not in RULES:
            rule = RULE_NEW_ONLY
        # 名称留空时用域名兜底，避免列表里出现空白项
        if not name:
            name = url.split("//", 1)[-1].split("/", 1)[0]

        try:
            sid = self._db.add_rss_source(name=name, url=url, rule=rule)
        except Exception as e:
            log.exception("新增订阅源失败: %s", e)
            self.failed.emit(f"新增失败：{e}")
            return 0

        log.info("新增订阅源 #%s：%s", sid, url)
        self.reload()
        self.message.emit(f"已添加订阅「{name}」")
        return sid

    @Slot(int, str, str, str, bool, result=bool)
    def updateSource(
        self,
        source_id: int,
        name: str,
        url: str,
        rule: str,
        enabled: bool,
    ) -> bool:
        """更新订阅源（名称 / 地址 / 规则 / 启用状态）。"""
        url = (url or "").strip()
        if not url:
            self.failed.emit("订阅地址不能为空")
            return False
        if rule not in RULES:
            rule = RULE_NEW_ONLY
        try:
            self._db.update_rss_source(
                source_id,
                name=(name or "").strip(),
                url=url,
                rule=rule,
                enabled=1 if enabled else 0,
            )
        except Exception as e:
            log.exception("更新订阅源 %s 失败: %s", source_id, e)
            self.failed.emit(f"保存失败：{e}")
            return False
        self.reload()
        self.message.emit("订阅已保存")
        return True

    @Slot(int, bool)
    def setEnabled(self, source_id: int, enabled: bool) -> None:
        """仅切换启用状态（列表里的开关）。"""
        try:
            self._db.update_rss_source(source_id, enabled=1 if enabled else 0)
        except Exception as e:
            log.exception("切换订阅状态失败: %s", e)
            self.failed.emit(f"切换失败：{e}")
            return
        self.reload()

    @Slot(int, int)
    def linkSubject(self, source_id: int, subject_id: int) -> None:
        """把订阅绑定到本地条目（同步其 bangumi_id）。

        绑定后「三层判新」才能知道该订阅对应哪部动漫、本地已有哪些集。
        `subject_id=0` 表示解除绑定。
        """
        try:
            if subject_id <= 0:
                self._db.update_rss_source(source_id, bangumi_id=None)
                self.message.emit("已解除绑定")
            else:
                subj = self._db.get_subject(subject_id)
                if subj is None:
                    self.failed.emit("条目不存在")
                    return
                if not subj.bangumi_id:
                    self.failed.emit("该条目尚未匹配 Bangumi，无法绑定")
                    return
                self._db.update_rss_source(source_id, bangumi_id=subj.bangumi_id)
                self.message.emit(
                    f"已绑定到「{subj.name_cn or subj.name}」")
        except Exception as e:
            log.exception("绑定订阅 %s 失败: %s", source_id, e)
            self.failed.emit(f"绑定失败：{e}")
            return
        self.reload()

    @Slot(int, result=bool)
    def removeSource(self, source_id: int) -> bool:
        """删除订阅源（连带删除其下载记录，外键 CASCADE）。"""
        try:
            # 外键 ON DELETE CASCADE 已在 schema 里声明，
            # 但 download_history 的 source_id 是 REFERENCES rss_sources(id)
            # 且 PRAGMA foreign_keys=ON 已开，因此会级联删除。
            self._db.delete_rss_source(source_id)
        except Exception as e:
            log.exception("删除订阅源 %s 失败: %s", source_id, e)
            self.failed.emit(f"删除失败：{e}")
            return False
        log.info("已删除订阅源 #%s", source_id)
        self.reload()
        self.message.emit("订阅已删除")
        return True

    # ---------- 下载记录 ----------
    _downloads_dirty = True
    _downloads_cache: list[dict] = []

    @Property("QVariantList", notify=downloadsChanged)
    def downloads(self) -> list[dict]:
        """全部下载记录（按集序号倒序）。"""
        if self._downloads_dirty:
            self._downloads_cache = self._load_downloads()
            self._downloads_dirty = False
        return self._downloads_cache

    def _load_downloads(self) -> list[dict]:
        try:
            rows = self._db.list_downloads()
        except Exception as e:
            log.exception("读取下载记录失败: %s", e)
            return []
        return [
            {
                "id": r.id,
                "sourceId": r.source_id,
                "subjectId": int(r.subject_id or 0),
                "epIndex": float(r.ep_index or 0),
                "torrentTitle": r.torrent_title or "",
                "status": r.status or "pending",
                "statusLabel": STATUS_LABELS.get(r.status or "pending", r.status),
                "createdAt": r.created_at or "",
            }
            for r in rows
        ]

    @Slot(int, result="QVariantMap")
    def downloadStats(self, source_id: int) -> dict:
        """单个订阅的下载统计（失败/完成计数）。"""
        try:
            stats = self._db.count_downloads_by_status(source_id)
        except Exception as e:
            log.exception("统计下载失败: %s", e)
            return {}
        return {k: int(v) for k, v in stats.items()}

    # ---------- 提示 ----------
    @Slot(result="QVariantList")
    def ruleOptions(self) -> list[dict]:
        """判新规则选项（QML 的下拉框用）。"""
        return [
            {"value": RULE_NEW_ONLY, "label": "只下新集（本地没有的）"},
            {"value": RULE_ALL, "label": "全部下载"},
        ]
