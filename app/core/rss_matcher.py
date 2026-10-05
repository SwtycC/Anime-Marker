"""新集判定 / 两层查重 / 下载规则决策（F19）。

对应 §5.10.2 与 §8.4：
1. 标题清洗 → 提取动漫名 + 集数序号
2. 两层查重：本地媒体库 → qBittorrent 任务
3. 按该订阅自己的规则（`rss_sources.rule`：只下新集 / 全部下载）决定是否下发

注：原第 3 层"download_history"已按实测需求去掉，见 `_dedup`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.config import Config
from app.core.database import Database, RssSource
from app.core.qbittorrent_api import QbClient
from app.core.rss_feed import FeedEntry
from app.utils.title_parser import extract_episode_index, clean_anime_title

log = logging.getLogger(__name__)

# 下载规则（**只剩两个**）
#
#
# **踩坑（实测，很隐蔽）**：`RULE_ALL` 原先**没有被定义**，`VALID_RULES`
# 里也没有 `"all"` —— 而界面上的「全部下载」存的正是 `"all"`。
# 于是用户选「全部下载」后，`_resolve_rule` 把它当成未知规则，
# **静默回退成 new_only**（只打一条 warning 日志）：
#     用户看到的现象是"两个选项选哪个都一样"。
# 现在补上定义并加进 VALID_RULES。
RULE_NEW_ONLY = "new_only"        # 只下本地媒体库没有的集
RULE_ALL = "all"                  # 不看本地，没下载过就下（补齐用）

VALID_RULES = {
    RULE_NEW_ONLY, RULE_ALL,
}

# 合集 / 整包关键词
PACK_PATTERNS = [
    r"(?i)\b(合集|全集|Complete|Batch|BDRip\s*全集|Fin)\b",
    r"(?i)\b(01\s*[-~]\s*\d{2})\b",       # 01-12
    r"(?i)\b(Vol\.?\s*\d+\s*[-~]\s*\d+)\b",
]
PACK_RE = [re.compile(p) for p in PACK_PATTERNS]

# 季度 / 年份等噪声（用于与本地条目比对）
SEASON_RE = re.compile(r"(?i)\b(S\d{1,2}|Season\s*\d+|第[一二三四五六七八九十\d]+季)\b")

# 过滤词的分隔符：逗号 / 顿号 / 分号 / 换行 / 竖线
#
# **不要用空格分隔**：过滤词常常自带空格（如「简体 1080p」「CHS MP4」），
# 按空格切会把一个词拆成两半、"必须包含"永远命不中。
_TERM_SEP_RE = re.compile(r"[,，、;；\n\r|]+")


def _split_terms(text: str) -> list[str]:
    """把过滤词串切成小写词表（去空、去重、保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for part in _TERM_SEP_RE.split(text or ""):
        w = part.strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


# Windows 文件名非法字符（新建下载目录时要摘掉，否则 mkdir 会抛 OSError）。
#
# **不能只替换 `/` 和 `\`**：Windows 还禁 `< > : " | ? *`，而且**结尾的
# 点与空格**同样非法（`CLANNAD .` 会让 mkdir 失败）。番名里带 `?` `:`
# 的不少（如「Fate/stay night」「Re:从零开始」）。
_ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# 季数表达：第N季 / 第N部分 / 第N期 / SN / Season N / Nth season / Ⅱ Ⅲ Ⅳ…
#
# **只用于"系列目录下的子目录名"**（方案 A），不参与任何匹配逻辑 ——
# 匹配靠 Bangumi 与标题清洗，这里纯粹是给文件找个不重名的落脚点。
#
# **顺序有讲究**（`_season_dir_name` 取**第一个**命中的）：越具体的排前面。
_SEASON_HINTS = [
    # ① 「第N季/期/部/篇」——最明确，优先
    r"第\s*[0-9一二三四五六七八九十]+\s*[季期部篇]",
    r"(?i)\bS(?:eason)?\s*\d+\b",
    r"(?i)\b\d+(?:st|nd|rd|th)\s+season\b",
    # 罗马数字（Ⅱ Ⅲ Ⅳ …）
    r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+",
    # ② 中文「篇章名 + 篇」——**必须排在最后且要求前置字非空**，
    #    否则会把「第N篇」里的单个"篇"字、以及番名里恰好以"篇"结尾的字
    #    （如某些作品名）一起吞掉。
    #    实测案例：'鬼灭之刃 刀匠村篇' → 期望 '刀匠村篇'。
    #    用 `[^\s]{1,6}篇` 限长，避免把整句都吃进来。
    r"[^\s]{1,6}篇",
]
_SEASON_RES = [re.compile(p) for p in _SEASON_HINTS]


def _split_series_season(full_name: str) -> tuple[str, str]:
    """把条目名切成 `(系列名, 季部分)`；没有季数标记时返回 `("", "")`。
    """
    s = (full_name or "").strip()
    if not s:
        return "", ""
    for rx in _SEASON_RES:
        m = rx.search(s)
        if not m:
            continue
        season = m.group(0).strip()
        series = (s[:m.start()] + s[m.end():]).strip(" -_/。，,、")
        # 系列名太短就没有意义（如名字开头就是「第二季」）——
        # 宁可退回平铺，也不要建一个叫「第」的目录。
        if len(series.strip()) >= 2:
            return series.strip(), season
    return "", ""


def _season_dir_name(full_name: str, series: str) -> str:
    r"""从条目名里摘出"季"的部分，作为系列目录下的子目录名。


    **为什么要单独摘一层**（而不是直接拿条目名当子目录）：
    条目名常常是「系列名 + 季」甚至再拼上外文名（如
    '青之芦苇 第二季 / Ao Ashi Season -'）。直接用来建目录会得到
    一长串带斜杠的怪名字；而 `_safe_dir_name` 会把 `/` 换成空格，
    变成 '青之芦苇 第二季 Ao Ashi Season -' —— 与用户既有的
    '第二季'、'S2' 风格不一致，也不整洁。

    **摘不到就退回整名**：像「CLANNAD 〜AFTER STORY〜」这种"篇"不叫季，
    它有自己完整的名字，直接用它反而更清楚。

    **注意剥掉系列名再摘**：`series='Overlord'` 时若整名里同时含
    'Overlord' 与 'S2'，直接搜 `S\d` 没问题；但某些名字里系列名本身
    就带数字（如 '86 -不存在的战区-'），先把系列名剥掉能避免误摘。
    """
    s = (full_name or "").strip()
    if not s:
        return ""
    rest = s
    if series and series in rest:
        rest = rest.replace(series, " ").strip(" -_/")
    for rx in _SEASON_RES:
        m = rx.search(rest)
        if m:
            seg = m.group(0).strip()
            # 统一成"第N季"风格（罗马数字/英文季数不做转换 —— 用户的
            # 目录里 S1/S2 与本名混用很常见，保持原样最不容易认错）
            return _safe_dir_name(seg) or _safe_dir_name(s)
    return _safe_dir_name(s)


def _safe_dir_name(name: str) -> str:
    """把条目名清理成**可用的目录名**（摘非法字符、去尾点空格、截断）。

    返回空串表示这个名字没法用（调用方应退回默认保存路径）。
    """
    s = _ILLEGAL_NAME_RE.sub(" ", name or "")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")                       # Windows 不允许结尾是点/空格
    if not s:
        return ""
    # 目录名不是越短越好，但过长会撑爆路径上限（Windows 260）。
    # 取 80：足够放下常见番名 + 季数后缀，又留足上级路径空间。
    return s[:80].rstrip(". ")


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

    def __init__(self, db: Database, qb: Optional[QbClient], config: Config,
                 assume_enabled: bool = False) -> None:
        self.db = db
        self.qb = qb
        self.config = config

        #: 预览用：**假装这个订阅已经启用**（跳过"下载器未保存"与
        #: "自动下载已关闭"两个闸门），但仍然真跑查重。
        self.assume_enabled = assume_enabled

    # ---------- 标题过滤（纯过滤，与判新无关）----------
    def title_filtered(self, title: str, source: RssSource) -> str:
        """标题是否被"必须包含 / 必须不包含"筛掉；返回非空 = 被筛掉的原因。

        **为什么不并进 `judge` 的判新逻辑里**：过滤是"这条内容我根本不想要"，
        与"这集下过没有"是两个维度 —— 分开之后：
          - 界面能单独告诉用户"被规则过滤掉了几条"，而不是混进"跳过"里；
          - `judge` 的语义保持干净（它只回答"要不要下这一集"）。

        规则：
          must_include —— **全部**命中才算通过（多个词是"且"的关系）。
                          写成"且"而不是"或"：用户填多个词时通常是想要
                          更精确的筛选（如「简日」+「1080p」），
                          "或"会让筛选形同虚设（命中任意一个都放行）。
          must_exclude —— **任一**命中即筛掉（多了任何一个都不要）。

        分隔符：逗号 / 顿号 / 换行 / 分号都认（用户可能从别处粘一串）。
        大小写不敏感（番名里的英文大小写常不统一）。
        """
        t = (title or "").lower()
        if not t:
            return ""

        includes = _split_terms(getattr(source, "must_include", ""))
        if includes:
            for kw in includes:
                if kw not in t:
                    return f"必须包含「{kw}」未命中"
        excludes = _split_terms(getattr(source, "must_exclude", ""))
        for kw in excludes:
            if kw in t:
                return f"命中「必须不包含」的「{kw}」"
        return ""

    # ---------- 主入口 ----------
    def judge(self, entry: FeedEntry, source: RssSource) -> JudgeResult:
        """判定单条 RSS 条目。"""
        title = entry.title or ""
        ep_index = extract_episode_index(title)
        is_pack = self._is_pack(title)

        # 判新要在**本地 episodes** 上做（查"这一集本地有没有"），所以这里
        # 要的是**本地 subjects.id**，不是 bangumi_id。
        #
        # **踩坑（实测）**：早期写的是 `source.bangumi_id or 模糊匹配` ——
        # 而 `_match_local_subject` 返回的和下面的 `list_local_ep_indices`
        # 需要的都是**本地主键**，两者混用会让"已绑定的订阅"查错条目
        # （用 bangumi_id 去当本地 id 查，永远查不到 → 判新失效、重复下载）。
        #
        # 现在优先取绑定关系（v10 起存 local_subject_id），没绑定才退回
        # 用标题模糊匹配本地条目。
        subj_id = (source.local_subject_id
                   or self._match_local_subject(title))

        # 集数解析失败：一律不自动下载（风险应对：防误判下错集）
        if ep_index is None and not is_pack:
            log.info("集数解析失败，转人工确认：%s", title)
            return JudgeResult(entry, 0.0, False, False,
                               "集数解析失败，需人工确认", subj_id, False)

        ep_for_db = float(ep_index) if ep_index is not None else 0.0

        # **规则必须在查重之前解析**（踩坑）：`_dedup` 要看规则 ——
        # `all` 要**整套跳过查重**，`new_only` 才走那两层。
        # 早期把 `_resolve_rule` 放在查重之后，于是查重永远拿不到规则、
        # 两个规则效果相同（实测反馈"行为一样"）。
        rule = self._resolve_rule(source)

        # ---- 顺序：闸门/开关在前，查重在后 ----
        #
        # 两级"要不要下"的判断：
        #   前置闸门（本节）—— 订阅**是否启用**（manual / 下载器未保存 /
        #                     自动下载开关）。与具体某一集无关，是整体状态。
        #   查重（下一节）  —— 这一集**是否已经有了**（本地 / qB）。
        #
        # 顺序定成"闸门在前"是为了让预览能说清原因：没启用时，用户最需
        # 知道的是"还没启用"，而不是让他误以为查重没跑（详见下面那段）。
        #
        # ---- 「下载器」闸门（v13）----
        #
        # "下载器必须点保存才能启用这个订阅的下载，
        # 相当于一个'启用'开关"。没点过保存就只入库为待确认，
        # **不往 qBittorrent 下发** —— 避免用户还没想好过滤规则/保存位置，
        # 刚加完订阅就被下一堆东西（还可能落到默认目录里）。
        #
        # 判据用独立的 `downloader_saved` 标记而不是"过滤词是否为空"：
        # 用户可能**就是**不想过滤（两个框留空），那也是一种有效的保存。
        #
        # **位置必须在查重之前**（实测修复）：早期这段放在 `_dedup` 之后，
        # 于是"没保存过"的订阅在预览里**每一条都返回这句话** —— 查重
        # 算出来的结论（本地已有 / qB 有同名任务）被这个原因文案盖掉，
        # 用户看到 12 条一模一样的"未配置下载器"，误以为查重根本没跑。
        # 而预览的**唯一用途**就是"我保存之后会下哪些"，恰恰发生在
        # `downloader_saved == 0` 的时候 —— 放在后面等于预览永远失效。
        #
        # 移到前面后语义仍然正确：先回答"这集要不要"（闸门/开关/查重），
        # 只是把"由于没启用所以不下"这类**前置原因**优先说清楚。
        # `assume_enabled`（预览）时跳过下面两个闸门 —— 见 __init__ 说明。
        # 预览必须跳过：用户在**还没保存**时点预览，问的就是"保存后会下
        # 哪些"；若这里照拦，预览会把每条都报成"未配置下载器"，等于没用。
        if not self.assume_enabled:
            if not getattr(source, "downloader_saved", 0):
                return JudgeResult(entry, ep_for_db, True, False,
                                   "未配置下载器（点「下载器」保存后才会下载）",
                                   subj_id, is_pack)

            auto = self.config.getbool("rss", "auto_download", False)
            if not auto:
                return JudgeResult(entry, ep_for_db, True, False,
                                   "自动下载已关闭，入库为待确认", subj_id, is_pack)

        # ---- 查重（两层）----
        #
        # ---- 「全部下载」：完全不做查重 ----
        #
        # 这与 `new_only` 的区别是"两个世界"：
        #   new_only —— 两层查重全走（本地已有 / qB 任务名）
        #   all      —— 一层都不走，筛完过滤词就下
        #
        # **代价要知道**：`all` 会重复下发已下过的集（这正是它的用途），
        # 所以它不该被当成日常规则用；界面上也要能看出这个差别。
        #
        # 注：「只下新集」与「全部下载」的差别**不在这里**，而是在
        # `_dedup` 的第①层（本地媒体库那一层只有 new_only 才检查）。
        # 走到这一步说明该集是"确实需要下载的新内容"，两个规则在此
        # 行为一致 —— 这正是期望的结果。
        if rule != RULE_ALL:
            hit = self._dedup(entry, source, subj_id, ep_for_db, is_pack, rule)
            if hit:
                return JudgeResult(entry, ep_for_db, False, False, hit,
                                   subj_id, is_pack)

        # 走到这里 = 允许下载。
        #
        # 两条规则在此汇合：
        #   `new_only` —— 上面查重的两层都没命中（本地没有、qB 也没有）；
        #   `RULE_ALL` —— 本来就**不走查重**（它的语义就是"不参考本地，
        #                 把订阅源里的内容全拉一份"），直接落到这里。
        # 所以这里**不需要按规则分支**，一个默认返回同时服务两者。
        reason = "整包资源命中" if is_pack else "判定为新集"
        return JudgeResult(entry, ep_for_db, True, True, reason, subj_id, is_pack)

    # ---------- 两层查重 ----------
    def _dedup(
        self,
        entry: FeedEntry,
        source: RssSource,
        subject_id: Optional[int],
        ep_index: float,
        is_pack: bool,
        rule: str,
    ) -> str:
        """返回非空字符串表示命中（已被下载/已存在）。

        **只在 `new_only` 下被调用**（`all` 在 `judge` 里就跳过了整套查重，
        见那里的说明），所以这里不再判断规则。

        **两层**都是"这集我这儿已经有了"的不同口径：
          ① 本地媒体库 —— 硬盘上有文件（可能是别处拷来的，程序没下过）
          ② qBittorrent 现有任务 —— 正在下或下完了
        （原第③层"下载历史"已按实测需求去掉，见下方说明）
        """
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

        # **没有第 ③ 层**（实测需求："本程序下载过就跳过不需要"）。
        #
        # 早期这里还有一层「下载历史中已存在」，去掉了。原因：
        #   - 这一层与 ①② 高度重叠 —— 下过的集要么文件已在媒体库
        #     （① 命中），要么 qB 里还有任务（② 命中），真正"只有下载
        #     记录能挡住"的只剩"下完就删了源文件、qB 任务也清了"这种
        #     边角情形；
        #   - 用户明确表示不需要这种拦截。
        #
        # **代价（写在这里免得以后当成 bug 来查）**：上面那种边角情形
        # 下，重新检查时会**再下一次**。如果需要"无论如何都不重下"，
        # 用 `all` 之外的行为无法表达 —— 这是刻意接受的取舍。
        return ""

    # ---------- 保存路径（下载到指定条目的目录）----------
    def plan_save_path(self, source: RssSource) -> tuple[str, str, bool]:
        """**纯推算**这次下载用哪个目录，**不创建任何东西**。

        返回 `(路径, 说明文案, 目录是否已存在)`：
          - 两个字符串都为空 = 交回 qBittorrent 的全局设置；
          - 说明文案直接给界面显示（"「xx」的目录：\nF:\\…"），
            **由这里算而不是 QML 拼**，否则界面与真正落盘的目录
            会变成两套逻辑，迟早对不上（用户看到 A、文件却进了 B）。

        """
        sid = int(getattr(source, "save_subject_id", 0)
                  or source.local_subject_id or 0)
        if not sid:
            return "", "", False
        subj = self.db.get_subject(sid)
        if subj is None:
            # 条目不存在（如它被删了，而订阅还记着这个 id）。
            # **不在这里打 warning**：本方法会被界面的预览频繁调用，
            # 每次刷新都刷一行日志没有意义；真正下发时的告警
            # 留在 resolve_save_path（那里才是"出问题"的地方）。
            return "", "", False

        label = subj.name_cn or subj.name or ""

        raw = (subj.folder_path or "").strip()
        if raw:
            p = Path(raw)
            if p.is_dir():
                return str(p), "「%s」的目录：\n%s" % (label, p), True
            # 已记路径但目录不在了（用户移动/改名）：不猜、不建
            return "", "", False

        # ---- 没有路径（订阅源新建的条目）→ 按「系列」归位----
        roots = self._library_roots()
        if not roots:
            return "", "", False
        root = roots[0]
        full_name = _safe_dir_name(subj.name_cn or subj.name or "")
        if not full_name:
            return "", "", False

        # 系列名优先用**扫描得来**的（那是磁盘上的真实结构，比从名字猜准）；
        # 扫描没有（"从订阅源新建"、从未扫描过的条目）就**从条目名推断**。
        series = _safe_dir_name(getattr(subj, "series_name", "") or "")
        season = ""
        if not series:
            guess_series, guess_season = _split_series_season(full_name)
            series = _safe_dir_name(guess_series)
            season = guess_season

        if series:
            if not season:
                season = _season_dir_name(full_name, series)
            target = root / series / _safe_dir_name(season)
        else:
            # 真的推不出系列（名字里没有季数标记，如「CLANNAD」）→
            # 平铺。此时它本身就是一整部作品，不该硬套一层。
            target = root / full_name
        exists = target.is_dir()
        note = ("「%s」的目录：\n%s" % (label, target))
        return str(target), note, exists

    def resolve_save_path(self, source: RssSource) -> str:
        """算出这次下载该用哪个保存路径；空串 = 不干预（用 qB 全局设置）。

        **推算交给 `plan_save_path`**（那里是纯计算，界面预览也用同一份
        逻辑），这里只负责**把目录建出来** —— 真正的下发需要目录存在。

        **推算规则全在 `plan_save_path`**（含"按系列归位"的方案 、
        以及"已有 folder_path 就原样用、不擅自改"的取舍），
        本方法只额外做一件事：**目录不存在就建出来**。

        **为什么要建**：qBittorrent 收到不存在的 `save_path` 时行为不一致
        （有的版本自动建、有的直接把文件丢到默认目录），与其赌版本，
        不如我们建好再给 —— 创建失败则返回空串（退回默认），
        并在日志说明，不让"建目录失败"演变成"下载到莫名其妙的地方"。
        """
        path, note, exists = self.plan_save_path(source)
        if not path:
            # 推算不出：说清是"哪个原因"掉进默认路径的，便于排查
            log.warning("订阅 #%s 算不出保存路径，用 qBittorrent 默认设置",
                        source.id)
            return ""
        if exists:
            return path

        # 目录还不存在（订阅源新建的条目）→ 建出来。
        #
        # **已记路径且真实存在**的情况在 plan_save_path 里就返回了、
        # 不会走到这里 —— 那种目录是用户磁盘上真实的结构，不该动它。
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            log.info("已创建下载目录：%s", path)
        except OSError as e:
            log.warning("创建下载目录失败（%s）：%s，用默认保存路径", path, e)
            return ""

        # ---- 把算出的目录**写回条目**（关键，见下方说明）----
        #
        # **为什么必须写回**：`startSubject`（详情页「重新扫描该条目」）
        # 的入口判据就是 `subjects.folder_path` ——
        #     folder = (subj.folder_path or "").strip()
        #     if not folder: self.failed.emit("该条目没有记录目录路径，无法重新扫描")
        # 而"从订阅源新建"的条目 `folder_path` 是空的，于是形成死循环：
        #     没有 folder_path → 算出的目录只用于这次下载、不落库
        #     → 用户下载完想扫一遍看看集数 → 「重新扫描」直接报错
        #     → 条目永远没有集数（详情页一直显示"没有集数"），
        #        `total_eps` 也一直是 0（连"是否看完"都判不了）。
        #
        # 写回之后这条链路就闭合了：下载 → 目录落库 → 可重新扫描 →
        # 拿到集数与标题。
        #
        # **只在"原来没有路径"时写**（本方法走到这里必然如此，
        # 因为已有路径的分支在上面 `exists` 就 return 了）——
        # 不会覆盖用户磁盘上既有的真实结构。
        #
        # 失败只记日志：写不进去不该让整次下载失败（文件照样下到 path）。
        try:
            sid = int(getattr(source, "save_subject_id", 0)
                      or source.local_subject_id or 0)
            if sid:
                self.db.update_rss_subject_folder(sid, path)
                log.info("已把下载目录记入条目 #%s：%s", sid, path)
        except Exception as e:          # pragma: no cover - 防御性
            log.warning("写回条目目录失败（不影响本次下载）：%s", e)
        return path

    def _library_roots(self) -> list[Path]:
        """配置里的媒体库根目录（可能有多个，取第一个已有的）。"""
        try:
            from app.utils.paths import project_root  # noqa: F401
            raw = self.config.get("general", "library_path", "")
        except Exception:              # pragma: no cover - 防御性
            return []
        out: list[Path] = []
        for part in (raw or "").split(";"):
            p = part.strip()
            if p:
                out.append(Path(p))
        return out

    def _is_already_local(self, subject_id: Optional[int], ep_index: float) -> bool:
        if subject_id is None:
            return False
        return ep_index in self.db.list_local_ep_indices(subject_id)

    # ---------- 辅助 ----------
    def _resolve_rule(self, source: RssSource) -> str:
        """取该订阅的下载规则；未知值回退 `new_only`。

        **不再有"全局默认规则"**（原 `config.get("rss", "rule")`）：设置页
        那排规则按钮已删，规则完全由**每个订阅自己**持有（`rss_sources.rule`，
        添加/编辑订阅时在表单里选）。旧配置里的 `rss.rule` 一并弃用，
        避免出现"界面上没有、行为却跟着它走"的隐性来源。

        回退仍然保留：`source.rule` 为空（极老的数据）或存着已废弃的
        `fill_gap` / `complete_pack` / `manual` 时，照常当作 `new_only`，
        不让整轮轮询因为一个无法识别的字符串而中断。
        """
        rule = (source.rule or "").strip()
        if rule not in VALID_RULES:
            if rule:
                log.warning("订阅 #%s 的规则 %r 无效（已废弃或为空），回退 new_only",
                            source.id, rule)
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
