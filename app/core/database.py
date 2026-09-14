"""SQLite 数据访问层。

- 标准库 sqlite3，check_same_thread=False + 线程锁
- 对外只暴露仓储方法，UI 不直接操作 SQL
- meta 表存 schema_version，便于后续迁移
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.utils.paths import database_path

SCHEMA_VERSION = 1

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
    match_state  TEXT DEFAULT 'auto',
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
"""


@dataclass
class Subject:
    id: int
    bangumi_id: int
    name: str
    name_cn: str
    cover_url: str
    cover_path: str
    total_eps: int
    folder_path: str
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
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

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
