"""PotPlayer 窗口标题解析。

前提：用户在 PotPlayer 中配置了「选项 → 播放 → 窗口标题」包含播放时间/总时长字段，
典型格式形如 "00:12:34 / 00:24:00" 或 "12:34/24:00"。
"""

from __future__ import annotations

import re
from typing import Optional

# 默认匹配 "00:12:34 / 00:24:00" 或 "12:34/24:00"
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*/\s*(\d{1,2}:\d{2}(?::\d{2})?)"
)


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
