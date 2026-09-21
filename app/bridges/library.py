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

from app.core.bangumi_api import COLLECT_TYPE_DOING
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
        """Bangumi「在看」收藏（来自 `inprogress_cache`，由 InProgressBridge 写入）。

        > 命名说明：表名/属性名一直是 F18 的 `inprogress*`，**语义从一开始就是
        > 「在看」**，中途一度改成「看过」，现在改回本意（`collect_type = 3`）——
        > 导航栏那一项也一直写着「在看」。
        >
        > **只取「在看」**：同一张表里还存着「看过」的番（那是给动态页当候选
        > 用的，见 `InProgressBridge`）。「看过」往往上百部，堆在本页既长又
        > 没用；它们的时间线在「动态」页里更合适。

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
            # 只取「在看」（type=3）：缓存表里两类都存着（「看过」是给动态页
            # 当候选用的，见 InProgressBridge），本页显式过滤 —— 页面语义是
            # "我正在追的番"，与导航栏标签一致。
            items = self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DOING)
        except Exception as e:
            log.exception("读取在看缓存失败: %s", e)
            return []

        # 「上传」统计：**一次查全量**再按条目取（逐条查会变成 2N 次查询）
        try:
            pend_map = self._db.pending_uploads()
            block_map = self._db.blocked_upload_counts()
        except Exception as e:
            log.exception("统计待补传失败: %s", e)
            pend_map, block_map = {}, {}

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

            # 「下一集」按钮：要播的集 ID（0 = 播不了）+ 播不了时的原因文案。
            # 按钮**不隐藏**，点不动时把原因报到状态栏（见 _next_episode）。
            next_ep_id, next_ep_hint = self._next_episode(local_id, it.ep_status)
            # 「上传」按钮：本地看过但 Bangumi 未标的集数（0 = 无事可做），
            # 以及本地看过却没有 bangumi_ep_id、压根传不了的集数
            pending_up = len(pend_map.get(local_id, []))
            blocked_up = int(block_map.get(local_id, 0))

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
                "nextEpisodeId": next_ep_id,
                "nextEpisodeHint": next_ep_hint,
                "pendingUpload": pending_up,
                "blockedUpload": blocked_up,
            })
        return out

    @Slot(result="QVariantList")
    def pendingUploads(self) -> list[dict]:
        """「本地看过、Bangumi 未标」的**逐集**清单 —— 动态页「上传」小窗的数据源。

        **一行 = 一集**（小窗里勾选的就是"这一集"），字段：
            {episodeId, bangumiEpId, subjectId, title, epIndex, epTitle}
        `title` 是动漫名 —— 小窗里与集名一起显示成「碧蓝之海 第三季 · EP7 妈妈」，
        否则用户根本看不出待传的是哪一集 ✗（实测反馈）。

        判据统一由 `Database.pending_uploads()` 给出 —— 与实际上传时的筛选
        **是同一个查询** ✓（否则会出现"显示 3 条只传了 1 条"）。
        """
        try:
            pend_map = self._db.pending_uploads()
        except Exception as e:
            log.exception("统计待补传失败: %s", e)
            return []
        out: list[dict] = []
        for sid, eps in pend_map.items():
            try:
                subj = self._db.get_subject(int(sid))
            except Exception:
                subj = None
            if subj is None or not subj.bangumi_id:
                continue            # 没匹配到 Bangumi 的传不了，不列
            title = subj.name_cn or subj.name or "（未命名条目）"
            for e in eps:
                out.append({
                    "episodeId": int(e["episode_id"]),
                    "bangumiEpId": int(e["bangumi_ep_id"]),
                    "subjectId": int(sid),
                    "title": title,
                    "epIndex": float(e["ep_index"] or 0),
                    "epTitle": e.get("ep_title") or "",
                })
        # 按动漫名 + 集号排：同一部的待传集挨在一起，便于逐部核对
        out.sort(key=lambda x: (x["title"], x["epIndex"]))
        return out

    @Slot(result=int)
    def blockedUploadCount(self) -> int:
        """本地看过但**没有 Bangumi 集号**、无法补传的集数（小窗里提示用）。

        实测本机 1446 集里有 455 集属于这种情况（扫描时没拿到集数元数据）——
        它们必须被**明确告知**，不能静默 ✗。
        """
        try:
            return sum(self._db.blocked_upload_counts().values())
        except Exception as e:
            log.exception("统计无法补传的集数失败: %s", e)
            return 0

    def _next_episode(self, subject_id: int, ep_status: int) -> tuple[int, str]:
        """「下一集」对应的本地集 ID 与**播不了时的原因**；可播时原因为空串。

        **按 Bangumi 的 `ep_status`（已看到第 N 集）算，不用本地的 `watched`
        标记** —— 实测本账号「在看」的 11 部里本地 `watched` 全是 0（那些集是
        在 Bangumi 网页 / 别的设备上标的，本程序没有播放记录），若按本地标记
        取"第一个未看过的"，会一律算成第 1 集 ✗。按 `ep_status` 算与页面上的
        进度条（读的也是 `ep_status`，如「5 / 12 集」）口径一致。

        规则：优先取 `ep_index == ep_status + 1` 的那一集（"接着看"的那集）；
        编号对不上（本地缺集/续篇从中间编号）时退回"第一集 ep_index 大于
        ep_status 的"；都没有则返回 0，并给出一句能解释清楚的原因 ——
        界面上按钮**不隐藏**，点不动时把原因报到状态栏（实测要求）。

        为什么要区分原因：三种"播不了"的处置完全不同 ——
        没入库（去入库）、本地已看到最新（正常，无需做什么）、
        集号数据坏了（是扫描解析的问题，得回去修）。
        """
        if subject_id <= 0:
            return 0, "未入库，无法播放"
        try:
            # 只考虑有本地文件的集（没文件没法播）
            eps = [e for e in self._db.list_episodes(subject_id)
                   if (e.file_path or "").strip()]
        except Exception as e:
            log.exception("查找下一集失败: %s", e)
            return 0, "读取本地集数失败（详见日志）"
        if not eps:
            return 0, "本地没有可播放的剧集文件"

        want = float(ep_status or 0) + 1
        for e in eps:                       # list_episodes 已按 ep_index 升序
            if abs(float(e.ep_index or 0) - want) < 1e-6:
                return int(e.id), ""
        later = [e for e in eps if float(e.ep_index or 0) > want]
        if later:
            return int(later[0].id), ""
        # 走到这里说明本地没有任何"第 want 集及以后"的集。两种成因要分开说：
        if len({float(e.ep_index or 0) for e in eps}) == 1:
            # 所有集号一模一样 → 扫描时标题没解析出来（实测：某部 12 集全是 2.0）
            return 0, ("本地集号异常（%d 集全是第 %g 集，扫描解析可能失败），"
                       "无法判断下一集" % (len(eps), float(eps[0].ep_index or 0)))
        return 0, ("本地没有更靠后的集了（Bangumi 进度 %d 集，本地共 %d 集）"
                   % (int(ep_status or 0), len(eps)))

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
            # 上限 5000：全量同步后本表约 1000+ 条（旧上限 800 会截断历史）。
            # 动态页的"加载更多"在 QML 侧切片，所以这里一次给全。
            rows = self._db.list_watched_episodes(limit=5000)
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

        条数只算「在看」—— 与 `inProgress` 的过滤保持一致，
        否则页头数字会比列表实际条数大（差额是"看过"的那批，上百部）。
        """
        try:
            age = self._db.inprogress_cache_age()
            count = len(self._db.load_inprogress_cache(collect_type=COLLECT_TYPE_DOING))
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
