"""媒体库扫描 + Bangumi 匹配。

核心改进（相对早期版本）：
1. **递归下探到叶子目录**：支持「系列名/第X季/视频」与「系列名/剧场版/视频」结构。
   纯容器目录（无直接视频、只有子目录）不产出条目。
2. **多候选关键词 + 打分择优**：不再盲信搜索结果第一条。
   打分逻辑见 app/core/matcher.py。
3. **SP/OVA 归入所属季**：附属内容目录不下探，视频并入父条目，集数排在正片之后。
4. **manual 记录保护**：用户手动指定的条目，重扫时不覆盖其 bangumi_id。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

from app.core.bangumi_api import BangumiClient, BangumiError
from app.core.database import Database
from app.core.matcher import (
    SubjectMatcher, extract_season, is_extra_dir, is_movie_like, is_noise_dir,
)
from app.utils import bgm_log
from app.utils.cover_cache import download as download_cover

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".rmvb", ".ts", ".flv", ".mov", ".wmv"}

NOISE_PATTERNS = [
    r"(?i)\b(BDRip|WEB[- ]?DL|WEBRip|BluRay|HDTV|x264|x265|HEVC|AVC|10bit|8bit|1080p|720p|2160p|4K|60FPS|120FPS)\b",
    r"(?i)\b(简体|繁体|简繁|内嵌|外挂|中字|日配|国配|合集|完全版|无修|招募|发布组)\b",
    r"(?i)\b(TVRip|DVDRip|CR-WebRip|Baha|LoliHouse|Sakurato|CheeseAni|SubsPlease|Nekomoe|KTXP|KissSub)\b",
    r"(?i)\b(Fin|Complete|Batch|v\d)\b",
    r"_+",
]

# 括号块（[] 【】 () （）），由 _strip_bracket_blocks 判断内部是否为噪声
_BRACKET_BLOCK_RE = re.compile(r"[\[【(（]([^\[\]【】()（）]*)[\]】)）]")

# 常见发布组/字幕组/画质用词（用于判断括号内容是否为噪声）
_NOISE_OUTER_RE = re.compile(
    r"(?i)(字幕组|发布组|压制|招募|rip|raw|fps|bit|hevc|avc|aac|flac|mkv|mp4|"
    r"1080|720|2160|4k|web|bd|tv|chs|cht|gb|big5|jp|sc|tc)",
)

# 符号分隔符：统一替换为空格，提高搜索命中率
# 覆盖：「86 -不存在的战区-」「Fate/strange Fake」「Re：从零开始」「A·B」「A~B」
# 注意不含「-」单独出现的情况（由 _EP_RANGE_RE / 收尾步骤处理）
_SYMBOL_SEP_RE = re.compile(r"[/／\\：:；;、，,．・·〜～~｜|—–=＋+＆&！!？?。\u3000\s]+")

# 纯季数标记（用于判断关键词是否"只有季数、没有作品名"）
_SEASON_ONLY_RE = re.compile(
    r"第[一二三四五六七八九十\d]+[季部]"
    r"|Season\s*\d+"
    r"|(?<![A-Za-z])S\d{1,2}(?![A-Za-z])"
    r"|\d+(?:st|nd|rd|th)\s*Season"
    r"|Part\s*\d+"
    r"|(?<![A-Za-z])(?:III|IV|IX|VIII|VII|VI|II)(?![A-Za-z])",
    re.IGNORECASE,
)

# 集数区间标记（如 01-23、48.5-72），出现在目录名里属于噪声
# 不用 \b（点号会让边界失效），改用非数字断言
_EP_RANGE_RE = re.compile(r"(?<!\d)\d{1,3}(?:\.\d+)?\s*[-~～]\s*\d{1,3}(?:\.\d+)?(?!\d)")

# 单独的集数数字（如目录名末尾的 01-23 被去掉后残留的孤立数字）
_STANDALONE_NUM_RE = re.compile(r"(?<![\w.])\d{1,4}(?:\.\d{1,2})?(?![\w.])")

# 拉丁文长片段阈值：超过则截断（目录名常带罗马字副标题/制作组信息）
_LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\.\-]*(?:\s+[A-Za-z][A-Za-z0-9'\.\-]*)*")
MAX_LATIN_RUN = 24
# 单个拉丁词（用于「中文优先」时判断保留还是丢弃）
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")

# 递归下探的最大深度（防止异常目录结构导致无限递归）
MAX_DEPTH = 4
# 视频数量与 Bangumi 总集数的容忍差
EP_TOLERANCE = 3


def _trim_latin_runs(text: str) -> str:
    """处理中英混排的目录名。

    目录名常见两种形态：
    ① 「无职转生 Mushoku Tensei Isekai Ittara Honki Dasu」
       → 罗马音是副标题，**中文名已足够独特**，罗马音反而降低搜索命中率
    ② 「86 -不存在的战区- —Eitishikkusu—」「白圣女与黑牧师 Shiro Seijo to Kuro」
       → 同理，只保留中文/数字部分

    策略：若文本含 ≥2 个连续中文字符，则**只保留中日文字符与数字**，
    丢弃拉丁片段（对 Bangumi 中文搜索更友好）；
    否则（纯拉丁名，如「Fate strange Fake」）保留原文并做长度截断。
    """
    has_cjk = len(re.findall(r"[\u4e00-\u9fff]", text)) >= 2
    if has_cjk:
        # 中文优先：丢弃罗马音副标题（如 Mushoku Tensei Isekai），
        # 但保留**开头的短拉丁词**——它们常是作品名的正式组成部分：
        #   「Re：从零开始的异世界生活」的 Re、「86 -不存在的战区-」的 86
        # 中间/结尾的短词（to、Kuro 等）多为罗马音碎片，一并丢弃。
        head_match = re.match(r"^([A-Za-z]{1,4})\b", text.strip())
        head_word = head_match.group(1) if head_match else ""

        # **紧贴数字的短拉丁词也要保留**（实测踩坑）：
        # 「从Lv2开始无敌的原勇者候补」的开头是汉字「从」，于是 `Lv`
        # 既不是 head_word、又被当成罗马音碎片删掉 → 关键词变成
        # 「从 2开始无敌的原勇者候补」。而 Bangumi 的正式名是
        # 「从**Lv2**开始…」，少这两个字母就**完全对不上**，只能靠词级
        # 重合拿 20 分，总分 50 < 60 阈值 → 白白转人工。
        # 这类「字母+数字」是作品名的正式组成部分（Lv2 / S2 / No.1 /
        # 第2章 等），与 to / no / datta 这类纯字母碎片有本质区别，
        # 判据就是**后面是否紧跟数字**。
        glued_words = {
            m.group(1)
            for m in re.finditer(r"(?<![A-Za-z])([A-Za-z]{1,4})(?=\d)", text)
        }

        def _keep_short(m: re.Match) -> str:
            word = m.group(0)
            if (head_word and word == head_word) or word in glued_words:
                return word
            return " "

        kept = _LATIN_WORD_RE.sub(_keep_short, text)
        # 保留必要的英文季数标记，避免「第3季」信息丢失。
        # 注意：必须带上数字才保留（如「Season 3」「Part 2」「S4」），
        # 否则会残留孤立的 "Season" 反而干扰搜索。
        extras: list[str] = []
        for m in re.finditer(r"(?i)\b(Season|Part)\s*(\d+)", text):
            extras.append(f"{m.group(1).capitalize()} {m.group(2)}")
        for m in re.finditer(r"(?i)\b(\d+)(?:st|nd|rd|th)\s+Season\b", text):
            extras.append(f"Season {m.group(1)}")
        # 「S1」「S02」这类短季数标记。
        #
        # **注意不要把已保留的词再加一遍**：`S3` 的 `S` 紧贴数字，会被
        # 上面的 `glued_words` 保留在主串里；这里若再无脑 append，
        # 结果就是「鬼灭之刃 S3 S3」（实测踩到）。故先检查主串是否已含它。
        for m in re.finditer(r"(?i)(?<![A-Za-z])S(\d{1,2})(?![A-Za-z])", text):
            if m.group(0) not in kept:
                extras.append(m.group(0))
        # 去重保序
        seen_x: set[str] = set()
        extras = [x for x in extras if not (x.lower() in seen_x or seen_x.add(x.lower()))]
        kept = re.sub(r"\s+", " ", kept).strip()
        # 收尾：清掉因删词产生的孤立标点、连接符与孤立数字
        kept = re.sub(r"[\-~～—–!！?？.。]{1,3}", " ", kept)
        kept = re.sub(r"(?<![\w.])\d{1,4}(?![\w.])", " ", kept)
        kept = re.sub(r"\s+", " ", kept).strip(" -_·・,，、:：")
        if extras:
            kept = f"{kept} {' '.join(extras)}".strip()
        return kept or text

    def _cut(m: re.Match) -> str:
        run = m.group(0).strip()
        if len(run) <= MAX_LATIN_RUN:
            return run
        head = run[:MAX_LATIN_RUN]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        return head

    return _LATIN_RUN_RE.sub(_cut, text)


def _stem_of(name: str) -> str:
    """取文件名主干，但**不把作品名里的斜杠当路径分隔符**。

    Path('Fate/strange Fake').stem 会返回 'strange Fake'（丢掉 Fate），
    因此仅在字符串确实是文件路径（含盘符或反斜杠、且不是作品名）时才用 Path。
    """
    if re.search(r"[\\/]", name) and not re.search(r"[\\]", name):
        # 只含正斜杠：可能是作品名（Fate/strange Fake），也可能是相对路径
        # 判断依据：若首段是盘符或常见目录词，才当路径
        first = name.split("/", 1)[0]
        if re.fullmatch(r"[A-Za-z]:", first) or first in ("", "."):
            return Path(name).stem
        # 否则视为作品名：仅去掉结尾的扩展名
        return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    return Path(name).stem


def clean_title(name: str) -> str:
    """清洗文件夹/文件名，提取搜索关键词。

    顺序很关键：先去完整括号块，再处理区间数字。
    若先删区间数字，会破坏 `[...]` 的配对，导致括号残留。
    """
    s = _stem_of(name)

    # ① 去掉「纯噪声」的括号块（[Sakurato]、[01-23 Fin]、【喵萌】等），
    #    但若括号内是作品名本身（如「【我推的孩子】」）则只去括号、保留内容。
    s = _strip_bracket_blocks(s)

    # ② 符号归一化：把各类分隔符统一成空格，提高搜索命中率
    #    「86 -不存在的战区-」「Fate/strange Fake」「Re：从零开始」等
    s = _SYMBOL_SEP_RE.sub(" ", s)

    # ③ 去掉裸露的集数区间（如「01-23」「48.5-72」，不带括号的情况）
    s = _EP_RANGE_RE.sub(" ", s)
    # ④ 去掉孤立数字（年份、集数残留）
    s = _STANDALONE_NUM_RE.sub(" ", s)
    # ⑤ 处理中英混排（中文优先，丢弃罗马音噪声）
    s = _trim_latin_runs(s)
    # ⑥ 收尾：清理残留分隔符与空白
    s = re.sub(r"\s*[-~～]\s*$", "", s)
    s = re.sub(r"^\s*[-~～]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.·・,，")
    return s


def _strip_bracket_blocks(text: str) -> str:
    """去掉噪声括号块，但保留「括号本身就是作品名」的情况。

    【我推的孩子】 → 我推的孩子   （保留内容，仅去括号）
    【喵萌奶茶屋】葬送的芙莉莲 → 葬送的芙莉莲  （整块删除）
    [LoliHouse] 无职转生 → 无职转生
    """
    def _repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not inner:
            return " "
        # 括号内像"作品名"（含中文且长度≥2，且不含常见噪声词）→ 保留内容
        if re.search(r"[\u4e00-\u9fff]{2,}", inner) and not _is_noise_word(inner):
            return f" {inner} "
        return " "

    return _BRACKET_BLOCK_RE.sub(_repl, text)


def _is_noise_word(text: str) -> bool:
    """判断括号内容是否为发布组/画质等噪声。"""
    if _NOISE_OUTER_RE.search(text):
        return True
    # 纯拉丁/数字且较短 → 多为发布组名（如 Sakurato、LoliHouse、KTXP）
    stripped = re.sub(r"[\s\-_&+0-9]", "", text)
    if stripped and re.fullmatch(r"[A-Za-z]+", stripped) and len(stripped) <= 14:
        return True
    # 字幕组/发布组常见后缀（如「喵萌奶茶屋」「桜都字幕组」）
    if re.search(r"(字幕组|奶茶屋|压制组|汉化组|发布组|搬运|字幕社|字幕組|動畫|动漫之家)$", text):
        return True
    return False


# ---- 集数序号提取 ----
# 「第01話」「第 1 集」「第3回」
_EP_CN_RE = re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话集回話]")
# 「EP01」「E01」（要求前面不是字母，避免命中 MAD 里的随机串）
_EP_EN_RE = re.compile(r"(?i)\bE[P]?\s*0*(\d+(?:\.\d+)?)\b")

# 完结标记（可拼进各集数正则）：
# 「[66END]」「66 END」「(12完)」「[25 Fin]」—— 完结集常在集数后跟标记。
# **必须允许它**（实测踩坑）：数字正则原本要求"数字后面是边界/结尾"，
# 而 [66END] 里 66 后面紧贴字母 END，三层正则全部失配 → 这一集解析不出
# 集号，落进兜底桶被顺延成 max+0.5（Re:零 S3 的 66 集显示成 65.5）。
# 注意标记后的边界检查（尾部 $ / 前瞻 (?![\w.])）仍然生效，所以
# 「2 endings」这类普通英文单词不会误命中。
_EP_END_MARK = r"(?:\s*(?:END|FIN|完|終|终))?"

# 以数字结尾：整名就是数字（01.mkv）或「... - 01」「[01]」「 01」等。
# 用 (?<!\d) 保证「12」整体被捕获（写成 [\s\-_\[\]]0*(\d+)$ 会切掉首位数字，
# 把 12.mkv 解析成 2）。
_EP_TAIL_RE = re.compile(
    r"(?<!\d)0*(\d+(?:\.\d+)?)(?:v\d+)?" + _EP_END_MARK + r"\s*$",
    re.IGNORECASE)
# 纯数字括号块：[01]、[01v2]、[66END]、（05）—— 发布组对集数的标准写法。
# **括号内必须只有集数（可带 v2 版本号 / 完结标记）**，不能放宽成
# "括号内含数字"：[AVC-8bit 1080p AAC] 会被抠出 8 / 1080 这类画质参数。
# 注意这不能替代 _EP_MID_RE（它要求括号紧贴集数），两者取值范围不同。
_EP_BRACKET_RE = re.compile(
    r"[\[【(（]\s*0*(\d+(?:\.\d+)?)(?:v\d+)?\s*" + _EP_END_MARK + r"\s*[\]】)）]",
    re.IGNORECASE)
# 独立数字（前后都不是字母数字）：如「[01]」夹在括号中、「66END 1080p」
_EP_MID_RE = re.compile(
    r"(?<![\w.])0*(\d+(?:\.\d+)?)(?:v\d+)?" + _EP_END_MARK + r"(?![\w.])",
    re.IGNORECASE)

# 括号块集数的可信度上限（实测踩坑）：
# 「(2021) 01」里的年份、「[1080]」里的画质参数也会命中 _EP_BRACKET_RE。
# 正片集数几乎不可能 ≥200（超长篇也多为重播编号），超过上限的一律不进
# 括号层级 —— 它们仍会被 _EP_MID_RE 当低可信度候选捡回来，只是不再
# 抢占高可信度位置。
_EP_BRACKET_MAX = 199.5

# 附属内容关键词：命中即视为「非正片」，不参与集数编号（预告/菜单/OP/ED 等）
# 说明：仅按文件名匹配，不影响目录级归并（目录归并见 matcher.is_extra_dir）
EXTRA_FILE_PATTERNS = [
    # NCOP / NCED / OP / ED（无字幕版）
    r"(?i)\bNC(OP|ED)\d*\b",
    r"(?i)\bOP\s*\d*\b(?![a-z])",
    r"(?i)\bED\s*\d*\b(?![a-z])",
    # 预告 / 宣传
    r"预告", r"(?i)\bWEB予告\b", r"予告", r"(?i)\bTrailers?\b",
    r"(?i)\bPreviews?\b", r"(?i)\bTeasers?\b", r"宣传影像", r"(?i)\bPV\s*\d*\b",
    r"(?i)\bCM\s*\d*\b", r"劇中ゲーム", r"剧中游戏",
    # 菜单 / 特典 / 花絮 / 舞台挨拶
    r"(?i)\bMenu\b", r"特典", r"花絮", r"访谈", r"舞台挨拶", r"舞台问候",
    r"(?i)\bMaking\b", r"(?i)\bInterview\b",
    # 音声特典 / 评论音轨
    r"音声特典", r"(?i)\bAudio\s*Comment",
]


# ---- 附加内容（SP / OVA / NCOP / 特典…）识别 ----
#
# 这些都不是"正片集数"，而是**附加内容**，各自有独立的编号语义。
# 常见写法（实测覆盖）：
#    [DMG][OVERLORD_II][SP05][1080P]...       → 标签 SP05
#    [DMG][OVERLORD][SP08 END][720P]...       → 标签 SP08
#    [Group][Show][OVA01][1080p]...           → 标签 OVA01
#    [Group][Show][OAD2][1080p]...            → 标签 OAD2
#    [DMG]... NCOP3 [BDRip]...                → 标签 NCOP3
#    [DMG]... NCED2 [BDRip]...                → 标签 NCED2
#    [DMG]... WEB予告 #02 [BDRip]...           → 标签 WEB予告 #02
#    [Group][Show][特典1]...                   → 标签 特典1
#    [Group][Show][SP][1080p]...              → 无序号，按出现顺序补号
#
# **必须在正片解析之前判定**：`SP05` 里的 `05` 会被 `_EP_MID_RE` 命中，
# 若先走正片分支，SP 会被当成第 5 集与正片撞号。
_EXTRA_RE = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"(?P<kind>"
    r"SP|OVA|OAD|EX|"                       # 英文缩写
    r"NC(?:OP|ED)|"                         # NCOP / NCED（无字幕 OP/ED）
    r"WEB予告|予告|预告|PV|CM|Trailer|Preview|Teaser|"
    r"特典|映像特典|番外篇|番外|花絮|访谈|菜单|Making|Interview"
    r")"
    # 序号与关键词之间可能有分隔符：`SP01`、`SP 01`、`WEB予告 #02`、`特典-1`
    r"[\s#\-_]*"
    r"(?P<num>\d{1,3})?"                    # 可选序号（**保留前导零**）
)


def extract_extra_index(name: str) -> Optional[str]:
    """从文件名提取「附加内容」的显示标签（如 `SP01` / `OVA2` / `NCOP3`）。

    不是附加内容返回 None，交由正片逻辑处理。

    **返回标签而不是数值**（实测需求）：用户要求"文件名写啥就是啥" ——
    `SP01` 显示成 `SP01`（不抹掉前导零）、`OVA01` 显示成 `OVA01`、
    `NCOP3` 显示成 `NCOP3`。数值装不下这些前缀，所以这里直接给字符串，
    由 `episodes.ep_label` 列存储（见 database 的 v8 迁移）。

    无序号时返回**光秃的关键词**（如 `SP`、`特典`），由调用方按出现顺序
    补号（`SP` → `SP1`、下一个 → `SP2`），避免多个无序号项互相覆盖。

    排序用的数值另由 `extra_sort_index()` 给出（排在正片之后）。
    """
    stem = Path(name).stem
    m = _EXTRA_RE.search(stem)
    if not m:
        return None
    kind = m.group("kind")
    num = m.group("num")
    # 关键词统一大写（NCOP/SP/OVA 这类），中文/原文保持原样
    kind = kind.upper() if kind.isascii() else kind
    # 标签 = 原关键词 + 原序号（前导零保留）：`SP01`、`WEB予告02`、`特典1`
    return f"{kind}{num}" if num else kind


def extra_sort_index(label: str, pos: int, main_max: float) -> float:
    """附加内容的**排序用数值**：排在正片之后，且彼此有序。

    参数：
        label    —— `extract_extra_index` 给出的标签（仅用于日志/调试）
        pos      —— 该文件在**已排序的附加内容列表**里的位置（1 起）
        main_max —— 正片的最大 ep_index（附加内容一律排在它之后）

    为什么用 `pos` 而不是标签里的序号：序号在不同类型间会重复
    （`SP1` 与 `OVA1` 都是 1），用它排序会让两个不同类型的内容撞到
    同一个槽位、顺序不稳定；`pos` 是列表里的唯一位置，天然有序。

    为什么不用负数（早期实现）：负数会把附加内容排到**正片之前**，
    而用户的预期是"附加内容在正片下面"（实测反馈）。

    为什么加 1000 的偏置：正片将来可能变多（长篇番 100+ 集），
    直接把附加内容排在 `main_max + 1` 会与正片撞号；
    偏置 1000 留足余量，且 1000 以内的附加内容数量足够。
    """
    return main_max + 1000 + pos


def is_extra_file(name: str) -> bool:
    """文件名是否为附属内容（预告/菜单/NCOP/PV 等），不应参与正片集数编号。

    例：「… Menu Vol.01 …」「… WEB予告 #01 …」「… NCOP …」「… 初日舞台挨拶 …」
    """
    stem = Path(name).stem if ("." in name and "\\" not in name) else name
    return any(re.search(p, stem) for p in EXTRA_FILE_PATTERNS)


def extract_ep_candidates(name: str) -> list[float]:
    """从文件名提取**全部**集数候选，按可信度从高到低排列。

    为什么返回候选列表而不是单一值（实测踩坑）：
    「[Sakurato] 86—Eitisnikkusu— [01v2][AVC-8bit 1080p AAC][CHS]」这类
    **标题本身就是数字**的文件名，标题里的 86 与括号里的 01 都是
    "看起来合法"的集数 —— 只取第一个命中的话，86 排在前面、整部作品
    每一集都会被解析成 86（详情页左列全显示"86"就是这一原因）。

    调用方应拿候选列表去 Bangumi 官方集数表里对号（`_pick_ep_index`）：
    能对上官方集数的候选才是真集数；对不上时退回最高可信度候选
    （与旧行为一致，用于未匹配条目 / 接口缺数据的兜底）。

    层级（从高到低）：
      ① 「第X话/集/回」 —— 写法明确，唯一候选
      ② 预告/菜单/NCOP/PV 等附属内容 → 无候选（不参与编号）
      ③ EP01 / E01
      ④ 数字结尾（01.mkv、- 01）
      ⑤ 纯数字括号块 [01] / [01v2] / （05）
      ⑥ 其余独立数字（标题里的 86、裸写的 01 等）—— 兜底
    """
    stem = Path(name).stem
    cands: list[float] = []

    # ① 明确的「第 X 话/集/回」写法 —— 可信度最高，直接定案
    m = _EP_CN_RE.search(stem)
    if m:
        return [float(m.group(1))]

    # ② 附属内容（预告/菜单/NCOP/PV…）不参与编号，避免它们占掉 max_idx
    #    导致后续 SP 顺延到更大的序号上（正片 6 → 6.5/7/7.5…）
    if is_extra_file(stem):
        return []

    # ③ EP01 / E01
    m = _EP_EN_RE.search(stem)
    if m:
        return [float(m.group(1))]

    # ④ 数字结尾（含整名就是数字的 01.mkv）
    m = _EP_TAIL_RE.search(stem)
    if m:
        cands.append(float(m.group(1)))

    # ⑤ 纯数字括号块（发布组标准命名，如 [01v2]）。
    #    放在"数字结尾"之后：「[1080] 01.mkv」应取结尾的 01 而非画质 1080；
    #    但必须放在"独立数字"之前：86 的案例里若先跑独立数字，
    #    标题里的 86 会抢在 [01v2] 之前命中。
    #    超过可信度上限的（1080 / 2021 这类画质参数、年份）跳过。
    for m in _EP_BRACKET_RE.finditer(stem):
        val = float(m.group(1))
        if val <= _EP_BRACKET_MAX:
            cands.append(val)

    # ⑥ 其余独立数字 —— 兜底，可信度最低，靠官方集数表消歧
    for m in _EP_MID_RE.finditer(stem):
        cands.append(float(m.group(1)))

    return cands


def extract_ep_index(name: str) -> Optional[float]:
    """单一集号（保留的旧接口）：取最高可信度候选。

    新代码请改用 `extract_ep_candidates` + `_pick_ep_index` ——
    标题带数字的作品名（86、11eyes…）必须靠官方集数表消歧，
    单一值无法表达"这里有两个候选"。
    """
    cands = extract_ep_candidates(name)
    return cands[0] if cands else None


def _pick_ep_index(
    candidates: list[float], ep_map: dict[float, dict]
) -> Optional[float]:
    """从候选集号里挑出真集数：优先挑能对上官方集数表的那个。

    对不上时（SP、接口缺集、未匹配条目没有 ep_map）退回最高可信度
    候选 —— 与旧版行为一致，保证无接口数据时仍可编号。
    """
    if not candidates:
        return None
    for c in candidates:
        if c in ep_map:
            return c
    return candidates[0]


def _align_by_order(
    main_videos: list[tuple[float, Path]],
    ep_map: dict[float, dict],
    number_matched: bool,
) -> tuple[list[tuple[float, Path]], bool]:
    """官方集数与本地序号**完全对不上**但数量一致时，按顺序配对。

    背景（实测）：Re:零 第三季在 Bangumi 被拆成「袭击篇」「反击篇」
    两个条目，官方 sort 跨篇章连续（袭击篇 51~58、反击篇 59~66），而
    字幕组的文件名按本篇章从 [01] 编起 —— 按号匹配全部落空，集标题
    永远回填不上（详情页左列显示的是本地序号 1~8、右边一排文件名）。

    做法：两个列表都按各自序号排好、数量又一致，那么第 k 个本地文件
    就对应第 k 个官方集数，并把 **ep_index 改成官方序号** —— 与
    Bangumi 网页、动态、订阅页「已看集数」的口径保持一致
    （字幕组的 01 实际是官方第 51 话）。改号后 `ep_map.get(idx)` 顺理
    成章命中，标题与 bangumi_ep_id 走正常回填路径，无需特判。

    **触发条件必须同时满足**：
      1. 有官方集数表（ep_map 非空 —— 未匹配条目没有）；
      2. 本地正片数 == 官方正片数；
      3. 按号匹配**一个都没命中**（number_matched=False）。
         第 3 条是安全阀：只要有一部分按号对上了，说明两套编号大体
         一致，对不上的少数派多半是 SP/特别篇 —— 此时按顺序硬配反而
         会把已经正确的对应打乱（例如本地 03~06、官方 1/2/4/5，
         4、5 按号命中是**正确**的，顺序配对却会错开）。

    返回 `(配对后的列表, 是否发生了按顺序配对)` —— 后者写入
    `subjects.ep_align`，详情页打开时会弹黄色提示「对应可能不准确」
    （毕竟它是猜测，不是按号实锤）。
    """
    if not ep_map or number_matched:
        return main_videos, False
    if not main_videos or len(main_videos) != len(ep_map):
        return main_videos, False
    by_sort = sorted(ep_map.items())
    return [
        (sort_key, video)
        for (sort_key, _bgm), (_local_idx, video) in zip(by_sort, main_videos)
    ], True


def is_season_like(name: str, season_mode: str = "cn") -> bool:
    """目录名是否「像季数/篇章」而非系列名。

    用于判断下探时该继承父级系列名，还是把当前目录当作新系列。
    例：「第二季」「剧场版 蕾塞篇」→ True；「无职转生」「Fate」→ False
    """
    cleaned = clean_title(name)
    if not cleaned:
        return True
    # 含季数标记
    if extract_season(cleaned, season_mode) is not None:
        return True
    # 剧场版/篇章类
    if is_movie_like(cleaned):
        return True
    # 纯编号/集数（如「01-12」「全12话」）
    if re.fullmatch(r"[\d\-~\s]+[话集]?", cleaned):
        return True
    # 「XX篇」「XX编」「XX章」结尾
    if re.search(r"[篇编章]$", cleaned) and len(cleaned) <= 12:
        return True
    return False


def _videos_in(directory: Path, recursive: bool = False) -> list[Path]:
    """目录下的视频文件（recursive=True 时含子目录）。"""
    if not directory.is_dir():
        return []
    it = directory.rglob("*") if recursive else directory.iterdir()
    out: list[Path] = []
    for p in it:
        try:
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                out.append(p)
        except OSError:
            continue
    return sorted(out)


@dataclass
class ScanCandidate:
    """一条待匹配的本地条目。"""

    folder_path: Path
    video_files: list[Path]
    keywords: list[str] = field(default_factory=list)   # 多候选，按优先级排列
    series_name: str = ""                               # 所属系列（用于聚合展示）
    bangumi_name: str = ""                              # 匹配到的 Bangumi 名称（日志用）

    @property
    def primary_keyword(self) -> str:
        return self.keywords[0] if self.keywords else ""

    @property
    def display_name(self) -> str:
        """本地展示名：清洗后的「系列名 + 当前层次名」。

        用于 pending 条目的占位标题（尚无 Bangumi 名称时）。
        当当前层清洗后为空（下载工具多建的打包层），回退用原始目录名，
        避免多条条目重名。
        """
        current = clean_title(self.folder_path.name)
        if self.series_name and self.folder_path.name != self.series_name:
            if current and current not in self.series_name:
                return f"{self.series_name} {current}"
            if not current:
                # 打包层：用原始目录名（截断）保证可区分
                raw = self.folder_path.name
                return f"{self.series_name} [{raw[:40]}]"
            return self.series_name
        return current or self.folder_path.name


class ScanWorker(QThread):
    """后台扫描线程。"""

    progress_changed = Signal(int, int)        # current, total
    item_matched = Signal(int, str)            # subject_id, name
    log_message = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        library_paths: Iterable[Path],
        api: BangumiClient,
        db: Database,
        season_mode: str = "cn",
        season_display: str = "flat",
        accept_score: int = 60,
        accept_gap: int = 20,
        align_by_order: bool = True,
        only_folder: Optional[Path] = None,
        extra_show: bool = True,
        extra_numbering: bool = True,
        no_match: bool = False,
    ) -> None:
        super().__init__()
        self.library_paths = [Path(p) for p in library_paths]
        self.api = api
        self.db = db
        # 不做 Bangumi 匹配（「添加动漫」在未填 Token 时由 QML 传 True）：
        # 跳过全部网络搜索，直接把目录里的视频作为本地条目入库。
        # 见 bridges/scanner.addFolder 分支。
        self.no_match = no_match
        # 本次扫描**新建/更新**的 subject_id（按处理顺序）。
        #
        # 用途：`addFolder` 完成后要告诉 QML"刚加进来的是哪一条"，
        # 以便直接跳详情页。worker 是独立线程，不能直接改桥接层属性，
        # 因此在这里收集，由桥接层在 finished_ok 时读取。
        self.created_subject_ids: list[int] = []
        # 附加内容**是否入库**（设置页「附加内容显示」开关，scanner.extra_show）。
        #
        # 关闭时：SP/OVA/NCOP/特典… 既不识别为附加内容、也**不落进兜底桶**
        # —— 直接从本次扫描里剔除。这样它们既不出现在集数列表里，也不会
        # 被顺延成「13.5 集」这种假集号（那种做法只是"换个方式显示"，
        # 依然占着列表位置，用户要的是"彻底不出现"）。
        # 排序值里的 max_idx 也因此只按正片算 —— 与剔除后的结果一致。
        self.extra_show = extra_show
        # 附加内容独立编号（左列显示 `SP01`/`OVA01`/`NCOP3`…，排序排在正片后）。
        # 关闭时走旧行为：落进兜底桶、在正片之后顺延 13.5/14/14.5…
        # 见 config.DEFAULTS 的 extra_numbering 说明。
        # 仅在上面的 extra_show 打开时才有意义（外层关了里面就无从谈起）。
        self.extra_numbering = extra_numbering
        # 「单个重新扫描」（详情页的按钮）：只处理这一个目录，不遍历媒体库。
        #
        # **为什么复用整个 ScanWorker 而不是另写一条流程**：单扫与全扫
        # 在"匹配 → 写库 → 填集数 → 打标记"上完全一致，差别只在**候选来源**。
        # 另写一份必然与主流程漂移（集数对齐、别名、公司、tag 这些增量
        # 迟早漏掉）。这里只替换候选来源，其余原样复用。
        self.only_folder = Path(only_folder) if only_folder else None
        self.season_mode = season_mode
        self.season_display = season_display
        # 官方序号对不上但数量一致时，是否允许按顺序兜底配对
        # （设置页「集数按顺序对应」开关，scanner.ep_align_order）
        self.align_by_order = align_by_order
        self.matcher = SubjectMatcher(
            api, season_mode=season_mode, accept_score=accept_score
        )
        self.matcher.accept_gap = accept_gap

    def run(self) -> None:
        try:
            self._run()
            self.finished_ok.emit()
        except Exception as e:
            log.exception("扫描失败")
            self.failed.emit(str(e))

    def _run(self) -> None:
        candidates = list(self._collect_candidates())
        total = len(candidates)
        if total == 0:
            self.log_message.emit("未发现任何视频文件")
            return
        self.log_message.emit(f"共发现 {total} 个条目，开始匹配…")

        for i, cand in enumerate(candidates, 1):
            self.progress_changed.emit(i, total)
            self.log_message.emit(f"[{i}/{total}] {cand.display_name}")
            # 绑定当前动漫名：Bangumi 请求失败的日志会带上「哪个动漫」
            bgm_log.set_current_name(cand.display_name)
            try:
                self._process(cand)
            except BangumiError as e:
                log.warning("匹配失败 %s err=%s", cand.folder_path, e)
                self.log_message.emit(f"  Bangumi 错误：{e}")
                self._write_pending(cand, reason=f"API 错误：{e}")
            except Exception as e:
                log.exception("处理失败 %s", cand.folder_path)
                self.log_message.emit(f"  处理失败：{e}")

    # ========== 一、目录遍历（递归下探） ==========
    def _collect_candidates(self) -> Iterable[ScanCandidate]:
        # 单目录重扫：只从目标目录下探，且**直接以它为根**（不再要求它是
        # 媒体库的子目录 —— 用户可能改过媒体库路径，旧条目仍应能单扫）。
        if self.only_folder is not None:
            folder = self.only_folder
            if not folder.exists():
                log.warning("单条目扫描：目录不存在 %s", folder)
                return
            if folder.is_file():
                # 根目录散落文件的条目：folder_path 是父目录，实际文件是它本身
                yield ScanCandidate(
                    folder_path=folder.parent,
                    video_files=[folder],
                    keywords=[clean_title(folder.stem)],
                    series_name="",
                )
                return
            # **推断上级系列名**（实测踩坑）：
            #
            # 单扫不能像全扫那样从媒体库根逐层下探，只能"以目标目录为起点"。
            # 但目标目录本身可能是**季数目录**（典型：`Overlord\S2`），
            # 此时它的父目录名（`Overlord`）才是系列名。
            #
            # 早期这里直接 `_walk(folder, series_name="")`，把 `S2` 当成了
            # 系列根 —— 于是：
            #   ① 关键词退化成 `['S2']`（丢掉作品名），搜索出一堆无关/近似
            #      条目，前二名差距不足而反复转人工（实测日志：
            #      `[S2] 名称+60 季数一致(2)+40 类型一致+30` → 差距 15）；
            #   ② `series_name` 存成空串，详情页「同系列」切换失效。
            #
            # 判据与 `_walk` 里"向下传递系列名"的第 ① 条一致：目录名像季数
            # /篇章时，沿用上层系列名。这里没有"上层传下来的 series_name"，
            # 就用父目录名充当（父目录名不像季数才有意义，否则继续上溯一层）。
            parent_hint = ""
            if is_season_like(folder.name, self.season_mode):
                parent_hint = folder.parent.name
            yield from self._walk(folder, series_name=parent_hint, depth=0)
            return

        for root in self.library_paths:
            if not root.exists():
                log.warning("根目录不存在：%s", root)
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    yield from self._walk(child, series_name="", depth=0)
                elif child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                    # 根目录散落文件：剧场版单文件
                    kw = clean_title(child.stem)
                    yield ScanCandidate(
                        folder_path=child.parent,
                        video_files=[child],
                        keywords=[kw],
                        series_name="",
                    )

    def _walk(
        self,
        directory: Path,
        series_name: str,
        depth: int,
    ) -> Iterable[ScanCandidate]:
        """递归下探：只在「叶子目录」产出条目。"""
        if depth > MAX_DEPTH or is_noise_dir(directory.name):
            return

        direct_videos = _videos_in(directory, recursive=False)

        # 分类子目录
        sub_dirs: list[Path] = []
        extra_dirs: list[Path] = []      # SP/OVA 等附属内容
        try:
            for sub in sorted(directory.iterdir()):
                if not sub.is_dir():
                    continue
                # 附属内容：不下探，视频并入父条目
                if is_extra_dir(sub.name):
                    extra_dirs.append(sub)
                    continue
                # 含视频（含深层）才算有效子目录
                if _videos_in(sub, recursive=True):
                    sub_dirs.append(sub)
        except OSError as e:
            log.warning("读取目录失败 %s: %s", directory, e)
            return

        # 附属内容目录里的视频也并入本层
        extra_videos: list[Path] = []
        for ed in extra_dirs:
            extra_videos.extend(_videos_in(ed, recursive=True))

        all_videos = direct_videos + extra_videos

        # 向下传递的系列名，分三种情况：
        # ① 当前目录名像季数/篇章（「第二季」「剧场版 蕾塞篇」）→ 沿用上层系列名
        # ② 当前目录名清洗后为空（纯噪声打包层，如「[KTXP][XX][01-12][BDRip]」）
        #    → 沿用上层系列名，它是下载工具多建的一层，不构成新系列
        # ③ 其余（「无职转生」「Fate」）→ 自己就是系列名
        dir_clean = clean_title(directory.name)
        if is_season_like(directory.name, self.season_mode) or not dir_clean:
            next_series = series_name
        else:
            next_series = series_name or directory.name

        # --- 情况 A：纯容器（无直接视频，只有子目录）→ 继续下探，不产出 ---
        if not all_videos and sub_dirs:
            for sub in sub_dirs:
                yield from self._walk(sub, next_series, depth + 1)
            return

        # --- 情况 B：叶子目录 → 产出条目 ---
        if all_videos:
            kw_list = self._build_keywords(directory, series_name)
            yield ScanCandidate(
                folder_path=directory,
                video_files=all_videos,
                keywords=kw_list,
                series_name=series_name,
            )

        # --- 情况 C：既有视频又有子目录 → 子目录各自继续 ---
        if sub_dirs:
            for sub in sub_dirs:
                yield from self._walk(sub, next_series, depth + 1)

    def _build_keywords(
        self,
        directory: Path,
        series_name: str,
    ) -> list[str]:
        """生成多候选搜索关键词，按优先级排列。"""
        current = clean_title(directory.name)
        keywords: list[str] = []
        parent = clean_title(series_name) if series_name else ""

        # 打包层（清洗后为空）：从原始目录名里提取可用信息
        # 例：「[KTXP][Dungeon_..._Familia_Myth_II][01-12][BDRip]」→「Familia Myth II」
        if not current:
            current = self._extract_from_packed(directory.name)

        if parent and current and current not in parent:
            # ① 父级 + 当前（季数/剧场版最常见）
            keywords.append(f"{parent} {current}")
            # ② 仅当前（外传/独立作品）
            keywords.append(current)
            # ③ 仅父级（兜底）—— **仅当"当前"不是纯季数标记时才加**
            #
            # 实测踩坑（Overlord 四季合并成一个条目）：目录结构是
            # `Overlord/S1`、`S2`、`S3`、`S4`，四季各自生成
            # 「Overlord S1」…「Overlord S4」 + 兜底「Overlord」。
            # 而 Bangumi 上第一季标题就叫「OVERLORD」（无季数标记）、
            # 集数又同为 13 —— 兜底候选让它对四个季度都拿最高分，
            # 四季全部匹配到第一季，再按 bangumi_id 合并成一个 subject
            # （详情页集数重复 1,1,1,1,2,2…）。
            #
            # 判据：`current` 是纯季数标记时（S3 / 第二季 / III…），
            # 裸父级候选**没有任何区分四季的能力**，留着只会制造撞车。
            # 真正的兜底作用由 ① 的「父级 + 季数」承担 —— 它搜不到时
            # 说明该季在 Bangumi 上确实没有独立条目，那也该转人工确认，
            # 而不是悄悄归到第一季。
            if not self._is_season_only(current):
                keywords.append(parent)
        elif parent:
            keywords.append(parent)
            # 打包层提取出的信息若与父级不同，补一个「父级 + 提取值」
            if current and current not in parent:
                keywords.insert(0, f"{parent} {current}")
        elif current:
            keywords.append(current)

        # 去重保序，并剔除「无效候选」
        seen: set[str] = set()
        out: list[str] = []
        for k in keywords:
            k = re.sub(r"\s+", " ", k).strip()
            if not k or k in seen:
                continue
            # 无效候选：清洗后只剩季数标记（如「第二季」「S1」）
            # 搜索这类关键词会召来大量无关作品（「第一季」→ 我叫MT/遮天/开心宝贝…），
            # 且它们靠"名称包含季数"就能拿到 60 分，反而干扰正确条目。
            if self._is_season_only(k):
                log.debug("丢弃无效候选关键词（仅季数）: %s", k)
                continue
            seen.add(k)
            out.append(k)
        return out or [clean_title(directory.name)]

    def _is_season_only(self, text: str) -> bool:
        """判断关键词是否只由季数标记构成（不含作品名）。"""
        stripped = _SEASON_ONLY_RE.sub(" ", text)
        stripped = re.sub(r"[\s\-_·・:：,，]+", "", stripped)
        return not stripped

    @staticmethod
    def _extract_from_packed(raw_name: str) -> str:
        """从「打包层」目录名提取可用的季数/篇章信息。

        「[KTXP][Dungeon_..._Familia_Myth_II][01-12][BDRip][HEVC]」
            → 「Familia Myth II」→ 归一为「第二季」
        「[KissSub][... S4][00-11][1080P]」
            → 「第四季」
        提取不到则返回空串。
        """
        # 优先：识别明确的季数标记（S4 / 2nd Season / 第X季 / II）
        season = extract_season(raw_name, "all")
        if season is not None:
            cn = "一二三四五六七八九十十一十二"
            if 1 <= season <= 12:
                return f"第{cn[season - 1]}季"
            return f"第{season}季"

        # 次选：从方括号块里挑一段「像作品副标题」的拉丁文
        blocks = re.findall(r"\[([^\]]+)\]", raw_name)
        for block in blocks:
            # 跳过纯数字/参数块（01-12、1080p、BDrip…）
            if re.fullmatch(r"[\d\-\s~.]+", block):
                continue
            if re.search(r"(?i)(1080p|720p|bdrip|webrip|hevc|x264|x265|gb|chs|cht|mp4|mkv|aac|flac)", block):
                continue
            # 跳过纯字幕组名（单个词且短）
            if len(block) < 6 or " " not in block.strip():
                continue
            cleaned = clean_title(block)
            if cleaned:
                return cleaned
        return ""

    # ========== 二、匹配与入库 ==========
    def _process(self, cand: ScanCandidate) -> None:
        local_ep_count = len(cand.video_files)

        # 「添加动漫」在**没填 Token** 时的路径：完全不联网，直接把目录里的
        # 视频作为本地条目入库（match_state="manual"，bangumi_id=0）。
        #
        # 为什么用 manual 而不是 pending：pending 的语义是"匹配过、但结果
        # 需要人工确认"（详情页会出现「⚠ 匹配待确认，请点右上角重新匹配」
        # 的提示）。而这里是用户**主动选择不匹配**，并没有失败的匹配要他
        # 处理，套用 pending 会凭空多出一条待办提示。manual 表示"由用户
        # 指定/认可的状态"，与详情页 buildMeta 的「已手动指定」文案一致。
        if self.no_match:
            # **不能用 upsert_subject(bangumi_id=0)**（踩坑）：subjects 表的
            # `bangumi_id` 有 UNIQUE 约束，而未匹配条目本就没有 bomgumi_id ——
            # 用 0 当哨兵会导致**第二条未匹配番覆盖第一条**（0 只能存在一个）。
            # 表里未匹配用的是 NULL（SQLite 的 UNIQUE 允许多个 NULL），
            # 因此这里走 `upsert_local_subject`（按 folder_path 判重、写 NULL）。
            subject_id = self.db.upsert_local_subject(
                folder_path=str(cand.folder_path),
                display_name=cand.display_name,
                series_name=cand.series_name,
                total_eps=len(cand.video_files),
            )
            self.created_subject_ids.append(subject_id)
            self.log_message.emit(f"  ✓ 已加入（未匹配）：{cand.display_name}")
            self._fill_episodes(subject_id, None, cand)
            return

        result = self.matcher.search_best(cand.keywords, local_ep_count)

        if result.subject is None:
            self.log_message.emit(f"  待手动确认：{result.reason}")
            self._write_pending(cand, reason=result.reason)
            return

        subj = result.subject
        bangumi_id = subj["id"]

        # manual 记录保护：用户手动指定过的条目不覆盖
        manual = self.db.find_manual_subject_by_folder(str(cand.folder_path))
        if manual is not None:
            self.log_message.emit(
                f"  跳过：该目录已有手动匹配（{manual.name_cn or manual.name}）"
            )
            self._fill_episodes(manual.id, bangumi_id, cand)
            return

        name = subj.get("name", "")
        name_cn = subj.get("name_cn", "") or name
        cand.bangumi_name = name_cn
        cover_url = (subj.get("images") or {}).get("large", "")
        total_eps = subj.get("total_episodes") or subj.get("eps_count") or 0

        cover_path = ""
        if cover_url:
            try:
                cover_path = str(download_cover(
                    bangumi_id, cover_url, self.api.session, label=name_cn))
            except Exception as e:
                log.warning("封面下载失败 %s: %s", name_cn, e)

        subject_id = self.db.upsert_subject(
            bangumi_id=bangumi_id,
            name=name,
            name_cn=name_cn,
            cover_url=cover_url,
            cover_path=cover_path,
            total_eps=total_eps,
            folder_path=str(cand.folder_path),
            series_name=cand.series_name,
            # 顺路存 infobox 别名（就在本次搜索响应里，零额外请求）。
            # 用途：本地按别名搜已入库条目（匹配阶段用的是内存里的那份，
            # 见 matcher.score_subject，与此列无关）。
            aliases=self.db.aliases_from_subject(subj),
            # 顺路存**动画制作公司**：优先用搜索响应里的 infobox（零额外请求），
            # 它没有这一行时才补一个 /persons 请求（实测约三分之二的条目要
            # 这一下 —— 见 BangumiClient.studio_for）。海报墙的"制作公司"
            # 筛选栏靠它，写一次就永久在库里，之后不再请求。
            studio=self.api.studio_for(subj, bangumi_id),
            match_state="auto",
        )
        # 顺路存接口前 10 个 tag（就在本次搜索响应里，零额外请求）。
        # 失败不阻塞扫描 —— tag 是展示性数据，丢了可手动重取。
        try:
            self.db.replace_subject_tags(subject_id, self.db.tags_from_subject(subj))
        except Exception as e:
            log.warning("写入条目标签失败 %s: %s", name_cn, e)
        self.log_message.emit(f"  ✓ {name_cn}（{result.score} 分：{result.reason}）")
        self.created_subject_ids.append(subject_id)
        self.item_matched.emit(subject_id, name_cn)
        self._fill_episodes(subject_id, bangumi_id, cand)

    def _write_pending(self, cand: ScanCandidate, reason: str) -> None:
        """匹配失败：写入占位条目，状态 pending，等待用户手动指定。"""
        try:
            subject_id = self.db.upsert_pending_subject(
                folder_path=str(cand.folder_path),
                display_name=cand.display_name,
                series_name=cand.series_name,
            )
            log.info("待手动确认 %s：%s", cand.folder_path, reason)
            self._fill_episodes(subject_id, None, cand)
        except Exception as e:
            log.warning("写入待确认条目失败 %s: %s", cand.folder_path, e)

    def _fill_episodes(
        self,
        subject_id: int,
        bangumi_id: Optional[int],
        cand: ScanCandidate,
    ) -> None:
        """写入该条目的集数记录。"""
        bgm_eps: list[dict] = []
        if bangumi_id:
            # 优先用 Bangumi 正名（比本地目录名更准确），未匹配到则退回本地展示名
            bgm_log.bind_subject(bangumi_id, cand.bangumi_name or cand.display_name)
            try:
                bgm_eps = self.api.get_episodes(bangumi_id)
            except BangumiError:
                pass
        # 官方集数表：键 = 官方序号（sort 优先、ep 兜底），值 = 接口集对象。
        # 用途有二：① 集标题回填（name_cn / name）；② `_pick_ep_index`
        # 的候选消歧 —— 文件名里有多个疑似集号时（如标题带数字的
        # 「86—Eitisnikkusu— [01v2]」），能对上官方集数的那个才是真集号。
        #
        # 键统一转成 float：本地解析出的是 float，而接口 JSON 的 sort 可能
        # 是 int 或个别情况下的字符串，不转会静默查不到（并且不报错）。
        ep_map: dict[float, dict] = {}
        for e in bgm_eps:
            try:
                key = float(e.get("sort") or e.get("ep"))
            except (TypeError, ValueError):
                continue
            ep_map.setdefault(key, e)

        # 四类文件：
        #   main   —— 正片（有明确集数序号）
        #   extras —— 附加内容（SP/OVA/NCOP/特典…），带自己的显示标签，
        #             序号排在正片之后
        #   extra  —— 未能识别为附加内容、但明显不是正片的（顺延兜底）
        #   plain  —— 既非正片又非附属（无法识别集数的视频），序号顺延在最后
        main_videos: list[tuple[float, Path]] = []
        extras_videos: list[tuple[str, Path]] = []      # (显示标签, 文件)
        extra_videos: list[tuple[str, Path]] = []
        plain_videos: list[tuple[str, Path]] = []
        number_matched = False    # 是否有文件按号命中了官方集数表
        for v in cand.video_files:
            # ⓪ 附加内容整体关闭（设置页「附加内容显示」）：**直接跳过该文件**。
            #
            # 必须早于下面所有分支：不能识别成附加内容（①），也不能落到
            # 「不是正片但像附属」的兜底桶（②）—— 那些兜底同样会把文件
            # 写进集数列表（只是换个编号），而这里的语义是"完全不要"。
            #
            # 代价：这些文件的历史播放记录会因收尾的 prune 一起清理
            # （重扫时该 subject 下只有本次写入的行存活）。这是"开关关掉
            # 后集数列表里不再出现任何附加内容"的必然结果。
            if not self.extra_show:
                if extract_extra_index(v.name) is not None:
                    continue
                if is_extra_file(v.name):
                    continue
            # ① 附加内容优先判定：**必须在正片解析之前**
            #
            # 原因：`extract_ep_candidates` 对「...SP05...」会命中内部数字
            # （SP05 里的 05），若先走正片分支，SP 会被当成第 5 集，
            # 与真正的第 5 集撞号。实测早期版本这些内容干脆解析不出集号、
            # 落进兜底桶被顺延成「13.5 集」这种假编号（截图反馈）。
            label = (extract_extra_index(v.name)
                     if self.extra_numbering else None)
            if label is not None:
                extras_videos.append((label, v))
                continue
            idx = _pick_ep_index(extract_ep_candidates(v.name), ep_map)
            if idx is not None:
                if idx in ep_map:
                    number_matched = True
                main_videos.append((idx, v))
            elif is_extra_file(v.name):
                extra_videos.append((v.stem, v))
            else:
                plain_videos.append((v.stem, v))
        main_videos.sort(key=lambda x: x[0])
        # ---- 附加内容：先补号、再排序 ----
        #
        # **顺序不能反**（实测踩坑）：先排序的话，无序号项的排序键只能
        # 取一个哨兵值（如 -1），会排到同类的最前面 —— 补号后变成
        # `SP4` 却排在 `SP01` 前面，列表看起来是乱的。
        #
        # 补号规则：**按关键词分组**（`[SP][SP]` → SP1/SP2；
        # `[NCOP][SP]` → NCOP1/SP1），从 1 开始往上找第一个空位，
        # 避免不同类互相占号，也避免与已有的 `SP01`/`SP02` 撞车。
        if extras_videos:
            used_by_kind: dict[str, set[int]] = {}
            for label, _ in extras_videos:
                m = re.search(r"(\d+)$", label)
                if m:
                    kind = re.sub(r"\d+$", "", label)
                    used_by_kind.setdefault(kind, set()).add(int(m.group(1)))
            filled_extras: list[tuple[str, Path]] = []
            for label, v in extras_videos:
                if re.search(r"\d+$", label):
                    filled_extras.append((label, v))
                    continue
                used = used_by_kind.setdefault(label, set())
                n = 1
                while n in used:
                    n += 1
                used.add(n)
                filled_extras.append((f"{label}{n}", v))
            extras_videos = filled_extras

        # 排序：按（关键词, 序号）—— 先按类型分组（NCOP…、OVA…、SP…），
        # 同类型内按序号升序。**不能只按序号排**：不同类型会互相交错
        # （NCOP1/NCOP2/SP01/SP02… 混着显示很难读）。
        def _extras_sort_key(item: tuple[str, Path]) -> tuple[str, float]:
            label, _ = item
            m = re.search(r"(\d+)$", label)
            return (re.sub(r"\d+$", "", label),
                    float(m.group(1)) if m else 0.0)

        extras_videos.sort(key=_extras_sort_key)
        # 官方序号与本地编号完全对不上但数量一致 → 按顺序配对并改用官方
        # 序号（Re:零 袭击篇：本地 [01]~[08] vs 官方 51~58，见 _align_by_order）。
        # 受设置页「集数按顺序对应」开关控制（scanner.ep_align_order）：
        # 关闭时跳过，保持本地编号；此时把 ep_align 标记也一并清掉 ——
        # 它的语义是"**当前**集数来自顺序配对"，功能关了就不该再警示。
        if self.align_by_order:
            main_videos, aligned_by_order = _align_by_order(
                main_videos, ep_map, number_matched)
        else:
            aligned_by_order = False
        extra_videos.sort(key=lambda x: x[0])
        plain_videos.sort(key=lambda x: x[0])

        # 附属/未知内容统一从 max+0.5 起顺延（保持 0.5 间隔，便于「第 6.5 集」式展示）
        max_idx = max((i for i, _ in main_videos), default=0)
        next_extra_idx = max_idx + 0.5

        # 记录本次写入/更新的所有 episodes.id —— 收尾时据此清理陈旧行
        # （见方法末尾"清理陈旧记录"的说明）。用实例属性而不是局部变量：
        # 写入分散在下面四个循环里，逐个传参太啰嗦。
        self._written_episode_ids: set[int] = set()

        for idx, v in main_videos:
            bgm = ep_map.get(idx) or {}
            self._written_episode_ids.add(self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=bgm.get("id"),
                ep_index=idx,
                # **必须显式清空 ep_label**（踩坑）：`upsert_episode` 的
                # `DO UPDATE SET` 只覆盖**传入的列**，没传的列保留旧值 ——
                # 于是"开关从独立编号切到顺延编号后重扫"时，同一文件的
                # 旧标签（SP01）会一直留着（实测反馈"关闭后重新扫描，
                # 依旧存在 SP"）。正片传空串覆盖。
                ep_label="",
                title=bgm.get("name_cn") or bgm.get("name") or v.stem,
                file_path=str(v),
            ))

        # ---- 附加内容（SP / OVA / NCOP / 特典…）----
        #
        # 三个设计点：
        #   ① **显示标签**存进 `ep_label`（`SP01`/`OVA01`/`NCOP3`…）——
        #      用户要求"文件名写啥就是啥"，前导零也保留；
        #   ② **排序值**排在正片之后（`extra_sort_index`），符合"附加内容
        #      在正片下面"的直觉（早期用负数排在正片之前，实测反馈要改）；
        #   ③ **标题**优先取接口数据（`type != 0` 的条目），拿不到用文件名。
        #      实测 Bangumi 的 /v0/episodes 目前不返回 SP（`type=1` 查询
        #      返回 0 条、`total` 也不含），所以实际走文件名分支 —— 这段
        #      映射留着：接口哪天补上，标题自动恢复，不必再改一遍。
        extra_title_map: dict[float, dict] = {}
        for e in bgm_eps:
            try:
                if int(e.get("type") or 0) == 0:
                    continue
                extra_title_map[float(e.get("sort") or e.get("ep"))] = e
            except (TypeError, ValueError):
                continue

        # 附加内容的排序值：序号取标签内的数字（`SP01` → 1、`NCOP3` → 3），
        # 取不到时用累计计数兜底（无序号项已在上面补过号，正常不会走到）。
        # 注意序号在**不同类型间会重复**（SP1 与 OVA1 都是 1），
        # 所以排序值再叠加一个"类型内计数"才能保证彼此不撞 —— 用 enumerate
        # 的全局序号做偏置，天然唯一且保持列表顺序。
        for pos, (label, v) in enumerate(extras_videos, start=1):
            m = re.search(r"(\d+)\s*$", label)
            seq = int(m.group(1)) if m else pos
            bgm = extra_title_map.get(float(seq)) or {}
            self._written_episode_ids.add(self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=bgm.get("id"),
                ep_index=extra_sort_index(label, pos, max_idx),
                ep_label=label,
                title=bgm.get("name_cn") or bgm.get("name") or v.stem,
                file_path=str(v),
            ))

        # 预告/菜单/NCOP 等：排在正片之后，不占用正片编号
        for title, v in extra_videos:
            self._written_episode_ids.add(self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=None,
                ep_index=next_extra_idx,
                ep_label="",          # 显式清空旧标签（理由见正片分支）
                title=title,
                file_path=str(v),
            ))
            next_extra_idx += 0.5

        # 兜底：其余识别不出含义的视频，继续顺延（避免互相覆盖 file_path）
        for title, v in plain_videos:
            self._written_episode_ids.add(self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=None,
                ep_index=next_extra_idx,
                ep_label="",          # 显式清空旧标签（理由见正片分支）
                title=title,
                file_path=str(v),
            ))
            next_extra_idx += 0.5

        # ---- 清理陈旧记录（重扫时必须做）----
        #
        # `upsert_episode` 以 `file_path` 为唯一键、**只增不改**，所以：
        #   ① 用户在文件夹里删掉的集，重扫后那一行仍留在库里；
        #   ② 集数规则变化（如开关从"独立编号"切到"顺延编号"）时，
        #      同一文件会以新编号再写一行，**旧编号的行成为孤儿** ——
        #      表现为"关掉开关重扫，SP 还在"（实测反馈）。
        #
        # 修法：以**本次扫描实际产生的行 id 集合**为准，删掉该 subject 下
        # 不在集合里的行。这比"重扫前先清空"更安全：清空会先删掉全部行，
        # 万一中间失败（网络中断导致提前 return），用户会看到空的集数列表；
        # 而"先写后删"任何时刻都有一份完整数据。
        try:
            alive_ids = self._written_episode_ids
            if alive_ids:
                removed = self.db.prune_episodes(subject_id, alive_ids)
                if removed:
                    log.info("清理陈旧集数记录 subject_id=%s：%s 条",
                             subject_id, removed)
        except Exception as e:  # pragma: no cover - 清理失败不影响集数写入
            log.warning("清理陈旧集数记录失败 subject_id=%s: %s", subject_id, e)

        # 记录「集数是按顺序对应的」这一事实：详情页打开时弹黄色提示
        # 「可能不准确」。每次填充都会重写 —— 下次重扫若按号对上了，
        # 标记自动清除（见 Database.set_subject_ep_align）。
        try:
            self.db.set_subject_ep_align(subject_id, aligned_by_order)
        except Exception as e:  # pragma: no cover - 标记失败不影响集数写入
            log.warning("写入集数对应方式标记失败 subject_id=%s: %s",
                        subject_id, e)
