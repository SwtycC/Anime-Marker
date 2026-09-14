"""Bangumi API 客户端。

- 所有 HTTP 调用统一封装在此
- 支持代理、重试、异常转换为 BangumiError
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class BangumiError(RuntimeError):
    """Bangumi API 业务异常。"""


class BangumiClient:
    """对 https://api.bgm.tv 的轻量封装。"""

    def __init__(
        self,
        token: str = "",
        api_base: str = "https://api.bgm.tv",
        proxy: str = "",
        user_agent: str = "AnimeMarker/1.0",
        timeout: float = 10.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=3, connect=3, read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
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

    # ---------- 底层 ----------
    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.api_base}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning("Bangumi GET %s 失败: %s", url, e)
            raise BangumiError(str(e)) from e

    def _post(self, path: str, json: Any = None) -> Any:
        url = f"{self.api_base}{path}"
        try:
            resp = self.session.post(url, json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as e:
            log.warning("Bangumi POST %s 失败: %s", url, e)
            raise BangumiError(str(e)) from e

    # ---------- 业务 ----------
    def search_subjects(self, keyword: str, limit: int = 10) -> list[dict]:
        """POST /v0/search/subjects。type=2 限定动画。"""
        body = {"keyword": keyword, "filter": {"type": [2]}}
        data = self._post("/v0/search/subjects", json=body)
        if isinstance(data, dict):
            return data.get("data", [])[:limit]
        return []

    def get_subject(self, subject_id: int) -> dict:
        return self._get(f"/v0/subjects/{subject_id}")

    def get_episodes(self, subject_id: int, limit: int = 200) -> list[dict]:
        data = self._get(f"/v0/subjects/{subject_id}/episodes", limit=limit, offset=0)
        if isinstance(data, dict):
            return data.get("data", [])
        return []

    def mark_episode_watched(self, subject_id: int, episode_id: int) -> bool:
        """type=2 表示「看过」。"""
        body = {"episode_id": [episode_id], "type": 2}
        self._post(f"/v0/users/-/collections/{subject_id}/episodes", json=body)
        return True

    def get_collection(self, subject_id: int) -> Optional[dict]:
        try:
            return self._get(f"/v0/users/-/collections/{subject_id}")
        except BangumiError:
            return None
