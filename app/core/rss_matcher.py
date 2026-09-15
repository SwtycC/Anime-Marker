"""新集判定 / 三层查重 / 下载规则决策（F19）。

对应 §5.10.2 与 §8.4：
1. 标题清洗 → 提取动漫名 + 集数序号
2. 三层查重：本地媒体库 → qBittorrent 任务 → download_history
3. 按规则（source.rule 优先，否则 rss.rule）决定是否下发
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.qbittorrent_api import QbClient
from app.core.rss_feed import FeedEntry
from app.utils.title_parser import extract_episode_index, clean_anime_title

log = logging.getLogger(__name__)

# 下载规则
RULE_NEW_ONLY = "new_only"
RULE_FILL_GAP = "fill_gap"
RULE_COMPLETE_PACK = "complete_pack"
RULE_MANUAL = "manual"

VALID_RULES = {RULE_NEW_ONLY, RULE_FILL_GAP, RULE_COMPLETE_PACK, RULE_MANUAL}

# 合集 / 整包关键词
PACK_PATTERNS = [
    r"(?i)\b(合集|全集|Complete|Batch|BDRip\s*全集|Fin)\b",
    r"(?i)\b(01\s*[-~]\s*\d{2})\b",       # 01-12
    r"(?i)\b(Vol\.?\s*\d+\s*[-~]\s*\d+)\b",
]
PACK_RE = [re.compile(p) for p in PACK_PATTERNS]

# 季度 / 年份等噪声（用于与本地条目比对）
SEASON_RE = re.compile(r"(?i)\b(S\d{1,2}|Season\s*\d+|第[一二三四五六七八九十\d]+季)\b")


@dataclass
class JudgeResult:
    """单条 RSS 条目的判定结果。"""

    entry: FeedEntry
    ep_index: float
    is_new: bool
    should_download: bool
    reason: str
    subject_id: Optional[int] = None
    is_pack: bool = False


class RssMatcher:
    """判新与决策。"""

    def __init__(self, db: Database, qb: Optional[QbClient], config: Config) -> None:
        self.db = db
        self.qb = qb
        self.config = config

    # ---------- 主入口 ----------
    def judge(self, entry: FeedEntry, source: RssSource) -> JudgeResult:
        """判定单条 RSS 条目。"""
        title = entry.title or ""
        ep_index = extract_episode_index(title)
        is_pack = self._is_pack(title)

        subj_id = source.bangumi_id or self._match_local_subject(title)

        # 集数解析失败：一律不自动下载（风险应对：防误判下错集）
        if ep_index is None and not is_pack:
            log.info("集数解析失败，转人工确认：%s", title)
            return JudgeResult(entry, 0.0, False, False,
                               "集数解析失败，需人工确认", subj_id, False)

        ep_for_db = float(ep_index) if ep_index is not None else 0.0

        # 三层查重
        hit = self._dedup(entry, source, subj_id, ep_for_db, is_pack)
        if hit:
            return JudgeResult(entry, ep_for_db, False, False, hit, subj_id, is_pack)

        # 规则决策
        rule = self._resolve_rule(source)
        auto = self.config.getbool("rss", "auto_download", False)

        if rule == RULE_MANUAL:
            return JudgeResult(entry, ep_for_db, True, False,
                               "规则=manual，仅通知不下发", subj_id, is_pack)

        if not auto:
            return JudgeResult(entry, ep_for_db, True, False,
                               "自动下载已关闭，入库为待确认", subj_id, is_pack)

        if rule == RULE_COMPLETE_PACK and not is_pack:
            # 整包规则下，单集是否下载取决于是否补缺
            if self._has_gap(subj_id, ep_for_db):
                return JudgeResult(entry, ep_for_db, True, True,
                                   "整包规则但存在缺集，下载该单集", subj_id, is_pack)
            return JudgeResult(entry, ep_for_db, True, False,
                               "整包规则，等待合集资源", subj_id, is_pack)

        if rule == RULE_FILL_GAP and self._is_already_local(subj_id, ep_for_db):
            return JudgeResult(entry, ep_for_db, False, False,
                               "补缺规则：本地已有该集", subj_id, is_pack)

        reason = "整包资源命中" if is_pack else "判定为新集"
        return JudgeResult(entry, ep_for_db, True, True, reason, subj_id, is_pack)

    # ---------- 三层查重 ----------
    def _dedup(
        self,
        entry: FeedEntry,
        source: RssSource,
        subject_id: Optional[int],
        ep_index: float,
        is_pack: bool,
    ) -> str:
        """返回非空字符串表示命中（已被下载/已存在）。"""
        # ① 本地媒体库
        if not is_pack and self._is_already_local(subject_id, ep_index):
            return f"本地媒体库已有第 {ep_index:g} 集"

        # ② qBittorrent 现有任务
        if self.qb is not None:
            try:
                if self.qb.has_torrent_like(entry.title):
                    return "qBittorrent 中已存在同名任务"
            except Exception as e:
                log.warning("qBittorrent 查重失败（跳过该层）：%s", e)

        # ③ 历史下载记录
        if not is_pack and self.db.is_episode_downloaded(source.id, ep_index):
            return "下载历史中已存在"
        if is_pack and self.db.is_episode_downloaded(source.id, 0.0):
            return "该订阅已下载过整包"

        return ""

    def _is_already_local(self, subject_id: Optional[int], ep_index: float) -> bool:
        if subject_id is None:
            return False
        return ep_index in self.db.list_local_ep_indices(subject_id)

    def _has_gap(self, subject_id: Optional[int], ep_index: float) -> bool:
        """是否存在缺集（本地没有该集即算缺）。"""
        return not self._is_already_local(subject_id, ep_index)

    # ---------- 辅助 ----------
    def _resolve_rule(self, source: RssSource) -> str:
        rule = (source.rule or "").strip() or self.config.get("rss", "rule", RULE_NEW_ONLY)
        if rule not in VALID_RULES:
            log.warning("未知下载规则 %s，回退 new_only", rule)
            return RULE_NEW_ONLY
        return rule

    def _match_local_subject(self, title: str) -> Optional[int]:
        """用清洗后的标题在本地 subjects 中做模糊匹配。"""
        key = clean_anime_title(title)
        if not key:
            return None
        norm = self._normalize(key)
        best: tuple[int, int] = (0, 0)  # (score, subject_id)

        for s in self.db.list_subjects():
            for cand in (s.name_cn, s.name):
                if not cand:
                    continue
                nc = self._normalize(cand)
                if not nc:
                    continue
                if nc == norm:
                    return s.id
                if nc and (nc in norm or norm in nc):
                    score = min(len(nc), len(norm))
                    if score > best[0]:
                        best = (score, s.id)
        return best[1] or None

    @staticmethod
    def _normalize(text: str) -> str:
        t = SEASON_RE.sub(" ", text)
        t = re.sub(r"[\s\-_·:：]+", "", t)
        return t.lower()

    @staticmethod
    def _is_pack(title: str) -> bool:
        return any(rx.search(title) for rx in PACK_RE)
