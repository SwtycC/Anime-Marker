"""检查更新：拿 GitHub Releases 的最新 tag 与本地版本比。

**只做"提示"，不做自动更新**（取舍）：
自动更新要下载新包、替换**正在运行**的 exe、再重启自己 —— Windows 上这
必须先把 exe 改名或用外部脚本接续，还要处理下载校验、杀软误报、失败回滚。
对一个自用工具，投入产出比太低。这里只回答"有没有新版"，剩下的交给用户
点「打开发布页」自己去下。

**为什么走 GitHub API 而不是自己搭一个版本接口**：仓库是公开的
（`SwtycC/Anime-Marker`），匿名读 API 就够（限 60 次/小时/IP，手动点 +
启动查一次的量级完全够），不用在包里塞 token、也不用维护服务端。

**永不抛异常**：检查更新是个"锦上添花"的动作，断网/限流/仓库改名都不该
影响主流程，也不该弹错误框。所有失败都收敛成 `UpdateInfo.state`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from app import USER_AGENT, __version__
from app.core.bangumi_api import describe_connection_error

log = logging.getLogger(__name__)

#: 发布源。与 `app/__init__.py` 里 USER_AGENT 写的主页保持一致 ——
#: 两处指向同一个仓库，改名时记得一起改。
REPO = "SwtycC/Anime-Marker"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
REPO_PAGE = f"https://github.com/{REPO}"
ISSUES_NEW = f"https://github.com/{REPO}/issues/new"

#: 请求超时（秒）。检查更新不该让用户等 —— 8 秒已经足够宽容。
TIMEOUT = 8.0


@dataclass
class UpdateInfo:
    """检查更新的结果。

    `state` 四态，调用方据此决定说什么（**别只看"有没有 tag"**，
    "没发布过"和"查失败"对用户是两件事）：

        "newer"      —— 有比本地新的版本，`tag` / `url` 有效
        "latest"     —— 已是最新
        "no_release" —— 仓库里还没有发布过版本
                        （GitHub 对"没有 release"返回 404；
                        注意仓库被改名/删除也是 404，这里不区分 ——
                        那种情况本来也没法给出更有用的信息）
        "failed"     —— 网络 / 限流 / 其它 HTTP 错误，`error` 里有原因
    """

    state: str
    tag: str = ""
    url: str = RELEASES_PAGE
    error: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    """`v1.2.3` / `1.2.3` / `1.2` → 整数元组，用于比较。

    **不能用字符串比**（踩坑要点）：字典序下 `"1.0.10" < "1.0.9"` 成立 ——
    真发了 1.0.10 会被判成"没有更新"。拆成元组比才是对的。

    先砍掉预发布/构建后缀（`-rc1` / `-beta.2` / `+build`），再剥前缀 `v`，
    最后取数字段；取不到数字就当 0（`""` → `(0,)`），保证比较永不抛异常。
    最多取 4 段，防住"版本号被人写成含时间戳"这种怪数据。

    **为什么必须砍后缀**（踩坑，自测发现）：直接抓数字的话
    `v1.3.0-rc1` → `(1, 3, 0, 1)`，比 `1.3.0` 的 `(1, 3, 0)` **大** ——
    于是拿一个 rc 版本去催用户更新。砍掉后两者相等，判"没有更新"。
    宁可漏报一次，也不要拿预发布版骚扰人。
    """
    core = re.split(r"[-+]", (text or "").strip(), maxsplit=1)[0]
    nums = re.findall(r"\d+", core.lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    """远端版本是否比本地新。

    **比较前把两个元组补齐到等长**（踩坑，自测发现）：`(1, 2, 0) > (1, 2)`
    在 Python 里成立（等前缀时更长的那个更大），于是远端 `1.2.0` 与本地
    `1.2` 会被判成"有新版本" —— 而这俩是同一个版本。补零后相等，
    判"没有更新"。
    """
    a, b = parse_version(remote), parse_version(local)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def fetch_latest(timeout: float = TIMEOUT) -> UpdateInfo:
    """问一次 GitHub，返回 `UpdateInfo`。**不抛异常**（见模块说明）。"""
    try:
        resp = requests.get(
            API_LATEST,
            timeout=timeout,
            # GitHub 要求带 User-Agent（缺了会 403）；这里复用项目那份 ——
            # 里面的联系方式也正是"出问题该找谁"。
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"},
        )
    except Exception as e:
        msg = describe_connection_error(e) or str(e)
        log.info("检查更新失败（网络）：%s", msg)
        return UpdateInfo("failed", error=msg)

    if resp.status_code == 404:
        # 没发布过任何 release（见 UpdateInfo 的说明）
        log.info("检查更新：仓库还没有发布过版本")
        return UpdateInfo("no_release")
    if resp.status_code == 403:
        # 匿名请求被限流（60 次/小时/IP）。正常用量碰不到，值得单独记一笔。
        log.info("检查更新失败：GitHub API 限流")
        return UpdateInfo("failed", error="请求过于频繁（GitHub 限流），稍后再试")
    if resp.status_code != 200:
        log.info("检查更新失败：HTTP %s", resp.status_code)
        return UpdateInfo("failed", error=f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        log.info("检查更新失败：响应不是 JSON（%s）", e)
        return UpdateInfo("failed", error="响应格式异常")

    tag = str(data.get("tag_name") or "").strip()
    url = str(data.get("html_url") or "").strip() or RELEASES_PAGE
    if not tag:
        return UpdateInfo("failed", error="发布信息里没有版本号")
    state = "newer" if is_newer(tag) else "latest"
    log.info("检查更新：远端 %s / 本地 %s → %s", tag, __version__, state)
    return UpdateInfo(state, tag=tag, url=url)
