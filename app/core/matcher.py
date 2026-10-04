"""Bangumi 条目匹配打分。

解决「搜索结果第一条不是最相关」的问题：
Bangumi 搜索接口不保证按相关度排序，直接取 results[0] 会导致
「Fate」→「Fate/stay night」、「无职转生」→「同人动画」等误匹配。

策略：取前 N 条候选，逐条打分，选最高分；低于阈值则判为待手动确认。
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.core.bangumi_api import describe_connection_error, extract_aliases

log = logging.getLogger(__name__)

# ---- 评分权重 ----
SCORE_EXACT = 100        # 名称完全一致
SCORE_CONTAIN = 60       # 互相包含
SCORE_PREFIX = 55        # 前缀一致（应对「X～副标题～ 第3季」这类长名）
SCORE_ALIAS_EXACT = 78   # 别名完全一致（低于正式名，见下方说明）
SCORE_ALIAS_CONTAIN = 45 # 别名互相包含
SCORE_WORD_OVERLAP = 20  # 有共同词
SCORE_NAME_SIMILAR = 85  # 名称高度相似（民间译名与官方名只差一两个字）
# 「候选名是关键词的一个小片段」时要求的最低覆盖率（见 _coverage）。
#
# **为什么需要**（实测踩坑）：目录名很长时（「命运石之门 全集 第一季 第二季
# 剧场版 简繁中日双语内封字幕」「游戏人生 剧场版」），Bangumi 上那些**两个
# 字的条目**（「命运」「游戏」）会因为名字被关键词整串包含而拿到 60 分，
# 再加「集数接近 +20」就变成 80 —— 反倒压过真正该匹配的「命运石之门」（60）。
# 覆盖率一算就露馅：「命运」只解释了关键词 30 个字里的 2 个（6.7%）。
#
# 取 0.5：实测该拦的都在 0.07~0.4（命运 0.07、游戏 0.29、轻音少女 0.4），
# 该留的都在 0.5 以上（「咒术回战」占「咒术回战 死灭回游」正好 0.5）。
CONTAIN_MIN_COVERAGE = 0.5
SCORE_SEASON_MATCH = 40  # 季数一致
SCORE_PART_MATCH = 40    # 篇章一致（「第N部分」，与季数平行，见 extract_part）
SCORE_TYPE_MATCH = 30    # 条目类型一致（TV/剧场版）
SCORE_EP_NEAR = 20       # 集数接近

# 名称相似度判定的阈值与最小长度（见 score_subject 的「相似度兜底」）。
#
# **为什么要单独一档**（实测，青春猪头少年）：Baha 的繁体源码流把作品名写作
# 「青春笨蛋少年不会梦到圣诞服女郎」，而 Bangumi 的正式名是
# 「青春猪头少年不会梦到圣诞服女郎」——
#   * 互相包含不成立（差两个字），
#   * 词级重合也不成立（下面 `[一-鿿]{2,}` 对纯中文标题会**整串**
#     匹配成一个"词"，两串不同名就一个共同词都没有），
#   * 别名里只有「青春笨蛋少年不作圣诞服女郎的梦」（语序不同），也对不上。
# 于是 -1000 被否决，只剩父级「青春猪头少年」这根稻草 —— 而它对同系列
# 每一部都恰好 60 分，前二名差距 0，最终整部作品转人工。
#
# 两个条件**必须同时**满足，且都按"实测里最接近的两对"卡过：
#
#   正确命中：青春笨【蛋】少年… ↔ 青春猪【头】少年…   相似度 0.867，差 2 字 ✓
#   必须拒绝：…献上【祝福】    ↔ …献上【爆焰】        相似度 0.800，差 2 字 ✗
#             （「爆焰」是「为美好的世界献上祝福」的**外传**，不是同一部；
#               只卡差异长度会把它放进来，前二名差距被压到 15 而转人工）
#   必须拒绝：…【兔女郎学姐】   ↔ …【怀梦美少女】      相似度 0.733，差 4 字 ✗
#   必须拒绝：…【兔女郎学姐】   ↔ …【圣诞服女郎】      相似度 0.800，差 3 字 ✗
#
# 阈值取 0.85：与"正确命中"的 0.867 只隔 0.017，看着很窄，但另一条
# 连续差异的条件与之独立（这两组误命中分别是差 2 字和差 5 字，被它拦下），
# 两条一起才是判据。最小长度 6 是防止「X战记」这类短名互相误判。
#
# 得分取 85（高于别名一致 78、低于正式名一致 100）的理由：
# 它比"别名包含"这种弱证据强得多（整串 85% 以上逐字相同），
# 又必须**严格低于**正式名一致，保证官方名精确命中时永远优先。
# 另外它要在数值上压过"父级兜底关键词"给同系列其他作品的
# 60(包含)+30(类型)+20(集数)=110 —— 85+30+20=135，差距 25 才够稳。
NAME_SIMILAR_RATIO = 0.85
NAME_SIMILAR_MIN_LEN = 6
# 允许的**最长连续差异长度**（见 _max_diff_run）——
# 真正的"民间译名差异"是**局部**的（只差一两个字），
# 而"同系列里换了个篇章"是整段不同（兔女郎学姐 / 怀梦美少女 → 差 5 字）。
NAME_SIMILAR_MAX_DIFF_RUN = 2


# 「剧场版/电影」标记。Bangumi 习惯放**前面**（「电影 为美好的世界献上祝福！红传说」
# 「剧场版 咒术回战 0」），本地目录习惯放**后面或干脆不写**
# （「咒术回战 剧场版」「为美好的世界献上祝福！红传说」）。
_MOVIE_MARK_RE = re.compile(r"(?i)(剧场版|劇場版|电影|電影|the\s*movie|movie)")


def _strip_movie_marks(text: str) -> str:
    """去掉「剧场版/电影」字样，用于名称比对的**第二次尝试**。

    **为什么需要**（实测，KONOSUBA 红传说 / 咒术回战 0）：本地目录名
    「为美好的世界献上祝福！红传说」与官方名「电影 为美好的世界献上祝福！
    红传说」**只差一个前置的「电影」**，可就是这个错位让两边互相不包含 ——
    于是正确条目只拿 60 分，反倒输给只解释了关键词前 10 个字的**正传**
    （它拿 60+30=90）。挪开这几个字再比，两边完全一致 → 100 分。

    只做"多一次尝试"（取更高分），不比原样更低 —— 所以不会让任何原本
    能匹配的条目变差。
    """
    return _MOVIE_MARK_RE.sub("", text)


def _name_match(norm_kw: str, nc: str) -> tuple[int, bool]:
    """两串规范化名称的匹配分。返回 `(分数, 是否走了"挪开剧场版"那条)`。

    第二条只在调用方要求时才会为真 —— 它决定"类型一致"那 30 分怎么算
    （见 score_subject：靠挪开剧场版才匹配上的，说明那 30 分正是被挪掉的
    「剧场版/电影」承担的，不该再判成"类型不符"扣掉）。
    """
    if nc == norm_kw:
        return SCORE_EXACT, False
    if norm_kw in nc:
        return SCORE_CONTAIN, False
    if nc in norm_kw and _coverage(nc, norm_kw) >= CONTAIN_MIN_COVERAGE:
        # 候选名是关键词里的一小段 —— 覆盖率太低说明关键词里**多出来的
        # 那些字**（「剧场版」「全集…」）候选根本解释不了，它多半只是
        # 恰好同名的一个短条目。见 CONTAIN_MIN_COVERAGE。
        #
        return SCORE_CONTAIN, False
    return 0, False


def _coverage(short: str, long_: str) -> float:
    """短串占长串的比例（长串为空时返回 0）。见 CONTAIN_MIN_COVERAGE。"""
    if not long_:
        return 0.0
    return len(short) / len(long_)


def _max_diff_run(a: str, b: str) -> int:
    """两串的**最长连续差异长度**（取两侧差距的较大者）。

    例：("青春笨蛋少年","青春猪头少年") → 2（"笨蛋"/"猪头"）
        ("兔女郎学姐","怀梦美少女")     → 4（整段都不一样）
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    i = j = 0
    worst = 0
    for ai, bj, size in sm.get_matching_blocks():
        worst = max(worst, ai - i, bj - j)
        i, j = ai + size, bj + size
    return worst

# 别名命中为什么要**低于**正式名（实测权衡）：
# 别名是更宽的口径 —— 一个条目可能有七八个别名，且包含"ANOHANA"这种
# 会被别的作品撞上的简称。若与正式名同权，会引入新的误匹配：
#   「未闻花名」+别名100+季数40 = 140，而正确条目若只因别名命中就同分，
#   两者的 gap 会被压到阈值以下而转人工（反而更糟）。
# 取值依据：别名一致(78) + 季数一致(40) = 118 > 默认接受线 60 ✓；
# 而仅别名包含(45) + 季数(40) = 85 > 60 ✓，仍能救回"俗称 vs 全名"的情形。
# 同时 78 < 100，保证同一关键词下**正式名命中的候选一定胜出**。
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
#
# （那个常量的定义已删除：它在移除该机制时被漏掉，成了从未被引用的死代码，
#   2026-09 排查 Overlord 季数问题时发现并清理。）

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
    # 「第X部」算一季，但「第X部分」**不算** —— 后者是"同一季的前半/后半"，
    # 不是季数，见 extract_part。
    (r"第\s*([一二三四五六七八九十\d]+)\s*(?:季|部(?!分))", "cn"),
]

# 「第 N 部分」（同一季的前半/后半）
_PART_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*部分")


def extract_part(text: str) -> Optional[int]:
    """从文本提取「第 N 部分」里的 N（没有则 None）。

    **为什么要单列**（实测，无职转生 / 86 的目录结构
    `<作品>/<季>/<第N部分>/`）：`extract_season` 原本把「第X部」当作"第X季"
    的同义词，于是「**第二部分**」被读成"第 2 季"、「第一部分」被读成
    "第 1 季" —— 可 Bangumi 上的「第2部分」根本不是一季，它是**同一季的后半**
    （「无职转生～到了异世界就拿出真本事～ 第2部分」= 第一季的第 12~23 集）。

    两者混为一谈之后，**第一季第2部分和第二季第2部分在打分上完全一样**
    （季数都算 2、集数都是 12、名字都带「第2部分」），前二名差距 0，
    只能反复转人工。拆成两个维度就能区分：
        季数来自目录链（第一季/第二季），篇章来自目录名（第N部分）。
    """
    m = _PART_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw.isdigit():
        return int(raw)
    return None
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
    # 本次匹配是否**因网络/接口失败而根本没搜到东西**（与"搜到了但都不像"
    # 是两回事）。
    #
    # **为什么需要它**（踩坑，实测反馈）：`search_best` 里对搜索异常是
    # `except: continue` —— 超时/断网与"关键词搜不出结果"最终都归结为
    # `"无候选结果"`，调用方无法区分。用户看到的就是"添加动漫后没匹配上"，
    # 完全没有线索指向"其实是网络不通"（日志里那一大段 urllib3 重试
    # 只有开发者会去看）。这里显式把"联网失败"这一事实带出去，让上层能
    # 弹一条说明原因的提示。
    network_failed: bool = False
    # 失败原因摘要（network_failed 为真时用于提示文案）
    network_error: str = ""


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
    """文本是否像剧场版/单独篇章（含「～篇/～章」后缀）。"""
    if is_movie_type(text):
        return True
    return bool(re.search(r"篇$|编$|章$", text.strip()))


def is_movie_type(text: str) -> bool:
    """文本是否**明确**写着剧场版/电影（只看显式字样）。

    **为什么与 `is_movie_like` 分开**（实测踩坑，咒术回战 死灭回游）：
    「～篇」后缀只是"篇章"的写法，不代表条目是剧场版 ——
    Bangumi 的正名是「咒术回战 死灭回游 **前篇**」，
    而本地目录叫「死灭回游」。用 `is_movie_like` 去做"类型是否一致"的比对，
    两边就被判成"一个是剧场版、一个不是"，白白扣掉 30 分（110 → 80），
    于是**输给了同名的第一季**（90）——整个死灭回游被并进第一季。

    `is_movie_like` 仍用于 `scanner.is_season_like`（判断目录名是不是
    "篇章型"的一层），那里的宽口径是对的，不能一起收窄。
    """
    return any(h in text for h in SUBTYPE_MOVIE_HINTS)


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _strip_season(text: str, mode: str) -> str:
    """去掉季数标记，得到作品主名（用于前缀/主名比对）。

    例：「无职转生～到了异世界就拿出真本事～ 第3季」→「无职转生到了异世界就拿出真本事」
        「无职转生 第三季」→「无职转生」
    """
    out = normalize(text)
    # 「第X部分」与季数是**两个维度**（见 extract_part），所以不论有没有季数
    # 都要先去掉它 —— 否则「无职转生 第二部分」会残留一个「分」，
    # 主名比对（前缀/包含）全线失准。
    out = re.sub(r"第[一二三四五六七八九十\d]+部分", "", out)
    season = extract_season(text, mode)
    if season is None:
        return out
    # 去掉中文季数
    out = re.sub(r"第[一二三四五六七八九十\d]+(?:季|部(?!分))", "", out)
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
    # 「候选 ⊂ 关键词」这一向要过覆盖率关，理由见 _contains_score
    if kw_main in cand_main:
        return SCORE_PREFIX, "主名包含"
    if cand_main in kw_main and _coverage(cand_main, kw_main) >= CONTAIN_MIN_COVERAGE:
        return SCORE_PREFIX, "主名包含"
    # 关键词主名是候选主名的前缀（如「无职转生」vs「无职转生到了异世界就拿出真本事」）
    common = 0
    for a, b in zip(kw_main, cand_main):
        if a != b:
            break
        common += 1
    # 至少 4 个字符的前缀重合才认（避免「魔法」这类短前缀误命中）
    if common >= 4 and common >= len(kw_main) * 0.6:
        # **前缀之外的那一截必须也能在候选里找到**（实测踩坑，地错 第四季）：
        # 关键词「在地下城寻求邂逅是否搞错了什么 **灾厄篇**」与
        # 「…第四季 深章 **灾厄篇**」「…第四季 新章 **迷宫篇**」都有 19 字公共
        # 前缀 —— 官方名中间夹着「第四季 深章」，所以两边既非包含也非被包含，
        # 只能落到这条前缀规则上。可两队的前缀一样长，**光看前缀分不出
        # 灾厄篇和迷宫篇**（实测两者都是 55 分，谁也赢不了谁）。
        # 把"灾厄篇"这截也要求出现在候选名里，才真正区分得开。
        tail = kw_main[common:]
        if tail and tail not in cand_main:
            return 0, ""
        return SCORE_CONTAIN if tail else SCORE_PREFIX, (
            f"主名前缀{common}字+尾段「{tail}」命中" if tail
            else f"主名前缀{common}字")
    return 0, ""


def score_subject(
    subject: dict,
    keyword: str,
    local_ep_count: int = 0,
    season_mode: str = "cn",
    keyword_season: Optional[int] = None,
    keyword_part: Optional[int] = None,
) -> tuple[int, str]:
    """给单个 Bangumi 候选打分，返回 (分数, 理由)。

    **别名的参与方式（实测踩坑）**：别名与正式名**分开打分、按各自权重取最大**，
    而不是混在一个池子里。原因：

    1. 权重不同 —— 别名是更宽的口径（一个条目常有七八个），命中应略低于
       正式名（见 SCORE_ALIAS_* 的说明），否则会引入新的误匹配。
    2. "名称比对过滤"（下面那段）要求候选**去季数后仍匹配**，别名同样要过
       这一关，否则「ANOHANA」会把它季数不同的条目也放进来。
    3. 别名一致(78) 高于 `SCORE_CONTAIN`(60)：这是因为**正式名包含**这条
       本身已能救回不少场景，而"俗称 vs 全名"（未闻花名 ↔ 我们仍未知道…）
       只能靠别名 —— 所以别名的"完全一致"必须给到够高的档位。
    """
    name = subject.get("name", "") or ""
    name_cn = subject.get("name_cn", "") or ""
    candidates = [n for n in (name_cn, name) if n]
    # 别名单独一份：打分权重与"名称比对过滤"都要区别对待
    aliases = extract_aliases(subject)

    score = 0
    reasons: list[str] = []

    norm_kw = normalize(keyword)
    # 去掉「剧场版/电影」标记后再比一次的版本（见 _strip_movie_marks）
    kw_strip = normalize(_strip_movie_marks(keyword))
    best_name_score = 0
    best_via_strip = False     # 当前的最高分是否来自"挪开剧场版/电影"那次比对
    for cand in candidates:
        nc = normalize(cand)
        if not nc or not norm_kw:
            continue
        score_c, via_strip = _name_match(norm_kw, nc)
        if score_c < SCORE_EXACT and kw_strip:
            nc_strip = normalize(_strip_movie_marks(cand))
            if nc_strip:
                score_s, _ = _name_match(kw_strip, nc_strip)
                # **「剧场版」是从关键词里删掉的时候要更严**：只认"候选把剩下
                # 的部分完整覆盖了"这一种结论，不接受"完全相等 / 候选更短"。
                #
                # 否则会闹出这种笑话（实测，命运石之门\剧场版）：关键词
                # 「命运石之门 剧场版」删掉「剧场版」后正好等于 **TV 正传**的名字，
                # 于是正传白拿 100 分（130），把真正的剧场版
                # 「命运石之门 负荷领域的既视感」(90) 挤掉。
                # 而候选若真覆盖了剩下的「命运石之门 + 别的字」，那才说明它是
                # 带副标题的剧场版。
                if kw_strip != norm_kw and (score_s >= SCORE_EXACT
                                            or kw_strip not in nc_strip):
                    score_s = 0
                if score_s > score_c:
                    score_c, via_strip = score_s, True
        if score_c > best_name_score:
            best_name_score, best_via_strip = score_c, via_strip
        if score_c == 0:
            # 词级重合：按 2-gram 粗算，避免完全不相关的条目得分
            kw_words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", norm_kw))
            cand_words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", nc))
            if kw_words and cand_words and (kw_words & cand_words):
                best_name_score = max(best_name_score, SCORE_WORD_OVERLAP)

    # ---- 相似度兜底：民间译名只差一两个字 ----
    #
    # 上面三档（完全一致 / 互相包含 / 词级重合）对**纯中文标题**是有盲区的：
    #   * 「青春笨蛋少年不会梦到圣诞服女郎」与正式名
    #     「青春猪头少年不会梦到圣诞服女郎」互相不包含；
    #   * 词级重合那一档按 `[一-鿿]{2,}` 切词，对**没有空格的中文标题**
    #     会整串匹配成一个"词"，两串一旦不同名就一个共同词都没有 ——
    #     这一档实际上只对中英混排的标题有效。
    # 结果就是"只差两个字"的正确条目拿 -1000 被否决（实测踩坑）。
    #
    # 这里用 difflib 的序列相似度补一档，只在**两边长度都不短**时启用，
    # 避免短名（「X战记」/「X战记 2」）互相误判。
    #
    # 只比对**正式名**（name_cn / name），不比别名：别名一个条目动辄七八个，
    # 放进来等于把误命中概率乘上别名数量，而"近义异译"这档本就是为了
    # 救正式名对不上的情况 —— 别名能用早就用上面那两档命中了。
    #
    # 还有一条硬条件：**季数必须一致**（同为"无季数标记"也算一致）。
    # 否则「进击的巨人 最终季」与「进击的巨人 第三季」只差两个字，
    # 会被当成"民间译名"而错误自动匹配 —— 它们是**不同的季**。
    best_reason_early = ""
    if (not best_name_score and len(norm_kw) >= NAME_SIMILAR_MIN_LEN
            and keyword_season == extract_season(name_cn or name, season_mode)):
        for cand in candidates:
            nc = normalize(cand)
            if len(nc) < NAME_SIMILAR_MIN_LEN:
                continue
            sm = difflib.SequenceMatcher(None, norm_kw, nc, autojunk=False)
            ratio = sm.ratio()
            if ratio < NAME_SIMILAR_RATIO:
                continue
            run = _max_diff_run(norm_kw, nc)
            if run > NAME_SIMILAR_MAX_DIFF_RUN:
                continue
            best_name_score = SCORE_NAME_SIMILAR
            best_reason_early = f"名称高度相似({ratio:.2f} 差{run}字)：{cand}"
            break

    # 别名打分：只给"完全一致 / 互相包含"两档，**不给词级重合**
    # （别名数量多，词级重合会大幅抬高低质量候选的分数）
    best_alias_score = 0
    matched_alias = ""
    for alias in aliases:
        na = normalize(alias)
        if not na or not norm_kw:
            continue
        if na == norm_kw:
            if SCORE_ALIAS_EXACT > best_alias_score:
                best_alias_score, matched_alias = SCORE_ALIAS_EXACT, alias
        elif norm_kw in na or na in norm_kw:
            if SCORE_ALIAS_CONTAIN > best_alias_score:
                best_alias_score, matched_alias = SCORE_ALIAS_CONTAIN, alias

    # ---- 名称比对过滤：必须"作品名"匹配，不能只靠季数雷同 ----
    # 例：关键词「第一季」与「我叫MT 第一季」都含「第一季」，但作品无关，
    # 若仅凭此给 60 分，会让大量无关作品挤进候选并干扰正确条目。
    if keyword_season is not None:
        kw_main = _strip_season(keyword, season_mode)
        if not kw_main:
            # 关键词本身就是纯季数（无作品名）→ 无法据此判断作品，直接否决
            return SCORE_IRRELEVANT, f"关键词仅含季数，无法确定作品（{keyword}）"
        if best_name_score > 0 or best_alias_score > 0:
            # 关键词含作品名 → 要求候选去季数后也与之匹配。
            # **别名同样要过这一关**：否则「ANOHANA」会把它季数不同的
            # 同名衍生条目一并放行，与"季数是硬条件"的既有约定冲突。
            matched_main = False
            for cand in candidates + aliases:
                cand_main = _strip_season(cand, season_mode)
                if not cand_main:
                    continue
                if cand_main == kw_main or kw_main in cand_main or cand_main in kw_main:
                    matched_main = True
                    break
            if not matched_main:
                best_name_score = 0
                best_alias_score = 0

    # 别名命中并入主分（取两者较大者，因为它们是**同一个维度**的两种口径，
    # 不该叠加 —— 叠加会让"名称+别名都命中"的条目被虚高抬分）
    if best_alias_score > best_name_score:
        best_name_score = best_alias_score
        best_alias_hit = matched_alias
    else:
        best_alias_hit = ""
    # 主名比对兜底：名称未能直接命中时，去掉季数后再比（应对长副标题）
    #
    # **别名命中时不要走这里**：兜底会把 best_name_score 覆盖成正式名的
    # `_main_name_score` 结果（通常更低或为 0），等于把别名的贡献抹掉。
    # 别名已是可用结论，无需再用正式名兜底。
    best_reason = best_reason_early
    if best_alias_hit:
        best_reason = f"别名命中：{best_alias_hit}"
    elif best_name_score < SCORE_CONTAIN:
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

    # ---- 季数一致性（明确不符 → 一票否决；无标记 → 降级）----
    #
    # 这一段**必须在 `score += best_name_score` 之前**：它会按季数情况
    # 调整 best_name_score（降级）或直接否决，先加分就改不动了。
    if keyword_season is not None:
        subject_season = extract_season(name_cn or name, season_mode)
        if subject_season is not None and subject_season != keyword_season:
            # 季数明确不同：直接否决。季数是硬条件，
            # 「第一季」匹配到「第二季」属于错误结果，不应靠其他项补救。
            return (
                SCORE_IRRELEVANT,
                f"季数不符（条目={subject_season}，期望={keyword_season}）",
            )
        if subject_season is None and best_name_score > SCORE_CONTAIN:
            # **关键词带季数、候选却不带任何季数标记 → 名称分降级**
            # （实测踩坑：OVERLORD 四季被合并成一个条目）。
            #
            # 场景：目录 S1~S4 的关键词分别是「Overlord S1」…「Overlord S4」，
            # 而 Bangumi 上第一季的标题就叫「OVERLORD」（**不含任何季数
            # 标记**）且集数同为 13 —— 于是它对**每一个**关键词都拿到
            # 「名称完全一致 100 + 类型 30 + 集数接近 20 = 150」的最高分，
            # 四季全部匹配到它（`search_best` 是跨所有关键词取最高分），
            # `upsert_subject` 再按 bangumi_id 合并，最终四季文件堆在同一
            # subject 下（详情页集数重复 1,1,1,1,2,2…）。
            #
            # 判据：关键词明确说了"要第 N 季"，一个没有季数标记的条目
            # **无法证明自己是第 N 季** —— 它可能是第一季，也可能是总集篇。
            # 因此不能给"名称完全一致"的满分：降一档到 `SCORE_CONTAIN`，
            # 让真正标了季数的条目（「OVERLORD 第二季」145 分）胜出。
            #
            # 保守之处：**只降级、不否决**。无季数标记的条目仍可能以较低分
            # 胜出 —— 当官方库里确实没有带季数的对应条目时这是对的
            # （例如只有一季的番，目录名却写了「S1」）。
            reasons.append(
                f"无季数标记，名称分 {best_name_score}→{SCORE_CONTAIN}"
                f"(关键词要求第{keyword_season}季)")
            best_name_score = SCORE_CONTAIN

    # ---- 篇章一致性（「第N部分」，与季数平行的第二个硬条件）----
    #
    # 目录结构 `<作品>/<季>/<第N部分>/` 里的「第N部分」说的是**同一季的前半/后半**
    # （Bangumi 的「…～ 第2部分」= 第一季第 12~23 集），跟"第几季"是两回事，
    # 见 extract_part。判据与季数完全对称：
    #   * 条目标了别的篇章（条目=2、期望=1）→ 一票否决；
    #   * 条目没标篇章 → **只在 N ≥ 2 时降级**：Bangumi 上"第 1 部分"通常就是
    #     **不带任何篇章标记的那个条目**（「无职转生～到了异世界就拿出真本事～」
    #     正是第一季前半），所以 N=1 时"没标记"是正常现象，不能扣。
    if keyword_part is not None:
        subject_part = extract_part(name_cn or name)
        if subject_part is not None and subject_part != keyword_part:
            return (
                SCORE_IRRELEVANT,
                f"篇章不符（条目=第{subject_part}部分，期望=第{keyword_part}部分）",
            )
        if subject_part is None and keyword_part > 1 and best_name_score > SCORE_CONTAIN:
            reasons.append(
                f"无篇章标记，名称分 {best_name_score}→{SCORE_CONTAIN}"
                f"(关键词要求第{keyword_part}部分)")
            best_name_score = SCORE_CONTAIN

    if best_name_score:
        suffix = f"（{best_reason}）" if best_reason else ""
        reasons.append(f"名称+{best_name_score}{suffix}")

    score += best_name_score

    # 季数一致的加分（上一段已处理"不符"与"无标记"两种情况）
    if keyword_season is not None:
        if extract_season(name_cn or name, season_mode) == keyword_season:
            score += SCORE_SEASON_MATCH
            reasons.append(f"季数一致({keyword_season})+{SCORE_SEASON_MATCH}")

    # 篇章一致的加分（同上，与季数那段平行）
    if keyword_part is not None:
        if extract_part(name_cn or name) == keyword_part:
            score += SCORE_PART_MATCH
            reasons.append(f"篇章一致(第{keyword_part}部分)+{SCORE_PART_MATCH}")

    # 类型一致性：关键词像剧场版，条目也该像
    # 靠"挪开剧场版/电影"才匹配上的（best_via_strip），说明那层差异正是被
    # 挪掉的那几个字承担的，不该再当成"类型不符"倒扣 30 分 —— 否则
    # 「电影 X 红传说」这类条目永远比不过「X」。
    if best_via_strip:
        kw_movie = is_movie_type(_strip_movie_marks(keyword))
        subj_movie = is_movie_type(_strip_movie_marks(name_cn or name))
    else:
        kw_movie = is_movie_type(keyword)
        subj_movie = is_movie_type(name_cn or name)
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
        part = extract_part(keyword)
        return score_subject(
            subject, keyword, local_ep_count, self.season_mode, season, part
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
        # 记录"搜索调用失败"（超时/断网/被阻断…）。见 MatchResult 的说明：
        # 不记的话，调用方无法把"网络不通"和"确实没有匹配项"区分开，
        # 用户只能看到一句"没匹配上"，毫无头绪。
        net_failed = False
        net_error = ""

        for kw in keywords:
            if not kw:
                continue
            season = extract_season(kw, self.season_mode)
            part = extract_part(kw)
            try:
                results = self.api.search_subjects(kw, limit=10)
            except Exception as e:
                log.warning("搜索失败 keyword=%s err=%s", kw, e)
                net_failed = True
                # 优先保留第一条错误摘要（通常就是最根本的那个原因）
                if not net_error:
                    net_error = describe_connection_error(e) or str(e)
                continue

            for subj in results:
                s, reason = score_subject(
                    subj, kw, local_ep_count, self.season_mode, season, part
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
            # 一个候选都没有：区分"搜过但没结果"与"压根没搜成功"
            reason = "无候选结果"
            if net_failed:
                reason = f"搜索请求失败：{net_error}" if net_error else "搜索请求失败"
            return MatchResult(None, 0, reason,
                               network_failed=net_failed,
                               network_error=net_error)

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
