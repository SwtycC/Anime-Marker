"""标题解析：PotPlayer 播放进度 + RSS/文件名集数与动漫名提取。

- parse_progress：解析 PotPlayer 窗口标题里的播放进度
- extract_episode_index / clean_anime_title：供 F19 RSS 判新使用
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
