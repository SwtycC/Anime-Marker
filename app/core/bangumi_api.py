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

from app import USER_AGENT
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


def describe_connection_error(exc: Exception) -> str:
    """把连接类异常翻译成一句**能照着做**的提示；认不出来时返回空串。

    为什么需要它：`bgm.tv` 在国内网络下会被 **DNS 污染 + SNI 阻断**
    （2026-09 实测：DNS 返回 Dropbox / Facebook 段的随机 IP，且每次查询
    都不同；改用真实 IP 直连时 TLS 握手被 RST，而同一个 IP 换成别的域名
    能正常返回 200）。这类故障在日志里是一长串 urllib3 堆栈，
    原样丢到状态栏只会让人以为"软件坏了"，而实际该做的是**开代理**。

    因此按症状分类。**只给症状，不给动作** —— 具体该怎么办取决于
    "流量有没有走代理"（见 `_network_hint`），在这里猜容易给出错建议。
    详细原因仍进日志（`_get` / `_post` 里的 `log.warning` 保留原始异常文本）。
    """
    text = str(exc)
    low = text.lower()
    # 5xx 被重试耗尽：请求**已经到达服务器**，是它对端出错。
    # 别把用户引去查代理 —— 实测：Bangumi 后端故障期间
    # `/collections` 稳定 502 而同一秒 `/subjects` 稳定 200，
    # 响应头带 `CF-RAY`、502 页面是源站 nginx 的默认错误页。
    m = re.search(r"too many (\d{3}) error responses", text)
    if m:
        return f"Bangumi 服务端故障（连续返回 {m.group(1)}）—— 与本地网络无关，稍后重试"
    if re.search(r"\b5\d\d\b server error", low):
        return "Bangumi 服务端故障（5xx）—— 与本地网络无关，稍后重试"
    if "too many 502" in text or "502" in text:
        return "拿到 502 响应（网关错误）"
    if "reset" in low or "aborted" in low or "eof occurred" in low:
        return "连接被重置（疑似被网络阻断）"
    if "timed out" in low or "timeout" in low:
        return "连接超时（疑似被阻断或节点不通）"
    if "ssl" in low or "certificate" in low:
        return "TLS 握手失败（疑似被中间设备干扰）"
    if "max retries" in low:
        return "多次重试仍失败（网络不通）"
    return ""


def is_server_side_error(exc: Exception) -> bool:
    """这个失败是不是**服务端**的问题（请求已到达服务器，只是它返回了 5xx）。

    用来决定提示里要不要追加"检查代理"：服务端故障时流量明明通了，
    再让用户去查代理/换节点只会误导（实测踩过）。
    """
    text = str(exc)
    if re.search(r"too many \d{3} error responses", text):
        return True
    return bool(re.search(r"\b5\d\d\b", text)) and "server error" in text.lower()


class BangumiClient:
    """对 https://api.bgm.tv 的轻量封装。"""

    def __init__(
        self,
        token: str = "",
        api_base: str = "https://api.bgm.tv",
        proxy: str = "",
        user_agent: str = USER_AGENT,   # 合规 UA 见 app/__init__.py 的说明
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
            if status >= 500:
                # 单个 5xx（没走重试路径时）：同样是服务端的问题，
                # 不要让它显示成"网络错误"而把用户引去查代理
                log.warning("Bangumi GET %s 服务端错误 %s%s: %s",
                            url, status, label, e)
                raise BangumiError(
                    f"Bangumi 服务端故障（HTTP {status}）—— 与本地网络无关，稍后重试"
                ) from e
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(str(e)) from e
        except requests.RequestException as e:
            # 连接类故障 → 换成能照着做的提示（原始异常已在上面进日志）
            log.warning("Bangumi GET %s 失败%s: %s", url, label, e)
            raise BangumiError(self._network_hint(e, url)) from e

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
            raise BangumiError(self._network_hint(e, url)) from e

    def _network_hint(self, exc: Exception, url: str) -> str:
        """连接失败的用户可读提示，并补一句"流量到底走没走代理"。

        **为什么要补这一句（实测踩坑）**：用户看到网络错误后，跑去代理客户端
        里**反复换节点**，但问题根本不在节点 —— 代理客户端的**分流规则**把
        `bgm.tv` 判给了直连（它是国内域名，常被国内规则集收录），于是
        流量压根没进隧道，换哪个节点都一样。不点明这一点，用户会在错误的
        方向上试很久。

        `session.proxies` 为空时 requests 会退回环境变量 / 系统代理，
        所以要用 `get_environ_proxies` 看**实际生效**的代理再下结论
        （否则会把"用着系统代理"误报成"未走代理"）。
        """
        hint = describe_connection_error(exc)
        if not hint:
            return str(exc)
        if is_server_side_error(exc):
            # 服务端 5xx：流量已经打通了，别再让用户去查代理/换节点
            return hint
        proxies = dict(self.session.proxies)
        if not proxies:
            try:
                proxies = requests.utils.get_environ_proxies(url) or {}
            except Exception:
                proxies = {}
        addr = proxies.get("https") or proxies.get("http") or ""
        if addr:
            # 去掉 scheme 少占几个字符（状态栏是单行，右端会被省略号截断）
            shown = addr.split("://")[-1]
            hint += f" —— 已走代理 {shown}，请确认它没把 bgm.tv 分流成直连"
        else:
            hint += " —— 当前未走代理，请在「设置 → Bangumi → 代理」配置"
        return hint

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
        """GET /v0/me，用 Token 解析当前用户（未配置 username 时使用）。

        **失败原因必须分类记日志**：早期这里把两类失败写成同一句
        「Token 可能无效」，而实际上一半的情况（5xx / 连接失败）请求
        根本没到鉴权环节 —— 实测让用户以为 Token 坏了，跑去重新生成
        （Token 一直是好的）。判据很硬：只有 401/403 才与 Token 有关。
        """
        try:
            data = self._get("/v0/me")
            return data if isinstance(data, dict) else None
        except BangumiAuthError as e:
            # 401/403：这才是 Token 本身的问题
            log.warning("解析当前用户失败（Token 无效或权限不足）: %s", e)
            return None
        except BangumiError as e:
            # 5xx / 连接失败：与 Token 无关，别让用户去重新生成
            log.warning("获取当前用户失败（未能验证 Token，与 Token 无关）: %s", e)
            return None

    def get_user_collections(
        self,
        username: str,
        subject_type: int = SUBJECT_TYPE_ANIME,
        collect_type: Optional[int] = COLLECT_TYPE_DOING,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """GET /v0/users/{username}/collections。

        默认取「动画 + 在看」（subject_type=2, type=3）。
        `collect_type=None` → **不传 `type`** → 官方 OpenAPI 的默认行为
        「全部收藏状态」（想看/在看/看过/搁置/抛弃 一次拿全）。

        `None` 之所以等价于"不传"：`requests` 在编码查询串时会丢弃值为
        `None` 的参数（`models._encode_params` 里的 `if v is not None`），
        所以这里直接把 `None` 透传即可，不必拼两个分支。
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
        collect_type: Optional[int] = COLLECT_TYPE_DOING,
        page_size: int = 50,
        max_items: int = 500,
    ) -> list[dict]:
        """分页拉取收藏（带 max_items 上限保护）。

        `collect_type=None` 表示全部状态 —— 注意此时 `max_items` 是**所有
        状态共用一个额度**，触顶会在尾部静默截断（调用方拿到的是不完整
        名单）。触顶时记一条 warning，避免"收藏变少了"这种无声故障。
        """
        out: list[dict] = []
        offset = 0
        truncated = False
        while len(out) < max_items:
            page = self.get_user_collections(
                username, subject_type, collect_type, page_size, offset
            )
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break                      # 已翻到底
            offset += page_size
        else:
            # 循环因 len(out) >= max_items 退出，且上一页是满的 —— 说明
            # 后面还有数据没取
            truncated = True
        if truncated:
            log.warning("收藏分页达到上限 %s 条，尾部条目被截断"
                        "（collect_type=%s）", max_items, collect_type)
            out = out[:max_items]
        return out

    # ---------- F20：集级观看记录 ----------
    def get_subject_episode_collections(
        self,
        subject_id: int,
        episode_type: int = 0,
        limit: int = 500,
    ) -> list[dict]:
        """拉取某条目下**当前用户**的逐集收藏状态。

        端点：`GET /v0/users/-/collections/{subject_id}/episodes`
        （路径里的 `-` 表示"当前 Token 对应的用户"，无需 username）

        参数 `episode_type`：0=正片、1=SP、2=OP、3=ED、4=预告。
        默认只取正片，避免 OP/ED 混进「观看记录」时间线。

        返回每项结构（字段名以文档为准）：
            {
              "episode": { "id", "ep", "sort", "name", "name_cn", "airdate", ... },
              "type": 2,               # 2 = 看过
              "updated_at": 1786707528 # 该集被标记「看过」的时间（Unix 秒）
            }

        **注意（实测记录，两次结论不同）**：
        - 早期实测：服务端**未填充** `updated_at`，多部动漫（含 11/24/25 集的
          条目）全部返回 0 —— 因此不能直接拿它当观看时间。
        - 2026-09-19 复查：当前数据里该字段**是有值的**（本账号 30/30 行都与
          收藏级时间不同，精确到秒），走的是真实单集时间戳。

        结论：**保留两级兜底**（单集 `updated_at` → 动漫收藏级 `updated_at`），
        见 `bridges/inprogress.py`。真实时间戳缺失时若没有兜底，整批记录
        会因"无时间"被丢弃（早期版本正是如此，表现为动态页一片空白）。
        """
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            page_size = min(limit - len(out), 100)
            data = self._get(
                f"/v0/users/-/collections/{subject_id}/episodes",
                episode_type=episode_type,
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
        return out[:limit]
