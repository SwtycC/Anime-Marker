"""封面下载与缓存。

- 输入 Bangumi 封面 URL（lain.bgm.tv）
- 缓存文件名：<subject_id>.<ext>
- 下载失败时使用本地占位图
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from app.utils.paths import covers_dir, resource_path

log = logging.getLogger(__name__)

PLACEHOLDER = resource_path("icons/placeholder_cover.png")


def _ext_from_url(url: str) -> str:
    """从 URL 推断扩展名。"""
    url = url.split("?")[0].split("#")[0]
    name = url.rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()[:4]
    return "jpg"


def cover_path_for(subject_id: int, url: str) -> Path:
    """按 **subject_id** 计算原版封面缓存路径（不实际下载）。

    注意调用方是谁：`scanner` / `match` 传的是 **bangumi_id**，
    因此原版缓存文件**已经**按 bangumi_id 命名；只有 `library` 侧的
    「恢复原版海报」才用本函数的 subject_id 形式（见 `known_cover_files`）。
    两者混用会出现"路径推得出来但文件不存在"（见下方说明）。

    扩展名与 `_ext_from_url` 一致（URL 末段的后缀），需要"扩展名无关"
    地找文件时请用 `first_existing`。
    """
    ext = _ext_from_url(url) or "jpg"
    return covers_dir() / f"{subject_id}.{ext}"


def first_existing(paths) -> Path | None:
    """返回第一个真实存在的路径；都不存在返回 None。

    用途：封面文件名的扩展名由**下载时的 URL** 决定，而调用方往往只能
    反推出一个"猜的"扩展名（如 URL 改了、或源图从 jpg 变 webp）。
    按猜测的单一扩展名去找会漏，这里按候选顺序逐个探测。
    """
    for p in paths:
        if p is None:
            continue
        try:
            if Path(p).exists():
                return Path(p)
        except OSError:
            continue
    return None


def known_cover_files(*keys: int) -> list[Path]:
    """按 `covers/<key_count>.<ext>` 规则列出所有**已存在**的候选文件。

    传入多个 key（如 `subject_id` 与 `bangumi_id`）时，按**传入顺序**
    返回存在的文件 —— `covers` 目录里两种命名的文件都可能存在（历史原因：
    扫描/匹配按 bangumi_id 写，恢复原版按 subject_id 找），调用方应按
    自己的优先级取第一个。
    """
    out: list[Path] = []
    for key in keys:
        if not key:
            continue
        try:
            # 扩展名不限，jpg / png / webp 都收
            matches = sorted(covers_dir().glob(f"{int(key)}.*"))
        except OSError:
            continue
        for m in matches:
            if m.exists() and m not in out:
                out.append(m)
    return out


def download(
    subject_id: int,
    url: str,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> Path:
    """下载封面到缓存目录，返回本地路径。失败返回占位图路径。

    参数名沿用 `subject_id`，但**实际调用方传的是 bangumi_id**
    （scanner / match 都是），文件名因此是 `covers/<bangumi_id>.<ext>`。
    """
    dst = cover_path_for(subject_id, url)
    if dst.exists():
        return dst

    s = session or requests
    try:
        resp = s.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        log.info("封面下载完成 subject_id=%s path=%s", subject_id, dst)
        return dst
    except Exception as e:
        log.warning("封面下载失败 subject_id=%s url=%s err=%s", subject_id, url, e)
        return PLACEHOLDER
