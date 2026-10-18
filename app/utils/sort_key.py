"""海报墙排序用的键生成（纯函数，无 Qt 依赖）。

**排序规则**：

    1. 中文 → 转拼音全拼，按字母序比较
    2. 英文/拉丁字母 → 直接按字母序，与拼音混在同一个字母表里
       （所以 "CLANNAD"(c-l-a) 与「草莓」(cao-mei) 会挨在一起 ——
       它们都以 c 开头，"ca…" 比 "cl…" 靠前）
    3. 数字开头 → **全部排最后**
    4. 其它（符号、日文假名等）→ 也排最后，与数字同组

**为什么"数字排最后"要用前缀标记**：拼音/英文字母都是 a~z，
数字 0~9 的 ASCII 比字母小，直接比较会让它们**跑到最前面**。
给每类一个分组前缀（`0` = 正常、`1` = 排最后），一眼看得出意图。

**为什么英文不单独分组**：需求要的是"英文按首字母在拼音中排序"，
也就是**与中文混排**。若给英文单独一个前缀，就会变成"中文全部在前、
英文全部在后"，那是另一种语义。
"""

from __future__ import annotations

import re

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:          # pragma: no cover - 依赖缺失时的降级
    lazy_pinyin = None
    Style = None

#: 分组前缀：正常（字母开头）在前，数字/符号开头在后。
_GROUP_LETTER = "0"
_GROUP_OTHER = "1"

#: 用于判断"首个有效字符是不是数字"。名字可能以 `[` / `《` / 空格开头，
#: 所以先剥掉这些装饰再判断首字符。
_LEAD_STRIP_RE = re.compile(
    "^[\\s\\[\\(【（《「『"
    "\"'“”‘’"
    ",，\\.\\-_~!！?？:：;；]+")


def _strip_lead(text: str) -> str:
    """去掉开头的符号/空白，返回用于判断首字符的字符串。"""
    return _LEAD_STRIP_RE.sub("", text or "", count=1)


def has_letter_head(text: str) -> bool:
    """名字的首个有效字符是不是"字母类"（字母 / 汉字 / 假名）。

    数字、符号开头一律返回 False（→ 排最后）。
    """
    head = _strip_lead(text)
    if not head:
        return False
    ch = head[0]
    if ch.isdigit():
        return False
    return ch.isalnum() or "\u4e00" <= ch <= "\u9fff"


def pinyin_key(text: str) -> str:
    """把名字转成可用于字典序比较的**归一化串**。

    - 汉字 → 全拼（不带声调），如「青春猪头」→ `qingchunzhutou`
    - 英文 → 小写
    - 数字开头 → 加 `1` 前缀（排最后）
    - 其它 → 加 `1` 前缀（排最后）

    **依赖缺失时降级**：没有 `pypinyin` 就退回原串小写比较 ——
    顺序不理想（中文按码点），但不会崩。打包时务必带上依赖。
    """
    raw = (text or "").strip()
    if not raw:
        # 完全空的名字：排在"数字开头"之后（同组内按空串排最前）
        return _GROUP_OTHER

    if lazy_pinyin is not None:
        # Style.NORMAL：不带声调；errors="default"：非汉字原样保留
        parts = lazy_pinyin(raw, style=Style.NORMAL, errors="default")
        norm = "".join(parts).lower()
    else:
        norm = raw.lower()

    # **去掉开头的装饰符号**再比较：`【我推的孩子】` 若带着 `【`
    # 参与比较，`【`(U+3010) 的码点比字母大得多，会把它挤到字母组末尾。剥掉后它按
    # `wotuidehaizi` 正常落在 w 附近。
    norm = _strip_lead(norm)

    if has_letter_head(raw):
        return _GROUP_LETTER + norm
    return _GROUP_OTHER + norm


def display_name(name_cn: str, name: str) -> str:
    """排序时用哪个名字：**中文名优先**，没有才用原名。

    与 `ORDER BY name_cn, name` 的口径保持一致，否则会出现
    "按名称排了但顺序看着和卡片上的字对不上"。
    """
    return (name_cn or "").strip() or (name or "").strip()
