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
#
# **这张表 `clean_anime_title` 也在用**（去集号片段，兼着 RSS 判新的模糊
# 匹配）。集号提取另有一套更严格的规则，见下面的 `_scan_episodes` —— 改
# 这里会同时改匹配，要整库回放核对，不值当。
_EP_PATTERNS = [
    re.compile(r"第\s*(\d+(?:\.\d+)?)\s*[话集回]"),
    re.compile(r"(?i)\bE[P]?\s*0*(\d+(?:\.\d+)?)\b"),
    re.compile(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?\s*$"),
    re.compile(r"[\s\-\_\[\]]0*(\d+(?:\.\d+)?)(?:v\d+)?[\s\-\_\[\]]"),
]

#: 分隔符后的集号（`Title - 01`）。**必须单独一层并排在通用规则之前**，
#: 否则 `Season 2 - 01` 里季号的 `2` 会被通用规则先撞上（见 `_scan_episodes`）。
_EP_SEP_RE = re.compile(r"[-–—]\s+0*(\d+(?:\.\d+)?)(?:v\d+)?(?![\w.])")

#: 纯数字括号块（`[01v2]` / `（05）`）——发布组的标准命名，可信度高。
_EP_BRACKET_RE = re.compile(r"[\[【(（]\s*0*(\d+(?:\.\d+)?)(?:v\d+)?\s*[\]】)）]")

#: 正片集号的上限。超过它的基本是画质参数或年份（`- 1080`、`(2021)`）。
_EP_MAX = 199.5

#: 「这是季数，不是集数」——`Season 2` / `S2` / `第2季` 里的那个数字。
#: 与 `app/core/scanner.py` 的 `_SEASON_PREFIX_RE` / `_SEASON_SUFFIX_RE`
#: **同一口径**（那边踩过这个坑，长注释在 scanner 里）。
_SEASON_PREFIX_RE = re.compile(
    r"(?i)(?:\bS(?:eason)?\s*$|第\s*$|\b\d+(?:st|nd|rd|th)\s+season\s*$)")
_SEASON_SUFFIX_RE = re.compile(r"(?i)^\s*(?:st|nd|rd|th)\s+season\b")

#: 视频扩展名。**只有认出来才剥后缀**，见 `_stem`。
_MEDIA_EXT_RE = re.compile(
    r"(?i)\.(?:mp4|mkv|avi|rmvb|wmv|flv|mov|m4v|mpg|mpeg|ts|webm|iso)$")


def _stem(name: str) -> str:
    """去掉视频扩展名；**不是视频文件就原样返回**。

    **为什么不能用 `Path(name).stem`**：RSS 条目给的是**标题**不是文件名。
    `Path('Tantei wa Mou, Shindeiru. S2 - 01').stem` 会把最后一个点后面
    整段当扩展名砍掉，变成 `'Tantei wa Mou, Shindeiru'` —— 集号 `- 01`
    一起没了，解析直接返回 None。只认已知的视频扩展名即可避开。
    """
    return _MEDIA_EXT_RE.sub("", name)


def _is_season(stem: str, m: "re.Match[str]") -> bool:
    """这个数字是不是季号（看它紧挨着的前后缀）。"""
    return bool(_SEASON_PREFIX_RE.search(stem[:m.start()])
                or _SEASON_SUFFIX_RE.match(stem[m.end():]))


def _push(out: list[float], raw: str) -> None:
    """收一个候选：超上限的（画质参数/年份）丢掉，重复的不再收。"""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return
    if val <= _EP_MAX and val not in out:
        out.append(val)


def _scan_episodes(stem: str, explicit_only: bool = False) -> list[float]:
    """按可信度从高到低收集集号候选（输入须已过 `_stem`）。

    `explicit_only=True` 时**只收明确写法**（①②③④），不收第⑤层
    「标题里随便一个独立数字」的兜底 —— 那一层连季号都可能收进来，
    拿它去比对本地媒体库会漏下真正缺的那集（见 `episode_candidates`）。
    """
    out: list[float] = []

    # ① 「第X话 / EP01」—— 写法明确，唯一候选，直接定案
    for rx in (_EP_PATTERNS[0], _EP_PATTERNS[1]):
        m = rx.search(stem)
        if m:
            _push(out, m.group(1))
            return out

    # ② 分隔符后的集号（`Title - 01`）
    for m in _EP_SEP_RE.finditer(stem):
        if not _is_season(stem, m):
            _push(out, m.group(1))

    # ③ 数字结尾（`01.mkv`、`… - 01`）
    m = _EP_PATTERNS[2].search(stem)
    if m and not _is_season(stem, m):
        _push(out, m.group(1))

    # ④ 纯数字括号块（`[01v2]`）
    for m in _EP_BRACKET_RE.finditer(stem):
        _push(out, m.group(1))

    if explicit_only:
        return out

    # ⑤ 其余独立数字 —— 兜底，可信度最低，**排除季号**
    for m in _EP_PATTERNS[3].finditer(stem):
        if not _is_season(stem, m):
            _push(out, m.group(1))
    return out


def extract_episode_index(name: str) -> Optional[float]:
    """从标题/文件名提取集数序号，失败返回 None。

    原实现把 `_EP_PATTERNS`依次 `search`、谁先命中就返回谁，
    而最后那条通用规则用的是**最左匹配**。于是

        [黒ネズミたち] 青之芦苇 第二季 / Ao Ashi Season 2 - 01 (Baha …)

    里 `Season` 后面的 **2 先被撞上** → 判成「第 2 集」；而扫描侧
    （`scanner.extract_ep_candidates`，有季号排除）算的是**第 1 集**。
    同一个文件两边号码不同，查重第①层拿 2.0 去比本地库的 {1.0} 永远
    不命中 —— **每轮轮询都把这条已经下过的第 1 集当成新集再下一次**，
    并顺带把 qBittorrent 拉起来。

    现在改成按可信度分层（见 `_scan_episodes`），并排除季号，与扫描侧
    口径对齐。顺带修掉 `Path(name).stem` 把 RSS 标题当文件名切的问题
    （见 `_stem`）。
    """
    cands = _scan_episodes(_stem(name))
    return cands[0] if cands else None


def episode_candidates(name: str) -> list[float]:
    """RSS 标题里的**高可信度**集号候选，按可信度从高到低。

    用途：查重第①层的**容错比对**。RSS 侧与扫描侧是两套解析器，同一集
    算出来的号有可能不一致，拿单值去比就会一直判成新集。把明确写法的
    候选都拿去比，任一命中即认为本地已有。

    **刻意不收第⑤层兜底**：那层连季号都可能收进来
    （`… Nouka 2 - 01` → `[2, 1]`），拿它去比会把**真正缺的那集**
    误判成"已有"。漏下比重复下更难发现，宁可窄一点。
    """
    return _scan_episodes(_stem(name), explicit_only=True)


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
