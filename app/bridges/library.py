"""媒体库桥接层：把 Database 的只读查询暴露给 QML。

设计原则（§5.12.5）：
1. **只暴露数据，不暴露 ORM 对象** —— QML 拿不到 Python dataclass，
   一律转成 `dict`（QML 侧直接当 JS 对象用）。
2. **返回值必须是 QML 能理解的基本类型** —— `QVariantList` / `QVariantMap`
   会自动转换 list[dict]，不要返回自定义类实例。
3. 耗时操作（扫描、网络）走 QThread + Signal，见 `scanner.py`；
   本地 SQLite 查询很快，直接同步返回即可（避免过度设计）。

封面路径：QML 的 `Image.source` 需要 URL 形式，且**不认 Windows 反斜杠**，
因此统一用 `as_file_url()` 转成 `file:///D:/...`（见 §5.12.5 的路径坑）。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from PySide6.QtCore import QObject, Property, Signal, Slot

from app.core.bangumi_api import COLLECT_TYPE_DONE
from app.core.database import Database, Episode, Subject

log = logging.getLogger(__name__)

# 多季展示模式
DISPLAY_FLAT = "flat"
DISPLAY_GROUPED = "grouped"


def as_file_url(path: str) -> str:
    """本地路径 → QML 可用的 file:/// URL。

    必须做两件事，否则含中文/空格的封面加载不出来：
    1. 反斜杠转正斜杠（QML 的 Image.source 不认 `\\`）
    2. 按 URL 规则转义（空格、中文），保留 `/` 与 `:` 作为路径分隔
    """
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        # as_posix() 得到 D:/a/b，前面补三个斜杠构成 file:///D:/a/b
        return "file:///" + quote(p.as_posix(), safe="/:")
    except OSError:
        return ""


class LibraryBridge(QObject):
    """媒体库数据源。"""

    # 数据变化通知（QML 侧用它触发列表重建）
    subjectsChanged = Signal()
    episodesChanged = Signal()
    inProgressChanged = Signal()
    watchedEpsChanged = Signal()

    def __init__(self, db: Database, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._display_mode = DISPLAY_FLAT
        # 查询缓存：Property 会被 QML 频繁读取（每次绑定重算都会调 getter），
        # 若每次都跑 SQL + 组装 dict 会很浪费。改为「变更时失效」。
        self._subjects_cache: list[dict] = []
        self._dirty = True
        self._inprogress_cache: list[dict] = []
        self._inprogress_dirty = True
        self._eps_cache: list[dict] = []
        self._eps_dirty = True

    # ---------- 配置 ----------
    def set_display_mode(self, mode: str) -> None:
        """由 QmlApp 在启动 / 设置保存后调用。"""
        mode = mode if mode in (DISPLAY_FLAT, DISPLAY_GROUPED) else DISPLAY_FLAT
        if mode != self._display_mode:
            self._display_mode = mode
            self.reload()

    # ---------- 刷新 ----------
    @Slot()
    def reload(self) -> None:
        """标记缓存失效并通知 QML 重新取数。"""
        self._dirty = True
        self._inprogress_dirty = True
        self._eps_dirty = True
        self.subjectsChanged.emit()
        self.inProgressChanged.emit()
        self.watchedEpsChanged.emit()

    @Slot()
    def reloadInProgress(self) -> None:
        """仅刷在看列表（在线拉取完成后由 InProgressBridge 调用）。"""
        self._inprogress_dirty = True
        self.inProgressChanged.emit()

    # ---------- Bangumi 收藏列表（阶段 7）----------
    @Property("QVariantList", notify=inProgressChanged)
    def inProgress(self) -> list[dict]:
        """Bangumi「看过」收藏（来自 `inprogress_cache`，由 InProgressBridge 写入）。

        > 命名说明：表名/属性名沿用 F18 的 `inprogress*`，但**语义是「看过」**
        > （`collect_type = 2`）。改名要动整条链路而收益仅是"名字好看"，
        > 故保留并在文档中标注。
        >
        > **只取「看过」**：同一张表里还存着「在看」的番（那是给动态页当
        > 候选用的，见 `InProgressBridge`），本属性显式过滤掉它们 ——
        > 本页标题就是「看过」，混进正在追的番会改变页面语义。

        缓存表字段：bangumi_id / name / name_cn / cover_url /
        ep_status / total_eps / collect_type / updated_at /
        collection_updated_at。
        这里额外补两个 QML 侧要用的字段：
          - `localSubjectId`：本地是否有对应条目（用于「播放/详情」按钮）
          - `coverUrl`：本地缓存的封面（`cover_path`）优先，否则在线 URL

        **`updatedAt` 对外给的是 `collection_updated_at`**（Bangumi 的收藏
        修改时间），而不是表里的 `updated_at`（那是缓存写入时刻，整批相同，
        对界面没有意义）。老库该列为空时回落到缓存写入时间。
        """
        if self._inprogress_dirty:
            self._inprogress_cache = self._load_inprogress()
            self._inprogress_dirty = False
        return self._inprogress_cache

    def _load_inprogress(self) -> list[dict]:
        try:
            # 只取「看过」（type=2）：缓存表里还存着「在看」的番，那是给
            # 动态页当候选用的（见 InProgressBridge）。本页标题就是「看过」，
            # 混进正在追的番会改变这个页面的语义。
            items = self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DONE)
        except Exception as e:
            log.exception("读取在看缓存失败: %s", e)
            return []

        out: list[dict] = []
        for it in items:
            # 本地关联：有同 bangumi_id 的条目就能一键跳详情
            local_id = 0
            try:
                local_id = self._db.find_subject_by_bangumi_id(it.bangumi_id) or 0
            except Exception:
                pass

            local_cover = ""
            if local_id:
                try:
                    s = self._db.get_subject(local_id)
                    local_cover = as_file_url(s.cover_path or "") if s else ""
                except Exception:
                    pass

            out.append({
                "bangumiId": int(it.bangumi_id or 0),
                "name": it.name or "",
                "nameCn": it.name_cn or "",
                "title": it.name_cn or it.name or "",
                "coverUrl": local_cover or (it.cover_url or ""),
                "epStatus": int(it.ep_status or 0),
                "totalEps": int(it.total_eps or 0),
                # 2 = 看过（当前唯一使用的类型，见 InProgressBridge）
                "collectType": int(it.collect_type or 2),
                # 收藏的最后修改时间（见上方说明：不是缓存写入时刻）
                "updatedAt": it.collection_updated_at or it.updated_at or "",
                "localSubjectId": local_id,
                "inLibrary": local_id > 0,
            })
        return out

    # ---------- F20：集级观看记录（动态页时间线）----------
    @Property("QVariantList", notify=watchedEpsChanged)
    def watchedEpisodes(self) -> list[dict]:
        """集级观看记录，按标记时间倒序（动态页 merged 模式的数据源）。

        数据来自 `watched_episodes` 表，由 InProgressBridge 的第二阶段
        拉取写入（见 §5.12.11.5）。字段与 `timeline()` 对齐，
        便于动态页复用同一套聚合逻辑。
        """
        if self._eps_dirty:
            self._eps_cache = self._load_watched_eps()
            self._eps_dirty = False
        return self._eps_cache

    def _load_watched_eps(self) -> list[dict]:
        try:
            rows = self._db.list_watched_episodes(limit=800)
        except Exception as e:
            log.exception("读取集级观看记录失败: %s", e)
            return []
        out: list[dict] = []
        for r in rows:
            # 本地关联：优先用表里存的 subject_id，其次按 bangumi_id 现查
            local_id = int(r.get("subject_id") or 0)
            if not local_id:
                try:
                    local_id = self._db.find_subject_by_bangumi_id(
                        int(r.get("bangumi_id") or 0)) or 0
                except Exception:
                    pass
            out.append({
                "episodeId": int(r.get("bangumi_ep_id") or 0),
                "subjectId": local_id,
                "subjectName": r.get("subject_name") or "",
                "epIndex": float(r.get("ep_index") or 0),
                "epTitle": r.get("ep_name") or "",
                "watchedAt": r.get("watched_at") or "",
                "bangumiId": int(r.get("bangumi_id") or 0),
                "inLibrary": local_id > 0,
                "isInProgress": False,   # 复用动态页既有字段
                "isBangumi": True,        # 标记来源是 Bangumi（非本地播放记录）
            })
        return out

    @Slot()
    def reloadWatchedEpisodes(self) -> None:
        """标记集级记录缓存失效并通知 QML。"""
        self._eps_dirty = True
        self.watchedEpsChanged.emit()

    @Slot(result="QVariantMap")
    def watchedEpisodesMeta(self) -> dict:
        """集级记录的元信息（条数 + 数据年龄），页面标题区展示用。"""
        try:
            count = self._db.count_watched_episodes()
        except Exception:
            return {"count": 0, "ageSeconds": -1}
        return {"count": count, "ageSeconds": -1}

    @Slot(result="QVariantMap")
    def inProgressMeta(self) -> dict:
        """收藏页的元信息（缓存年龄 + 条数），页面标题区展示用。

        条数只算「看过」—— 与 `inProgress` 的过滤保持一致，
        否则页头数字会比列表实际条数大（差额是在看的那几部）。
        """
        try:
            age = self._db.inprogress_cache_age()
            count = len(self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DONE))
        except Exception as e:
            log.exception("读取在看缓存元信息失败: %s", e)
            return {"count": 0, "ageSeconds": -1, "stale": True}
        # 超过 1 小时视为"数据较旧"，页面提示用户刷新
        stale = age is None or age > 3600
        return {
            "count": count,
            "ageSeconds": -1 if age is None else int(age),
            "stale": stale,
        }

    # ---------- 条目列表 ----------
    @Property("QVariantList", notify=subjectsChanged)
    def subjects(self) -> list[dict]:
        """全部条目（海报墙用）。

        `display_mode == grouped` 时按 series_name 聚合为「系列卡片」，
        字段与单条一致，额外带 `isGroup` / `childCount` / `childIds`。
        """
        if self._dirty:
            self._subjects_cache = self._load_subjects()
            self._dirty = False
        return self._subjects_cache

    def _load_subjects(self) -> list[dict]:
        try:
            rows = self._db.list_subjects()
        except Exception as e:
            log.exception("读取条目失败: %s", e)
            return []
        if self._display_mode == DISPLAY_GROUPED:
            return self._build_groups(rows)
        return [self._subject_to_dict(s) for s in rows]

    # ---------- 单条 ----------
    @Slot(int, result="QVariantMap")
    def subject(self, subject_id: int) -> dict:
        """按本地主键取单条详情。"""
        try:
            s = self._db.get_subject(subject_id)
        except Exception as e:
            log.exception("读取条目 %s 失败: %s", subject_id, e)
            return {}
        return self._subject_to_dict(s) if s else {}

    @Slot(int, result="QVariantList")
    def episodes(self, subject_id: int) -> list[dict]:
        """条目的集数列表（详情页用），按 ep_index 升序。"""
        try:
            eps = self._db.list_episodes(subject_id)
        except Exception as e:
            log.exception("读取集数 %s 失败: %s", subject_id, e)
            return []
        return [self._episode_to_dict(e) for e in eps]

    @Slot(int, result="QVariantList")
    def series_siblings(self, subject_id: int) -> list[dict]:
        """同系列的其他季（详情页「同系列」切换用）。"""
        try:
            cur = self._db.get_subject(subject_id)
            if cur is None or not (cur.series_name or "").strip():
                return []
            series = cur.series_name.strip()
            return [
                self._subject_to_dict(s)
                for s in self._db.list_subjects()
                if (s.series_name or "").strip() == series and s.id != subject_id
            ]
        except Exception as e:
            log.exception("读取同系列失败: %s", e)
            return []

    @Slot(int, result="QVariantList")
    def timeline(self, limit: int = 200) -> list[dict]:
        """观看动态时间线（动态页用）。"""
        try:
            entries = self._db.list_watched_timeline(limit=limit)
        except Exception as e:
            log.exception("读取时间线失败: %s", e)
            return []
        return [
            {
                "episodeId": e.episode_id,
                "subjectId": e.subject_id,
                "subjectName": e.subject_name or "",
                "epIndex": float(e.ep_index or 0),
                "epTitle": e.ep_title or "",
                "watchedAt": e.watched_at or "",
            }
            for e in entries
        ]

    @Slot(int, result="QVariantMap")
    def stats(self, subject_id: int) -> dict:
        """条目统计：已看 / 总数。海报卡片副标题用。"""
        try:
            eps = self._db.list_episodes(subject_id)
        except Exception as e:
            log.exception("统计条目 %s 失败: %s", subject_id, e)
            return {"watched": 0, "total": 0}
        return {
            "watched": sum(1 for e in eps if e.watched),
            "total": len(eps),
        }

    # ---------- 转换 ----------
    def _subject_to_dict(self, s: Subject) -> dict:
        return {
            "id": s.id,
            "bangumiId": s.bangumi_id or 0,
            "name": s.name or "",
            "nameCn": s.name_cn or "",
            "title": s.name_cn or s.name or "",
            "seriesName": s.series_name or "",
            "matchState": s.match_state or "auto",
            "totalEps": int(s.total_eps or 0),
            "folderPath": s.folder_path or "",
            # 封面转 URL，QML 的 Image 才能加载（含中文路径也能用）
            "coverUrl": as_file_url(s.cover_path or ""),
            "isGroup": False,
            "childCount": 1,
            "childIds": [s.id],
        }

    @staticmethod
    def _episode_to_dict(e: Episode) -> dict:
        return {
            "id": e.id,
            "subjectId": e.subject_id,
            "epIndex": float(e.ep_index or 0),
            "title": e.title or "",
            "filePath": e.file_path or "",
            "watched": bool(e.watched),
            "progress": float(e.watch_progress or 0.0),
            "watchedAt": e.watched_at or "",
        }

    def _build_groups(self, rows: list[Subject]) -> list[dict]:
        """按 series_name 聚合为系列卡片。"""
        groups: "OrderedDict[str, list[Subject]]" = OrderedDict()
        for s in rows:
            groups.setdefault((s.series_name or "").strip(), []).append(s)

        out: list[dict] = []
        for series, items in groups.items():
            # 无系列名或只有一部 → 平铺展示
            if not series or len(items) == 1:
                out.append(self._subject_to_dict(items[0]))
                continue
            watched = total = 0
            for s in items:
                eps = self._db.list_episodes(s.id)
                total += len(eps) or (s.total_eps or 0)
                watched += sum(1 for e in eps if e.watched)
            cover = next((s.cover_path for s in items if s.cover_path), "")
            d = self._subject_to_dict(items[0])
            d.update({
                "title": series,
                "name": series,
                "nameCn": series,
                "coverUrl": as_file_url(cover),
                "isGroup": True,
                "childCount": len(items),
                "childIds": [s.id for s in items],
                "watchedEps": watched,
                "totalEps": total,
            })
            out.append(d)
        return out
