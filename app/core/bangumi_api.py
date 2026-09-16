"""Bangumi API 客户端。

- 所有 HTTP 调用统一封装在此
- 支持代理、重试、异常转换为 BangumiError
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.utils import bgm_log

log = logging.getLogger(__name__)

# 从 URL 里反解条目 ID（记日志时把 ID 换成动漫名）
_SUBJECT_ID_RE = re.compile(r"/v0/subjects/(\d+)")

# 条目类型（subject_type）
SUBJECT_TYPE_BOOK = 1
SUBJECT_TYPE_ANIME = 2
SUBJECT_TYPE_MUSIC = 3
SUBJECT_TYPE_GAME = 4
SUBJECT_TYPE_REAL = 6

# 收藏类型（type）
COLLECT_TYPE_WISH = 1      # 想看
COLLECT_TYPE_DONE = 2      # 看过
COLLECT_TYPE_DOING = 3     # 在看
COLLECT_TYPE_ON_HOLD = 4   # 搁置
COLLECT_TYPE_DROPPED = 5   # 抛弃


class BangumiError(RuntimeError):
    """Bangumi API 业务异常。"""


class BangumiAuthError(BangumiError):
    """Token 无效或权限不足（401/403）。"""


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
    @staticmethod
    def _subject_label(path: str, params: dict) -> str:
        """反解条目 ID，返回可直接拼进日志的「（动漫名 bgm=id）」。

        两种来源：
        ① 路径里的 ID（`/v0/subjects/{id}`）；
        ② 查询参数里的 `subject_id`（`/v0/episodes?subject_id=`）。
        """
        m = _SUBJECT_ID_RE.search(path)
        if m:
            return bgm_log.describe(int(m.group(1)))
        sid = params.get("subject_id")
        if sid:
            return bgm_log.describe(int(sid))
        return ""

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.api_base}{path}"
        label = self._subject_label(path, params)
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", 0)
            if status in (401, 403):
                log.warning("Bangumi GET %s 权限不足: %s%s", url, status, label)
                raise BangumiAuthError(
                    "Token 无效或权限不足（请重新生成 Token 并勾选读取收藏）"
                ) from e
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(str(e)) from e
        except requests.RequestException as e:
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(str(e)) from e

    def _post(self, path: str, json: Any = None) -> Any:
        url = f"{self.api_base}{path}"
        # POST 的订阅 ID 在 body / 路径里，这里只处理路径形式（如标记单集已看）
        label = self._subject_label(path, {})
        try:
            resp = self.session.post(url, json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as e:
            log.warning("Bangumi POST %s 失败%s: %s", url, label, e)
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
        bgm_log.bind_subject(subject_id)
        return self._get(f"/v0/subjects/{subject_id}")

    def get_episodes(
        self,
        subject_id: int,
        limit: int = 200,
        include_specials: bool = False,
    ) -> list[dict]:
        """拉取条目集数列表。

        端点：`GET /v0/episodes?subject_id=<id>`。

        **注意**：早期版本用的是 `/v0/subjects/{id}/episodes`，该端点目前已失效，
        一律返回 404（响应体是默认的 "This is default response..."），
        表现为「集标题全部退化成文件名、Bangumi 集 ID 为空、无法自动标记看过」。
        改用 `/v0/episodes?subject_id=` 后正常返回，字段结构（`id`/`sort`/`ep`/`name_cn`）完全一致。

        `include_specials=False`（默认）时过滤掉 type≠0 的条目：type 0=正片、
        1=SP、2=OP、3=ED、4=预告 等，只保留正片更贴合「本地文件数 ↔ 正片集数」的比对。
        """
        # 绑定 ID ↔ 名称，失败日志里就能看出是哪个动漫（见 utils/bgm_log.py）
        bgm_log.bind_subject(subject_id)
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            page_size = min(limit - len(out), 100)
            data = self._get(
                "/v0/episodes",
                subject_id=subject_id,
                limit=page_size,
                offset=offset,
            )
            if not isinstance(data, dict):
                break
            page = data.get("data", [])
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        if not include_specials:
            out = [e for e in out if (e.get("type") or 0) == 0]
        return out[:limit]

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

    # ---------- F18：用户在看列表 ----------
    def get_me(self) -> Optional[dict]:
        """GET /v0/me，用 Token 解析当前用户（未配置 username 时使用）。"""
        try:
            data = self._get("/v0/me")
            return data if isinstance(data, dict) else None
        except BangumiError as e:
            log.warning("解析当前用户失败（Token 可能无效）: %s", e)
            return None

    def get_user_collections(
        self,
        username: str,
        subject_type: int = SUBJECT_TYPE_ANIME,
        collect_type: int = COLLECT_TYPE_DOING,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """GET /v0/users/{username}/collections。

        默认取「动画 + 在看」（subject_type=2, type=3）。
        """
        if not username:
            raise BangumiError("未配置 Bangumi 用户名，且无法从 Token 解析")
        data = self._get(
            f"/v0/users/{username}/collections",
            subject_type=subject_type,
            type=collect_type,
            limit=limit,
            offset=offset,
        )
        if isinstance(data, dict):
            return data.get("data", [])
        if isinstance(data, list):
            return data
        return []

    def iter_user_collections(
        self,
        username: str,
        subject_type: int = SUBJECT_TYPE_ANIME,
        collect_type: int = COLLECT_TYPE_DOING,
        page_size: int = 50,
        max_items: int = 500,
    ) -> list[dict]:
        """分页拉取全部在看收藏（带 max_items 上限保护）。"""
        out: list[dict] = []
        offset = 0
        while len(out) < max_items:
            page = self.get_user_collections(
                username, subject_type, collect_type, page_size, offset
            )
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out[:max_items]
