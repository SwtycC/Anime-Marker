"""Bangumi 请求日志的名称解析。

问题：`bangumi_api._get()` 只看到 URL（如 `/v0/subjects/116287/episodes`），
失败日志里只有一堆数字 ID，无法判断是哪个动漫出的问题：

    Bangumi GET https://api.bgm.tv/v0/subjects/116287/episodes 失败: 404

解决：在**后台线程发起请求前**把 subject_id → 「名称 [id]」注册进线程局部表，
`_get()` 记日志时即可把 ID 翻译成名称：

    Bangumi GET .../subjects/116287/episodes 失败（[无职转生 第二季] bgm=116287）: 404

接口：
    bind_subject(subject_id, name)  → 绑定一条（请求完成后自动换下一条）
    subject_label(subject_id)       → 取名称，未绑定时回退 "[bgm=116287]"
    describe(subject_id)            → 返回 "（name bgm=id）" 片段，直接拼进日志
    set_current_name(name)          → 换作品时绑定当前动漫名（ID 用名称兜底解析）

实现用 `threading.local`：每个 QThread 各持一份，绑定/换绑无需加锁。
"""

from __future__ import annotations

import threading
from typing import Optional

_state = threading.local()

# 线程局部状态：
#   _state.current_name → 当前正在匹配的动漫名（如「无职转生 第二季」）
#   _state.id_names     → {subject_id: name}
MAX_CACHED_IDS = 64


def _id_names() -> dict[int, str]:
    names = getattr(_state, "id_names", None)
    if names is None:
        names = {}
        _state.id_names = names
    return names


def set_current_name(name: str) -> None:
    """设置当前正在处理的动漫名（扫描/手动匹配在换作品时调用）。"""
    _state.current_name = (name or "").strip()


def current_name() -> str:
    return getattr(_state, "current_name", "") or ""


def bind_subject(subject_id: Optional[int], name: str = "") -> None:
    """绑定 subject_id 与名称。name 省略时用当前动漫名兜底。"""
    if not subject_id:
        return
    label = (name or current_name()).strip()
    if not label:
        return
    names = _id_names()
    if len(names) >= MAX_CACHED_IDS:
        names.clear()
    names[int(subject_id)] = label


def subject_label(subject_id: Optional[int]) -> str:
    """取 subject_id 对应的展示名；未绑定则回退 "[bgm=<id>]"。"""
    if not subject_id:
        return ""
    name = _id_names().get(int(subject_id)) or current_name()
    if name:
        return f"{name} bgm={subject_id}"
    return f"bgm={subject_id}"


def describe(subject_id: Optional[int]) -> str:
    """返回可直接拼进日志的片段：有 ID 则 "（name bgm=id）"，否则 "（name）"。"""
    if not subject_id:
        name = current_name()
        return f"（{name}）" if name else ""
    return f"（{subject_label(subject_id)}）"
