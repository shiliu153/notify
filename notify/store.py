"""SQLite 状态：去重键与限流计数。

兼容老 SQLite（CentOS 7 自带 3.7.17）：只用 INSERT OR REPLACE，
不用 ON CONFLICT ... DO UPDATE。连接以 isolation_level=None 打开，
事务显式 BEGIN IMMEDIATE，避免隐式事务互相打架。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent (
    dedup_key TEXT PRIMARY KEY,
    sent_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    channel   TEXT NOT NULL,
    ok        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
"""


def local_day_start(ts: float) -> float:
    """ts 所在本地自然日的 00:00:00。"""
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class Store:
    def __init__(self, path, now: Optional[Callable[[], float]] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or time.time
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 去重 ----

    def last_sent(self, dedup_key: str) -> Optional[float]:
        row = self._conn.execute(
            "SELECT sent_at FROM sent WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return float(row[0]) if row else None

    def is_duplicate(self, dedup_key: str, window_minutes: int) -> bool:
        if window_minutes <= 0:
            return False
        last = self.last_sent(dedup_key)
        if last is None:
            return False
        return (self._now() - last) < window_minutes * 60

    def mark_sent(self, dedup_key: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO sent (dedup_key, sent_at) VALUES (?, ?)",
                (dedup_key, self._now()),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---- 限流 ----

    def record_attempt(self, channel: str, ok: bool) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO attempts (ts, channel, ok) VALUES (?, ?, ?)",
                (self._now(), channel, 1 if ok else 0),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def count_attempts(self, since_ts: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE ts >= ?", (since_ts,)
        ).fetchone()
        return int(row[0])

    def rate_limited(self, per_minute: int, per_day: int) -> Optional[str]:
        now = self._now()
        if per_minute and self.count_attempts(now - 60) >= per_minute:
            return "per_minute"
        if per_day and self.count_attempts(local_day_start(now)) >= per_day:
            return "per_day"
        return None

    # ---- 清理 ----

    def prune(self, keep_days: int = 30) -> None:
        cutoff = self._now() - keep_days * 86400
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM attempts WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM sent WHERE sent_at < ?", (cutoff,))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
