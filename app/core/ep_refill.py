"""集标题补全：扫描时没拉到官方集数表的条目，事后自动回填。

**怎么补**：
    ① 找出「有 Bangumi 条目、但正片集仍缺 `bangumi_ep_id`」的条目
       （`Database.list_subjects_missing_ep_meta`）；
    ② 逐部重拉一次集数表，按官方 `sort` ↔ 本地 `ep_index` 回填
       `title` + `bangumi_ep_id`；
    ③ **已有 `bangumi_ep_id` 的行一律不碰** —— 那是之前成功拉到的，
       官方改名会由下一次扫描刷新，不属于"补全"的职责。

**失败处理**（`refill_batch` 的返回值）：
    - 网络失败：立刻停掉本批（每部都等一次超时只会更慢）；
    - 鉴权失败：同上，并明确报 `ERR_AUTH`，让调度器**不要**按网络错误
      反复重试（Token 不改，重试一万次也是 401）；
    - 单部意外异常：记日志、继续下一部，最后报 `ERR_OTHER`。

线程模型：`EpisodeRefillWorker` 是 QThread，由 `bridges/refill.py`
调度；`refill_batch` / `refill_subject_episode_meta` 是纯函数，方便
脱离 Qt 直接验证。
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from PySide6.QtCore import Signal

from app.core.bangumi_api import BangumiAuthError, BangumiClient, BangumiError
from app.core.database import Database, Episode
from app.utils import bgm_log
from app.utils.qt_thread import NamedThread

log = logging.getLogger(__name__)

#: 本批没有遇到错误
ERR_NONE = ""
#: 网络类失败（超时 / 连接被拒 / DNS …）：可以按退避重试
ERR_NETWORK = "network"
#: 鉴权失败（401/403）：Token 不改就不会好，不要自动重试
ERR_AUTH = "auth"
#: 其它异常（数据库、接口结构变化…）：记日志，不自动重试
ERR_OTHER = "other"


def refill_subject_episode_meta(
    db: Database,
    api: BangumiClient,
    subject_id: int,
) -> int:
    """补一个条目的集标题，返回回填的行数。

    只处理同时满足以下条件的行：
      - `bangumi_ep_id` 为空（说明当时没拿到官方数据）；
      - `ep_label` 为空（附加内容的标题本来就该用文件名，不补）；
      - `ep_index` 能对上官方集数表的 `sort` / `ep`。

    对不上的行**保持原样**：可能是 Bangumi 还没录入这一集，也可能本地
    集号与官方口径不同（那种情况该走一次真正的重扫，本模块不擅自
    按顺序重新配对）。
    """
    subj = db.get_subject(int(subject_id))
    if subj is None:
        return 0
    bangumi_id = int(subj.bangumi_id or 0)
    if bangumi_id <= 0:
        return 0

    # 让请求日志带上「哪个动漫」，失败时不用只盯着一串数字 ID
    bgm_log.bind_subject(bangumi_id, subj.name_cn or subj.name or "")

    eps = api.get_episodes(bangumi_id)

    # 键统一转 float：接口的 sort 可能是 int，本地解析出来的是 float，
    # 不转会静默查不到（与 scanner._fill_episodes 同一口径）。
    ep_map: dict[float, dict] = {}
    for e in eps:
        try:
            key = float(e.get("sort") or e.get("ep"))
        except (TypeError, ValueError):
            continue
        ep_map.setdefault(key, e)

    filled = 0
    for row in db.list_episodes(int(subject_id)):
        if row.bangumi_ep_id:
            continue
        if (row.ep_label or "").strip():
            continue
        try:
            idx = float(row.ep_index)
        except (TypeError, ValueError):
            continue
        official = ep_map.get(idx)
        if not official:
            continue
        title = (official.get("name_cn") or official.get("name") or "").strip()
        ep_id = official.get("id")
        if not title or not ep_id:
            continue
        db.update_episode_title(int(row.id), title, int(ep_id))
        filled += 1
        log.info("补全集标题：%s 第 %s 集 -> %s（bgm_ep=%s）",
                 subj.name_cn or subj.name or f"#{subject_id}",
                 _fmt_index(row), title, ep_id)
    return filled


def _fmt_index(row: Episode) -> str:
    """集号展示（整数去掉 .0，便于日志阅读）。"""
    try:
        value = float(row.ep_index)
    except (TypeError, ValueError):
        return "?"
    return str(int(value)) if value.is_integer() else f"{value:g}"


def refill_batch(
    db: Database,
    api: BangumiClient,
    subject_ids: Iterable[int],
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> tuple[int, int, str]:
    """批量补全，返回 `(回填条数, 涉及条目数, 失败类型)`。

    `on_progress(已完成, 总数)` 每处理完一个条目回调一次；
    `stop()` 返回真时在**下一条**开始前退出（正在飞的请求无法打断）。
    """
    ids = [int(s) for s in subject_ids]
    total = len(ids)
    filled = 0
    touched = 0
    other_error = False

    for i, sid in enumerate(ids):
        if stop is not None and stop():
            log.info("集标题补全被中止（已处理 %s/%s 部）", i, total)
            return filled, touched, ERR_NONE
        try:
            n = refill_subject_episode_meta(db, api, sid)
        except BangumiAuthError as e:
            log.warning("集标题补全中止：Token 过期或无效（已补 %s 条）：%s",
                        filled, e)
            return filled, touched, ERR_AUTH
        except BangumiError as e:
            log.warning("集标题补全中止：网络请求失败（已补 %s 条）：%s",
                        filled, e)
            return filled, touched, ERR_NETWORK
        except Exception:
            # 单部失败不该拖垮整批：继续下一部，最后报 ERR_OTHER
            log.exception("补全集标题异常 subject_id=%s", sid)
            other_error = True
            n = 0
        if n:
            filled += n
            touched += 1
        if on_progress is not None:
            on_progress(i + 1, total)

    return filled, touched, (ERR_OTHER if other_error else ERR_NONE)


class EpisodeRefillWorker(NamedThread):
    """后台补全（QML 主线程只收信号）。

    `done` 的第三个参数是失败类型，调度器据此决定"退避重试 / 暂停"。
    """

    progress = Signal(int, int)         # 已完成, 总数
    done = Signal(int, int, str)        # 回填条数, 涉及条目数, 失败类型

    def __init__(
        self,
        db: Database,
        api: BangumiClient,
        subject_ids: list[int],
    ) -> None:
        super().__init__()
        self._db = db
        self._api = api
        self._subject_ids = subject_ids

    def run(self) -> None:
        filled, touched, error = refill_batch(
            self._db,
            self._api,
            self._subject_ids,
            on_progress=self.progress.emit,
            stop=self.isInterruptionRequested,
        )
        self.done.emit(filled, touched, error)
