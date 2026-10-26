"""标题解析：PotPlayer 播放进度 + RSS/文件名集数与动漫名提取。

- parse_progress：解析 PotPlayer 窗口标题里的播放进度
- extract_episode_index / clean_anime_title：供 F19 RSS 判新使用
- guess_anime_name / guess_anime_names：清洗的加强版，供「获取名称」预填输入框、
  以及新建条目时的 Bangumi 匹配用（后者会把「中文名 / 原名」拆成多个候选）
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

#: **季度标记** —— `★10月新番★` / `★7月新番★` 这类。
#:
#: **为什么要单独一条**：
#:   `_clean_for_name` 把 `【组名】`、`[番名]`、`[03]`、`[1080p]` 全当噪声
#:   删掉后，**只剩这个季度标记**；于是"清洗结果非空"→ 不再走
#:   `_pick_from_blocks` 兜底 → 番名（就在被删掉的 `[学生会也有洞 / ...]`
#:   里）根本没被看见。
#:
#: 星号不是括号，`_NOISE_PATTERNS` 里那几条都匹配不到它。
#: 只认「★…★」包起来的：裸的「10月新番」也可能是番名的一部分，
#: 而带星号的这种在资源标题里从来只是季度标签。
_SEASON_TAG_RE = re.compile(r"★[^★]{0,20}?新番[^★]{0,20}?★")

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


def _ep_remover(s: str):
    """生成替换函数：只把**确实是集号**的匹配删掉，其余原样留着。

    判据两条，与 `_scan_episodes` 同一口径：

      * `_is_season()` —— `Season 2` / `S 2` 里的那个数字是**季号**，删掉
        不是"清噪声"，是把季号从番名里抹了，拿去搜 Bangumi 会命中**错的
        那一季**。
      * `_EP_MAX` —— 超过 199.5 的不是集号，是年份 `(2021)` 或画质 `- 1080`。
    """
    def repl(m: "re.Match[str]") -> str:
        raw = m.group(1)
        try:
            if float(raw) > _EP_MAX:
                return m.group(0)
        except (TypeError, ValueError):
            return m.group(0)
        return m.group(0) if _is_season(s, m) else " "

    return repl


def _clean_for_name(title: str) -> str:
    """「获取名称」专用清洗：与 `clean_anime_title` 同一张表，三处**故意不同**。

    ① **只剥已知媒体扩展名**（`_stem`），不用 `Path(name).stem`。
       RSS 给的是**标题**不是文件名，`Path('Tantei wa Mou, Shindeiru. S2 - 01').stem`
       会把最后一个点之后**整段**当扩展名砍掉 —— 「S2 - 01」一起没了（`_stem`
       的注释里记着这个坑）。`clean_anime_title` 那条老路还留着它：它兼着
       RSS 判新的模糊匹配，动它要整库回放核对，不值当，所以这里绕开。

    ② **只删"确实是集号"的数字**（见 `_ep_remover`），不吃季号、不吃年份。

    ③ **不用兜底那一层**（`_EP_PATTERNS[3]`，"任意独立数字"）。扫描侧
       （`episode_candidates`）也只把它当最低可信度、刻意不收，理由是
       "连季号都可能收进来"；在番名上更是灾难 —— 「86 -不存在的战区-」
       的 `86` 会被当成第 86 集删掉，番名直接变成 `-不存在的战区-`。
    """
    s = _stem(title)
    # 季度标记先剔掉（`★10月新番★`）—— 它会让清洗结果"看起来非空"，
    # 从而挡住 `_pick_from_blocks` 兜底（见 `_SEASON_TAG_RE` 的说明）
    s = _SEASON_TAG_RE.sub(" ", s)
    remover = _ep_remover(s)
    # 写法明确的集号：第X话 / EP01 / 分隔符后的 01 / 结尾的 01 / [01]
    for rx in (_EP_PATTERNS[0], _EP_PATTERNS[1], _EP_SEP_RE,
               _EP_PATTERNS[2], _EP_BRACKET_RE):
        s = rx.sub(remover, s)
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    return re.sub(r"\s+", " ", s).strip()


#: 「中文名 / 原名」之间的分隔符 —— **要求两侧带空白**才算分隔符。
#: 反例：`Fate/strange Fake`、`Re:Zero`，斜杠/冒号是名字自己的一部分，
#: 无脑按 `/` 拆会把一个番名拆成两半（海报墙上就有 `Fate/strange Fake`）。
_NAME_SPLIT_RE = re.compile(r"\s+[/／|｜]\s+")

#: 候选两端**悬空**的分隔符（`青之芦苇 第二季 / Ao Ashi Season 2 -` 的尾巴）。
#:
#: **必须"分隔符紧挨着空白"才算悬空**：`86 -不存在的战区-` 这类名字自带的
#: 破折号是番名的一部分（它两侧没有空白），削掉就把名字改了。
_DANGLING_TAIL_RE = re.compile(r"(?:\s+[-–—|｜/／,，、])+\s*$")
_DANGLING_HEAD_RE = re.compile(r"^[-–—|｜/／,，、]+\s+")


def _split_candidates(cleaned: str) -> list[str]:
    """把清洗结果按「中文名 / 原名」拆成候选，去掉悬空分隔符与重复。"""
    out: list[str] = []
    seen: set[str] = set()
    for part in _NAME_SPLIT_RE.split(cleaned or ""):
        cand = _DANGLING_HEAD_RE.sub("", part)
        cand = _DANGLING_TAIL_RE.sub("", cand)
        cand = re.sub(r"\s+", " ", cand).strip()
        # 短于 2 个字符的（`-`、`01` 的残渣）不成名字
        if len(cand) < 2 or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
    return out


def _pick_from_blocks(title: str) -> str:
    """清洗结果为空时，回头从**括号块**里挑番名。

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
    """
    best = ""
    for m in _STRIPPED_BLOCK_RE.finditer(title):
        inner = next((g for g in m.groups() if g is not None), "").strip()
        if not _CJK_RE.search(inner) or _GROUP_SUFFIX_RE.search(inner):
            continue
        cand = _BLOCK_NOISE_RE.sub(" ", _clean_for_name(inner))
        cand = re.sub(r"\s+", " ", cand).strip()
        if len(cand) > len(best):
            best = cand
    return best


def _is_noise_only(cand: str) -> bool:
    """这个候选是不是"没有信息量的残渣"（季度标签 / 语言标记 / 纯数字）。

    做法：把它按**已知噪声词**全剔掉、再去掉标点与星号/空白，
    还剩东西 → 是真名字；什么都不剩 → 是残渣。
    """
    s = _SEASON_TAG_RE.sub(" ", cand or "")
    s = _BLOCK_NOISE_RE.sub(" ", s)
    # 去掉标点 / 星号 / 空白 / 纯数字（`10月` 的月份数字也算噪声）
    s = re.sub(r"(?i)(\d+\s*月|\d+)", " ", s)
    s = re.sub(r"[★☆\s\W_]+", "", s).strip()
    return len(s) < 2


def guess_anime_names(title: str, limit: int = 3) -> list[str]:
    """从资源标题里猜番名，**多个候选**（按可信度从高到低）。

    为什么要多个：RSS 标题常把两个名字并排写在一起

        [黒ネズミたち] 青之芦苇 第二季 / Ao Ashi Season 2 - 09 (Baha 1920x1080 MP4)

    整串丢给 Bangumi 搜**什么都搜不到**，而两个名字**各自**都能
    精确命中；Bangumi 的搜索索引本身也认别名与原名（扫描侧就是靠罗马音
    目录名命中别名的，见 `scanner._build_keywords`）。拆开交给
    `SubjectMatcher.search_best` 逐个搜，命中率立刻不一样。

    顺序：**含汉字的候选排在前面**。库里的显示口径是"中文名优先"
    （`_subject_to_dict`），中文名拿去搜也更准（`season_mode="cn"`）。

    **和 `clean_anime_title` 分开**：那个还兼着 RSS 判新的模糊匹配
    （rss_matcher），改它的语义要整库回放核对；这里只服务「获取名称」
    与"新建条目时的匹配"。
    """
    cands = _split_candidates(_clean_for_name(title))
    if not cands:
        # 标题通篇是括号时，番名也在被丢掉的那堆里（见 `_pick_from_blocks`）
        cands = _split_candidates(_pick_from_blocks(title))
    else:
        # **兜底二：清洗结果是"季度标签 / 纯数字"这类残渣时，也回头看括号块**
        #
        # 判据：**所有候选都不含汉字、或全是季度/语言标记** → 视为没取到名字。
        # 真正的番名（中文、日文原名）一定同时含汉字或较长的拉丁词，
        # 而"★10月新番★"「简日双语」这类残渣含汉字但没有任何信息量 ——
        # 用"去掉这些标记后还剩不剩东西"来判断，比列黑名单更稳。
        if all(_is_noise_only(c) for c in cands):
            block = _split_candidates(_pick_from_blocks(title))
            if block:
                cands = block
    # 中文名优先，其余保持原顺序（`sorted` 稳定）
    cands.sort(key=lambda c: 0 if _CJK_RE.search(c) else 1)
    return cands[:limit]


def guess_anime_name(title: str) -> str:
    """单个名字（多候选里的第一个）。只想要一个字符串填进输入框时用它。

    **只认含汉字的候选**：多候选排序把中文名放在最前，但整条标题都是
    罗马音时，第一个候选就是罗马音 —— 那仍然比"整串带 `/` 的脏名字"好，
    所以照常返回它。
    """
    names = guess_anime_names(title, limit=1)
    return names[0] if names else ""


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
