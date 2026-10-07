"""标题解析：PotPlayer 播放进度 + RSS/文件名集数与动漫名提取。

- parse_progress：解析 PotPlayer 窗口标题里的播放进度
- extract_episode_index / clean_anime_title：供 F19 RSS 判新使用
- guess_anime_name：清洗的加强版，供「获取名称」预填输入框用
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# 默认匹配 "00:12:34 / 00:24:00" 或 "12:34/24:00"
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*/\s*(\d{1,2}:\d{2}(?::\d{2})?)"
)

# ---- RSS / 文件名噪声（与 scanner.clean_title 保持一致的清洗口径）----
_NOISE_PATTERNS = [
    r"\[.*?\]",
    r"【.*?】",
    r"\(.*?\)",
    r"(?i)\b(BDRip|WEB[- ]?DL|WEBRip|BluRay|HDTV|x264|x265|HEVC|AVC|10bit|8bit|1080p|720p|2160p|4K|AAC|FLAC|MKV|MP4)\b",
    r"(?i)\b(简体|繁体|简繁|内嵌|外挂|中字|日配|国配|合集|完全版|无修|字幕组|招募|发布组)\b",
    r"(?i)\b(CHS|CHT|GB|BIG5|JPSC|JPTC)\b",
    r"_+",
]

#: 会被 `_NOISE_PATTERNS` 整块丢掉的括号内容（形状与那边保持一致）。
#: 只在 `guess_anime_name` 的兜底里用：标题**通篇都是括号**时，
#: 番名本身也在被丢掉的那堆里，得回头从括号里挑。
_STRIPPED_BLOCK_RE = re.compile(r"\[([^\[\]]*)\]|【([^【】]*)】|\(([^()]*)\)")

#: 发布组/字幕组的尾巴 —— 兜底挑番名时用来把「XX组」这类块排除掉。
#: 不是万能的（组名千奇百怪），但只要能把最常见的「XX组」挡掉，
#: 真正是番名的那个块就能靠长度胜出。
_GROUP_SUFFIX_RE = re.compile(
    r"(?i)(组|社|屋|工作室|制作|压制|搬运|Team|Subs?|Raws?|Fansub)$")

#: 只出现在括号里的**复合**语言/字幕标记 —— 基础噪声表只认得整词的
#: 「简体/繁体/简繁」，而标题里写的是「简繁日双语」这种连写，清不掉。
#: 不放进 `_NOISE_PATTERNS`：那张表 `clean_anime_title` 也在用（还兼着
#: RSS 判新的匹配），动它要整库回放核对，不值当。
_BLOCK_NOISE_RE = re.compile(
    r"(简繁|简体|繁体|双语|中字|内嵌|外挂|无修|合集|招募|新番|生肉|熟肉|字幕|搬运)")

#: 汉字（CJK 统一表意文字基本区）—— 用来判"这个括号块像不像番名"。
_CJK_RE = re.compile(r"[一-鿿]")

# 集数：第 1 话 / EP01 / E01 / [01] / - 01 / 12.5
_EP_PATTERNS = [
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话集回]"),
    re.compile(r"(?i)\bE[P]?\s*0*(\d+(?:\.\d+)?)\b"),
    re.compile(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?\s*$"),
    re.compile(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?[\s\-\_\[\]]"),
]


def extract_episode_index(name: str) -> Optional[float]:
    """从标题/文件名提取集数序号，失败返回 None。"""
    stem = Path(name).stem if ("." in name and "/" not in name and "\\" not in name) else name
    for rx in _EP_PATTERNS:
        m = rx.search(stem)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def clean_anime_title(name: str) -> str:
    """清洗标题得到动漫名关键词（供本地条目模糊匹配）。"""
    s = Path(name).stem if ("." in name and "/" not in name and "\\" not in name) else name
    # 去掉集数片段，避免把 "01" 当名字的一部分
    for rx in _EP_PATTERNS:
        s = rx.sub(" ", s)
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def guess_anime_name(title: str) -> str:
    """从资源标题里猜番名 —— 清洗结果为空时，改从**括号里**挑。

    comicat / 蜜柑这类站点的标题 **连番名都包在
    【】里**，而 `clean_anime_title` 把括号块一律当噪声丢掉，于是整条标题
    清完一个字不剩，「获取名称」只能报"未能推断出名字"。

    括号块既然被丢掉过，这里就回头在里面找，按"像番名的程度"取最长的：

      - **只认含汉字的块**。单个英文裸词（`Group`、`VCB-Studio`、
        `SubsPlease`）和番名在结构上根本没法区分，猜错就是**静默填错**；
        宁可返回空让用户手填，也不要塞一个看着像名字的组名进去。
        这条同时保证了"不会把原来能用的源搞坏"：非中文名的源本来就返回空。
      - 排除「XX组」这种一眼是发布组的块。
      - 复合语言标记（`简繁日双语`）先剔掉再比长度 —— 否则它比许多番名还长。

    **和 `clean_anime_title` 分开**：那个还兼着 RSS 判新的模糊匹配
    （rss_matcher），改它的语义要整库回放核对；这里只服务「获取名称」。
    """
    cleaned = clean_anime_title(title)
    if cleaned:
        return cleaned

    best = ""
    for m in _STRIPPED_BLOCK_RE.finditer(title):
        inner = next((g for g in m.groups() if g is not None), "").strip()
        if not _CJK_RE.search(inner) or _GROUP_SUFFIX_RE.search(inner):
            continue
        cand = _BLOCK_NOISE_RE.sub(" ", clean_anime_title(inner))
        cand = re.sub(r"\s+", " ", cand).strip()
        if len(cand) > len(best):
            best = cand
    return best


def _to_seconds(t: str) -> int:
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_progress(title: str, pattern: Optional[str] = None) -> Optional[float]:
    """从 PotPlayer 标题解析当前播放进度（0~1）。失败返回 None。"""
    regex = re.compile(pattern) if pattern else TIME_RE
    m = regex.search(title)
    if not m:
        return None
    try:
        cur = _to_seconds(m.group(1))
        total = _to_seconds(m.group(2))
    except (ValueError, IndexError):
        return None
    if total <= 0:
        return None
    return min(cur / total, 1.0)
