"""SQLite 数据访问层。

- 标准库 sqlite3，check_same_thread=False + 线程锁
- 对外只暴露仓储方法，UI 不直接操作 SQL
- meta 表存 schema_version，便于后续迁移
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.utils.paths import database_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 10

#: 「把收藏缓存里已知的状态写回 `subjects.collect_type`」的那条 SQL。
#:
#: 两个调用点用**同一条语句**（收藏同步落缓存时、v10 迁移回填旧库），
#: 抽成常量避免哪天只改一处。
#: 只覆盖"缓存里有对应行"的条目：没行 = 不知道（未收藏 / 从没同步过），
#: 不能据此判定"未收藏"（见 cached_collect_type 的说明）。
_COLLECT_TYPE_BACKFILL_SQL = """
UPDATE subjects
   SET collect_type = (
       SELECT c.collect_type FROM inprogress_cache c
        WHERE c.bangumi_id = subjects.bangumi_id)
 WHERE bangumi_id IN (SELECT bangumi_id FROM inprogress_cache)
"""

#: 「本地有状态、收藏缓存里却没有它」的**已匹配**条目 → 清成"未知"。
#:
#: **只用在同步路径**（`replace_inprogress_cache` 第 ② 步）：那里的缓存
#: 是刚拉下来的全量，所以"名单里没有"就是"服务端没有"这个判据成立。
#: **不要拿它做启动对齐**（试过、已撤）：启动时的缓存可能是几天前的，
#: 拿它去清会把用户刚在网站上新标的状态（详情页回查写进本地、缓存还没
#: 同步到）一起清掉 —— 实测：魔女之旅重启后又被算回「未标记」。
#: 只动已匹配条目：未匹配条目的状态只可能来自用户手动标记。
_COLLECT_TYPE_CLEAR_STALE_SQL = """
UPDATE subjects
   SET collect_type = 0
 WHERE bangumi_id IS NOT NULL
   AND COALESCE(collect_type, 0) != 0
   AND bangumi_id NOT IN (SELECT bangumi_id FROM inprogress_cache)
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS subjects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bangumi_id   INTEGER UNIQUE,
    name         TEXT,
    name_cn      TEXT,
    cover_url    TEXT,
    cover_path   TEXT,
    total_eps    INTEGER,
    folder_path  TEXT,
    series_name  TEXT,                       -- 所属系列（用于聚合展示）
    aliases      TEXT,                       -- infobox 别名，用换行分隔（见下方注释）
    studio       TEXT,                       -- 动画制作公司（规范名，见 bangumi_api.STUDIO_ALIASES）
    ep_align     TEXT,                       -- 集数对应方式：''=按号（默认）/'order'=按顺序（可能不准）
    collect_type INTEGER,                    -- Bangumi 收藏状态：0/NULL=未知，1想看 2看过 3在看 4搁置 5抛弃
    match_state  TEXT DEFAULT 'auto',        -- auto / manual / pending
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id     INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    bangumi_ep_id  INTEGER,
    ep_index       REAL,
    title          TEXT,
    file_path      TEXT UNIQUE,
    watched        INTEGER DEFAULT 0,
    watch_progress REAL DEFAULT 0,
    watched_at     TEXT,
    ep_label       TEXT                      -- 非正片的显示标签（SP01/OVA01/NCOP3…）
);

CREATE INDEX IF NOT EXISTS idx_episodes_subject ON episodes(subject_id);

CREATE TABLE IF NOT EXISTS watch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER,
    progress   REAL,
    created_at TEXT
);

-- F18：在看列表缓存（离线降级 / 首屏秒开）
--
-- 命名遗留：表名/属性名仍带 "inprogress"，但**语义已改为「看过」**
-- （collect_type = 2）。详见 bridges/inprogress.py 顶部说明。
CREATE TABLE IF NOT EXISTS inprogress_cache (
    bangumi_id   INTEGER PRIMARY KEY,
    name         TEXT,
    name_cn      TEXT,
    cover_url    TEXT,
    ep_status    INTEGER,
    total_eps    INTEGER,
    collect_type INTEGER,
    updated_at   TEXT,   -- **缓存写入时间**（本次拉取的时刻），用于判断缓存新鲜度
    collection_updated_at TEXT  -- **Bangumi 的收藏最后修改时间**（用户何时看完），
                                -- 用于「最近 N 部」排序，见 §5.12.11.5
);

-- F20：Bangumi 集级观看记录（「动态」页的时间线数据源）
--
-- 来源：`GET /v0/users/-/collections/{subject_id}/episodes`
-- 每条对应该用户标记为「看过」的一集，带该集的标记时间。
--
-- 为什么要单独一张表（而不是从 inprogress_cache 推）：
-- `inprogress_cache` 只有**动漫级**的 `updated_at`（最后修改收藏的时间），
-- 拿不到"第几集是什么时候看的"。而「动态」页要的正是集级粒度
-- （对应 Bangumi 网页的「看过 ep.5 TV取材 · 5天12小时前」）。
--
-- 拉取成本：每部动漫一次请求。因此**按需增量同步** —— 同步过且收藏没变
-- 的条目不再重复请求，判据见 ep_sync_state 与 bridges/inprogress.py 的
-- `sync_candidates()`。
CREATE TABLE IF NOT EXISTS watched_episodes (
    bangumi_ep_id INTEGER PRIMARY KEY,   -- Bangumi 的 episode id
    subject_id    INTEGER,               -- 本地 subjects.id（可为空，未入库时）
    bangumi_id    INTEGER,               -- Bangumi 条目 id
    subject_name  TEXT,                  -- 冗余存名字，避免 JOIN 失败时丢显示
    ep_index      REAL,                  -- 集序号（sort）
    ep_name       TEXT,                  -- 集标题
    watched_at    TEXT                   -- 该集被标记「看过」的时间（ISO）
);

CREATE INDEX IF NOT EXISTS idx_watched_ep_time
    ON watched_episodes(watched_at DESC);
CREATE INDEX IF NOT EXISTS idx_watched_ep_subject
    ON watched_episodes(bangumi_id);

-- F20：集级记录的**同步水位**（每个条目一行）—— 决定"这次要重新拉哪几部"
--
-- 为什么单独一张表、而不是给 inprogress_cache 加列：
-- `replace_inprogress_cache()` 是**整表 DELETE + INSERT**（每次刷新收藏列表
-- 都重建），加在那里的水位行会被一起抹掉。水位必须比收藏缓存活得更久。
--
-- 为什么不用"该部在 watched_episodes 里一行都没有"来反推待同步：
-- 那样对"请求成功但确实没有逐集标记"的条目（很常见：只标了整部状态、
-- 剧场版）会**每次刷新都重拉**，形成死循环。水位行只在**请求成功**时写，
-- 成功的 0 行也是终态答案；请求失败不写 → 下次自然重试。
--
-- 注意 `collection_updated_at` 存的是**原始字段值**，不能用
-- `_collection_time()` 的兜底值（那个会回落到本次拉取时刻，每次都变，
-- 会让该部每次刷新都重拉）。详见 bridges/inprogress.py 的 sync_key()。
CREATE TABLE IF NOT EXISTS ep_sync_state (
    bangumi_id            INTEGER PRIMARY KEY,
    collection_updated_at TEXT,     -- 同步时刻 inprogress_cache 里的原始收藏修改时间
    ep_status             INTEGER,  -- 同步时刻的 ep_status（"看到第几话"）
    ep_rows               INTEGER,  -- 本次写入的行数（仅诊断，不参与判定）
    synced_at             TEXT      -- 同步时刻（本地 ISO）
);

-- F19：RSS 订阅源
CREATE TABLE IF NOT EXISTS rss_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    url          TEXT UNIQUE,
    bangumi_id   INTEGER,
    enabled      INTEGER DEFAULT 1,
    rule         TEXT DEFAULT 'new_only',
    last_poll_at TEXT,
    last_error   TEXT,
    created_at   TEXT
);

-- F19：下载记录（判新第三层 + 状态回查）
CREATE TABLE IF NOT EXISTS download_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER REFERENCES rss_sources(id) ON DELETE CASCADE,
    subject_id    INTEGER,
    ep_index      REAL,
    torrent_title TEXT,
    magnet        TEXT,
    torrent_hash  TEXT,
    status        TEXT DEFAULT 'pending',
    created_at    TEXT,
    updated_at    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_download_source_ep
    ON download_history(source_id, ep_index);
CREATE INDEX IF NOT EXISTS idx_download_hash ON download_history(torrent_hash);

-- 条目标签（详情页展示 + 用户编辑）
--
-- 来源两路：① 扫描 / 手动匹配时从 Bangumi subject 响应存入**前 10 个**
-- （source='api'，pos 记接口原始顺序 = 打此 tag 的人数降序）；
-- ② 用户在详情页手动添加（source='user'）。
--
-- `deleted` 只对 api tag 有意义：用户删掉的接口 tag **不物理删除**，
-- 而是置删除标记 —— 再次进入编辑时仍以置灰形态可见、可恢复
-- （接口行为：删了就"不再展示"，但不是永远找不回来）。
-- user tag 删除即物理删除（用户自己加的，没"恢复原状"可言）。
--
-- 主键 (subject_id, name)：同一部不会出现同名 tag 两个来源。
-- 重扫时的覆盖策略见 replace_subject_tags（保留 deleted 标记）。
CREATE TABLE IF NOT EXISTS subject_tags (
    subject_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'api',  -- api=接口带来 / user=用户添加
    pos        INTEGER NOT NULL DEFAULT 0,   -- 排序：api 按接口顺序(0~9)，user 从 100 起
    deleted    INTEGER NOT NULL DEFAULT 0,   -- 1=用户已删除（仅 api tag 使用）
    PRIMARY KEY (subject_id, name)
);
CREATE INDEX IF NOT EXISTS idx_subject_tags_subject
    ON subject_tags(subject_id);
"""


@dataclass
class Subject:
    id: int
    bangumi_id: Optional[int]
    name: str
    name_cn: str
    cover_url: str
    cover_path: str
    total_eps: int
    folder_path: str
    series_name: str
    aliases: str
    studio: str
    match_state: str
    updated_at: str
    # 集数对应方式（v7+）：'' / NULL = 按号（默认）；'order' = 按顺序配对。
    # 带默认值：旧调用方按位置构造不受影响，Subject(**dict(row)) 也能对上。
    ep_align: str = ""
    # Bangumi 收藏状态（v9+）：0/NULL = 未知，1 想看 / 2 看过 / 3 在看 /
    # 4 搁置 / 5 抛弃。详情页那排状态选择器的当前选中项就取它。
    # 带默认值（同上）：旧调用方按位置构造不受影响。
    collect_type: int = 0


@dataclass
class Episode:
    id: int
    subject_id: int
    bangumi_ep_id: int
    ep_index: float
    title: str
    file_path: str
    watched: bool
    watch_progress: float
    watched_at: str
    # 非正片的显示标签（`SP01` / `OVA01` / `NCOP3` / `WEB予告 #01`…）。
    # 正片为空串。带默认值：旧库该列为 NULL，`Episode(**dict(row))` 也能对上。
    ep_label: str = ""


@dataclass
class TimelineEntry:
    """动态时间线条目（episodes JOIN subjects）。"""

    episode_id: int
    subject_id: int
    subject_name: str
    ep_index: float
    ep_title: str
    watched_at: str


@dataclass
class InProgressItem:
    """F18：在看列表缓存条目。"""

    bangumi_id: int
    name: str
    name_cn: str
    cover_url: str
    ep_status: int
    total_eps: int
    collect_type: int
    updated_at: str
    # Bangumi 给的最后修改收藏时间（用户何时"看完"）。与 updated_at 不同：
    # updated_at 是**我们拉取的时刻**（整批相同），本字段逐条不同。
    # 「最近 N 部」按它排序才有意义，见 bridges/inprogress.py。
    # 老库迁移后为 NULL，下次拉取补齐。
    collection_updated_at: Optional[str] = None
    # 运行时填充（不落库）
    local_subject_id: Optional[int] = None


@dataclass
class EpSyncState:
    """F20：某条目集级记录的同步水位（见 ep_sync_state 表）。

    三个字段都取**上一次成功同步时**的快照值，用于与当前收藏缓存比对，
    判断"这部要不要重新拉"。`synced_at` 供超期兜底（见 sync_candidates）。
    """

    bangumi_id: int
    collection_updated_at: str
    ep_status: int
    ep_rows: int
    synced_at: str


@dataclass
class RssSource:
    """F19：RSS 订阅源。"""

    id: int
    name: str
    url: str
    bangumi_id: Optional[int]
    enabled: bool
    rule: str
    last_poll_at: str
    last_error: str
    created_at: str


@dataclass
class DownloadRecord:
    """F19：下载记录。"""

    id: int
    source_id: int
    subject_id: Optional[int]
    ep_index: float
    torrent_title: str
    magnet: str
    torrent_hash: str
    status: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    """SQLite 仓储。线程安全。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._lock = threading.Lock()
        self._init_schema()

    # ---------- schema ----------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA_SQL)  # 全部 IF NOT EXISTS，旧库增量补齐
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            )
            row = cur.fetchone()
            old = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            self._migrate(old)
            if old != SCHEMA_VERSION:
                log.info("schema 版本 %s → %s", old, SCHEMA_VERSION)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self, old_version: int) -> None:
        """增量迁移：只做加法（新增列），不破坏既有数据。"""
        if old_version >= SCHEMA_VERSION:
            return
        # v3：subjects 新增 series_name（聚合展示用）
        cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(subjects)")
        }
        if "series_name" not in cols:
            log.info("迁移：subjects 增加 series_name 列")
            self._conn.execute("ALTER TABLE subjects ADD COLUMN series_name TEXT")
        # v3：subjects.bangumi_id 允许为空（pending 条目无 Bangumi ID）
        # SQLite 无法直接改列约束，旧库保持原样即可（NULL 仍可插入）
        # v4：inprogress_cache 新增 collection_updated_at（Bangumi 的收藏
        # 修改时间）。旧库该列为 NULL，下一次拉取会整表重写补齐；
        # 在此之前排序回落到 updated_at（COALESCE），不会报错。
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(inprogress_cache)")
        }
        if "collection_updated_at" not in cols:
            log.info("迁移：inprogress_cache 增加 collection_updated_at 列")
            self._conn.execute(
                "ALTER TABLE inprogress_cache"
                " ADD COLUMN collection_updated_at TEXT"
            )
        # v5：subjects 新增 aliases（Bangumi infobox 的别名，换行分隔）。
        #
        # 旧库该列为 NULL —— 不影响使用：别名只在**匹配阶段**用（在内存里
        # 参与打分，见 matcher.score_subject），本地这列只服务于"按别名搜
        # 已入库条目"。已入库的旧条目不重扫就没有别名，这是可接受的降级。
        cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(subjects)")
        }
        if "aliases" not in cols:
            log.info("迁移：subjects 增加 aliases 列")
            self._conn.execute("ALTER TABLE subjects ADD COLUMN aliases TEXT")
        # v6：subjects 新增 studio（动画制作公司，规范名）。
        #
        # 旧库该列为 NULL —— 与 aliases 同样是"不重扫就没有"的降级：
        # 公司名来自扫描时的搜索响应（infobox / tag，零额外请求），
        # 也可以之后进一次详情页补上（见 LibraryBridge.requestTagFetch）。
        if "studio" not in cols:
            log.info("迁移：subjects 增加 studio 列")
            self._conn.execute("ALTER TABLE subjects ADD COLUMN studio TEXT")
        # v7：subjects 新增 ep_align（集数对应方式标记）。
        #
        # 'order' = 该条目的集数是**按顺序**与官方配对的（官方序号与本地
        # 文件编号完全对不上、但数量一致时的兜底，见 scanner._align_by_order）
        # —— 这种对应没有按号匹配可靠，详情页打开时会弹黄色提示提醒用户。
        # 旧库该列为 NULL，与 '' 同义（按号对应），无降级问题。
        if "ep_align" not in cols:
            log.info("迁移：subjects 增加 ep_align 列")
            self._conn.execute("ALTER TABLE subjects ADD COLUMN ep_align TEXT")
        # v9：subjects 新增 collect_type（Bangumi 收藏状态快照）。
        #
        # 用途：详情页海报下方的「想看 / 看过 / 在看 / 搁置 / 抛弃」选择器
        # 需要一个"当前是哪个状态"的本地来源。
        #
        # 为什么要单独一列、而不是需要时去 `inprogress_cache` 现查：
        # 那张表**只覆盖已收藏的条目**，未收藏的条目压根没有行（而"未收藏"
        # 正是最常见的起点 —— 用户第一次点某个状态时才创建收藏），
        # 而且它会被整表刷新。有这一列才能把"用户在本页点过什么"存住。
        # 反过来，缓存表**有**行时确实可以拿来提前知道状态（少一次网络
        # 往返），见 `cached_collect_type` —— 那里不能反推的只是"没有行"
        # 这一种情况。
        #
        # 旧库该列为 NULL —— 与 0 同义（"未知"）。界面此时**一个都不选中**，
        # 并在进入详情页时补齐：先看缓存表，再退到网络回查真实状态
        # （见 bridges/library 的 _CollectTypeWorker）。
        if "collect_type" not in cols:
            log.info("迁移：subjects 增加 collect_type 列")
            self._conn.execute(
                "ALTER TABLE subjects ADD COLUMN collect_type INTEGER")
        # v10：**没有列变更，纯粹是数据回填** —— 把收藏同步缓存里已经知道的
        # 状态写进 subjects.collect_type。
        #
        # 为什么需要：那一列早先只在用户点过选择器、或进过某条目详情页时
        # 才有值，而海报墙的「收藏状态」筛选（尤其"未标记"）是按这一列筛的
        # —— 于是**明明在 Bangumi 上收藏过的番，在"未标记"里一大堆**
        # （用户实测反馈）。缓存表里本来就有全量收藏状态，回填一次即可。
        #
        # 之后由 replace_inprogress_cache 在每次同步时自动保持对齐，
        # 所以这一条只在旧库上跑一次。
        if old_version < 10:
            cur = self._conn.execute(_COLLECT_TYPE_BACKFILL_SQL)
            if cur.rowcount:
                log.info("迁移：按收藏缓存回填了 %s 个条目的收藏状态",
                         cur.rowcount)
        # v8：episodes 新增 ep_label（非正片的显示标签）。
        #
        # 用途：SP / OVA / NCOP / 特典 这类"附加内容"的序号是**字符串**语义
        # （`SP01`、`OVA01`、`NCOP3`），而 `ep_index` 是 REAL 装不下前缀。
        # 早期把它们顺延成 13.5 / 14 这种假集号（实测截图），既与正片混淆、
        # 也显示成"第 13.5 集"。现在用 ep_index 只负责排序，
        # ep_label 负责显示（见 scanner._fill_episodes 与 DetailPage.fmtIndex）。
        # 旧库该列为 NULL —— 老数据没有标签，QML 侧回落到数值显示，无降级问题。
        ep_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(episodes)")
        }
        if "ep_label" not in ep_cols:
            log.info("迁移：episodes 增加 ep_label 列")
            self._conn.execute("ALTER TABLE episodes ADD COLUMN ep_label TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ---------- subjects ----------
    def upsert_subject(self, **fields: Any) -> int:
        """写入/更新一条**已匹配**条目，返回本地主键。

        **同目录已有一条"未匹配占位行"时就地升级，而不是另插一行**（踩坑，
        实测）：bangumi_id 为 NULL 的条目有两个来源 —— 扫描没匹配上的
        `pending` 占位，以及「添加动漫」未填 Token 时建的纯本地条目
        （`upsert_local_subject`）。它们**后来匹配成功**时（重扫 / 详情页
        重新扫描），若只按 `bangumi_id` 做 ON CONFLICT，会因"库里那行没有
        bangumi_id"而**新插一行** —— 海报墙上同一个目录出现两张卡
        （一张有封面、一张没有），集数也分裂在两个 subject 下。
        所以这里先找同目录的未匹配行，命中就按主键 UPDATE：它原有的
        episodes（subject_id 指向它）因此原样保留，随后被正常回填。
        """
        bangumi_id = fields.get("bangumi_id")
        if not bangumi_id:
            raise ValueError("bangumi_id 必填")
        fields.setdefault("updated_at", _now())
        cols = list(fields.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "bangumi_id")
        sql = (
            f"INSERT INTO subjects ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(bangumi_id) DO UPDATE SET {updates}"
        )
        with self._cursor() as cur:
            cur.execute("SELECT id FROM subjects WHERE bangumi_id=?", (bangumi_id,))
            if cur.fetchone() is None:
                folder = fields.get("folder_path") or ""
                if folder:
                    cur.execute(
                        "SELECT id FROM subjects WHERE folder_path=?"
                        " AND bangumi_id IS NULL ORDER BY id LIMIT 1",
                        (folder,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        sets = ",".join(f"{c}=?" for c in cols)
                        cur.execute(
                            f"UPDATE subjects SET {sets} WHERE id=?",
                            [fields[c] for c in cols] + [int(row["id"])],
                        )
                        return int(row["id"])
            cur.execute(sql, [fields[c] for c in cols])
            cur.execute("SELECT id FROM subjects WHERE bangumi_id=?", (bangumi_id,))
            return int(cur.fetchone()["id"])

    def list_subjects(self) -> list[Subject]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM subjects ORDER BY name_cn, name")
            return [Subject(**dict(r)) for r in cur.fetchall()]

    def get_subject(self, subject_id: int) -> Optional[Subject]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM subjects WHERE id=?", (subject_id,))
            row = cur.fetchone()
            return Subject(**dict(row)) if row else None

    def set_cover_path(self, subject_id: int, cover_path: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subjects SET cover_path=?, updated_at=? WHERE id=?",
                (cover_path, _now(), subject_id),
            )

    def set_subject_ep_align(self, subject_id: int, by_order: bool) -> None:
        """标记该条目的集数是按号还是按顺序与官方对应的（详情页警示用）。

        每次扫描 `_fill_episodes` 都会重写：这次按顺序配对、下次重扫按号
        对上了，标记会自动清除，不会永久残留。
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subjects SET ep_align=? WHERE id=?",
                ("order" if by_order else "", subject_id),
            )

    def subject_collect_types(self) -> dict[int, int]:
        """`bangumi_id → 本地收藏状态`（只含有 bangumi_id 且值非 NULL 的条目）。

        **本地快照比收藏缓存更新**（缓存在每次同步时被整表替换，而这一列
        在用户点击的当下就被改写，同步时也会跟着对齐 —— 见
        `replace_inprogress_cache`）。「在看」列表用它做一遍过滤：用户在本
        程序里把某部从「在看」改成「想看」后，那一部应当**立刻**从列表里
        消失，而不是等下一次同步。

        值可能是 0（"查过、确实没收藏"）：调用方要把它当成"不知道"，
        别当成"不在看" —— 详情页那套"0 = 未知"的口径在这里同样适用。
        """
        with self._cursor() as cur:
            rows = cur.execute(
                """
                SELECT bangumi_id, collect_type FROM subjects
                 WHERE bangumi_id IS NOT NULL AND collect_type IS NOT NULL
                """
            ).fetchall()
        return {int(r["bangumi_id"]): int(r["collect_type"] or 0)
                for r in rows if r["bangumi_id"]}

    def subjects_by_collect_type(self, collect_type: int) -> list[Subject]:
        """本地收藏状态等于该值的条目（`collect_type` 的含义由调用方定）。

        **为什么要按状态查本地条目**（典型用途：补出「在看」页里那些
        *没连 Bangumi* 的番）：没配 Token、或条目没匹配到 Bangumi 的用户
        也能在详情页手动标「在看」，这些条目**不在 `inprogress_cache` 里**
        （那张表是服务端收藏的镜像），只有 `subjects.collect_type` 记着。
        不查这一份，"我在看、但没连 Bangumi"的番就永远不出现在「在看」页。

        去重由调用方做（按 bangumi_id 与缓存来的行比对）：这里只按状态过滤，
        不掺"缓存里有没有"的判断 —— 缓存里那一行可能是**旧状态**
        （如「看过」），此时本地这一条恰恰是该显示的。
        """
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM subjects WHERE collect_type=? ORDER BY id",
                (int(collect_type),),
            ).fetchall()
        return [Subject(**dict(r)) for r in rows]

    def collect_types_by_subject_id(self) -> dict[int, int]:
        """`本地主键 → 收藏状态`（全部条目，0 含"未知"）。

        海报墙的筛选要用它：那张墙的条目列表是**长生命周期的缓存**（重取会
        重建全部卡片），收藏状态变了它不一定跟着更新 —— 用这份只有两列的
        小映射做筛选，既实时又不用重建卡片（用户实测：详情页已显示「看过」、
        筛选里却还算「未标记」，要重扫才好）。
        """
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT id, collect_type FROM subjects"
            ).fetchall()
        return {int(r["id"]): int(r["collect_type"] or 0) for r in rows}

    def cached_collect_type(self, bangumi_id: int) -> int:
        """从**收藏同步缓存**里取该条目的状态；0 表示"表里没有这一条"。

        与 `subjects.collect_type` 的分工：那一列是自己写的快照（可能为空），
        这张表是最近一次全量同步拿到的**服务端收藏**（含 1~5 全部状态，
        见 `InProgressBridge`）。详情页状态选择器优先用它 —— 查得到就能
        **同步**给出选中态，不必等一个网络往返（用户实测反馈"最好跟展示图
        出现的时间一样快"：海报是立刻出现的，选中态不该慢半拍）。

        **0 的含义是"不知道"，不是"未收藏"**：表里只存用户**已收藏**的条目，
        没有行可能是"未收藏"，也可能是"还没同步过"。所以调用方拿到 0 必须
        回退到网络回查（见 bridges/library 的 requestCollectType）。
        这也是本表唯一能反推出的结论 —— 不能拿"没有行"当"未收藏"用
        （见 set_subject_collect_type 的注释）。

        `bangumi_id` 是主键，单行查询走索引。
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT collect_type FROM inprogress_cache WHERE bangumi_id=?",
                (int(bangumi_id),),
            ).fetchone()
        if row is None:
            return 0
        ctype = int(row["collect_type"] or 0)
        # 只认 1~5（不想为这一个判断把 bangumi_api 的枚举表引进存储层）。
        # 老库迁移留下的 NULL 在这里落成 0 = "不知道" → 调用方回查网络，
        # 结论同样正确；注意别顺手按 load_inprogress_cache 的
        # `COALESCE(collect_type, 2)` 当成「看过」—— 那是列表排序的口径，
        # 拿来当收藏状态会默默写错一条快照。
        return ctype if 1 <= ctype <= 5 else 0

    def set_subject_collect_type(self, subject_id: int, collect_type: int) -> None:
        """写入该条目的 Bangumi 收藏状态（详情页状态选择器）。

        **本地只做"快照"，权威在 Bangumi**：这个值有三个来源 ——
          ① 用户在详情页点某个状态 → 先写远端，成功后才回写这里；
          ② 进入详情页时本地没有值（0）→ 回查远端补齐（见
             bridges/library 的 _CollectTypeWorker）；
          ③ 只是"想快点显示"时，从 `inprogress_cache`（上次全量同步的
             服务端收藏）同步搬过来，见 `cached_collect_type`。
        不更新 `updated_at`：那一列语义是"条目元数据最后变更时间"
        （名称/封面/集数），收藏状态是另一维度，混在一起会让
        "最近更新"这类排序失去意义。
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subjects SET collect_type=? WHERE id=?",
                (int(collect_type or 0), subject_id),
            )

    # ---------- 条目别名 ----------
    #
    # 存储形态：**换行分隔的纯文本**，而不是 JSON。
    #
    # 理由：别名里可能含 `,` `"` `[` 等字符（实测有
    # "Anohana: The Flower We Saw That Day" 这类带冒号的，也有含逗号的
    # 罗马音副标题）。JSON 需要转义、读取方还要 try/except 兜住脏数据；
    # 而别名**本身不可能含换行符**（Bangumi 的 infobox 值是单行文本），
    # 所以 `\n` 是天然安全的定界符 —— 存储和解析都是零风险的一行代码。
    @staticmethod
    def aliases_to_text(aliases: list[str]) -> str:
        """别名列表 → 存储文本（换行分隔，去掉空项与重复）。"""
        out: list[str] = []
        seen: set[str] = set()
        for a in aliases or []:
            name = str(a).strip().replace("\n", " ").replace("\r", " ")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return "\n".join(out)

    @staticmethod
    def aliases_from_text(text: str) -> list[str]:
        """存储文本 → 别名列表。空/None 安全。"""
        if not text:
            return []
        return [t.strip() for t in str(text).split("\n") if t.strip()]

    @staticmethod
    def aliases_from_subject(subj: dict) -> str:
        """从 Bangumi subject 响应提取别名并转成存储文本。"""
        from app.core.bangumi_api import extract_aliases
        return Database.aliases_to_text(extract_aliases(subj))

    # ---------- 制作公司 ----------
    def set_subject_studio(self, subject_id: int, studio: str) -> None:
        """写入动画制作公司（详情页补拉时用；空串不覆盖已有值）。"""
        if not (studio or "").strip():
            return
        with self._cursor() as cur:
            cur.execute(
                "UPDATE subjects SET studio=?, updated_at=? WHERE id=?",
                (studio.strip(), _now(), subject_id),
            )

    # ---------- 条目标签 ----------
    @staticmethod
    def tags_from_subject(subj: dict) -> list[tuple[str, int]]:
        """从 Bangumi subject 响应提取**前 10 个** tag，供入库。

        接口的 `tags` 已按 `count`（打此 tag 的人数）降序排列（实测多部
        条目全部严格降序，文档虽未明说但搜索接口的 SlimSubject.tags
        注明"前 10 个 tag"，佐证有序），直接截取即可，无需再排。
        """
        out: list[tuple[str, int]] = []
        for i, t in enumerate(subj.get("tags") or []):
            name = (t.get("name") or "").strip()
            if name:
                out.append((name, i))
            if len(out) >= 10:
                break
        return out

    def replace_subject_tags(self, subject_id: int, tags: list[tuple[str, int]]) -> None:
        """扫描 / 手动匹配时写入接口的前 10 个 tag（source='api'）。

        **重扫不复活用户删除**：同名 tag 已存在时保留其 `deleted` 标记 ——
        用户删掉的 tag 在重扫后又冒出来，会被感知为"程序坏了"。
        user tag（source='user'）完全不动。
        """
        with self._cursor() as cur:
            old = {
                r["name"]: r["deleted"]
                for r in cur.execute(
                    "SELECT name, deleted FROM subject_tags"
                    " WHERE subject_id=? AND source='api'",
                    (subject_id,),
                )
            }
            cur.execute(
                "DELETE FROM subject_tags WHERE subject_id=? AND source='api'",
                (subject_id,),
            )
            cur.executemany(
                "INSERT OR REPLACE INTO subject_tags"
                " (subject_id, name, source, pos, deleted) VALUES (?,?,'api',?,?)",
                [(subject_id, name, pos, old.get(name, 0)) for name, pos in tags],
            )

    def list_subject_tags(self, subject_id: int) -> list[dict]:
        """该条目的全部 tag，展示顺序：接口 tag 按原始顺序在前，用户 tag 在后。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT name, source, pos, deleted FROM subject_tags"
                " WHERE subject_id=?"
                " ORDER BY (source='user'), pos, name",
                (subject_id,),
            ).fetchall()
        return [
            {
                "name": r["name"],
                "isApi": r["source"] == "api",
                "deleted": bool(r["deleted"]),
            }
            for r in rows
        ]

    def all_subject_tags(self) -> dict[int, list[str]]:
        """全库条目的 tag：`{subject_id: [名字, ...]}`，一次查完。

        给海报墙的 tag 筛选做数据源。**必须一次查全量**再按条目分组 ——
        海报墙有几条就要几份列表，逐条调 `list_subject_tags` 会变成 N 次
        查询（本机 87 条时已能感到卡顿，库大了更明显）。

        顺序与 `list_subject_tags` 一致（接口 tag 按原顺序在前、用户 tag 在后），
        只取未删除的 —— 用户在详情页删掉的 tag 不应再出现在筛选里。
        """
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT subject_id, name FROM subject_tags"
                " WHERE deleted=0"
                " ORDER BY (source='user'), pos, name",
            ).fetchall()
        out: dict[int, list[str]] = {}
        for r in rows:
            out.setdefault(int(r["subject_id"]), []).append(r["name"])
        return out

    def has_subject_tags(self, subject_id: int) -> bool:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT 1 FROM subject_tags WHERE subject_id=? LIMIT 1",
                (subject_id,),
            ).fetchone()
            return row is not None

    def save_edited_tags(
        self,
        subject_id: int,
        api_states: list[dict],
        user_names: list[str],
    ) -> None:
        """保存详情页的 tag 编辑结果（确定按钮）。

        - `api_states`：`[{"name": str, "deleted": bool}]`，只**更新**接口
          tag 的删除标记（不动 pos / source —— "恢复原状"的语义）；
        - `user_names`：用户 tag 的最终名单，整体替换（删掉的不留痕）。
        """
        with self._cursor() as cur:
            for st in api_states:
                name = (st.get("name") or "").strip()
                if not name:
                    continue
                cur.execute(
                    "UPDATE subject_tags SET deleted=?"
                    " WHERE subject_id=? AND name=? AND source='api'",
                    (1 if st.get("deleted") else 0, subject_id, name),
                )
            cur.execute(
                "DELETE FROM subject_tags WHERE subject_id=? AND source='user'",
                (subject_id,),
            )
            cur.executemany(
                "INSERT OR REPLACE INTO subject_tags"
                " (subject_id, name, source, pos, deleted) VALUES (?,?,'user',?,0)",
                [(subject_id, n, 100 + i) for i, n in enumerate(user_names)],
            )

    # ---------- episodes ----------
    def upsert_episode(self, **fields: Any) -> int:
        file_path = fields.get("file_path")
        if not file_path:
            raise ValueError("file_path 必填")
        cols = list(fields.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "file_path")
        sql = (
            f"INSERT INTO episodes ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(file_path) DO UPDATE SET {updates}"
        )
        with self._cursor() as cur:
            cur.execute(sql, [fields[c] for c in cols])
            cur.execute("SELECT id FROM episodes WHERE file_path=?", (file_path,))
            return int(cur.fetchone()["id"])

    def list_episodes(self, subject_id: int) -> list[Episode]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM episodes WHERE subject_id=? ORDER BY ep_index, file_path",
                (subject_id,),
            )
            return [Episode(**dict(r)) for r in cur.fetchall()]

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        """按主键取单集（进度监控需要"现查最新 watched 状态"，见 monitor._tick）。"""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM episodes WHERE id=?", (episode_id,))
            row = cur.fetchone()
            return Episode(**dict(row)) if row else None

    def find_episode_by_path(self, file_path: str) -> Optional[Episode]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM episodes WHERE file_path=?", (file_path,))
            row = cur.fetchone()
            return Episode(**dict(row)) if row else None

    def update_progress(self, episode_id: int, progress: float) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE episodes SET watch_progress=? WHERE id=?",
                (float(progress), episode_id),
            )
            cur.execute(
                "INSERT INTO watch_log(episode_id, progress, created_at) VALUES(?,?,?)",
                (episode_id, float(progress), _now()),
            )

    def update_episode_title(
        self,
        episode_id: int,
        title: str,
        bangumi_ep_id: Optional[int] = None,
    ) -> None:
        """更新集标题与 Bangumi 集 ID（手动匹配后回填，不改本地文件）。"""
        with self._cursor() as cur:
            if bangumi_ep_id is not None:
                cur.execute(
                    "UPDATE episodes SET title=?, bangumi_ep_id=? WHERE id=?",
                    (title, bangumi_ep_id, episode_id),
                )
            else:
                cur.execute(
                    "UPDATE episodes SET title=? WHERE id=?",
                    (title, episode_id),
                )

    def mark_watched(self, episode_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE episodes SET watched=1, watched_at=?, watch_progress=1.0 WHERE id=?",
                (_now(), episode_id),
            )

    # ---------- 动态时间线 ----------
    def list_watched_timeline(self, limit: int = 200) -> list[TimelineEntry]:
        """按 watched_at 倒序列出已看集数（JOIN subjects 取动漫名）。"""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT e.id         AS episode_id,
                       e.ep_index   AS ep_index,
                       e.title      AS ep_title,
                       e.watched_at AS watched_at,
                       s.id         AS subject_id,
                       COALESCE(NULLIF(s.name_cn, ''), s.name) AS subject_name
                FROM episodes e
                JOIN subjects s ON s.id = e.subject_id
                WHERE e.watched = 1 AND e.watched_at IS NOT NULL
                ORDER BY e.watched_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [TimelineEntry(**dict(r)) for r in cur.fetchall()]

    def clear_subject_episodes(self, subject_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM episodes WHERE subject_id=?", (subject_id,))

    def prune_episodes(self, subject_id: int, keep_ids: set[int]) -> int:
        """删除该条目下**不在 `keep_ids` 里**的集数记录，返回删除条数。

        用途：重扫后清理陈旧行（见 scanner._fill_episodes 末尾的说明）。
        `upsert_episode` 以 file_path 为唯一键、只增不改，因此：
          - 用户删掉的集，重扫不会消失；
          - 集数规则变化（开关切换）时旧编号的行会变成孤儿。
        两者都会让详情页出现"已经不存在/重复的集"。

        注意保留 `watched` 数据：这里删的是"本次扫描未产生"的行 ——
        它们对应的文件已经不在磁盘上了，观看记录没有保留意义。

        `keep_ids` 为空时**直接返回 0**（不做任何删除）：那意味着本次扫描
        没产出任何集数（如目录暂时不可访问），删光会造成数据丢失。
        """
        if not keep_ids:
            return 0
        marks = ",".join("?" for _ in keep_ids)
        with self._cursor() as cur:
            cur.execute(
                f"DELETE FROM episodes WHERE subject_id=? AND id NOT IN ({marks})",
                [subject_id, *keep_ids],
            )
            return int(cur.rowcount)

    def find_subject_by_bangumi_id(self, bangumi_id: int) -> Optional[int]:
        """按 Bangumi ID 查本地 subject 主键（F18 本地关联）。"""
        with self._cursor() as cur:
            cur.execute("SELECT id FROM subjects WHERE bangumi_id=?", (bangumi_id,))
            row = cur.fetchone()
            return int(row["id"]) if row else None

    # ---------- 匹配状态与手动匹配 ----------
    def find_subject_by_folder(self, folder_path: str) -> Optional[Subject]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM subjects WHERE folder_path=? ORDER BY id LIMIT 1",
                (folder_path,),
            )
            row = cur.fetchone()
            return Subject(**dict(row)) if row else None

    def find_manual_subject_by_folder(self, folder_path: str) -> Optional[Subject]:
        """查该目录下**用户手动指定过 Bangumi 条目**的记录（重扫时保护，不覆盖）。

        **必须排除 bangumi_id 为空的行**（踩坑，实测）：`match_state='manual'`
        有两种来源 ——
            ① 用户在「重新匹配」里手选了一部（`set_manual_match`，有 bangumi_id）
            ② 「添加动漫」在没填 Token / 主动不选匹配时建的**纯本地条目**
               （`upsert_local_subject`，bangumi_id 为 NULL）
        ②并没有"用户指定的匹配结果"可保护：若把它一并当成手动匹配跳过，
        用户之后填好 Token 点「重新扫描」只会得到"跳过：该目录已有手动匹配"，
        非得删掉重加才能拿到数据（实测反馈）。判据用 bangumi_id 才准确 ——
        有它是"用户的匹配决定"，没有它只是个还没匹配上的本地条目。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM subjects WHERE folder_path=? AND match_state='manual'"
                " AND bangumi_id IS NOT NULL ORDER BY id LIMIT 1",
                (folder_path,),
            )
            row = cur.fetchone()
            return Subject(**dict(row)) if row else None

    def upsert_pending_subject(
        self,
        folder_path: str,
        display_name: str,
        series_name: str = "",
    ) -> int:
        """写入/复用「待手动确认」条目（无 bangumi_id）。

        pending 条目没有 bangumi_id，无法用 ON CONFLICT(bangumi_id)，
        因此按 folder_path 手工判重。
        """
        existing = self.find_subject_by_folder(folder_path)
        if existing is not None:
            # 已是 manual 的不动；auto/pending 更新展示名与状态
            if existing.match_state != "manual":
                with self._cursor() as cur:
                    cur.execute(
                        "UPDATE subjects SET name=?, name_cn=?, series_name=?,"
                        " match_state='pending', updated_at=? WHERE id=?",
                        (display_name, display_name, series_name, _now(), existing.id),
                    )
            return existing.id

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO subjects
                    (bangumi_id, name, name_cn, cover_url, cover_path, total_eps,
                     folder_path, series_name, match_state, updated_at)
                VALUES (NULL,?,?,?,?,?,?,?,'pending',?)
                """,
                (display_name, display_name, "", "", 0,
                 folder_path, series_name, _now()),
            )
            return int(cur.lastrowid)

    def upsert_local_subject(
        self,
        folder_path: str,
        display_name: str,
        series_name: str = "",
        total_eps: int = 0,
    ) -> int:
        """写入/复用「本地条目」——**没有 bangumi_id** 的漫画。

        用途：「添加动漫」在**未填 Token** 时的路径（用户明确要求"没填
        Token 就不匹配"）。此时不联网，把目录里的视频直接作为本地条目入库。

        与 `upsert_pending_subject` 的区别只在 `match_state`：
            pending —— "匹配过、结果需人工确认"（详情页会弹「⚠ 匹配待确认」）
            manual  —— "由用户指定/认可的状态"（详情页显示「已手动指定」）
        用户是**主动选择不匹配**的，并没有失败的匹配要他处理，套用 pending
        会凭空多出一条待办提示，所以这里用 manual。

        **为什么不能用 `upsert_subject(bangumi_id=0)`**（踩坑）：subjects 表的
        `bangumi_id` 有 UNIQUE 约束，而 0 只能存在一行 —— 第二条未匹配番会
        把第一条**覆盖**掉。未匹配必须写 NULL（SQLite 的 UNIQUE 允许多个
        NULL 共存），并按 `folder_path` 手工判重（同 pending 的做法）。
        """
        existing = self.find_subject_by_folder(folder_path)
        if existing is not None:
            # 已有 manual 的不动（用户手动指定过就不该被本地添加覆盖）；
            # auto/pending 升级为 manual —— 用户这次明确要求"只加本地"。
            if existing.match_state != "manual" or not existing.bangumi_id:
                with self._cursor() as cur:
                    cur.execute(
                        "UPDATE subjects SET name=?, name_cn=?, series_name=?,"
                        " total_eps=?, match_state='manual', updated_at=?"
                        " WHERE id=?",
                        (display_name, display_name, series_name, total_eps,
                         _now(), existing.id),
                    )
            return existing.id

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO subjects
                    (bangumi_id, name, name_cn, cover_url, cover_path, total_eps,
                     folder_path, series_name, match_state, updated_at)
                VALUES (NULL,?,?,?,?,?,?,?,'manual',?)
                """,
                (display_name, display_name, "", "", total_eps,
                 folder_path, series_name, _now()),
            )
            return int(cur.lastrowid)

    def set_manual_match(
        self,
        subject_id: int,
        bangumi_id: int,
        name: str,
        name_cn: str,
        cover_url: str = "",
        cover_path: str = "",
        total_eps: int = 0,
        aliases: str = "",
        studio: str = "",
    ) -> None:
        """用户手动指定 Bangumi 条目（match_state='manual'，重扫不覆盖）。

        `aliases` / `studio` 传 `Database.aliases_from_subject(subj)` /
        `BangumiClient.studio_for(subj, bangumi_id)` 的结果；
        不传（旧调用方）时**保留原值不覆盖** —— 手动匹配的候选同样来自
        搜索接口，通常能拿到别名与公司。
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE subjects SET
                    bangumi_id=?, name=?, name_cn=?, cover_url=?, cover_path=?,
                    total_eps=?, aliases=COALESCE(NULLIF(?, ''), aliases),
                    studio=COALESCE(NULLIF(?, ''), studio),
                    match_state='manual', updated_at=?
                WHERE id=?
                """,
                (bangumi_id, name, name_cn, cover_url, cover_path,
                 total_eps, aliases or "", studio or "", _now(), subject_id),
            )

    def delete_subject(self, subject_id: int) -> None:
        """删除条目及其**全部关联数据**。

        **不要只删 subjects 主表**（踩坑）：`episodes` 建表时写了
        `ON DELETE CASCADE`，但 SQLite **默认不开外键约束**（需要
        `PRAGMA foreign_keys=ON`，本项目未开），级联不会生效 —— 只删主表
        会留下孤儿集数行；`subject_tags` 更是完全没有级联。

        所以这里**显式逐表清理**：
            episodes      —— 集数（详情页左列的数据源）
            subject_tags  —— 接口/用户标签（海报墙 tag 筛选的数据源）

        说明：`watched_episodes` 按 **bangumi_id** 存的是 Bangumi 侧记录，
        不属于本条的从属数据（同一部番重扫回来后仍要用），故**不删**。
        """
        with self._cursor() as cur:
            cur.execute("DELETE FROM episodes WHERE subject_id=?", (subject_id,))
            cur.execute("DELETE FROM subject_tags WHERE subject_id=?", (subject_id,))
            cur.execute("DELETE FROM subjects WHERE id=?", (subject_id,))

    # ---------- 聚合展示 ----------
    def list_series_groups(self) -> dict[str, list[Subject]]:
        """按 series_name 分组（空 series_name 视为独立条目）。"""
        groups: dict[str, list[Subject]] = {}
        for s in self.list_subjects():
            key = s.series_name or ""
            groups.setdefault(key, []).append(s)
        return groups

    def count_locally_watched_eps(self, subject_id: int) -> int:
        """本地已看集数（F19 订阅页「已完成集数」）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM episodes WHERE subject_id=? AND watched=1",
                (subject_id,),
            )
            return int(cur.fetchone()["n"])

    def max_watched_ep_index(self, subject_id: int) -> int:
        """本地已看的**最大集号**（「看到第几集」），没看过返回 0。

        **与 `count_locally_watched_eps()` 的区别很重要**（这是一个踩过的坑）：
            count  —— 已看**条数**（看了 1 集 → 1）
            max    —— 已看的**最大集号**（看了第 9 集 → 9）
        只有"从第 1 集一集不落看下来"时两者才相等。中间的漏看（或像本次
        实测那样只看了第 9 集）会让 count 远小于真实进度。

        详情页/在看页的「进度」语义是 Bangumi 的 `ep_status` = **看到第 N 集**，
        所以必须用这个 `max` 版本；拿 count 去比会把"第 9 集已看"算成
        `max(8, 1) = 8`，页面看起来完全没反应。

        只统计正片（`ep_label` 为空）：附加内容（SP/OVA）的 `ep_index` 是
        `main_max + 1000 + n` 这种排序值，拿来当"集号"会得到上千的假进度。
        取整后返回 int —— `ep_index` 允许小数（12.5 这种），但"看到第几集"
        是个整数概念，向下取整不会高估进度。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT MAX(ep_index) AS m FROM episodes"
                " WHERE subject_id=? AND watched=1"
                " AND (ep_label IS NULL OR ep_label='')",
                (subject_id,),
            )
            row = cur.fetchone()
            if not row or row["m"] is None:
                return 0
            try:
                return int(float(row["m"]))
            except (TypeError, ValueError):
                return 0

    def list_local_ep_indices(self, subject_id: int) -> set[float]:
        """本地已有集数序号集合（F19 三层查重第一层）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT ep_index FROM episodes WHERE subject_id=?", (subject_id,)
            )
            return {float(r["ep_index"]) for r in cur.fetchall() if r["ep_index"] is not None}

    # ---------- F18：在看缓存 ----------
    def replace_inprogress_cache(self, items: list[dict],
                                 truncated: bool = False) -> None:
        """整体替换在看缓存（先清后插，保证与线上一致）。

        **两个时间字段别搞混**（v4 起分列存储）：
            updated_at            —— 本次写入时刻（整批相同），供 TTL 判断
            collection_updated_at —— Bangumi 的收藏修改时间（逐条不同），
                                     供「最近 N 部」排序
        早期版本只存前者，导致「最近 N 部」实际退化成「按名字排序的前 N 部」。

        **顺带把 `subjects.collect_type` 与这批新数据对齐**（同一事务，
        见末尾那条 UPDATE）：详情页的状态选择器读的是那一列，
        而它可能来自"上次同步"甚至更早（`cached_collect_type` 那条快路径
        也会写它）。不在这里对齐的话，用户改过收藏后本地会一直显示旧状态，
        且因为"有值就不再回查"而**永远不会自愈**。
        搬这一列是安全的：收藏同步拉的就是服务端全量状态，
        至少和任何本地快照一样新。

        `truncated=True` 表示这次拉取触顶截断（见 `iter_user_collections`
        的 max_items）：此时**不会**把"名单外的条目"当成"已取消收藏"，
        见下面第 ② 步的说明。
        """
        now = _now()
        with self._cursor() as cur:
            cur.execute("DELETE FROM inprogress_cache")
            cur.executemany(
                """
                INSERT OR REPLACE INTO inprogress_cache
                    (bangumi_id, name, name_cn, cover_url,
                     ep_status, total_eps, collect_type, updated_at,
                     collection_updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        it.get("bangumi_id"),
                        it.get("name", ""),
                        it.get("name_cn", ""),
                        it.get("cover_url", ""),
                        int(it.get("ep_status") or 0),
                        int(it.get("total_eps") or 0),
                        int(it.get("collect_type") or 3),
                        now,
                        it.get("collection_updated_at") or "",
                    )
                    for it in items
                    if it.get("bangumi_id")
                ],
            )
            # ① 把**这次拉到的**状态写回 subjects.collect_type（同一条 SQL
            # 也用于 v10 迁移）。只覆盖"缓存里有行"的条目：名单外的条目
            # 保持原快照，不会被清成"未知"。
            cur.execute(_COLLECT_TYPE_BACKFILL_SQL)
            # ② 这次拉取是全量的（除触顶截断），所以**名单里没有的已匹配条目
            # = 服务端已不在收藏里** → 本地快照清成 0（"未知"）。
            #
            # **不做这一步会留下幽灵**：用户取消收藏后，本地快照仍是「在看」，
            # 「在看」页会把它当"本地标记的条目"继续列出来（见 LibraryBridge
            # 的合并段），而缓存里早已没有它 —— 一部已经取消收藏的番永远挂着。
            # 用整库的判据（而不是"与上一轮缓存比差集"）：后者漏掉
            # "更早以前就消失、当时没清干净"的遗留（实测：CLANNAD 一直显示
            # 在看在）。
            #
            # **截断时跳过**（`truncated`）：拉取触顶（MAX_ITEMS）时，
            # 名单外的条目不代表"已取消收藏"，只是没拉到 —— 此时宁可留着
            # 旧值，也不能把真实状态批量清掉。
            if not truncated:
                cur.execute(_COLLECT_TYPE_CLEAR_STALE_SQL)
                if cur.rowcount:
                    log.info("同步：%s 个条目已不在收藏里，本地状态清为未知",
                             cur.rowcount)

    def load_inprogress_cache(
        self, collect_type: Optional[int] = None
    ) -> list[InProgressItem]:
        """按「Bangumi 收藏修改时间」倒序列出。

        `collect_type` 给定时只取该状态（2 = 看过，3 = 在看）。
        **表里两类都有**：收藏页要 `collect_type=2`（页面标题就是「看过」），
        动态页的候选则要全部（见 `InProgressBridge`）—— 所以调用方必须
        按用途明确传参，别默认拿全量。

        排序用 COALESCE 兜住老库迁移后的 NULL/空值（回落到缓存写入时间），
        这样迁移当天也不会把顺序打乱成"未知"。
        """
        where, params = "", ()
        if collect_type is not None:
            # COALESCE 兜住迁移前写入的行（collect_type 为 NULL 时按"看过"）
            where = "WHERE COALESCE(collect_type, 2) = ?"
            params = (int(collect_type),)
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM inprogress_cache
                {where}
                ORDER BY COALESCE(NULLIF(collection_updated_at, ''), updated_at)
                         DESC,
                         name_cn
                """,
                params,
            )
            return [InProgressItem(**dict(r)) for r in cur.fetchall()]

    def inprogress_cache_age(self) -> Optional[float]:
        """缓存距今秒数；无缓存返回 None。"""
        with self._cursor() as cur:
            cur.execute("SELECT MAX(updated_at) AS t FROM inprogress_cache")
            row = cur.fetchone()
            if not row or not row["t"]:
                return None
            try:
                dt = datetime.fromisoformat(row["t"])
            except ValueError:
                return None
            return (datetime.now(dt.tzinfo) - dt).total_seconds()

    # ---------- F20：集级观看记录 ----------
    @staticmethod
    def _watched_ep_rows(items: list[dict]) -> list[tuple]:
        """集级记录 dict → executemany 用的元组序列。

        入参每项：
            { "bangumi_ep_id", "bangumi_id", "subject_id",
              "subject_name", "ep_index", "ep_name", "watched_at" }
        """
        return [
            (
                int(it.get("bangumi_ep_id") or 0),
                int(it.get("subject_id") or 0),
                int(it.get("bangumi_id") or 0),
                it.get("subject_name") or "",
                float(it.get("ep_index") or 0),
                it.get("ep_name") or "",
                it.get("watched_at") or "",
            )
            for it in items
            if it.get("bangumi_ep_id")
        ]

    def replace_subject_watched_episodes(
        self,
        bangumi_id: int,
        items: list[dict],
        collection_updated_at: str,
        ep_status: int,
    ) -> None:
        """**按单部**替换集级记录，并写入同步水位（同一个事务）。

        为什么不再整表替换：同步已改成增量的 —— 每次只重拉"收藏变过 +
        在看 + 超期"的那十几部，整表 DELETE 会把没重拉的部的历史一起抹掉。

        **入参为空也要执行**（与旧的整表版本相反）：调用方只在"请求成功"
        时调这里，而"成功但这部确实没有逐集标记"是个**终态答案** —— 必须
        落水位，否则下次还会重拉它（见 ep_sync_state 表的说明）。
        "请求成功但记录被过滤光了"这种情况由调用方拦下、根本不调这里。

        水位必须与记录在**同一个 `with self._cursor()`** 里：危险方向是
        "水位写了、记录没写"（该部永远不再同步）；反过来只是下次重拉。

        `ep_rows` 直接用 `len(rows)`，不接受外部传入 —— 避免两处口径不一致。
        """
        rows = self._watched_ep_rows(items)
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM watched_episodes WHERE bangumi_id=?",
                (int(bangumi_id),),
            )
            if rows:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO watched_episodes
                        (bangumi_ep_id, subject_id, bangumi_id, subject_name,
                         ep_index, ep_name, watched_at)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    rows,
                )
            cur.execute(
                """
                INSERT OR REPLACE INTO ep_sync_state
                    (bangumi_id, collection_updated_at, ep_status, ep_rows,
                     synced_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    int(bangumi_id),
                    collection_updated_at or "",
                    int(ep_status or 0),
                    len(rows),
                    _now(),
                ),
            )

    def list_watched_episodes(self, limit: int = 5000) -> list[dict]:
        """按标记时间倒序列出集级观看记录。

        默认上限 5000：全量同步后本表约 1000+ 行（实测密度 ≈6 条/部 × 160 部），
        旧默认 500 会静默截断掉历史。动态页的分页在 QML 侧做，这里给足。
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT bangumi_ep_id, subject_id, bangumi_id, subject_name,
                       ep_index, ep_name, watched_at
                FROM watched_episodes
                WHERE watched_at IS NOT NULL AND watched_at != ''
                ORDER BY watched_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def pending_uploads(
        self, subject_ids: Optional[list[int]] = None
    ) -> dict[int, list[dict]]:
        """**本地看过、Bangumi 未标**的集。

        返回 `{subject_id: [{episode_id, bangumi_ep_id, ep_index, ep_title}]}`
        （`episode_id` 是本地 `episodes.id` —— 小窗的勾选单位）。

        **「待补传」的判据只在这里定义一次**（小窗里的数字、按钮上的数字、
        实际上传时的筛选，三处都走这一个查询）—— 否则口径一旦分叉，
        用户会看到"显示 3 条却只传了 1 条"这类对不上的现象 ✗。

        判据三条件（缺一不可）：
            episodes.watched = 1        —— 本地确实看过
            episodes.bangumi_ep_id > 0  —— 有对应集号（没有就传不了）
            watched_episodes 里没有该集 —— Bangumi 上还没标（幂等的依据）
        """
        where, params = "", []
        if subject_ids:
            ids = [int(i) for i in subject_ids if i]
            if not ids:
                return {}
            where = " AND e.subject_id IN (%s)" % ",".join("?" for _ in ids)
            params = ids
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT e.subject_id AS sid, e.bangumi_ep_id AS ep_id,
                       e.id AS episode_id, e.ep_index AS ep_index,
                       e.title AS ep_title
                FROM episodes e
                LEFT JOIN watched_episodes w ON w.bangumi_ep_id = e.bangumi_ep_id
                WHERE e.watched = 1 AND e.bangumi_ep_id > 0
                  AND w.bangumi_ep_id IS NULL{where}
                ORDER BY e.subject_id, e.ep_index
                """,
                params,
            )
            out: dict[int, list[dict]] = {}
            for r in cur.fetchall():
                out.setdefault(int(r["sid"]), []).append(
                    {"bangumi_ep_id": int(r["ep_id"]),
                     # 本地集 id：小窗勾选的单位就是它（一行 = 一集）
                     "episode_id": int(r["episode_id"]),
                     "ep_index": float(r["ep_index"] or 0),
                     "ep_title": r["ep_title"] or ""})
            return out

    def blocked_upload_counts(
        self, subject_ids: Optional[list[int]] = None
    ) -> dict[int, int]:
        """`{subject_id: 本地看过但没有集号、无法补传的集数}`。

        实测本机 1446 集里有 455 集是这个情况（扫描时没拿到集数元数据）——
        它们必须**被明确告知跳过**，不能静默 ✗。
        """
        where, params = "", []
        if subject_ids:
            ids = [int(i) for i in subject_ids if i]
            if not ids:
                return {}
            where = " AND subject_id IN (%s)" % ",".join("?" for _ in ids)
            params = ids
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT subject_id AS sid, COUNT(*) AS n
                FROM episodes
                WHERE watched = 1 AND (bangumi_ep_id IS NULL OR bangumi_ep_id = 0){where}
                GROUP BY subject_id
                """,
                params,
            )
            return {int(r["sid"]): int(r["n"]) for r in cur.fetchall()}

    def upsert_watched_episodes(self, items: list[dict]) -> int:
        """按集 upsert 集级记录（**上传成功后立刻写回本地**）。

        为什么需要它：补传成功后，本地 `watched_episodes` 里还没有这一集 ——
        动态页那一行要等**下一次集级同步**才会多出 `bgm` 标记 ✗。
        用户刚点完「上传」却看不到任何变化，会以为没生效（实测反馈的同类问题）。
        这里直接按 `bangumi_ep_id` upsert：不必等同步，界面立刻就是对的 ✓
        （下一次同步会拉到同一集并覆盖，值一致 ✓）。

        `watched_at` 由调用方给（补传用**上传时刻** —— 与 Bangumi 网页一致，
        因为服务端记的就是那一刻）。
        """
        rows = self._watched_ep_rows(items)
        if not rows:
            return 0
        with self._cursor() as cur:
            cur.executemany(
                """
                INSERT OR REPLACE INTO watched_episodes
                    (bangumi_ep_id, subject_id, bangumi_id, subject_name,
                     ep_index, ep_name, watched_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                rows,
            )
            return len(rows)

    def watched_episode_ids(self, bangumi_id: int) -> set[int]:
        """该条目在 **Bangumi 上已标记「看过」** 的集 ID 集合。

        用途：「上传」的**幂等判断** —— 只上传"本地看过但 Bangumi 上还没有"的集，
        重复点按钮不会重复 POST；某几条失败后再点也只补那几条。
        注意判据是"拉回来的集级记录"（`watched_episodes`），也就是 Bangumi
        当前的真实状态，而不是本地标记 ✗。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT bangumi_ep_id FROM watched_episodes WHERE bangumi_id=?",
                (int(bangumi_id),),
            )
            return {int(r["bangumi_ep_id"]) for r in cur.fetchall()}

    def clear_watched_episodes(self) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM watched_episodes")

    def count_watched_episodes(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM watched_episodes")
            return int(cur.fetchone()["n"])

    # ---------- F20：同步水位 ----------
    def load_ep_sync_state(self) -> dict[int, EpSyncState]:
        """读出全部水位，供 `sync_candidates()` 判断哪些部要重拉。

        一次查完（一部一行，量级与收藏数同级），不要在候选循环里逐部查。
        """
        with self._cursor() as cur:
            cur.execute("SELECT * FROM ep_sync_state")
            return {
                int(r["bangumi_id"]): EpSyncState(**dict(r))
                for r in cur.fetchall()
            }

    def clear_ep_sync_state(self) -> None:
        """清空水位（"强制全量重新同步"的入口）。

        **注意**：单独清水位会让所有条目在下一次刷新时重拉一遍，这是
        预期行为；但**不要**与 `clear_watched_episodes()` 的语义搞混 ——
        见 `replace_subject_watched_episodes` 的说明。
        """
        with self._cursor() as cur:
            cur.execute("DELETE FROM ep_sync_state")

    def clear_ep_sync_state_for(self, bangumi_id: int) -> None:
        """让**单个**条目下次刷新时重拉（本软件自己标记完一集后调用）。"""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM ep_sync_state WHERE bangumi_id=?",
                (int(bangumi_id),),
            )

    def count_ep_sync_state(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM ep_sync_state")
            return int(cur.fetchone()["n"])

    # ---------- F19：订阅源 ----------
    def add_rss_source(
        self,
        name: str,
        url: str,
        bangumi_id: Optional[int] = None,
        rule: str = "new_only",
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO rss_sources(name, url, bangumi_id, enabled, rule, created_at)
                VALUES (?,?,?,1,?,?)
                ON CONFLICT(url) DO UPDATE SET name=excluded.name
                """,
                (name, url, bangumi_id, rule, _now()),
            )
            cur.execute("SELECT id FROM rss_sources WHERE url=?", (url,))
            return int(cur.fetchone()["id"])

    def list_rss_sources(self, only_enabled: bool = False) -> list[RssSource]:
        sql = "SELECT * FROM rss_sources"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with self._cursor() as cur:
            cur.execute(sql)
            out: list[RssSource] = []
            for r in cur.fetchall():
                d = dict(r)
                d["enabled"] = bool(d["enabled"])
                out.append(RssSource(**d))
            return out

    def update_rss_source(self, source_id: int, **fields: Any) -> None:
        allowed = {
            "name", "url", "bangumi_id", "enabled",
            "rule", "last_poll_at", "last_error",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        assignments = ",".join(f"{k}=?" for k in cols)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE rss_sources SET {assignments} WHERE id=?",
                [*cols.values(), source_id],
            )

    def delete_rss_source(self, source_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM rss_sources WHERE id=?", (source_id,))

    # ---------- F19：下载记录 ----------
    def is_episode_downloaded(self, source_id: int, ep_index: float) -> bool:
        """第三层查重：该订阅的该集是否已有下载记录。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM download_history WHERE source_id=? AND ep_index=? LIMIT 1",
                (source_id, float(ep_index)),
            )
            return cur.fetchone() is not None

    def add_download_record(
        self,
        source_id: int,
        ep_index: float,
        torrent_title: str,
        magnet: str,
        subject_id: Optional[int] = None,
        status: str = "pending",
    ) -> int:
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO download_history
                    (source_id, subject_id, ep_index, torrent_title,
                     magnet, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id, ep_index) DO UPDATE SET
                    torrent_title=excluded.torrent_title,
                    magnet=excluded.magnet,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (source_id, subject_id, float(ep_index), torrent_title,
                 magnet, status, now, now),
            )
            cur.execute(
                "SELECT id FROM download_history WHERE source_id=? AND ep_index=?",
                (source_id, float(ep_index)),
            )
            return int(cur.fetchone()["id"])

    def update_download_status(
        self,
        record_id: int,
        status: str,
        torrent_hash: str = "",
    ) -> None:
        with self._cursor() as cur:
            if torrent_hash:
                cur.execute(
                    "UPDATE download_history SET status=?, torrent_hash=?, updated_at=? WHERE id=?",
                    (status, torrent_hash, _now(), record_id),
                )
            else:
                cur.execute(
                    "UPDATE download_history SET status=?, updated_at=? WHERE id=?",
                    (status, _now(), record_id),
                )

    def list_downloads(self, source_id: Optional[int] = None) -> list[DownloadRecord]:
        with self._cursor() as cur:
            if source_id is None:
                cur.execute("SELECT * FROM download_history ORDER BY ep_index DESC")
            else:
                cur.execute(
                    "SELECT * FROM download_history WHERE source_id=? ORDER BY ep_index DESC",
                    (source_id,),
                )
            return [DownloadRecord(**dict(r)) for r in cur.fetchall()]

    def count_downloads_by_status(self, source_id: int) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS n FROM download_history
                WHERE source_id=? GROUP BY status
                """,
                (source_id,),
            )
            return {r["status"]: int(r["n"]) for r in cur.fetchall()}
