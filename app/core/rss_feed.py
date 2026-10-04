"""RSS 抓取与解析（F19）。

- 标准库 xml.etree.ElementTree，兼容 RSS 2.0 与 Atom
- 从 enclosure.url / link / description 中提取磁力链或 .torrent 链接
- 单源请求间隔 ≥ 2s，避免被站点限流
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\s\"'<>]*", re.I)
# 以 .torrent 结尾的直链
TORRENT_RE = re.compile(r"https?://[^\s\"'<>]+\.torrent", re.I)

# Torrent 的 MIME 类型（enclosure 用它声明"这是个种子"）。
#
# **踩坑（实测：comicat 的订阅全部显示"没有下载链接"）**：
# 早期只认"`magnet:` 开头"或"`.torrent` 结尾"两种形态，而 comicat 的
# enclosure 长这样：
#     <enclosure url="http://v2.uploadbt.com/?r=down&amp;hash=0bcdf2..."
#                type="application/x-bittorrent" />
# 链接**既不是磁力链、也不以 .torrent 结尾**，两条正则全部落空 →
# 12 个条目判定为"没有可用链接"、10 条下发失败。而同一个地址喂给
# qBittorrent 自己的 RSS 下载器却能正常列出 12 个种子（实测对比），
# 说明链接本身是好的，是**我们没认出来**。
#
# 正解：**以 type 为准**。`application/x-bittorrent` 是 RSS 规范里
# 声明种子文件的标准 MIME，服务端已经明确告诉我们是种子了，
# 不该再去猜 URL 的形状。
TORRENT_MIME_TYPES = {
    "application/x-bittorrent",
    "application/x-bittorrent; charset=utf-8",
    "application/bittorrent",
}

NS_ATOM = "{http://www.w3.org/2005/Atom}"

# 单源最小请求间隔（秒）
MIN_REQUEST_INTERVAL = 2.0
_last_request_at = 0.0


@dataclass
class FeedEntry:
    """一条 RSS 条目。"""

    title: str
    link: str = ""
    magnet: str = ""
    torrent_url: str = ""
    published: str = ""

    @property
    def download_url(self) -> str:
        """优先磁力链，其次种子链接。"""
        return self.magnet or self.torrent_url


class RssError(RuntimeError):
    """RSS 抓取/解析失败。"""


def _throttle() -> None:
    """全局节流：保证相邻请求间隔 ≥ MIN_REQUEST_INTERVAL。"""
    global _last_request_at
    delta = time.monotonic() - _last_request_at
    if delta < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - delta)
    _last_request_at = time.monotonic()


def fetch(url: str, proxy: str = "", user_agent: str = "AnimeMarker/1.0",
          timeout: float = 15.0) -> str:
    """抓取 RSS 原文。"""
    _throttle()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            proxies=proxies,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        raise RssError(f"抓取失败：{e}") from e


def _text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def _extract_links(item: ET.Element) -> tuple[str, str]:
    """返回 (magnet, torrent_url)。依次尝试 enclosure / link / description。

    **识别优先级**（踩坑后重排，见 TORRENT_MIME_TYPES 的说明）：
      ① **enclosure + type="application/x-bittorrent"** —— 最可靠，
         服务端明确声明了"这是种子"。**不看 URL 形状**：comicat 的
         `?r=down&hash=xxx` 既无 magnet 也无 .torrent 后缀，只看形状
         必然漏掉（实测 12 条全漏）。
      ② enclosure 的 url 若本身是磁力链 → 也算（有些站点混用）
      ③ link / guid / description 里的 magnet: 或 .torrent
    """
    magnet = ""
    torrent = ""

    # ① + ②：先看 enclosure（带 type 声明的最可信）
    for enc in item.findall("enclosure"):
        u = (enc.get("url") or "").strip()
        if not u:
            continue
        etype = (enc.get("type") or "").strip().lower()
        if not magnet and u.lower().startswith("magnet:"):
            m = MAGNET_RE.search(u)
            if m:
                magnet = m.group(0)
            continue
        # **type 声明是种子 → 直接当 torrent_url 收下**（这是本函数的核心修复）
        if etype in TORRENT_MIME_TYPES and not torrent:
            torrent = u
            continue
        # 没写 type（或 type 不是种子）时，退回"猜形状"
        if not magnet:
            m = MAGNET_RE.search(u)
            if m:
                magnet = m.group(0)
                continue
        if not torrent:
            t = TORRENT_RE.search(u)
            if t:
                torrent = t.group(0)

    # ③ 其它位置的候选（兜底）
    candidates: list[str] = []
    # Atom: <link href="...">
    for ln in item.findall(f"{NS_ATOM}link"):
        href = ln.get("href") or ""
        if href:
            candidates.append(href)
    # 常见：<link>...</link>
    candidates.append(_text(item.find("link")))
    candidates.append(_text(item.find(f"{NS_ATOM}id")))
    # description / content 里常内嵌磁力链
    for tag in ("description", f"{NS_ATOM}content", f"{NS_ATOM}summary"):
        candidates.append(_text(item.find(tag)))

    for c in candidates:
        if not c:
            continue
        if not magnet:
            m = MAGNET_RE.search(c)
            if m:
                magnet = m.group(0)
        if not torrent:
            t = TORRENT_RE.search(c)
            if t:
                torrent = t.group(0)
        if magnet and torrent:
            break
    return magnet, torrent


def parse(xml_text: str) -> list[FeedEntry]:
    """解析 RSS 2.0 / Atom 文本为 FeedEntry 列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RssError(f"XML 解析失败：{e}") from e

    entries: list[FeedEntry] = []

    # RSS 2.0: rss/channel/item
    for item in root.iter("item"):
        title = _text(item.find("title"))
        magnet, torrent = _extract_links(item)
        link = _text(item.find("link"))
        pub = _text(item.find("pubDate"))
        if title:
            entries.append(
                FeedEntry(title=title, link=link, magnet=magnet,
                          torrent_url=torrent, published=pub)
            )

    # Atom: feed/entry
    for entry in root.iter(f"{NS_ATOM}entry"):
        title = _text(entry.find(f"{NS_ATOM}title"))
        magnet, torrent = _extract_links(entry)
        link = ""
        ln = entry.find(f"{NS_ATOM}link")
        if ln is not None:
            link = ln.get("href") or ""
        pub = _text(entry.find(f"{NS_ATOM}updated")) or _text(entry.find(f"{NS_ATOM}published"))
        if title:
            entries.append(
                FeedEntry(title=title, link=link, magnet=magnet,
                          torrent_url=torrent, published=pub)
            )

    if not entries:
        log.warning("RSS 未解析出任何条目（可能是 HTML 反爬页面而非订阅源）")
    return entries


def fetch_and_parse(
    url: str, proxy: str = "", user_agent: str = "AnimeMarker/1.0"
) -> list[FeedEntry]:
    """抓取 + 解析一步到位。"""
    return parse(fetch(url, proxy=proxy, user_agent=user_agent))
