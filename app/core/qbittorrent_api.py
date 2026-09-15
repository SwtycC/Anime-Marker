"""qBittorrent Web UI 客户端（F19）。

- 依赖 qbittorrent-api（导入名 qbittorrentapi）
- 只做「推送任务 + 读状态」，不实现 BT 协议
- 连接失败不抛到 UI 崩溃，统一转换 QbError 并记录日志
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


class QbError(RuntimeError):
    """qBittorrent 交互异常。"""


@dataclass
class TorrentStatus:
    """任务状态（订阅页展示用）。"""

    name: str
    progress: float          # 0~1
    state: str
    dlspeed: int             # 字节/秒
    eta: int                 # 秒，8640000 表示未知
    hash: str
    category: str
    tags: str


class QbClient:
    """qBittorrent Web UI 封装。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        username: str = "admin",
        password: str = "",
        category: str = "Bangumi",
        save_path: str = "",
        webui_url: str = "",
        timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.category = category
        self.save_path = save_path
        self._webui_url = webui_url
        self.timeout = timeout
        self._client = None
        # 记录最近一次失败原因，避免日志被 urllib3 重试刷屏
        self.last_error = ""

    # ---------- 连接 ----------
    @property
    def webui_url(self) -> str:
        if self._webui_url:
            return self._webui_url
        return f"http://{self.host}:{self.port}"

    def _ensure_client(self):
        """惰性创建并登录。"""
        if self._client is not None:
            return self._client
        try:
            import qbittorrentapi
        except ImportError as e:  # 依赖缺失（如未装 requirements）
            raise QbError(
                "缺少依赖 qbittorrent-api，请执行：pip install qbittorrent-api"
            ) from e
        try:
            client = qbittorrentapi.Client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                REQUESTS_ARGS={
                    "timeout": self.timeout,
                    # 不重试：本机 Web UI 未开时立即失败，避免启动/操作被拖慢数秒
                    "adapter_kwargs": {"max_retries": 0},
                },
            )
            client.auth_log_in()
        except Exception as e:
            self.last_error = str(e)
            raise QbError(f"连接失败（{self.webui_url}）：{e}") from e
        self._client = client
        self.last_error = ""
        return client

    def test_connection(self) -> str:
        """连通性自检，返回 qBittorrent 版本号。"""
        client = self._ensure_client()
        try:
            version = client.app.version
            log.info("qBittorrent 连接成功，版本 %s", version)
            return str(version)
        except Exception as e:
            self._client = None
            raise QbError(f"读取 qBittorrent 版本失败：{e}") from e

    # ---------- 下发 ----------
    def add(
        self,
        url: str,
        tags: Optional[list[str]] = None,
        save_path: Optional[str] = None,
    ) -> None:
        """推送磁力链或 .torrent 链接。"""
        client = self._ensure_client()
        kwargs = {
            "urls": url,
            "category": self.category,
            "tags": tags or [],
            "save_path": save_path or self.save_path or None,
        }
        try:
            client.torrents_add(**kwargs)
            log.info("已推送任务到 qBittorrent：%s", url[:80])
        except Exception as e:
            self._client = None
            raise QbError(f"推送任务失败：{e}") from e

    def add_torrent_file(
        self,
        torrent_url: str,
        tags: Optional[list[str]] = None,
        save_path: Optional[str] = None,
    ) -> None:
        """下载 .torrent 到临时目录后以文件方式推送，推送后清理。"""
        client = self._ensure_client()
        tmp_path: Optional[Path] = None
        try:
            resp = requests.get(torrent_url, timeout=self.timeout)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as f:
                f.write(resp.content)
                tmp_path = Path(f.name)
            client.torrents_add(
                torrent_files=str(tmp_path),
                category=self.category,
                tags=tags or [],
                save_path=save_path or self.save_path or None,
            )
            log.info("已推送种子文件到 qBittorrent：%s", torrent_url[:80])
        except QbError:
            raise
        except Exception as e:
            self._client = None
            raise QbError(f"推送种子失败：{e}") from e
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def add_entry(
        self,
        magnet: str = "",
        torrent_url: str = "",
        tags: Optional[list[str]] = None,
        save_path: Optional[str] = None,
    ) -> None:
        """按可用链接二选一下发。"""
        if magnet:
            self.add(magnet, tags=tags, save_path=save_path)
        elif torrent_url:
            self.add_torrent_file(torrent_url, tags=tags, save_path=save_path)
        else:
            raise QbError("该条目没有可用的磁力链或种子链接")

    # ---------- 查询 ----------
    def list_torrents(self, tag: str = "") -> list[TorrentStatus]:
        """列出任务；tag 非空时按标签过滤（订阅名）。"""
        client = self._ensure_client()
        try:
            infos = client.torrents_info(tag=tag) if tag else client.torrents_info()
        except Exception as e:
            self._client = None
            raise QbError(f"读取任务列表失败：{e}") from e

        out: list[TorrentStatus] = []
        for t in infos:
            out.append(
                TorrentStatus(
                    name=getattr(t, "name", ""),
                    progress=float(getattr(t, "progress", 0.0) or 0.0),
                    state=str(getattr(t, "state", "")),
                    dlspeed=int(getattr(t, "dlspeed", 0) or 0),
                    eta=int(getattr(t, "eta", 0) or 0),
                    hash=str(getattr(t, "hash", "")),
                    category=str(getattr(t, "category", "") or ""),
                    tags=str(getattr(t, "tags", "") or ""),
                )
            )
        return out

    def has_torrent_like(self, title: str) -> bool:
        """二层查重：是否存在名称高度相似的任务。"""
        if not title:
            return False
        key = self._norm(title)
        for t in self.list_torrents():
            n = self._norm(t.name)
            if not n:
                continue
            if n == key or n in key or key in n:
                return True
        return False

    def find_by_hash(self, torrent_hash: str) -> Optional[TorrentStatus]:
        if not torrent_hash:
            return None
        for t in self.list_torrents():
            if t.hash == torrent_hash:
                return t
        return None

    @staticmethod
    def _norm(text: str) -> str:
        import re
        return re.sub(r"[\s\-_\.\[\]【】()（）]+", "", text).lower()
