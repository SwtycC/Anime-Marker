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

        def _keep_short(m: re.Match) -> str:
            word = m.group(0)
            if head_word and word == head_word:
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
        # 「S1」「S02」这类短季数标记
        for m in re.finditer(r"(?i)(?<![A-Za-z])S(\d{1,2})(?![A-Za-z])", text):
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
# 以数字结尾：整名就是数字（01.mkv）或「... - 01」「[01]」「 01」等。
# 用 (?<!\d) 保证「12」整体被捕获（写成 [\s\-_\[\]]0*(\d+)$ 会切掉首位数字，
# 把 12.mkv 解析成 2）。
_EP_TAIL_RE = re.compile(r"(?<!\d)0*(\d+(?:\.\d+)?)(?:v\d+)?$")
# 独立数字（前后都不是字母数字）：如「[01]」夹在括号中
_EP_MID_RE = re.compile(r"(?<![\w.])0*(\d+(?:\.\d+)?)(?:v\d+)?(?![\w.])")

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


def is_extra_file(name: str) -> bool:
    """文件名是否为附属内容（预告/菜单/NCOP/PV 等），不应参与正片集数编号。

    例：「… Menu Vol.01 …」「… WEB予告 #01 …」「… NCOP …」「… 初日舞台挨拶 …」
    """
    stem = Path(name).stem if ("." in name and "\\" not in name) else name
    return any(re.search(p, stem) for p in EXTRA_FILE_PATTERNS)


def extract_ep_index(name: str) -> Optional[float]:
    """从文件名中提取集数序号（正片集数）。不属正片的返回 None。

    支持 01 / 第1话 / EP01 / 12.5。
    """
    stem = Path(name).stem

    # ① 明确的「第 X 话/集/回」写法优先
    m = _EP_CN_RE.search(stem)
    if m:
        return float(m.group(1))

    # ② 附属内容（预告/菜单/NCOP/PV…）不参与编号，避免它们占掉 max_idx
    #    导致后续 SP 顺延到更大的序号上（正片 6 → 6.5/7/7.5…）
    if is_extra_file(stem):
        return None

    # ③ EP01 / E01
    m = _EP_EN_RE.search(stem)
    if m:
        return float(m.group(1))

    # ④ 数字结尾（含整名就是数字的 01.mkv）
    m = _EP_TAIL_RE.search(stem)
    if m:
        return float(m.group(1))

    # ⑤ 独立数字（如「[01]」夹在括号中）
    m = _EP_MID_RE.search(stem)
    if m:
        return float(m.group(1))
    return None


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
    ) -> None:
        super().__init__()
        self.library_paths = [Path(p) for p in library_paths]
        self.api = api
        self.db = db
        self.season_mode = season_mode
        self.season_display = season_display
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
            # ③ 仅父级（兜底）
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
                cover_path = str(download_cover(bangumi_id, cover_url, self.api.session))
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
            match_state="auto",
        )
        self.log_message.emit(f"  ✓ {name_cn}（{result.score} 分：{result.reason}）")
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
        ep_map = {e.get("sort") or e.get("ep"): e for e in bgm_eps}

        # 三类文件：
        #   main  —— 正片（有明确集数序号）
        #   extra —— 附属内容（预告/菜单/NCOP/PV…），序号顺延排在正片之后
        #   plain —— 既非正片又非附属（无法识别集数的视频），序号顺延排在最后
        main_videos: list[tuple[float, Path]] = []
        extra_videos: list[tuple[str, Path]] = []
        plain_videos: list[tuple[str, Path]] = []
        for v in cand.video_files:
            idx = extract_ep_index(v.name)
            if idx is not None:
                main_videos.append((idx, v))
            elif is_extra_file(v.name):
                extra_videos.append((v.stem, v))
            else:
                plain_videos.append((v.stem, v))
        main_videos.sort(key=lambda x: x[0])
        extra_videos.sort(key=lambda x: x[0])
        plain_videos.sort(key=lambda x: x[0])

        # 附属/未知内容统一从 max+0.5 起顺延（保持 0.5 间隔，便于「第 6.5 集」式展示）
        max_idx = max((i for i, _ in main_videos), default=0)
        next_extra_idx = max_idx + 0.5

        for idx, v in main_videos:
            bgm = ep_map.get(idx) or {}
            self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=bgm.get("id"),
                ep_index=idx,
                title=bgm.get("name_cn") or bgm.get("name") or v.stem,
                file_path=str(v),
            )

        # 预告/菜单/NCOP 等：排在正片之后，不占用正片编号
        for title, v in extra_videos:
            self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=None,
                ep_index=next_extra_idx,
                title=title,
                file_path=str(v),
            )
            next_extra_idx += 0.5

        # 兜底：其余识别不出含义的视频，继续顺延（避免互相覆盖 file_path）
        for title, v in plain_videos:
            self.db.upsert_episode(
                subject_id=subject_id,
                bangumi_ep_id=None,
                ep_index=next_extra_idx,
                title=title,
                file_path=str(v),
            )
            next_extra_idx += 0.5
