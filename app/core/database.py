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

SCHEMA_VERSION = 3

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
    watched_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_subject ON episodes(subject_id);

CREATE TABLE IF NOT EXISTS watch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER,
    progress   REAL,
    created_at TEXT
);

-- F18：在看列表缓存（离线降级 / 首屏秒开）
CREATE TABLE IF NOT EXISTS inprogress_cache (
    bangumi_id   INTEGER PRIMARY KEY,
    name         TEXT,
    name_cn      TEXT,
    cover_url    TEXT,
    ep_status    INTEGER,
    total_eps    INTEGER,
    collect_type INTEGER,
    updated_at   TEXT
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
    match_state: str
    updated_at: str


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
    # 运行时填充（不落库）
    local_subject_id: Optional[int] = None


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
        """查该目录下手动匹配的条目（重扫时保护，不覆盖）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM subjects WHERE folder_path=? AND match_state='manual'"
                " ORDER BY id LIMIT 1",
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

    def set_manual_match(
        self,
        subject_id: int,
        bangumi_id: int,
        name: str,
        name_cn: str,
        cover_url: str = "",
        cover_path: str = "",
        total_eps: int = 0,
    ) -> None:
        """用户手动指定 Bangumi 条目（match_state='manual'，重扫不覆盖）。"""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE subjects SET
                    bangumi_id=?, name=?, name_cn=?, cover_url=?, cover_path=?,
                    total_eps=?, match_state='manual', updated_at=?
                WHERE id=?
                """,
                (bangumi_id, name, name_cn, cover_url, cover_path,
                 total_eps, _now(), subject_id),
            )

    def delete_subject(self, subject_id: int) -> None:
        """删除条目及其集数（episodes 有 ON DELETE CASCADE，但需开外键）。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM episodes WHERE subject_id=?", (subject_id,))
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

    def list_local_ep_indices(self, subject_id: int) -> set[float]:
        """本地已有集数序号集合（F19 三层查重第一层）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT ep_index FROM episodes WHERE subject_id=?", (subject_id,)
            )
            return {float(r["ep_index"]) for r in cur.fetchall() if r["ep_index"] is not None}

    # ---------- F18：在看缓存 ----------
    def replace_inprogress_cache(self, items: list[dict]) -> None:
        """整体替换在看缓存（先清后插，保证与线上一致）。"""
        now = _now()
        with self._cursor() as cur:
            cur.execute("DELETE FROM inprogress_cache")
            cur.executemany(
                """
                INSERT OR REPLACE INTO inprogress_cache
                    (bangumi_id, name, name_cn, cover_url,
                     ep_status, total_eps, collect_type, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
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
                    )
                    for it in items
                    if it.get("bangumi_id")
                ],
            )

    def load_inprogress_cache(self) -> list[InProgressItem]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM inprogress_cache ORDER BY updated_at DESC, name_cn")
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
