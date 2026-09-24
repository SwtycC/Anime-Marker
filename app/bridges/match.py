"""手动匹配桥接（原 `ui/match_dialog.py` 的 QML 等价实现）。

职责：自动匹配失败或结果错误时，让用户搜索 Bangumi 并手动指定条目。

与旧版的一致行为：
1. 默认关键词 = 本地条目名（`/` 换成空格，避免 `Fate/strange Fake` 搜不到）
2. 结果取前 20 条，**按同一套打分规则排序**（`SubjectMatcher.score_with_reason`）
3. 被否决的候选（名称不相关 / 季数不符）**保留但排在后面**，
   前端灰度显示 —— 用户可能确实要一部名字不同的作品
4. 选中后写 `match_state='manual'`，重拉集数回填 `bangumi_ep_id`（不动本地文件）

实现要点：搜索走 QThread（网络请求可能数秒），结果用信号回传。
QML 侧不持有线程，只消费 `searchFinished` 信号。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.database import Database
from app.core.matcher import SCORE_IRRELEVANT, SubjectMatcher
from app.utils import bgm_log

log = logging.getLogger(__name__)

# 搜索结果上限（与旧版一致）
SEARCH_LIMIT = 20

# 自动匹配的最低分（用于给候选分档着色）
ACCEPT_SCORE = 60


class _SearchWorker(QThread):
    """后台搜索，避免阻塞 UI。"""

    finished_results = Signal(object)   # list[dict]

    def __init__(
        self,
        api: BangumiClient,
        keyword: str,
        ep_count: int,
        subject_name: str = "",
    ) -> None:
        super().__init__()
        self.api = api
        self.keyword = keyword
        self.ep_count = ep_count
        self.subject_name = subject_name

    def run(self) -> None:
        # 让 Bangumi 请求日志带上「哪个动漫」
        bgm_log.set_current_name(self.subject_name)
        matcher = SubjectMatcher(self.api)
        try:
            results = self.api.search_subjects(self.keyword, limit=SEARCH_LIMIT)
        except BangumiError as e:
            log.warning("手动匹配搜索失败: %s", e)
            self.finished_results.emit([])
            return
        except Exception:
            log.exception("手动匹配搜索异常")
            self.finished_results.emit([])
            return

        scored: list[dict] = []
        for subj in results:
            # 先绑定 ID ↔ 名称，后续拉集数若失败可定位到条目
            bgm_log.bind_subject(
                subj.get("id"), subj.get("name_cn") or subj.get("name"))
            score, reason = matcher.score_with_reason(
                subj, self.keyword, self.ep_count)
            scored.append(self._to_dict(subj, score, reason))

        # 按分数降序；同分保持原顺序（Bangumi 的相关度顺序仍有参考价值）
        scored.sort(key=lambda d: d["score"], reverse=True)
        self.finished_results.emit(scored)

    @staticmethod
    def _to_dict(subj: dict, score: int, reason: str) -> dict:
        name = subj.get("name") or ""
        name_cn = subj.get("name_cn") or ""
        images = subj.get("images") or {}
        date = subj.get("date") or ""
        return {
            "bangumiId": subj.get("id") or 0,
            "name": name,
            "nameCn": name_cn,
            "title": name_cn or name,
            "year": date[:4] if date else "",
            "totalEps": int(subj.get("total_episodes")
                            or subj.get("eps_count") or 0),
            "coverUrl": images.get("large") or images.get("common") or "",
            "score": score,
            "reason": reason,
            # 被否决 / 低分候选由前端灰度显示
            "rejected": score <= SCORE_IRRELEVANT,
            "lowScore": 0 < score < ACCEPT_SCORE,
        }


class MatchBridge(QObject):
    """手动匹配控制器。"""

    searchStarted = Signal()
    searchFinished = Signal(object)      # list[dict]
    #: 应用成功（参数：subject_id 本地主键, name_cn 新名称）
    applied = Signal(int, str)
    failed = Signal(str)

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._api = api
        self._worker: Optional[_SearchWorker] = None
        # 当前正在处理的本地条目
        self._subject_id = 0
        self._folder_path = ""

    def set_api(self, api: BangumiClient) -> None:
        self._api = api

    # ---------- 搜索 ----------
    @Slot(int, result=str)
    def defaultKeyword(self, subject_id: int) -> str:
        """默认关键词：本地条目名，`/` 换成空格。

        `Fate/strange Fake` 直接搜会被当路径，实测换成空格命中率更高。
        """
        subj = self._db.get_subject(subject_id)
        if subj is None:
            return ""
        return (subj.name_cn or subj.name or "").replace("/", " ")

    @Slot(int, str)
    def search(self, subject_id: int, keyword: str) -> None:
        """按关键词搜索候选（异步）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            self.failed.emit("请输入关键词")
            return
        if self._worker is not None and self._worker.isRunning():
            log.info("上一次搜索尚未结束，忽略本次请求")
            return

        subj = self._db.get_subject(subject_id)
        if subj is None:
            self.failed.emit("条目不存在")
            return

        self._subject_id = subject_id
        self._folder_path = subj.folder_path or ""
        ep_count = len(self._db.list_episodes(subject_id))

        self.searchStarted.emit()
        self._worker = _SearchWorker(
            self._api, keyword, ep_count, subj.name_cn or subj.name or "")
        self._worker.finished_results.connect(self._on_results)
        self._worker.start()

    def _on_results(self, results: list) -> None:
        log.info("手动匹配搜索完成：%s 条候选", len(results))
        self.searchFinished.emit(results)

    # ---------- 应用 ----------
    @Slot(int, result=bool)
    def apply(self, bangumi_id: int) -> bool:
        """把选中的 Bangumi 条目写入本地记录。

        返回是否成功。成功后会重拉集数并回填 `bangumi_ep_id`（本地文件不动）。
        """
        if not bangumi_id:
            self.failed.emit("未选择条目")
            return False
        if not self._subject_id:
            self.failed.emit("请先执行搜索")
            return False

        try:
            subj = self._api.get_subject(bangumi_id)
        except BangumiError as e:
            log.warning("拉取条目详情失败 bangumi_id=%s: %s", bangumi_id, e)
            self.failed.emit(f"拉取条目详情失败：{e}")
            return False

        name = subj.get("name") or ""
        name_cn = subj.get("name_cn") or name
        images = subj.get("images") or {}
        cover_url = images.get("large") or images.get("common") or ""
        total_eps = int(subj.get("total_episodes") or subj.get("eps_count") or 0)

        # 封面下载失败不阻塞匹配
        cover_path = ""
        if cover_url:
            try:
                from app.utils.cover_cache import download as download_cover
                cover_path = str(
                    download_cover(bangumi_id, cover_url, self._api.session))
            except Exception as e:
                log.warning("封面下载失败: %s", e)

        try:
            self._db.set_manual_match(
                subject_id=self._subject_id,
                bangumi_id=bangumi_id,
                name=name,
                name_cn=name_cn,
                cover_url=cover_url,
                cover_path=cover_path,
                total_eps=total_eps,
            )
        except Exception as e:
            log.exception("写入手动匹配失败 subject_id=%s", self._subject_id)
            self.failed.emit(f"写入失败：{e}")
            return False

        # 顺路存接口前 10 个 tag（get_subject 响应里就有，零额外请求）。
        # 与扫描路径同一策略：失败不阻塞匹配。
        try:
            self._db.replace_subject_tags(
                self._subject_id, self._db.tags_from_subject(subj))
        except Exception as e:
            log.warning("写入条目标签失败 subject_id=%s: %s", self._subject_id, e)

        # 重拉集数并回填 bangumi_ep_id（本地文件不动）
        self._refill_episodes(bangumi_id, name_cn or name)

        log.info("手动匹配完成：subject_id=%s -> bgm=%s（%s）",
                 self._subject_id, bangumi_id, name_cn)
        self.applied.emit(self._subject_id, name_cn)
        return True

    def _refill_episodes(self, bangumi_id: int, label: str) -> None:
        bgm_log.bind_subject(bangumi_id, label)
        try:
            bgm_eps = self._api.get_episodes(bangumi_id)
        except BangumiError as e:
            log.warning("拉取集数失败（不影响手动匹配）: %s", e)
            return
        ep_map = {e.get("sort") or e.get("ep"): e for e in bgm_eps}
        try:
            for ep in self._db.list_episodes(self._subject_id):
                bgm = ep_map.get(ep.ep_index)
                if bgm:
                    self._db.update_episode_title(
                        ep.id,
                        bgm.get("name_cn") or bgm.get("name") or ep.title,
                        bgm.get("id"),
                    )
        except Exception as e:
            log.exception("回填集数失败 subject_id=%s", self._subject_id)
