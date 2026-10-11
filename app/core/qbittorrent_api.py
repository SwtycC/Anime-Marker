"""qBittorrent Web UI 客户端（F19）。

- 依赖 qbittorrent-api（导入名 qbittorrentapi）
- 只做「推送任务 + 读状态」，不实现 BT 协议
- 连接失败不抛到 UI 崩溃，统一转换 QbError 并记录日志
"""

from __future__ import annotations

import logging
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

#: 从外网拉 `.torrent` 文件的超时（秒）。
_TORRENT_DL_TIMEOUT = 20.0


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
        exe_path: str = "",
        auto_start: bool = False,
        timeout: float = 3.0,
        read_timeout: float = 10.0,
        unavailable_ttl: float = 30.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.category = category
        self.save_path = save_path
        self._webui_url = webui_url
        #: qBittorrent 主程序路径（留空 = 不支持自动启动）。
        self.exe_path = (exe_path or "").strip()
        #: 连不上时是否自动拉起 qBittorrent（用户显式开启才做）。
        self.auto_start = bool(auto_start)
        #: **连接**超时（秒）—— 连不上时总耗时是这个数的 6 倍
        #: （见 `_note_failure`），但被系统自身的拒绝延迟兜住。
        self.timeout = timeout
        #: **读取**超时（秒）—— 响应本身可以慢（任务列表很大、磁盘正忙、
        #: qBittorrent 刚启动还在初始化），**不能跟着连接超时一起收紧**。
        self.read_timeout = read_timeout
        #: 「已知连不上」的冷却窗口（秒）——见 `_ensure_client`。
        self.unavailable_ttl = max(0.0, float(unavailable_ttl))
        #: 冷却窗口的截止时刻（`time.monotonic()` 口径；0 = 不在窗口中）
        self._unavailable_until = 0.0
        self._client = None
        # 记录最近一次失败原因，避免日志被 urllib3 重试刷屏
        self.last_error = ""
        #: 是否已经尝试过自动启动（**每次失败只拉起一次**，见 ensure_running）
        self._start_attempted = False

    # ---------- 连接 ----------
    @property
    def webui_url(self) -> str:
        if self._webui_url:
            return self._webui_url
        return f"http://{self.host}:{self.port}"

    @property
    def known_unavailable(self) -> bool:
        """当前是否处于「已知连不上」的冷却窗口内。

        给调用方一个**不发网络请求**就能问出来的判据 —— 查重那种
        「问不到也能继续」的可选查询据此直接跳过，省掉等待与日志噪声。
        """
        return self._unavailable_until > time.monotonic()

    def _note_failure(self, message: str) -> QbError:
        """统一登记一次失败：记原因 + 开启冷却窗口，返回可抛的 QbError。

        **为什么需要冷却窗口**：

        qbittorrentapi 内部对连接失败会做 **2 轮 × 3 个请求**的重试
        （`request.py` 里 `_retries` 的下限被写死成 1，参数上关不掉；
        `HTTPADAPTER_ARGS={"max_retries": 0}` 只关得掉 requests/urllib3
        那一层）。每个请求都要等满 `REQUESTS_ARGS['timeout']`：

            timeout=3.0 → 一次 auth_log_in() 失败 12.3 秒
            timeout=1.5 → 9.1 秒
            timeout=0.8 → 4.9 秒

        于是「qBittorrent 没开」这件很常见的事，会**按调用次数反复收
        这笔钱**：RSS 轮询里每条查重、订阅页每 5 秒一次的进度刷新……
        日志里那次 16:23:19→16:23:49 的 30 秒停顿就是这么来的。

        冷却窗口把重复调用压成「一个窗口内只真连一次」：窗口内后续
        调用立即失败、不发请求。窗口取的是一段短时间（默认 30 秒），
        所以用户真的把 qBittorrent 打开后，最多等一个窗口就能恢复；
        而**手动路径**（点「下发」）用 `force=True` 绕过窗口，不受影响。
        """
        self.last_error = message
        self._client = None
        self._unavailable_until = time.monotonic() + self.unavailable_ttl
        return QbError(message)

    def _ensure_client(self, force: bool = False):
        """惰性创建并登录。

        `force=True` 时忽略「已知连不上」的冷却窗口 —— 手动路径用：
        用户刚点了「下发 / 测试」，很可能顺手把 qBittorrent 打开了，
        不该被缓存挡住。
        """
        if self._client is not None:
            return self._client
        if not force and self.known_unavailable:
            raise QbError(
                f"qBittorrent 刚连过、没连上，{self.unavailable_ttl:.0f} 秒内"
                f"不再重试（上次：{self.last_error[:120]}）")
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
                REQUESTS_ARGS={"timeout": (self.timeout, self.read_timeout)},
                # 不重试：本机 Web UI 未开时立即失败，避免操作被拖慢数秒。
                # 注意参数名是 HTTPADAPTER_ARGS（不是 adapter_kwargs，后者会被
                # 透传给 Session.request 而报 TypeError）。
                HTTPADAPTER_ARGS={"max_retries": 0},
            )
            client.auth_log_in()
        except Exception as e:
            raise self._note_failure(f"连接失败（{self.webui_url}）：{e}") from e
        self._client = client
        self.last_error = ""
        self._unavailable_until = 0.0
        return client

    def test_connection(self, force: bool = False) -> str:
        """连通性自检，返回 qBittorrent 版本号。"""
        client = self._ensure_client(force=force)
        try:
            version = client.app.version
            log.info("qBittorrent 连接成功，版本 %s", version)
            return str(version)
        except Exception as e:
            raise self._note_failure(f"读取 qBittorrent 版本失败：{e}") from e

    # ---------- 按需启动 ----------
    def ensure_running(self, wait: float = 12.0, force_probe: bool = False) -> str:
        """**连不上就按需启动 qBittorrent**，返回一句结果说明。

        qBittorrent 的 GUI 与 Web UI 是**同一个进程**，
        用户从托盘「退出」后 Web UI 一起消失 —— 本程序随即连不上，新集
        只能落成「待确认」。这个方法让"需要时把它们拉起来"变成自动的。

        行为：
          ① 已经能连上 → 直接返回（不做任何多余动作）；
          ② 连不上且**没配 exe_path / 没开 auto_start** → 返回空串，
             调用方保持原有报错路径（不改变既有行为）；
          ③ 连不上但配了 → `Popen` 启动，然后**轮询等待端口可用**
             （最多 `wait` 秒），成功返回说明、失败也返回说明。

        **调用时机**：只在「确实要下发」的路径上（见 rss_service 里那段
        说明）。也就是说**没有需要下载的东西时不会启动 qBittorrent** ——
        这里不额外判断，判断的责任在调用方。

        `force_probe=True` 用于手动路径（用户点了「下发」）：忽略
        「已知连不上」的冷却窗口，真探测一次。

        **为什么返回字符串而不是抛异常**：调用方（下发流程）本来就有
        "连不上 → 落库为失败并给出原因"的路径；这里自动启动成功/失败
        都只是给那条路径补一句更准确的话，不该改变控制流。

        **为什么只启动一次**（`_start_attempted`）：一次轮询里可能有多条
        新集要下发，每条都试一次启动会反复 Popen（用户看到一堆进程）。
        首次失败后就记住，后续条目直接走"连不上"的分支。

        用 `Popen` 而不是 `os.startfile`/`shell=True`：路径来自配置，
        直接以列表形式传参可以避免空格与特殊字符被 shell 解释
        （`C:\\Program Files\\...` 里的空格正是常见坑）。
        """
        if force_probe:
            # 手动路径：把冷却窗口清掉，下面的探测才是真探测
            self._unavailable_until = 0.0

        # ① 已经能连上
        #
        # **注意这里不需要额外判断冷却窗口**：窗口内 `test_connection`
        # 会立刻抛 QbError（不发请求），正好直接落到下面的启动分支 ——
        # 这正是修掉「该拉起时反而先白等一轮超时」的地方。以前每次走到
        # 这里都要重连一次（qBittorrent 没开时 12 秒），而同一轮的查重
        # 刚刚才失败过，等于把同一笔等待付了两遍。
        try:
            return f"qBittorrent 已在运行（{self.test_connection(force=force_probe)}）"
        except QbError:
            pass

        # ② 没配路径 / 没开启
        if not self.auto_start or not self.exe_path:
            return ""
        if self._start_attempted:
            return "qBittorrent 仍未就绪（本次已尝试启动过，不再重复启动）"

        if not Path(self.exe_path).exists():
            self._start_attempted = True
            return f"qBittorrent 路径不存在：{self.exe_path}"

        self._start_attempted = True
        try:
            subprocess.Popen([self.exe_path])
            log.info("已按需启动 qBittorrent：%s", self.exe_path)
        except Exception as e:
            log.warning("启动 qBittorrent 失败：%s", e)
            return f"启动 qBittorrent 失败：{e}"

        # ③ 等它就绪（Web UI 服务起来要一点时间，冷启动尤其明显）
        #
        # **先探端口、再谈 HTTP**：刚 Popen 出来的 qBittorrent 里 Web UI
        # 还没开始监听，这时每次 `test_connection` 都要走完那 6 个请求
        # （见 `_note_failure`），一次就能吃掉整个 `wait`。端口没开就
        # 直接跳过，轮询才真的是「每 0.5 秒看一眼」。
        deadline = time.time() + max(0.0, wait)
        while time.time() < deadline:
            time.sleep(0.5)
            if not self._port_open():
                continue
            try:
                version = self.test_connection(force=True)
            except QbError:
                continue
            log.info("qBittorrent 已就绪（版本 %s）", version)
            return f"已启动 qBittorrent（版本 {version}）"
        return f"已启动 qBittorrent，但 {wait:.0f} 秒内 Web UI 仍未就绪"

    def _port_open(self, timeout: float = 0.5) -> bool:
        """Web UI 端口是否已在监听 —— 不建 HTTP 会话、不登录。

        只为 `ensure_running` 的等待循环服务（见那里的说明）。
        `settimeout` 兜住「端口关闭时系统的拒绝延迟」。
        """
        try:
            socket.create_connection(
                (self.host, int(self.port)), timeout=timeout).close()
            return True
        except OSError:
            return False

    def reset_start_flag(self) -> None:
        """允许下一次再尝试自动启动（用户改完设置后由设置页调用）。"""
        self._start_attempted = False
        self._unavailable_until = 0.0

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
            raise self._note_failure(f"推送任务失败：{e}") from e

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
            try:
                resp = requests.get(torrent_url, timeout=_TORRENT_DL_TIMEOUT)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".torrent",
                                                 delete=False) as f:
                    f.write(resp.content)
                    tmp_path = Path(f.name)
            except Exception as e:
                # **这一步是去外网下种子文件**，与 qBittorrent 无关 ——
                # 不能记成「qB 连不上」，否则会错误地打开冷却窗口，
                # 把真正的连接问题盖掉。
                raise QbError(f"下载种子文件失败：{e}") from e
            try:
                client.torrents_add(
                    torrent_files=str(tmp_path),
                    category=self.category,
                    tags=tags or [],
                    save_path=save_path or self.save_path or None,
                )
                log.info("已推送种子文件到 qBittorrent：%s", torrent_url[:80])
            except Exception as e:
                raise self._note_failure(f"推送种子失败：{e}") from e
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
            raise self._note_failure(f"读取任务列表失败：{e}") from e

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
