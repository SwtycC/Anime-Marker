"""Bangumi 条目匹配打分。

解决「搜索结果第一条不是最相关」的问题：
Bangumi 搜索接口不保证按相关度排序，直接取 results[0] 会导致
「Fate」→「Fate/stay night」、「无职转生」→「同人动画」等误匹配。

策略：取前 N 条候选，逐条打分，选最高分；低于阈值则判为待手动确认。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ---- 评分权重 ----
SCORE_EXACT = 100        # 名称完全一致
SCORE_CONTAIN = 60       # 互相包含
SCORE_PREFIX = 55        # 前缀一致（应对「X～副标题～ 第3季」这类长名）
SCORE_WORD_OVERLAP = 20  # 有共同词
SCORE_SEASON_MATCH = 40  # 季数一致
SCORE_TYPE_MATCH = 30    # 条目类型一致（TV/剧场版）
SCORE_EP_NEAR = 20       # 集数接近
# ---- 惩罚项 ----
PENALTY_DERIVATIVE = 50  # 同人 / MAD / 剪辑
PENALTY_SHORT_CLIP = 40  # OP / ED / PV / CM / 预告

# 否决分：名称不相关、季数不符等硬条件不满足时返回
SCORE_IRRELEVANT = -1000

# 判定为自动匹配的最低分
ACCEPT_SCORE = 60
# 与第二名的最小差距（避免两条难分伯仲时误选）
# 配合「名称不相关直接否决」后，有效候选之间的差距通常远大于 30，
# 故适当放宽到 30，减少同一部作品被多个近似条目干扰而反复转人工。
ACCEPT_GAP = 30

# 说明：曾尝试给"靠前的候选关键词"加分（KEYWORD_PRIORITY_BONUS），
# 但实测有害——同一部作品会被多个候选搜到，加分会让"脏关键词"胜出、
# 反而把正确的短关键词（如纯中文名）压下去，且抬高二三名分数触发 gap 拦截。
# 现改为：同一 Bangumi 条目取最高分；候选顺序仅在完全同分时决定。

# 候选关键词的优先级加成：靠前的候选更可信，给予分数加成。
# 目的：避免「父级名」这类宽泛候选（排在第 3 位）反超「父级+当前」的精确候选。
# 例：苍海之泪篇的候选 ③「史莱姆这档事」会让 TV 条目得 130 分，
#     超过剧场版的 110 分；加优先级加成后 ① 的 110+30 > ③ 的 130。
KEYWORD_PRIORITY_BONUS = [30, 15, 0, 0, 0]

# Bangumi 条目类型
SUBJECT_TYPE_ANIME = 2
# 条目 subtype（用于区分 TV / 剧场版）
SUBTYPE_MOVIE_HINTS = ("剧场版", "劇場版", "Movie", "MOVIE", "电影", "劇場")

# 同人 / 剪辑类关键词（命中即重罚）
DERIVATIVE_PATTERNS = [
    r"同人", r"MAD", r"AMV", r"手书", r"剪辑", r"混剪", r"合集",
    r"Fan\s*Made", r"二次创作",
]
# 短片 / 宣传片关键词
SHORT_CLIP_PATTERNS = [
    r"\bOP\b", r"\bED\b", r"\bPV\b", r"\bCM\b", r"预告", r"宣传片",
    r"Opening", r"Ending", r"手机游戏", r"游戏\s*OP",
]

# ---- 季数识别 ----
# 中文模式（默认启用）
SEASON_PATTERNS_CN = [
    (r"第\s*([一二三四五六七八九十\d]+)\s*[季部]", "cn"),
]
# 「S1 / Season 1 / 2nd Season / Part 2」也默认启用：
# 实测媒体库里这类写法很常见（如「Overlord S1」），不识别会导致关键词退化为「S1」而搜不到。
SEASON_PATTERNS_EN = [
    (r"(?i)\bSeason\s*(\d+)", "num"),
    (r"(?i)\bS(\d{1,2})\b", "num"),
    (r"(?i)\b(\d+)(?:st|nd|rd|th)\s*Season", "num"),
    (r"(?i)\bPart\s*(\d+)", "num"),
]
# 罗马数字仅在 all 模式下启用（容易与作品名混淆，如「X」「V」）
SEASON_PATTERNS_ROMAN = [
    (r"(?<![A-Za-z])(III|IV|IX|VIII|VII|VI|II)(?![A-Za-z])", "roman"),
]

_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12,
}
_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
}

# 附属内容目录名（不单独成条目，归入所属季）
# 说明：用 search 而非 fullmatch 语义，允许「NCOP&NCED」「SP+OVA」等组合写法
EXTRA_DIR_PATTERNS = [
    # SP / OVA / OAD 系列
    r"(?i)\bSPs?\b", r"(?i)\bOVAs?\b", r"(?i)\bOADs?\b",
    # OP/ED 无字幕版（NCOP = Non-Credit OP）
    r"(?i)\bNCOP\b", r"(?i)\bNCED\b",
    r"(?i)\bNCOP\s*[&＆+/]\s*NCED\b", r"(?i)\bNC(OP|ED)s\b",
    # 特典 / 菜单 / 预告 / 宣传影像
    r"特典", r"(?i)\bMenu\b", r"预告", r"宣传影像", r"花絮", r"访谈",
    # PV / CM / Trailer
    r"(?i)\bPVs?\b", r"(?i)\bCMs?\b", r"(?i)\bPV\s*[&＆/+]\s*CM\b",
    r"(?i)\bTrailers?\b", r"(?i)\bPreviews?\b",
]


@dataclass
class MatchResult:
    """匹配结果。"""

    subject: Optional[dict]
    score: int
    reason: str
    runner_up_score: int = 0


def is_extra_dir(name: str) -> bool:
    """目录名是否为附属内容（SP/OVA/NCOP/PV 等）。

    采用"包含即命中"语义，以覆盖「NCOP&NCED」「SP+OVA」等组合写法；
    但要求整名不包含季数标记（避免把「第二季 SP」这类误判为纯附属目录）。
    """
    clean = (name or "").strip()
    if not clean:
        return False
    if not any(re.search(p, clean) for p in EXTRA_DIR_PATTERNS):
        return False
    # 若含季数标记，说明它是「XX季的特典」目录，仍按附属处理即可（返回 True）
    return True


def is_noise_dir(name: str) -> bool:
    """目录名是否为纯噪声层（BDRip/1080p/合集等），应跳过继续下探。"""
    lowered = name.lower()
    noise = ["bdrip", "webrip", "web-dl", "webdl", "bluray", "1080p", "720p",
             "2160p", "4k", "hevc", "x264", "x265", "10bit", "8bit",
             "合集", "complete", "batch", "fin"]
    # 整个目录名就是噪声词（或只剩噪声）才算噪声层
    stripped = lowered
    for n in noise:
        stripped = stripped.replace(n, "")
    stripped = re.sub(r"[\s\-_\[\]()【】]+", "", stripped)
    return not stripped


def normalize(text: str) -> str:
    """规范化：去空格、标点、大小写，便于比对。"""
    t = re.sub(r"[\s\-_·・:：/／\\|｜~～!！?？,，.。'\"“”‘’()（）\[\]【】]+", "", text)
    return t.lower()


def extract_season(text: str, mode: str = "cn") -> Optional[int]:
    """从文本提取季数。

    mode: cn = 第X季 + 常见英文（S1/Season 1）；all = 额外启用罗马数字
    """
    patterns = list(SEASON_PATTERNS_CN) + list(SEASON_PATTERNS_EN)
    if mode == "all":
        patterns += SEASON_PATTERNS_ROMAN
    elif mode == "cn":
        # cn 模式下保留英文但去掉易误判的短写法（单独一个 S 后跟数字已够明确，保留）
        pass

    for pattern, kind in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        raw = m.group(1)
        if kind == "cn":
            if raw in _CN_NUM:
                return _CN_NUM[raw]
            if raw.isdigit():
                return int(raw)
        elif kind == "num":
            if raw.isdigit():
                return int(raw)
        elif kind == "roman":
            if raw.upper() in _ROMAN:
                return _ROMAN[raw.upper()]
    return None


def is_movie_like(text: str) -> bool:
    """文本是否像剧场版/单独篇章。"""
    if any(h in text for h in SUBTYPE_MOVIE_HINTS):
        return True
    return bool(re.search(r"篇$|编$|章$", text.strip()))


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _strip_season(text: str, mode: str) -> str:
    """去掉季数标记，得到作品主名（用于前缀/主名比对）。

    例：「无职转生～到了异世界就拿出真本事～ 第3季」→「无职转生到了异世界就拿出真本事」
        「无职转生 第三季」→「无职转生」
    """
    out = normalize(text)
    season = extract_season(text, mode)
    if season is None:
        return out
    # 去掉中文季数
    out = re.sub(r"第[一二三四五六七八九十\d]+[季部]", "", out)
    # 去掉英文/数字季数
    out = re.sub(r"season\d+", "", out)
    out = re.sub(r"\ds\d+", "", out)
    out = re.sub(r"\d(?:st|nd|rd|th)season", "", out)
    out = re.sub(r"part\d+", "", out)
    # 去掉罗马数字（仅当它就是季数标记时）
    return out


def _main_name_score(keyword: str, candidate: str, mode: str) -> tuple[int, str]:
    """主名比对：去掉季数后比较作品名本身，处理长副标题的情况。

    返回 (分数, 理由)。
    """
    kw_main = _strip_season(keyword, mode)
    cand_main = _strip_season(candidate, mode)
    if not kw_main or not cand_main:
        return 0, ""

    # 主名完全一致
    if kw_main == cand_main:
        return SCORE_PREFIX, "主名一致"
    # 主名互相包含（Bangumi 常带 ～副标题～）
    if kw_main in cand_main or cand_main in kw_main:
        return SCORE_PREFIX, "主名包含"
    # 关键词主名是候选主名的前缀（如「无职转生」vs「无职转生到了异世界就拿出真本事」）
    common = 0
    for a, b in zip(kw_main, cand_main):
        if a != b:
            break
        common += 1
    # 至少 4 个字符的前缀重合才认（避免「魔法」这类短前缀误命中）
    if common >= 4 and common >= len(kw_main) * 0.6:
        return SCORE_PREFIX, f"主名前缀{common}字"
    return 0, ""


def score_subject(
    subject: dict,
    keyword: str,
    local_ep_count: int = 0,
    season_mode: str = "cn",
    keyword_season: Optional[int] = None,
) -> tuple[int, str]:
    """给单个 Bangumi 候选打分，返回 (分数, 理由)。"""
    name = subject.get("name", "") or ""
    name_cn = subject.get("name_cn", "") or ""
    candidates = [n for n in (name_cn, name) if n]

    score = 0
    reasons: list[str] = []

    norm_kw = normalize(keyword)
    best_name_score = 0
    for cand in candidates:
        nc = normalize(cand)
        if not nc or not norm_kw:
            continue
        if nc == norm_kw:
            best_name_score = max(best_name_score, SCORE_EXACT)
        elif norm_kw in nc or nc in norm_kw:
            best_name_score = max(best_name_score, SCORE_CONTAIN)
        else:
            # 词级重合：按 2-gram 粗算，避免完全不相关的条目得分
            kw_words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", norm_kw))
            cand_words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", nc))
            if kw_words and cand_words and (kw_words & cand_words):
                best_name_score = max(best_name_score, SCORE_WORD_OVERLAP)

    # ---- 名称比对过滤：必须"作品名"匹配，不能只靠季数雷同 ----
    # 例：关键词「第一季」与「我叫MT 第一季」都含「第一季」，但作品无关，
    # 若仅凭此给 60 分，会让大量无关作品挤进候选并干扰正确条目。
    if keyword_season is not None:
        kw_main = _strip_season(keyword, season_mode)
        if not kw_main:
            # 关键词本身就是纯季数（无作品名）→ 无法据此判断作品，直接否决
            return SCORE_IRRELEVANT, f"关键词仅含季数，无法确定作品（{keyword}）"
        if best_name_score > 0:
            # 关键词含作品名 → 要求候选去季数后也与之匹配
            matched_main = False
            for cand in candidates:
                cand_main = _strip_season(cand, season_mode)
                if not cand_main:
                    continue
                if cand_main == kw_main or kw_main in cand_main or cand_main in kw_main:
                    matched_main = True
                    break
            if not matched_main:
                best_name_score = 0
    # 主名比对兜底：名称未能直接命中时，去掉季数后再比（应对长副标题）
    best_reason = ""
    if best_name_score < SCORE_CONTAIN:
        for cand in candidates:
            s, why = _main_name_score(keyword, cand, season_mode)
            if s > best_name_score:
                best_name_score, best_reason = s, why

    # ---- 门槛：名称完全不相关 → 直接否决 ----
    # 否则「季数一致 40 + 类型一致 30 + 集数接近 20 = 90」会让
    # 完全无关的条目（如关键词「我独自升级 第一季」匹配到「我叫MT 第一季」）
    # 拿到 90 分，与正确条目差距不足而触发 gap 拦截。
    if best_name_score <= 0:
        return SCORE_IRRELEVANT, f"名称不相关（{name_cn or name}）"

    if best_name_score:
        suffix = f"（{best_reason}）" if best_reason else ""
        reasons.append(f"名称+{best_name_score}{suffix}")

    score += best_name_score

    # ---- 季数一致性（明确不符 → 一票否决）----
    if keyword_season is not None:
        subject_season = extract_season(name_cn or name, season_mode)
        if subject_season == keyword_season:
            score += SCORE_SEASON_MATCH
            reasons.append(f"季数一致({keyword_season})+{SCORE_SEASON_MATCH}")
        elif subject_season is not None:
            # 季数明确不同：直接否决。季数是硬条件，
            # 「第一季」匹配到「第二季」属于错误结果，不应靠其他项补救。
            return (
                SCORE_IRRELEVANT,
                f"季数不符（条目={subject_season}，期望={keyword_season}）",
            )

    # 类型一致性：关键词像剧场版，条目也该像
    kw_movie = is_movie_like(keyword)
    subj_movie = is_movie_like(name_cn or name)
    if kw_movie == subj_movie:
        score += SCORE_TYPE_MATCH
        reasons.append(f"类型一致+{SCORE_TYPE_MATCH}")

    # 集数接近
    total_eps = subject.get("total_episodes") or subject.get("eps_count") or 0
    if local_ep_count and total_eps:
        if abs(int(total_eps) - local_ep_count) <= 2:
            score += SCORE_EP_NEAR
            reasons.append(f"集数接近({total_eps}≈{local_ep_count})+{SCORE_EP_NEAR}")

    # 惩罚
    text_for_penalty = f"{name_cn} {name}"
    if _has_pattern(text_for_penalty, DERIVATIVE_PATTERNS):
        score -= PENALTY_DERIVATIVE
        reasons.append(f"同人/剪辑-{PENALTY_DERIVATIVE}")
    if _has_pattern(text_for_penalty, SHORT_CLIP_PATTERNS):
        score -= PENALTY_SHORT_CLIP
        reasons.append(f"短片/宣传片-{PENALTY_SHORT_CLIP}")

    return score, " ".join(reasons) or "无匹配项"


def _same_series(a: dict, b: dict, season_mode: str = "cn") -> bool:
    """判断两个 Bangumi 条目是否属于同一系列（主名相同、仅季数不同）。

    同一系列的不同季分数接近是正常现象（如「无职转生 第二季」与「第三季」），
    此时不应因 gap 过小而拒绝匹配。
    """
    if a is None or b is None:
        return False
    names_a = [a.get("name_cn") or "", a.get("name") or ""]
    names_b = [b.get("name_cn") or "", b.get("name") or ""]
    for na in names_a:
        if not na:
            continue
        main_a = _strip_season(na, season_mode)
        if not main_a:
            continue
        for nb in names_b:
            if not nb:
                continue
            main_b = _strip_season(nb, season_mode)
            if not main_b:
                continue
            # 主名互相包含即视为同系列
            if main_a == main_b or main_a in main_b or main_b in main_a:
                return True
    return False


class SubjectMatcher:
    """候选打分与择优。"""

    def __init__(
        self,
        api,
        season_mode: str = "cn",
        accept_score: int = ACCEPT_SCORE,
        accept_gap: int = ACCEPT_GAP,
    ):
        self.api = api
        self.season_mode = season_mode
        self.accept_score = accept_score
        self.accept_gap = accept_gap

    def score_with_reason(
        self,
        subject: dict,
        keyword: str,
        local_ep_count: int = 0,
    ) -> tuple[int, str]:
        """给单个候选项打分（供手动匹配对话框排序用）。"""
        season = extract_season(keyword, self.season_mode)
        return score_subject(
            subject, keyword, local_ep_count, self.season_mode, season
        )

    def search_best(
        self,
        keywords: list[str],
        local_ep_count: int = 0,
    ) -> MatchResult:
        """用多个候选关键词搜索，返回得分最高的条目。

        keywords 按优先级排列（越靠前越可信），相同时分取靠前者。
        """
        best_subject: Optional[dict] = None
        best_score = -9999
        best_reason = ""
        second_score = -9999
        second_subject: Optional[dict] = None

        for kw in keywords:
            if not kw:
                continue
            season = extract_season(kw, self.season_mode)
            try:
                results = self.api.search_subjects(kw, limit=10)
            except Exception as e:
                log.warning("搜索失败 keyword=%s err=%s", kw, e)
                continue

            for subj in results:
                s, reason = score_subject(
                    subj, kw, local_ep_count, self.season_mode, season
                )
                # 被否决的候选（名称不相关 / 季数不符）不参与竞争
                if s <= SCORE_IRRELEVANT:
                    continue
                # 同一 Bangumi 条目可能被多个候选关键词搜到，取最高分即可，
                # 不能因"候选顺序"加减分——那会惩罚正确的短关键词。
                if best_subject is not None and subj.get("id") == best_subject.get("id"):
                    if s > best_score:
                        best_score, best_reason = s, f"[{kw}] {reason}"
                    continue
                if s > best_score:
                    second_score, second_subject = best_score, best_subject
                    best_score, best_subject = s, subj
                    best_reason = f"[{kw}] {reason}"
                elif s > second_score:
                    second_score, second_subject = s, subj

        if best_subject is None:
            return MatchResult(None, 0, "无候选结果")

        # 差距过小 → 不自动接受
        # 注意：只在"第二名是不同条目"时才比较差距。
        # 同一部作品的不同季（如「无职转生 第二季」vs「第三季」）分数接近是正常的，
        # 此时不应拒绝——只要最高分够高就采纳。
        gap = best_score - max(second_score, -9999)
        if best_score < self.accept_score:
            return MatchResult(
                None, best_score,
                f"最高分 {best_score} < 阈值 {self.accept_score}（{best_reason}）",
                second_score,
            )
        # 若第二名与第一名是同一系列的不同季（主名相同），视为"已区分"，不做 gap 拦截
        same_series = (
            second_subject is not None
            and best_subject is not None
            and _same_series(best_subject, second_subject, self.season_mode)
        )
        if second_score > -9999 and gap < self.accept_gap and not same_series:
            return MatchResult(
                None, best_score,
                f"前二名差距 {gap} 过小，难以确定（{best_reason}）",
                second_score,
            )
        return MatchResult(best_subject, best_score, best_reason, second_score)
