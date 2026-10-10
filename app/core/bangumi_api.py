"""Bangumi API 客户端。

- 所有 HTTP 调用统一封装在此
- 支持代理、重试、异常转换为 BangumiError
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import USER_AGENT
from app.utils import bgm_log

log = logging.getLogger(__name__)

# 从 URL 里反解条目 ID（记日志时把 ID 换成动漫名）
_SUBJECT_ID_RE = re.compile(r"/v0/subjects/(\d+)")

# 条目类型（subject_type）
SUBJECT_TYPE_BOOK = 1
SUBJECT_TYPE_ANIME = 2
SUBJECT_TYPE_MUSIC = 3
SUBJECT_TYPE_GAME = 4
SUBJECT_TYPE_REAL = 6

# 收藏类型（type）
COLLECT_TYPE_WISH = 1      # 想看
COLLECT_TYPE_DONE = 2      # 看过
COLLECT_TYPE_DOING = 3     # 在看
COLLECT_TYPE_ON_HOLD = 4   # 搁置
COLLECT_TYPE_DROPPED = 5   # 抛弃

# 收藏类型的中文名。
#
# **唯一权威定义**：界面上那排「想看 / 看过 / 在看 / 搁置 / 抛弃」的状态选择
# 与后端日志都从这里取，避免两处各写一份字面量后漂移。
# 顺序即界面顺序（Bangumi 官网的排列：想看 → 看过 → 在看 → 搁置 → 抛弃）。
COLLECT_TYPE_NAMES: dict[int, str] = {
    COLLECT_TYPE_WISH: "想看",
    COLLECT_TYPE_DONE: "看过",
    COLLECT_TYPE_DOING: "在看",
    COLLECT_TYPE_ON_HOLD: "搁置",
    COLLECT_TYPE_DROPPED: "抛弃",
}


class BangumiError(RuntimeError):
    """Bangumi API 业务异常。"""


# 401/403 时给用户/日志看的那句话。**必须点明"过期"**：
# 正确的动作只有一个：到设置页重新生成一个填进来。所以这句话必须把
# **动作**说全，而不是把 requests 的原文
# （`401 Client Error: Unauthorized for url: ...`）直接甩出来 ——
# 那串东西看不出"该去换 Token"。
TOKEN_EXPIRED_HINT = "Token 已过期或无效（请到设置页重新填写 Access Token）"

# 读接口（拉收藏 / 逐集记录）专用的那句：除了换 Token，还得勾权限。
TOKEN_EXPIRED_READ_HINT = (
    "Token 已过期或无效（请到设置页重新生成，并勾选「读取收藏」权限）")


class BangumiAuthError(BangumiError):
    """Token 已过期 / 无效 / 权限不足（401/403）。"""


class BangumiNotFound(BangumiError):
    """HTTP 404 —— 资源不存在。

    **为什么值得单独一个异常类**：404 在 Bangumi 有两种截然不同的含义 ——
    「这个东西本来就没有」（该条目未收藏、路径里的用户/条目不匹配）与
    「请求本身发错了」（端点或占位符用错）。前者是**正常结论**，不该当成
    故障告警，更不该让调用方把"没查成"与"确实没有"混为一谈（混了会把
    网络故障写成本地快照，此后一直是错的，见 bridges/library 的收藏状态
    回查）。有了这个类，调用方可以 `except BangumiNotFound` 精确分开。

    它继承 `BangumiError`：所有既有的 `except BangumiError` 行为不变。
    """


def describe_connection_error(exc: Exception) -> str:
    """把连接类异常翻译成一句**能照着做**的提示；认不出来时返回空串。

    `bgm.tv` 在国内网络下会被 **DNS 污染 + SNI 阻断**。这类故障在日志里是一长串 urllib3 堆栈，
    原样丢到状态栏只会让人以为"软件坏了"，而实际该做的是**开代理**。

    因此按症状分类。**只给症状，不给动作** —— 具体该怎么办取决于
    "流量有没有走代理"（见 `_network_hint`），在这里猜容易给出错建议。
    详细原因仍进日志（`_get` / `_post` 里的 `log.warning` 保留原始异常文本）。
    """
    text = str(exc)
    low = text.lower()
    # 5xx 被重试耗尽：请求**已经到达服务器**，是它对端出错。
    # 别把用户引去查代理。
    m = re.search(r"too many (\d{3}) error responses", text)
    if m:
        return f"Bangumi 服务端故障（连续返回 {m.group(1)}）—— 与本地网络无关，稍后重试"
    if re.search(r"\b5\d\d\b server error", low):
        return "Bangumi 服务端故障（5xx）—— 与本地网络无关，稍后重试"
    if "too many 502" in text or "502" in text:
        return "拿到 502 响应（网关错误）"
    if "reset" in low or "aborted" in low or "eof occurred" in low:
        return "连接被重置（疑似被网络阻断）"
    if "timed out" in low or "timeout" in low:
        return "连接超时（疑似被阻断或节点不通）"
    if "ssl" in low or "certificate" in low:
        return "TLS 握手失败（疑似被中间设备干扰）"
    if "max retries" in low:
        return "多次重试仍失败（网络不通）"
    return ""


def is_server_side_error(exc: Exception) -> bool:
    """这个失败是不是**服务端**的问题（请求已到达服务器，只是它返回了 5xx）。

    用来决定提示里要不要追加"检查代理"：服务端故障时流量明明通了，
    再让用户去查代理/换节点只会误导。
    """
    text = str(exc)
    if re.search(r"too many \d{3} error responses", text):
        return True
    return bool(re.search(r"\b5\d\d\b", text)) and "server error" in text.lower()


# infobox 里"别名"行的 key。Bangumi 的简体/繁体 wiki 用词不同，都收进来；
# 小写 "alias" 是少数条目（尤其英文译名条目）的写法。
_ALIAS_KEYS = frozenset({"别名", "別名", "alias"})

# 过滤掉过短的别名：2 个字符以下的中文/英文简称（如 "AB"、"花"）
# 极易误命中其他作品，收益远小于风险。**注意按"字符数"而非"词数"算**，
# 因为中文别名一个字就是一个字符。
MIN_ALIAS_LEN = 3


def extract_aliases(subject: dict) -> list[str]:
    """从 Bangumi subject 响应的 `infobox` 里取出所有别名。

    `score_subject()` 对"名称完全不相关"的候选
    直接一票否决，而它原先只看 `name_cn` / `name` 两个字段。像
    「未闻花名」这种**本地文件夹用俗称、Bangumi 用全名**的场景：

        name_cn = "我们仍未知道那天所看见的花的名字。"
        name    = "あの日見た花の名前を僕達はまだ知らない。"
        别名    = ["未闻花名", "那朵花", "あの花", "ANOHANA", ...]

    三个正式名与"未闻花名"毫无字符重合 → 必然被判"名称不相关" → 转人工。
    而别名信息**本来就在搜索结果里**（`POST /v0/search/subjects` 的每条
    data 都带 `infobox`），零额外请求，只是从没被读过。

    infobox 结构：
        [
          {"key": "中文名", "value": "我们仍未知道那天所看见的花的名字。"},
          {"key": "别名",   "value": [{"v": "Anohana: The Flower..."},
                                       {"v": "那朵花"}, ...]},
          {"key": "话数",   "value": "11"},
          ...
        ]
    注意 `value` 的类型**不固定**：别名行是 `[{v: str}, ...]`，
    而「中文名」「话数」等是**纯字符串**。所以取值必须双分支处理，
    不能假定 `v.get()` 一定存在。

    返回：去重后的别名列表（保持原顺序）；没有别名时返回空列表。
    """
    rows = subject.get("infobox")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("key", "")).strip().lower() not in _ALIAS_KEYS:
            continue
        value = row.get("value")
        # 形态一：别名行 —— [{"v": "..."}, ...]
        if isinstance(value, list):
            items = [
                str(v.get("v", "")).strip()
                for v in value if isinstance(v, dict)
            ]
        # 形态二：纯字符串（少数条目如此）
        elif isinstance(value, str):
            items = [value.strip()]
        else:
            items = []
        for name in items:
            if len(name) < MIN_ALIAS_LEN or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


# infobox 里"动画制作"那一行的 key（Bangumi 的简繁/日文写法都收进来）。
#
# **故意不收「製作」/「制作」**：那一行是**制作委员会**，不是动画公司 ——
# 「白聖女と黒牧師」的 `製作` 是
# `「白聖女と黒牧師」製作委員会（講談社、Aniplex、Crunchyroll、動画工房）`，
# 拿来当公司名会得到一长串无意义文本（公司名只偶然出现在括号里）。
_STUDIO_KEYS = frozenset({
    "动画制作", "动画制作公司", "动画制作会社",
    "アニメーション制作", "アニメ制作", "アニメーション制作会社",
})

#: 动画制作公司词表：**官方名 → 其他写法**（日文名 / 英文名 / 中文俗称）。
#:
#: 两个用途：
#:   ① **认公司**：infobox 的「动画制作」写的是哪种写法都能归到同一个官方名
#:      （`京都アニメーション` / `京都动画` / `Kyoto Animation` → 同一家），
#:      否则同一家公司会在筛选面板里裂成好几个选项（各带一两部番）；
#:   ② **摘 tag**：认出"哪些 tag 是公司名"（见 is_studio_name），好在海报墙的
#:      标签筛选里把它们摘出去 —— 公司已在"制作公司"栏单列，再以 tag 的形式
#:      散落在"其他"栏里就是重复。
#:
#: 为什么必须是一份词表，而不是"看名字像不算像公司"：启发式只认得出
#: `StudioXXX` / `XXXPictures` 这类，而 MAPPA、ufotable、SHAFT、京阿尼、
#: MADHOUSE、J.C.STAFF 全都长得不像公司名。
#:
#: 表外的新公司 tag 不会被摘出去，会照常落在"其他"栏 —— 想收编就在这里加一行。
STUDIO_ALIASES: dict[str, tuple[str, ...]] = {
    "京都アニメーション": ("京都动画", "京都アニメ", "Kyoto Animation", "KyotoAnimation"),
    "動画工房": ("动画工房", "Doga Kobo", "DogaKobo"),
    "A-1 Pictures": ("A-1Pictures", "A1 Pictures", "A1Pictures"),
    "MAPPA": ("マッパ",),
    "ufotable": ("ユーフォーテーブル",),
    "WHITE FOX": ("WHITEFOX", "White Fox"),
    "Studio Bind": ("StudioBind", "スタジオバインド"),
    "MADHOUSE": ("MADHouse", "MAD HOUSE", "マッドハウス"),
    "J.C.STAFF": ("JCSTAFF", "JC Staff", "ジェー・シー・スタッフ"),
    "CloverWorks": ("クローバーワークス",),
    "WIT STUDIO": ("WITSTUDIO", "ウィットスタジオ"),
    "P.A.WORKS": ("PAWORKS", "PA Works", "ピーエーワークス"),
    "SHAFT": ("シャフト",),
    "SILVER LINK.": ("SILVERLINK.", "SILVER LINK", "シルバーリンク"),
    "8bit": ("エイトビット", "Eight Bit"),
    "Studio DEEN": ("studiodeen", "スタジオディーン"),
    "Studio五組": ("Studio五组", "スタジオ五組"),
    "Studio Gaina": ("StudioGaina", "ガイナ", "GAINA"),
    "Studio EEK": ("StudioEEK",),
    "C2C": (),
    "Nexus": ("ネクサス",),
    "Feel.": ("フィール",),
    "ZERO-G": ("ゼロジー",),
    "Passione": ("パッショーネ",),
    "Lerche": ("ラルケ",),
    "Bibury Animation Studios": ("BiburyAnimationStudios", "Bibury Animation",
                                 "ビブリーアニメーションスタジオ", "バイブリーアニメーションスタジオ"),
    "キネマシトラス": ("KINEMACITRUS", "KINEMA CITRUS"),
    "CygamesPictures": ("Cygames Pictures", "サイゲームスピクチャーズ"),
    "Production I.G": ("Production.I.G", "ProductionIG", "プロダクションI.G"),
    "SUNRISE": ("サンライズ",),
    "domerica": ("ドメリカ",),
    "TNK": ("ティー・エヌ・ケー",),
    "ドライブ": ("Drive",),
    "ハヤブサフィルム": ("Hayabusa Film",),
    "BONES": ("ボンズ",),
    "TRIGGER": ("トリガー",),
    "東映アニメーション": ("Toei Animation", "ToeiAnimation"),
    "スタジオジブリ": ("Studio Ghibli", "StudioGhibli", "ジブリ"),
    "CoMix Wave Films": ("CoMixWave", "CoMix Wave", "コミックス・ウェーブ"),
    "OLM": ("オー・エル・エム",),
    "ぴえろ": ("スタジオぴえろ", "Pierrot", "Studio Pierrot"),
    "シンエイ動画": ("Shin-Ei Animation", "ShinEi"),
    "トムス・エンタテインメント": ("TMS Entertainment", "TMS", "トムス"),
    "GAINAX": ("ガイナックス",),
    "手塚プロダクション": ("Tezuka Productions", "TezukaProduction"),
    "タツノコプロ": ("Tatsunoko", "竜の子プロダクション"),
    "サテライト": ("Satelight",),
    "ブレインズ・ベース": ("Brain'sBase", "Brains Base", "Brain's Base"),
    "ライデンフィルム": ("LIDEN FILMS", "LIDENFILMS"),
    "GONZO": ("ゴンゾ",),
    "亜細亜堂": ("亚细亚堂",),
    "スタジオコロリド": ("StudioColorido", "Studio Colorido"),
    "サイエンスSARU": ("ScienceSARU", "Science SARU"),
    "ラパントラック": ("LapinTrack", "Lapin Track"),
    "XEBEC": ("ジーベック",),
    "ディオメディア": ("Diomedea", "Diomedéa"),
    "ENGI": ("エンジ",),
    "スタジオKAI": ("StudioKAI", "Studio KAI"),
    "横浜アニメーションラボ": ("Yokohama Animation Lab",),
    "クラウドハーツ": ("Cloud Hearts",),
    "颱風グラフィックス": ("Typhoon Graphics",),
    "絵夢": ("絵梦", "绘梦", "Haoliners"),
    "サンジゲン": ("Sanzigen",),
    "ポリゴン・ピクチュアズ": ("Polygon Pictures", "PolygonPictures"),
    "オレンジ": ("Orange",),
    "ゼクシズ": ("ZEXCS",),
    "マングローブ": ("Manglobe",),
    "セブン・アークス": ("Seven Arcs", "SevenArcs"),
}


#: 有通行中文名 / 中文俗称的公司：`官方名 → 中文名`。
#: 展示时拼成「官方名（中文名）」（见 studio_label）—— 用户想看的是**这是哪家**，
#: 而 Bangumi 的「动画制作」多数写日文原名（京都アニメーション），
#: 对中文用户不如"京阿尼"直观。
#: 没有通行中文名的（MAPPA、SHAFT、A-1 Pictures…）不列，原样显示即可。
STUDIO_CN_NAMES: dict[str, str] = {
    "京都アニメーション": "京阿尼",
    "動画工房": "动画工房",
    "WHITE FOX": "白狐",
    "MADHOUSE": "疯屋",
    "ufotable": "飞碟社",
    "BONES": "骨头社",
    "TRIGGER": "扳机社",
    "東映アニメーション": "东映动画",
    "スタジオジブリ": "吉卜力",
    "ぴえろ": "小丑社",
    "シンエイ動画": "新锐动画",
    "手塚プロダクション": "手冢制作",
    "タツノコプロ": "龙之子",
    "亜細亜堂": "亚细亚堂",
    "横浜アニメーションラボ": "横滨动画",
    "颱風グラフィックス": "台风图形",
    "絵夢": "绘梦",
    "キネマシトラス": "橘子社",
    "SUNRISE": "日昇",
    "トムス・エンタテインメント": "TMS",
}


def _norm_studio(name: str) -> str:
    """公司名的比较用归一形：小写 + 去掉空格与标点。

    于是 `A-1 Pictures` / `A-1Pictures` / `a1 pictures` 是同一个键。
    `\\W` 在 Python 的 str 正则里是 Unicode 感知的，中文/日文名不受影响。
    """
    return re.sub(r"[\W_]+", "", (name or "").lower())


#: 归一后的写法 → 官方名（模块加载时算一次）
_STUDIO_LOOKUP: dict[str, str] = {
    _norm_studio(name): official
    for official, aliases in STUDIO_ALIASES.items()
    for name in (official, *aliases, STUDIO_CN_NAMES.get(official, ""))
    if _norm_studio(name)
}

#: 合作署名里常见的分隔符：「WIT STUDIO×CloverWorks」「A、B」。
#: **全角也要收**（`＆` U+FF06、`／` U+FF2F）
_STUDIO_SPLIT = re.compile(r"[×✕✗&＆+＋/／、,，;；]|\s+x\s+")


def is_studio_name(name: str) -> bool:
    """这个词是不是**已知的**动画公司名。

    海报墙用它把公司 tag（京阿尼、MAPPA、A-1Pictures…）从标签栏里摘掉 ——
    它们已经在"制作公司"栏单列了，再散落进"其他"里会重复。
    """
    return _norm_studio(name) in _STUDIO_LOOKUP


def _latin_alias(official: str) -> str:
    """官方名是**纯日文**（不含拉丁字母）时，挑一个拉丁写法当括号里的说明。

    让 `ドライブ` 显示成 `ドライブ（Drive）`、`ティー・エヌ・ケー` 显示成
    `ティー・エヌ・ケー（TNK）` —— 中文名没有，但英文名总比片假名好认。
    官方名本身含拉丁字母的（`SILVER LINK.`、`MAPPA`）不需要这一手。
    """
    if re.search(r"[A-Za-z]", official):
        return ""
    for alias in STUDIO_ALIASES.get(official, ()):
        if re.search(r"[A-Za-z]", alias):
            return alias
    return ""


def studio_label(name: str) -> str:
    """公司名 → 展示用标签：`官方名（中文名）`，没有中文名时就是官方名。

    例：`京都アニメーション` → `京都アニメーション（京阿尼）`；
        `ドライブ` → `ドライブ（Drive）`；`MAPPA` → `MAPPA`；
        表外的（`Project No.9`）原样返回。

    **这个标签是存进数据库的**（subjects.studio），不是渲染时才拼 ——
    省得每次筛选都算，也省得界面层再持有第二份词表（见 database.set_subject_studio）。
    """
    text = (name or "").strip()
    if not text:
        return ""
    official = _STUDIO_LOOKUP.get(_norm_studio(text), text)
    cn = STUDIO_CN_NAMES.get(official, "") or _latin_alias(official)
    if cn and _norm_studio(cn) != _norm_studio(official):
        return "%s（%s）" % (official, cn)
    return official


def extract_studio(subject: dict) -> str:
    """从 Bangumi subject 响应的 infobox 里取**动画制作**（= 制作公司）。

    **只认 `动画制作` 这一行**（含日文/繁体的同义写法，见 _STUDIO_KEYS）：
    infobox 里的 `製作` 是**制作委员会**（一长串出资方），不是制作公司，
    拿它当公司名会得到无意义的文本。取不到就返回空串 —— 该条目在海报墙的
    "制作公司"栏里不出现，不拿别处的数据凑。

    取到的名字先经词表归一（`京都アニメーション` = `京都动画` = `Kyoto
    Animation`），再按 `studio_label` 拼上中文名 —— 于是同一种写法的
    不同拼写会**并成同一个筛选项**，面板上看到的是
    「京都アニメーション（京阿尼）」这种一眼能认出来的形式。
    表外的公司原样保留（`Project No.9` 就显示 `Project No.9`）。

    合作署名（`WIT STUDIO×CloverWorks`）拆成多个名字、用 ` / ` 连接 ——
    海报墙按 ` / ` 拆开来分别匹配，按其中任一家都能筛到。
    """
    for row in subject.get("infobox") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("key", "")).strip() not in _STUDIO_KEYS:
            continue
        value = row.get("value")
        # 与别名行同样：value 可能是 [{"v": str}, ...] 也可能是纯字符串
        if isinstance(value, list):
            items = [str(v.get("v", "")).strip()
                     for v in value if isinstance(v, dict)]
        elif isinstance(value, str):
            items = [value.strip()]
        else:
            items = []
        labels: list[str] = []
        for part in items:
            for name in _STUDIO_SPLIT.split(part):
                label = studio_label(name)
                if label and label not in labels:
                    labels.append(label)
        if labels:
            return " / ".join(labels)
    return ""


#: `/v0/subjects/{id}/persons` 里"动画制作"这个关系名的各种写法
_STUDIO_RELATIONS = frozenset({
    "动画制作", "アニメーション制作", "アニメ制作", "动画制作公司",
})


class BangumiClient:
    """对 https://api.bgm.tv 的轻量封装。"""

    def __init__(
        self,
        token: str = "",
        api_base: str = "https://api.bgm.tv",
        proxy: str = "",
        user_agent: str = USER_AGENT,   # 合规 UA 见 app/__init__.py 的说明
        # 单次请求的超时（连接 + 读取）。
        timeout: float = 6.0,
        fast_probe: bool = False,
        # 配置里的「用户 ID」—— 即 `/v0/me` 的 `username` 字段（**不是昵称**）。
        #
        # 只有少数端点需要它（如 `GET /v0/users/{username}/collections/{id}`，
        # 那里 `-` 占位符不生效，见 get_collection），所以允许留空：
        # 留空时由 `resolve_username()` 用 Token 调 `/v0/me` 现解析（带缓存）。
        username: str = "",
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        # 是否配了 Token —— 决定"能不能写远端"。
        #
        # **不能靠 `self._api is None` 判断**：`app/qml_app.py` 无论有没有
        # Token 都会构造一个客户端（只是不带 Authorization 头），于是
        # "没配 Token"的用户点收藏状态会走到 POST → 401 → 回滚，
        # 看起来就是"点了没反应"。有这个标志，后端才能改走"只写本地"的
        # 分支（见 bridges/library.setCollectType 的模式 ②）。
        self.has_token = bool(token)
        self._configured_username = (username or "").strip()
        self._resolved_username = ""
        self.session = requests.Session()
        # **fast_probe：给"交互式探测"用的快速模式**（设置页「检测」按钮）。
        #
        # 默认的重试策略是 `total=3, connect=3`，
        # 即连接超时会被重试 **4 次** —— 单次 10s 超时下总共要等约 40 秒。
        # 后台批量任务能忍受（它只是慢），但用户点一下「检测」按钮却要
        # 盯着转圈 40 秒，体感就是"卡死"。探测场景**不需要重试**：
        # 一次连不上，结论就是"网络不通/需要代理"，重试 3 次不会改变结论。
        if fast_probe:
            retry = Retry(
                total=0,
                backoff_factor=0,
                status_forcelist=(),
                allowed_methods=frozenset(["GET"]),
            )
        else:
            # **连接重试从 3 次降到 1 次**：
            # 原策略 `connect=3` = 连接不通时重试 3 次（共 4 次尝试），
            # 单次 6s 超时下最坏也要约 24 秒才报错 —— 而"连不上"这种失败
            # 重试几乎不会改变结论（实测日志里三次全是同一个 ConnectTimeout）。
            # read=3 保留：读超时可能是服务端偶发慢，重试有意义。
            # 这样最坏等待 ≈ 6s×2（连接）+ 少量读重试。
            retry = Retry(
                total=3, connect=1, read=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                # PATCH 也要列进来：标记单集看过用的是 PATCH（见 mark_episode_watched）。
                # 该操作是幂等的（把某集置为"看过"），重试不会产生副作用。
                allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
            )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            # ---- **必须同时关掉 trust_env**----
            #
            # 只设 `session.proxies` 是不够的：requests 合并时**环境代理会
            # 覆盖它**。`Session.merge_environment_settings()` 里先
            # `proxies.setdefault(k, env_proxies[k])` 把环境代理塞进来，
            # 紧接着 `proxies = merge_setting(proxies, self.proxies)` ——
            # 而 `merge_setting(request_setting, session_setting)` 是
            # **request_setting（= 环境那份）赢**。Windows 上"环境代理"
            # 就是「Internet 选项」注册表里那个（本机 127.0.0.1:7890），
            # 于是设置页填了代理也会被系统代理顶掉。
            #
            # 语义与设置页那行的说明对齐（"留空则用系统代理"）：
            #   填了 → 只用填的这个（trust_env=False，环境/系统代理一概不参与）
            #   留空 → 不动 trust_env，照旧走系统代理
            #
            # 代价（可接受）：trust_env 还管着 `REQUESTS_CA_BUNDLE` 之类的
            # 环境证书设置。本应用没有任何 `verify=` 配置，用不上那些。
            # 这个会话也被封面下载复用（cover_cache.download 传的就是它），
            # 一并受益 —— 否则会出现"API 走代理、封面不走"的半截状态。
            self.session.trust_env = False

    # ---------- 底层 ----------
    @staticmethod
    def _subject_label(path: str, params: dict) -> str:
        """反解条目 ID，返回可直接拼进日志的「（动漫名 bgm=id）」。

        两种来源：
        ① 路径里的 ID（`/v0/subjects/{id}`）；
        ② 查询参数里的 `subject_id`（`/v0/episodes?subject_id=`）。
        """
        m = _SUBJECT_ID_RE.search(path)
        if m:
            return bgm_log.describe(int(m.group(1)))
        sid = params.get("subject_id")
        if sid:
            return bgm_log.describe(int(sid))
        return ""

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.api_base}{path}"
        label = self._subject_label(path, params)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", 0)
            if status in (401, 403):
                log.warning("Bangumi GET %s 鉴权失败（Token 已过期或无效）: "
                            "%s%s", url, status, label)
                raise BangumiAuthError(TOKEN_EXPIRED_READ_HINT) from e
            if status >= 500:
                # 单个 5xx（没走重试路径时）：同样是服务端的问题，
                # 不要让它显示成"网络错误"而把用户引去查代理
                log.warning("Bangumi GET %s 服务端错误 %s%s: %s",
                            url, status, label, e)
                raise BangumiError(
                    f"Bangumi 服务端故障（HTTP {status}）—— 与本地网络无关，稍后重试"
                ) from e
            if status == 404:
                # 404 降到 debug：它对**某些调用是正常结论**（未收藏的条目
                # 回查就是这个响应，见 get_collection），按 warning 记会让
                # 日志被这类"正常"刷屏。真正需要人看的 404 由调用方自己
                # 决定记不记（收到 BangumiNotFound 想告警就告警）。
                log.debug("Bangumi GET %s 404%s: %s", url, label, e)
                # 消息里保留 requests 的原文（含 "404 Client Error"）：
                # bridges/inprogress 靠 `"404" in msg` 判断"用户名填成了昵称"。
                raise BangumiNotFound(str(e)) from e
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(str(e)) from e
        except requests.RequestException as e:
            # 连接类故障 → 换成能照着做的提示（原始异常已在上面进日志）
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(self._network_hint(e, url)) from e

    def _patch(self, path: str, json: Any = None) -> Any:
        """PATCH 请求（与 `_post` 同形）。

        单独写一个是因为 Bangumi 的"更新"类端点用 PATCH（如章节收藏），
        复用一个 `_request(method, ...)` 反而更绕。
        """
        url = f"{self.api_base}{path}"
        label = self._subject_label(path, {})
        try:
            resp = self.session.patch(url, json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as e:
            if self._auth_status(e):
                raise self._auth_error("PATCH", url, label, e)
            log.warning("Bangumi PATCH %s 失败%s: %s", url, label, e)
            raise BangumiError(self._network_hint(e, url)) from e

    def _post(self, path: str, json: Any = None) -> Any:
        url = f"{self.api_base}{path}"
        # POST 的订阅 ID 在 body / 路径里，这里只处理路径形式（如标记单集已看）
        label = self._subject_label(path, {})
        try:
            resp = self.session.post(url, json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as e:
            if self._auth_status(e):
                raise self._auth_error("POST", url, label, e)
            log.warning("Bangumi POST %s 失败%s: %s", url, label, e)
            raise BangumiError(self._network_hint(e, url)) from e

    # 写接口的 401/403 必须和读接口一样分类成 `BangumiAuthError`：
    # 原先它们只抛一个普通的 `BangumiError`，里面裹着 requests 的原文
    #     Bangumi PATCH .../episodes 失败: 401 Client Error: Unauthorized for url: ...
    # 于是调用方只能把它当成"网络类失败"处理 —— 「补传」告诉用户
    # "可再点一次只补失败的"（Token 都过期了，点一百次也一样 ✗），
    # 播放监控告诉用户"可用「上传」补齐"（同上 ✗）。分不开类就只能误报。
    @staticmethod
    def _auth_status(e: Exception) -> bool:
        """这个 HTTP 错误是不是鉴权类的（401/403）。"""
        return getattr(getattr(e, "response", None), "status_code", 0) in (401, 403)

    @staticmethod
    def _auth_error(method: str, url: str, label: str,
                    e: Exception) -> BangumiAuthError:
        """记一条能直接照着做的日志，并造出对应的异常。"""
        log.warning("Bangumi %s %s 鉴权失败（Token 已过期或无效，"
                    "请到设置页重新填写）: %s%s", method, url, label, e)
        return BangumiAuthError(TOKEN_EXPIRED_HINT)

    def _network_hint(self, exc: Exception, url: str) -> str:
        """连接失败的用户可读提示，并补一句"流量到底走没走代理"。

        用户看到网络错误后，跑去代理客户端
        里**反复换节点**，但问题根本不在节点 —— 代理客户端的**分流规则**把
        `bgm.tv` 判给了直连（它是国内域名，常被国内规则集收录），于是
        流量压根没进隧道，换哪个节点都一样。不点明这一点，用户会在错误的
        方向上试很久。

        `session.proxies` 为空时 requests 会退回环境变量 / 系统代理，
        所以要用 `get_environ_proxies` 看**实际生效**的代理再下结论
        （否则会把"用着系统代理"误报成"未走代理"）。
        """
        hint = describe_connection_error(exc)
        if not hint:
            return str(exc)
        if is_server_side_error(exc):
            # 服务端 5xx：流量已经打通了，别再让用户去查代理/换节点
            return hint
        proxies = dict(self.session.proxies)
        if not proxies:
            try:
                proxies = requests.utils.get_environ_proxies(url) or {}
            except Exception:
                proxies = {}
        addr = proxies.get("https") or proxies.get("http") or ""
        if addr:
            # 去掉 scheme 少占几个字符（状态栏是单行，右端会被省略号截断）
            shown = addr.split("://")[-1]
            hint += f" —— 已走代理 {shown}"
        # 这里只说清"是什么错"，具体怎么配代理交给文档与设置页说明。
        return hint

    # ---------- 业务 ----------
    def search_subjects(self, keyword: str, limit: int = 10) -> list[dict]:
        """POST /v0/search/subjects。type=2 限定动画。

        **返回的每条都带 `infobox`**，其中「别名」行就是我们要的
        别名列表 —— 见模块级 `extract_aliases()`。**零额外请求**。
        """
        body = {"keyword": keyword, "filter": {"type": [2]}}
        data = self._post("/v0/search/subjects", json=body)
        if isinstance(data, dict):
            return data.get("data", [])[:limit]
        return []

    def get_subject(self, subject_id: int) -> dict:
        bgm_log.bind_subject(subject_id)
        return self._get(f"/v0/subjects/{subject_id}")

    # ---------- 制作公司（动画制作）----------
    def studio_from_persons(self, subject_id: int) -> str:
        """`GET /v0/subjects/{id}/persons` → 动画制作公司；取不到返回空串。

        infobox 里那一行 `动画制作`**只有一部分条目有**。而 Bangumi 网页
        左栏显示的「动画制作: 京都アニメーション」来自**制作人员**这份数据，
        它只在这个端点里（`relation == "动画制作"`，`type=2` 表示公司）。
        只读 infobox 的话，覆盖率约三分之一，名作反而漏掉。

        多个公司（合作署名）用 ` / ` 连接，与 infobox 那条路保持一致。
        """
        try:
            rows = self._get(f"/v0/subjects/{subject_id}/persons")
        except Exception as e:
            # 拉不到不算错误：公司是展示性数据，缺了就缺了（扫描不因此中断）
            log.warning("拉取制作人员失败 subject_id=%s: %s", subject_id, e)
            return ""
        if not isinstance(rows, list):
            return ""
        # 优先取公司（type=2）；只有个人署名时再退回全体
        companies = [
            str(r.get("name") or "").strip()
            for r in rows
            if isinstance(r, dict)
            and str(r.get("relation") or "").strip() in _STUDIO_RELATIONS
            and r.get("type") == 2
        ]
        if not companies:
            companies = [
                str(r.get("name") or "").strip()
                for r in rows
                if isinstance(r, dict)
                and str(r.get("relation") or "").strip() in _STUDIO_RELATIONS
            ]
        labels: list[str] = []
        for name in companies:
            label = studio_label(name)
            if label and label not in labels:
                labels.append(label)
        return " / ".join(labels)

    def studio_for(self, subject: dict, subject_id: int) -> str:
        """条目响应 → 制作公司：先解析 infobox（**零额外请求**），缺失时再查一次。

        优先 infobox 是因为多数情况下它就在搜索响应里（扫描时白拿）；
        只有它没有「动画制作」时才多发一个 `/persons` 请求 ——
        约三分之二的条目需要这一下（见 studio_from_persons 的说明）。
        结果由调用方写进数据库，之后不再请求。
        """
        studio = extract_studio(subject or {})
        return studio or self.studio_from_persons(subject_id)

    def get_episodes(
        self,
        subject_id: int,
        limit: int = 200,
        include_specials: bool = False,
    ) -> list[dict]:
        """拉取条目集数列表。

        端点：`GET /v0/episodes?subject_id=<id>`。

        **注意**：早期版本用的是 `/v0/subjects/{id}/episodes`，该端点目前已失效，
        一律返回 404（响应体是默认的 "This is default response..."），
        表现为「集标题全部退化成文件名、Bangumi 集 ID 为空、无法自动标记看过」。
        改用 `/v0/episodes?subject_id=` 后正常返回，字段结构（`id`/`sort`/`ep`/`name_cn`）完全一致。

        `include_specials=False`（默认）时过滤掉 type≠0 的条目：type 0=正片、
        1=SP、2=OP、3=ED、4=预告 等，只保留正片更贴合「本地文件数 ↔ 正片集数」的比对。
        """
        # 绑定 ID ↔ 名称，失败日志里就能看出是哪个动漫（见 utils/bgm_log.py）
        bgm_log.bind_subject(subject_id)
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            page_size = min(limit - len(out), 100)
            data = self._get(
                "/v0/episodes",
                subject_id=subject_id,
                limit=page_size,
                offset=offset,
            )
            if not isinstance(data, dict):
                break
            page = data.get("data", [])
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        if not include_specials:
            out = [e for e in out if (e.get("type") or 0) == 0]
        return out[:limit]

    def mark_episode_watched(self, subject_id: int, episode_id: int) -> bool:
        """把一集标记为「看过」（`type=2`）。

        端点路径与请求体早期就写对了，
        但方法写成了 **`POST`** ✗ —— 服务端返回
        **404 `{"title":"Not Found","details":{"path":...}}`**，
        这是"**路径/方法不存在**"（和"资源不存在"不是一回事 ✗），
        表现为"看完的自动标记、以及补传全都失败，却看着像条目 ID 不对"。

        以官方 spec（`https://bangumi.github.io/api/dist.json`）为准：
            PATCH /v0/users/-/collections/{subject_id}/episodes
                  body {"episode_id": [<int>...], "type": EpisodeCollectionType}
        （同路径的 `PUT /v0/users/-/collections/-/episodes/{episode_id}`
          是单集版，body 只要 `{"type": N}`；本项目用批量版，一次一集。）
        该文档还说：**PATCH 会顺便重算条目的完成度** ✓
        """
        body = {"episode_id": [episode_id], "type": 2}
        self._patch(f"/v0/users/-/collections/{subject_id}/episodes", json=body)
        return True

    def get_collection(self, subject_id: int) -> Optional[dict]:
        """回查该条目在 Bangumi 的收藏记录；**未收藏返回 None**。

        端点：`GET /v0/users/{username}/collections/{subject_id}`

        **路径里必须用真实 username，不能用 `-`**：`-`（"当前 Token 对应用户"）这个占位符 Bangumi 只在**部分
        路由**上实现，同一个 `/collections/{subject_id}` 路径下两个方法的
        表现恰好相反 ——

            GET  /v0/users/-/collections/506677          → 404（永远）
            GET  /v0/users/933287/collections/506677     → 200 type=2
            GET  /v0/users/-/collections/506677/episodes → 200 ✅（读集数用，没错）
            POST /v0/users/-/collections/506677          → 400（路由存在，只是 body 不合法）
            POST /v0/users/933287/collections/506677     → 404 ❌（**写不能用 username**）

        即：**这个路径 GET 认 username、POST 只认 `-`**，不能凭"同族接口一样"推断。

        "未收藏"就是 404（官方语义），所以这里吞掉 BangumiNotFound 返回
        None；**其它错误照抛** —— 调用方要能区分"确实没收藏"与"没查成"，
        否则网络故障会被写成本地快照，此后一直是错的。
        """
        username = self.resolve_username()
        try:
            data = self._get(
                f"/v0/users/{username}/collections/{int(subject_id)}")
        except BangumiNotFound:
            return None
        return data if isinstance(data, dict) else None

    def set_collection_type(self, subject_id: int, collect_type: int) -> None:
        """设置该条目在 Bangumi 的收藏状态（想看/看过/在看/搁置/抛弃）。

        端点与方法以官方 spec 为准：
            POST /v0/users/-/collections/{subject_id}
                 body {"type": <int 1~5>}

        **为什么用 POST 而不是 PATCH**：与集级收藏（`mark_episode_watched`
        用 PATCH）不同，条目级这个端点在 spec 里就是 POST ，所以要按端点各查
        一次，不能凭"同族接口方法一样"推断。POST 是幂等的：条目还没收藏
        时它会新建收藏行，已收藏时更新 type。

        与 `get_collection` 的关系：那个用来在本地没有缓存时回查真实状态
        （见 bridges/library 的收藏状态 worker），这里是写入。

        `subject_id` 是 **Bangumi 条目 ID**（不是本地 subjects.id），
        传错会 404 或改到别的条目上 —— 调用方必须先确认条目已 match。

        **这里的 `-` 是对的，别顺手"统一"成 username**：同一个
        `/v0/users/{x}/collections/{subject_id}` 路径，GET 只认真实
        username、**POST 只认 `-`**（换成 username 会 404）。
        """
        if collect_type not in COLLECT_TYPE_NAMES:
            raise ValueError(f"非法的收藏状态：{collect_type}")
        body = {"type": int(collect_type)}
        self._post(f"/v0/users/-/collections/{int(subject_id)}", json=body)
        log.info("已设置收藏状态：bgm=%s → %s（%s）",
                 subject_id, collect_type,
                 COLLECT_TYPE_NAMES.get(collect_type, "?"))

    # ---------- F18：用户在看列表 ----------
    def resolve_username(self) -> str:
        """当前 Token 对应用户的 `username`（路径参数用），带缓存。

        **要的是 `username` 而不是昵称**：接口路径里的那个值是账号的
        `username` 字段，而界面上
        显示的是 `nickname` —— 拿昵称去请求会 404「用户不存在」。

        解析顺序与那边一致（先权威、后配置）：`/v0/me` 优先，失败才退回
        配置值；两者都拿不到就抛错。**每次进程只解析一次**（缓存结果），
        否则每次回查收藏都要多打一个 `/v0/me`。
        """
        if self._resolved_username:
            return self._resolved_username
        me = self.get_me() or {}
        username = str(me.get("username") or "").strip()
        if not username:
            username = self._configured_username
            if username:
                log.info("未能用 Token 解析 username，改用配置值：%r", username)
        if not username:
            raise BangumiError(
                "无法确定 Bangumi 用户名（Token 无效且设置里没填用户 ID）")
        self._resolved_username = username
        return username

    def get_me(self) -> Optional[dict]:
        """GET /v0/me，用 Token 解析当前用户（未配置 username 时使用）。
        """
        try:
            data = self._get("/v0/me")
            return data if isinstance(data, dict) else None
        except BangumiAuthError as e:
            # 401/403：这才是 Token 本身的问题
            log.warning("解析当前用户失败（Token 已过期或无效）: %s", e)
            return None
        except BangumiError as e:
            # 5xx / 连接失败：与 Token 无关，别让用户去重新生成
            log.warning("获取当前用户失败（未能验证 Token，与 Token 无关）: %s", e)
            return None

    def get_user_collections(
        self,
        username: str,
        subject_type: int = SUBJECT_TYPE_ANIME,
        collect_type: Optional[int] = COLLECT_TYPE_DOING,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """GET /v0/users/{username}/collections。

        默认取「动画 + 在看」（subject_type=2, type=3）。
        `collect_type=None` → **不传 `type`** → 官方 OpenAPI 的默认行为
        「全部收藏状态」（想看/在看/看过/搁置/抛弃 一次拿全）。

        `None` 之所以等价于"不传"：`requests` 在编码查询串时会丢弃值为
        `None` 的参数（`models._encode_params` 里的 `if v is not None`），
        所以这里直接把 `None` 透传即可，不必拼两个分支。
        """
        if not username:
            raise BangumiError("未配置 Bangumi 用户名，且无法从 Token 解析")
        data = self._get(
            f"/v0/users/{username}/collections",
            subject_type=subject_type,
            type=collect_type,
            limit=limit,
            offset=offset,
        )
        if isinstance(data, dict):
            return data.get("data", [])
        if isinstance(data, list):
            return data
        return []

    def iter_user_collections(
        self,
        username: str,
        subject_type: int = SUBJECT_TYPE_ANIME,
        collect_type: Optional[int] = COLLECT_TYPE_DOING,
        page_size: int = 50,
        max_items: int = 500,
    ) -> list[dict]:
        """分页拉取收藏（带 max_items 上限保护）。

        `collect_type=None` 表示全部状态 —— 注意此时 `max_items` 是**所有
        状态共用一个额度**，触顶会在尾部静默截断（调用方拿到的是不完整
        名单）。触顶时记一条 warning，避免"收藏变少了"这种无声故障。
        """
        out: list[dict] = []
        offset = 0
        truncated = False
        while len(out) < max_items:
            page = self.get_user_collections(
                username, subject_type, collect_type, page_size, offset
            )
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break                      # 已翻到底
            offset += page_size
        else:
            # 循环因 len(out) >= max_items 退出，且上一页是满的 —— 说明
            # 后面还有数据没取
            truncated = True
        if truncated:
            log.warning("收藏分页达到上限 %s 条，尾部条目被截断"
                        "（collect_type=%s）", max_items, collect_type)
            out = out[:max_items]
        return out

    # ---------- F20：集级观看记录 ----------
    def get_subject_episode_collections(
        self,
        subject_id: int,
        episode_type: int = 0,
        limit: int = 500,
    ) -> list[dict]:
        """拉取某条目下**当前用户**的逐集收藏状态。

        端点：`GET /v0/users/-/collections/{subject_id}/episodes`
        （路径里的 `-` 表示"当前 Token 对应的用户"，无需 username）

        参数 `episode_type`：0=正片、1=SP、2=OP、3=ED、4=预告。
        默认只取正片，避免 OP/ED 混进「观看记录」时间线。

        返回每项结构（字段名以文档为准）：
            {
              "episode": { "id", "ep", "sort", "name", "name_cn", "airdate", ... },
              "type": 2,               # 2 = 看过
              "updated_at": 1786707528 # 该集被标记「看过」的时间（Unix 秒）
            }

        结论：**保留两级兜底**（单集 `updated_at` → 动漫收藏级 `updated_at`），
        见 `bridges/inprogress.py`。真实时间戳缺失时若没有兜底，整批记录
        会因"无时间"被丢弃。
        """
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            page_size = min(limit - len(out), 100)
            data = self._get(
                f"/v0/users/-/collections/{subject_id}/episodes",
                episode_type=episode_type,
                limit=page_size,
                offset=offset,
            )
            if not isinstance(data, dict):
                break
            page = data.get("data", [])
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out[:limit]
