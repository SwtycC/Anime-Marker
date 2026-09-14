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
    """根据 subject_id 和 URL 计算缓存路径（不实际下载）。"""
    ext = _ext_from_url(url) or "jpg"
    return covers_dir() / f"{subject_id}.{ext}"


def download(
    subject_id: int,
    url: str,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> Path:
    """下载封面到缓存目录，返回本地路径。失败返回占位图路径。"""
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
