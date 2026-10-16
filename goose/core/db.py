"""SQLite storage: one connection, WAL, one writer. The schema lives here so every module shares it."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import Any, Iterator, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    joined_ts REAL NOT NULL,
    show INTEGER NOT NULL DEFAULT 0,
    rest INTEGER NOT NULL DEFAULT 0,
    quiet INTEGER NOT NULL DEFAULT 0,
    level_floor INTEGER NOT NULL DEFAULT 1,
    consent_version TEXT NOT NULL DEFAULT '',
    frozen_until REAL NOT NULL DEFAULT 0,
    last_channel TEXT NOT NULL DEFAULT '',
    announced_level INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key TEXT NOT NULL UNIQUE,
    week_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- msg | thanks | helpful | star | bigstar
    counterparty TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    amount REAL,
    state TEXT NOT NULL DEFAULT 'pending',   -- pending | final | voided
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    message_ts TEXT NOT NULL DEFAULT '',
    void_reason TEXT NOT NULL DEFAULT '',
    void_actor TEXT NOT NULL DEFAULT '',
    voided_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_facts_user_week ON facts(user_id, week_id);
CREATE INDEX IF NOT EXISTS idx_facts_week ON facts(week_id, state);
CREATE INDEX IF NOT EXISTS idx_facts_msg ON facts(channel, message_ts);

CREATE TABLE IF NOT EXISTS week_status (
    week_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,                -- provisional | confirmed | skipped
    snapshot_json TEXT NOT NULL DEFAULT '[]',
    confirmers_json TEXT NOT NULL DEFAULT '[]',
    closed_ts REAL,
    confirmed_ts REAL,
    skipped_reason TEXT NOT NULL DEFAULT '',
    organizer_posted INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0,
    announced INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                -- week | final
    week_id TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    total REAL NOT NULL,
    label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- disqualify | freeze | note
    scope TEXT NOT NULL DEFAULT '',     -- a week_id or 'all'
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    opened_ts REAL NOT NULL,
    closed_ts REAL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- report | appeal
    text TEXT NOT NULL DEFAULT '',
    opened_ts REAL NOT NULL,
    due_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    outcome TEXT NOT NULL DEFAULT '',
    resolved_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed (key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS seen_hash (user_id TEXT NOT NULL, hash TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_seen_hash_user ON seen_hash(user_id, ts);

CREATE TABLE IF NOT EXISTS mood_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mood_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS german_notes (
    channel TEXT NOT NULL, message_ts TEXT NOT NULL, de_ids TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (channel, message_ts)
);
CREATE TABLE IF NOT EXISTS explain_log (key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gloss_seen (user_id TEXT NOT NULL, de_id TEXT NOT NULL, PRIMARY KEY (user_id, de_id));

CREATE TABLE IF NOT EXISTS line_history (moment TEXT NOT NULL, line_id TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reaction_log (user_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remark_log (user_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_remark_log ON remark_log(kind, ts);

CREATE TABLE IF NOT EXISTS voice_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, user_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    latency REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS voice_person (user_id TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_voice_person ON voice_person(user_id, ts);

CREATE TABLE IF NOT EXISTS consents (user_id TEXT NOT NULL, version TEXT NOT NULL, ts REAL NOT NULL);
"""


class Database:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0
        if path != ":memory:":
            import os
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def tx(self) -> Iterator["Database"]:
        """A write transaction (BEGIN IMMEDIATE). Re-entrant on the same thread."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
