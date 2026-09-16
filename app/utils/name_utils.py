"""名称清洗工具：从 Bangumi 条目名中提取「相对系列的差异部分」。

用途：详情页「同系列」切换按钮，直接显示完整名称会过长撑破按钮。
例：
    系列「从零开始的异世界生活」+ 条目「Re：从零开始的异世界生活 第三季」
        → 「第三季」
    系列「在地下城寻求邂逅是否搞错了什么」+ 条目「在地下城寻求邂逅是否搞错了什么 第四季」
        → 「第四季」
"""

from __future__ import annotations

import re

# 季数/篇章标记
_SEASON_MARK_RE = re.compile(
    r"第[一二三四五六七八九十\d]+[季部]"
    r"|Season\s*\d+"
    r"|(?<![A-Za-z])S\d{1,2}(?![A-Za-z])"
    r"|\d+(?:st|nd|rd|th)\s*Season"
    r"|Part\s*\d+"
    r"|(?<![A-Za-z])(?:III|IV|IX|VIII|VII|VI|V|II|X)(?![A-Za-z])"
    r"|[^\s]{2,8}篇"
    r"|[^\s]{2,12}编"
    r"|剧场版"
    r"|Movie",
    re.IGNORECASE,
)


def short_name(full_name: str, series_name: str = "") -> str:
    """生成适合按钮显示的短名。

    优先返回「相对系列名的差异部分」；若无差异则退回完整名。
    注意：不修改名称本身（如「Re：从零开始…」的 Re 是作品名的一部分）。
    """
    name = (full_name or "").strip()
    series = (series_name or "").strip()
    if not name:
        return ""

    # ① 系列名是名称的前缀 → 取剩余部分
    if series and name.startswith(series):
        rest = name[len(series):].strip(" -_·・:：")
        # 只剩空/标点时，尝试从完整名里抽季数标记
        if rest:
            return rest

    # ② 从完整名中抽取季数/篇章标记
    marks = _SEASON_MARK_RE.findall(name)
    marks = [m.strip() for m in marks if m and m.strip()]
    if marks:
        # 取最后一个标记（通常「第三季」在末尾）
        return marks[-1]

    # ③ 系列名包含在名称中 → 去掉系列名部分
    if series and series in name:
        rest = name.replace(series, "").strip(" -_·・:：")
        if rest:
            return rest

    return name
