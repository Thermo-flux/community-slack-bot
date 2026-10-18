"""
==============================================================================
DATABASE MODULE (src/database.py)
==============================================================================
Thread-safe SQLite storage with WAL journal mode, atomic transactions, and
safe user/alias resolution.
==============================================================================
"""

from contextlib import contextmanager
from pathlib import Path
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from config import DB_PATH

logger = logging.getLogger("SlackBotDatabase")

_DB_LOCK = threading.RLock()
_THREAD_STATE = threading.local()
_INITIALIZED_DBS = set()

DEFAULT_ALIASES: Dict[str, str] = {
    "netic": "U0C7811N0CT",
    "jerry": "U0896R8U9FT",
    "gooselee": "U0C9JMMGDDH",
}
BOT_USER_IDS = {"U0C9JMMGDDH"}

_VALID_ALIAS_RE = re.compile(r"^[a-z0-9_.\-]{1,32}$")
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{8,14}$", re.IGNORECASE)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    resolved = os.path.abspath(db_path)
    if resolved not in _INITIALIZED_DBS:
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


@contextmanager
def _managed_connection(db_path: str = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Reuses the current thread's active connection for nested calls, closing once at the top level."""
    resolved = os.path.abspath(db_path or DB_PATH)
    active: Dict[str, Tuple[sqlite3.Connection, int]] = getattr(_THREAD_STATE, "active", None)
    if active is None:
        active = {}
        _THREAD_STATE.active = active

    if resolved in active:
        conn, depth = active[resolved]
        active[resolved] = (conn, depth + 1)
        try:
            yield conn
        finally:
            active[resolved] = (conn, depth)
        return

    conn = get_connection(resolved)
    active[resolved] = (conn, 1)
    try:
        yield conn
    finally:
        active.pop(resolved, None)
        conn.close()


def _ensure_user_columns(conn) -> None:
    """Idempotently add crumb-system columns to an existing users table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    wanted = {
        "opted_in": "INTEGER NOT NULL DEFAULT 0",
        "joined_at": "REAL NOT NULL DEFAULT 0.0",
        "level_floor": "INTEGER NOT NULL DEFAULT 1",
        "last_scored_ts": "REAL NOT NULL DEFAULT 0.0",
        "last_scored_text": "TEXT NOT NULL DEFAULT ''",
        "privacy_show": "INTEGER NOT NULL DEFAULT 1",
        "privacy_set": "INTEGER NOT NULL DEFAULT 0",
        "quiet": "INTEGER NOT NULL DEFAULT 0",
        "resting": "INTEGER NOT NULL DEFAULT 0",
        "last_roast_ts": "REAL NOT NULL DEFAULT 0.0",
    }
    for name, decl in wanted.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl};")


def init_db(db_path: str = DB_PATH) -> None:
    resolved = os.path.abspath(db_path)
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        with _managed_connection(resolved) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        xp INTEGER NOT NULL DEFAULT 0,
                        level INTEGER NOT NULL DEFAULT 1,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        last_message_ts REAL NOT NULL DEFAULT 0.0,
                        first_seen_ts REAL NOT NULL DEFAULT 0.0,
                        updated_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        xp_gained INTEGER NOT NULL,
                        message_length INTEGER NOT NULL DEFAULT 0,
                        is_suggestion INTEGER NOT NULL DEFAULT 0,
                        timestamp REAL NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_aliases (
                        alias TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS crumbs_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        amount REAL NOT NULL DEFAULT 0.0,
                        week_key TEXT NOT NULL DEFAULT '',
                        day_key TEXT NOT NULL DEFAULT '',
                        source_ts REAL NOT NULL DEFAULT 0.0,
                        channel_id TEXT NOT NULL DEFAULT '',
                        message_ts TEXT NOT NULL DEFAULT '',
                        ref TEXT NOT NULL DEFAULT '',
                        voided INTEGER NOT NULL DEFAULT 0,
                        void_reason TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS bot_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS channel_state (
                        channel_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value REAL NOT NULL DEFAULT 0.0,
                        updated_at REAL NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (channel_id, name)
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pond_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL DEFAULT 'week',
                        week_key TEXT NOT NULL DEFAULT '',
                        channel_id TEXT NOT NULL DEFAULT '',
                        message_ts TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at REAL NOT NULL DEFAULT 0.0,
                        updated_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS appeals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        ledger_event_id INTEGER NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'open',
                        note TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0.0,
                        updated_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        actor TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL DEFAULT '',
                        target TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS crumbs_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        week_key TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL,
                        crumbs REAL NOT NULL DEFAULT 0.0,
                        level INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL DEFAULT 0.0
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS voice_usage (
                        user_id TEXT NOT NULL,
                        hour_key TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, hour_key)
                    );
                """)
                _ensure_user_columns(conn)
                conn.execute(
                    "UPDATE users SET privacy_show = 1 "
                    "WHERE privacy_show = 0 AND privacy_set = 0;"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_xp ON users(xp DESC, message_count DESC);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_aliases_user ON user_aliases(user_id);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crumbs_user ON crumbs_ledger(user_id, voided);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crumbs_week ON crumbs_ledger(week_key, kind);"
                )
                conn.execute(
                    """
                    DELETE FROM crumbs_ledger
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM crumbs_ledger
                        WHERE voided = 0 AND message_ts != ''
                          AND kind IN ('message', 'thanks', 'helpful')
                        GROUP BY user_id, kind, channel_id, message_ts, ref
                    )
                    AND voided = 0 AND message_ts != ''
                    AND kind IN ('message', 'thanks', 'helpful');
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_crumbs_event_unique
                    ON crumbs_ledger(user_id, kind, channel_id, message_ts, ref)
                    WHERE voided = 0 AND message_ts != ''
                      AND kind IN ('message', 'thanks', 'helpful');
                    """
                )

                now = time.time()
                for alias, uid in DEFAULT_ALIASES.items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO user_aliases (alias, user_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (alias.lower(), uid, now),
                    )
            _INITIALIZED_DBS.add(resolved)
            logger.info("Database initialized successfully.")


def get_user(user_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None


def get_or_create_user(user_id: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)

            now = time.time()
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO users (
                        user_id, xp, level, message_count, last_message_ts, first_seen_ts, updated_at
                    )
                    VALUES (?, 0, 1, 0, 0.0, ?, ?)
                    """,
                    (user_id, now, now),
                )
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            created = cursor.fetchone()
            return dict(created) if created else {
                "user_id": user_id,
                "xp": 0,
                "level": 1,
                "message_count": 0,
                "last_message_ts": 0.0,
                "first_seen_ts": now,
                "updated_at": now,
            }


def check_cooldown(user_id: str, cooldown_seconds: int, db_path: str = DB_PATH) -> Tuple[bool, int]:
    if cooldown_seconds <= 0:
        return True, 0
    with _DB_LOCK:
        user = get_user(user_id, db_path)
        if not user:
            return True, 0

        now = time.time()
        elapsed = now - float(user["last_message_ts"] or 0.0)
        if elapsed >= cooldown_seconds:
            return True, 0
        return False, max(1, int(cooldown_seconds - elapsed))


def record_message_and_add_xp(
    user_id: str,
    channel_id: str,
    xp_to_add: int,
    new_level: int,
    message_length: int = 0,
    is_suggestion: bool = False,
    update_cooldown: bool = True,
    db_path: str = DB_PATH,
) -> Dict[str, Any]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            now = time.time()
            with conn:
                if update_cooldown:
                    conn.execute(
                        """
                        UPDATE users
                        SET xp = xp + ?,
                            level = MAX(level, ?),
                            message_count = message_count + 1,
                            last_message_ts = ?,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (xp_to_add, new_level, now, now, user_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE users
                        SET xp = xp + ?,
                            level = MAX(level, ?),
                            message_count = message_count + 1,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (xp_to_add, new_level, now, user_id),
                    )

                conn.execute(
                    """
                    INSERT INTO interactions (
                        user_id, channel_id, xp_gained, message_length, is_suggestion, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, channel_id, xp_to_add, message_length, 1 if is_suggestion else 0, now),
                )

            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return dict(cursor.fetchone())


def process_user_message(
    user_id: str,
    channel_id: str,
    cooldown_seconds: int,
    xp_to_add: int,
    level_calculator: Callable[[int], Tuple[int, str]],
    message_length: int = 0,
    is_suggestion: bool = False,
    db_path: str = DB_PATH,
) -> Dict[str, Any]:
    """
    Atomically checks cooldown, increments message count, and awards XP if eligible
    in a single locked transaction.
    """
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            current = get_or_create_user(user_id, db_path)
            now = time.time()
            old_xp = int(current.get("xp", 0))
            old_level, old_title = level_calculator(old_xp)
            elapsed = now - float(current.get("last_message_ts") or 0.0)

            if cooldown_seconds > 0 and current.get("last_message_ts", 0.0) > 0 and elapsed < cooldown_seconds:
                with conn:
                    conn.execute(
                        """
                        UPDATE users
                        SET message_count = message_count + 1,
                            updated_at = ?
                        WHERE user_id = ?
                        """,
                        (now, user_id),
                    )
                return {
                    "earned_xp": False,
                    "xp_added": 0,
                    "old_level": old_level,
                    "new_level": old_level,
                    "new_title": old_title,
                    "leveled_up": False,
                }

            new_total_xp = old_xp + xp_to_add
            new_level, new_title = level_calculator(new_total_xp)
            with conn:
                conn.execute(
                    """
                    UPDATE users
                    SET xp = ?,
                        level = ?,
                        message_count = message_count + 1,
                        last_message_ts = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (new_total_xp, new_level, now, now, user_id),
                )
                conn.execute(
                    """
                    INSERT INTO interactions (
                        user_id, channel_id, xp_gained, message_length, is_suggestion, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, channel_id, xp_to_add, message_length, 1 if is_suggestion else 0, now),
                )
            return {
                "earned_xp": True,
                "xp_added": xp_to_add,
                "old_level": old_level,
                "new_level": new_level,
                "new_title": new_title,
                "leveled_up": new_level > old_level,
            }


def increment_message_count_only(user_id: str, db_path: str = DB_PATH) -> None:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            with conn:
                conn.execute(
                    """
                    UPDATE users
                    SET message_count = message_count + 1,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (time.time(), user_id),
                )


def get_leaderboard(limit: int = 10, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute(
                """
                SELECT user_id, xp, level, message_count
                FROM users
                WHERE user_id NOT IN ('U0C9JMMGDDH')
                ORDER BY xp DESC, message_count DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_community_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total_members,
                    COALESCE(SUM(message_count), 0) as total_messages,
                    COALESCE(SUM(xp), 0) as total_xp
                FROM users
                WHERE user_id NOT IN ('U0C9JMMGDDH')
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else {"total_members": 0, "total_messages": 0, "total_xp": 0}


def reset_user(user_id: str, db_path: str = DB_PATH) -> None:
    if not user_id:
        return
    uid = user_id.strip()
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                conn.execute("DELETE FROM users WHERE user_id = ? OR user_id = ?", (uid, uid.upper()))
                conn.execute("DELETE FROM interactions WHERE user_id = ? OR user_id = ?", (uid, uid.upper()))
                conn.execute(
                    "DELETE FROM user_aliases WHERE (user_id = ? OR user_id = ?) AND alias NOT IN ('netic', 'jerry', 'gooselee')",
                    (uid, uid.upper()),
                )


def register_user_alias(alias: str, user_id: str, db_path: str = DB_PATH) -> bool:
    """Registers or updates a safe username/nickname alias for a user ID."""
    clean_alias = (alias or "").strip().lower().lstrip("@")
    clean_uid = (user_id or "").strip().upper()
    if not clean_alias or not clean_uid:
        return False
    if not _VALID_ALIAS_RE.fullmatch(clean_alias):
        return False
    if clean_alias in DEFAULT_ALIASES and DEFAULT_ALIASES[clean_alias] != clean_uid:
        return False

    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO user_aliases (alias, user_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (clean_alias, clean_uid, time.time()),
                )
            logger.info("Registered alias '%s' -> '%s'", clean_alias, clean_uid)
            return True


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_user_by_alias(
    alias: str,
    db_path: str = DB_PATH,
    create_if_missing: bool = False,
) -> Optional[Dict[str, Any]]:
    """Looks up a user record by friendly username or nickname alias without polluting the DB."""
    clean_alias = (alias or "").strip().lower().lstrip("@")
    if not clean_alias:
        return None
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute(
                "SELECT user_id FROM user_aliases WHERE alias = ?",
                (clean_alias,),
            )
            row = cursor.fetchone()
            if not row and len(clean_alias) >= 2 and _VALID_ALIAS_RE.fullmatch(clean_alias):
                escaped = _escape_like(clean_alias)
                cursor = conn.execute(
                    """
                    SELECT user_id FROM user_aliases
                    WHERE alias LIKE ? ESCAPE '\\'
                    ORDER BY length(alias) ASC, alias ASC
                    LIMIT 1
                    """,
                    (f"%{escaped}%",),
                )
                row = cursor.fetchone()
            if not row:
                return None
            uid = row["user_id"]
            if uid in BOT_USER_IDS and not create_if_missing:
                return get_user(uid, db_path)
            return get_or_create_user(uid, db_path) if create_if_missing else get_user(uid, db_path)


def get_aliases_for_user(user_id: str, db_path: str = DB_PATH) -> List[str]:
    """Returns all known aliases for a given user ID."""
    if not user_id:
        return []
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute(
                "SELECT alias FROM user_aliases WHERE user_id = ? ORDER BY alias",
                (user_id.strip().upper(),),
            )
            return [row["alias"] for row in cursor.fetchall()]


def _looks_like_slack_user_id(raw: str) -> bool:
    """
    True only for genuine Slack User IDs (e.g. U0C7811N0CT, W012345678).
    Prevents ordinary English words starting with 'u' or 'w' (e.g. 'understand', 'wolverine')
    from being mistaken for Slack User IDs.
    """
    if not _SLACK_ID_RE.fullmatch(raw):
        return False
    return any(ch.isdigit() for ch in raw) or raw == raw.upper()


def resolve_user(
    identifier: str,
    db_path: str = DB_PATH,
    create_if_missing: bool = False,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Resolves an identifier (Slack mention, raw user ID, username, or alias)
    into a canonical (user_id, user_data) tuple without creating ghost users on read lookups.
    """
    if not identifier:
        return None, None

    raw = identifier.strip()

    # 1. Slack mention: <@U12345678> or <@U12345678|nickname>
    mention_match = re.search(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>", raw, re.IGNORECASE)
    if mention_match:
        uid = mention_match.group(1).upper()
        alias = mention_match.group(2)
        if alias:
            register_user_alias(alias, uid, db_path)
        user = get_or_create_user(uid, db_path) if create_if_missing else get_user(uid, db_path)
        if user:
            return uid, user
        return None, None

    # 2. Raw Slack user ID (must contain digits or be uppercase, e.g. U0C7811N0CT)
    if _looks_like_slack_user_id(raw):
        uid = raw.upper()
        user = get_or_create_user(uid, db_path) if create_if_missing else get_user(uid, db_path)
        if user:
            return uid, user
        return None, None

    # 3. Check alias or friendly name
    clean_name = raw.lower().lstrip("@").strip()
    if clean_name:
        user = get_user_by_alias(clean_name, db_path, create_if_missing=create_if_missing)
        if user:
            return user["user_id"], user

    return None, None


def get_all_users_summary(limit: int = 25, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Returns a summary of tracked community members with stats and safe aliases."""
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    u.user_id,
                    u.xp,
                    u.level,
                    u.message_count,
                    GROUP_CONCAT(a.alias, ', ') as aliases
                FROM users u
                LEFT JOIN user_aliases a ON u.user_id = a.user_id
                WHERE u.user_id NOT IN ('U0C9JMMGDDH')
                GROUP BY u.user_id
                ORDER BY u.xp DESC, u.message_count DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Bot state, counters, reports, appeals, audit, snapshots, voice quota
# ---------------------------------------------------------------------------

def get_state(key: str, default: str = "", db_path: str = DB_PATH) -> str:
    try:
        with _DB_LOCK:
            with _managed_connection(db_path) as conn:
                row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
                return row["value"] if row else default
    except sqlite3.OperationalError:
        return default


def set_state(key: str, value: str, db_path: str = DB_PATH) -> None:
    try:
        with _DB_LOCK:
            with _managed_connection(db_path) as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (key, str(value), time.time()),
                    )
    except sqlite3.OperationalError as exc:
        logger.warning("Could not persist state '%s' (%s)", key, type(exc).__name__)


def get_flag(key: str, default: bool = False, db_path: str = DB_PATH) -> bool:
    raw = get_state(key, "1" if default else "0", db_path)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def set_flag(key: str, value: bool, db_path: str = DB_PATH) -> None:
    set_state(key, "1" if value else "0", db_path)


def get_all_state(db_path: str = DB_PATH) -> Dict[str, str]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            rows = conn.execute("SELECT key, value FROM bot_state").fetchall()
            return {r["key"]: r["value"] for r in rows}


def increment_channel_counter(
    channel_id: str, name: str, amount: float = 1.0, db_path: str = DB_PATH
) -> float:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO channel_state (channel_id, name, value, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id, name) DO UPDATE SET
                        value = channel_state.value + excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (channel_id, name, float(amount), time.time()),
                )
            row = conn.execute(
                "SELECT value FROM channel_state WHERE channel_id = ? AND name = ?",
                (channel_id, name),
            ).fetchone()
            return float(row["value"]) if row else 0.0


def set_channel_counter(channel_id: str, name: str, value: float, db_path: str = DB_PATH) -> None:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO channel_state (channel_id, name, value, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id, name) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (channel_id, name, float(value), time.time()),
                )


def get_channel_counter(channel_id: str, name: str, db_path: str = DB_PATH) -> float:
    try:
        with _DB_LOCK:
            with _managed_connection(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM channel_state WHERE channel_id = ? AND name = ?",
                    (channel_id, name),
                ).fetchone()
                return float(row["value"]) if row else 0.0
    except sqlite3.OperationalError:
        return 0.0


def touch_channel_counter(
    channel_id: str, name: str, amount: float = 1.0, reset_hours: float = 24.0,
    db_path: str = DB_PATH,
) -> float:
    """Increment a counter, resetting it first when the last touch is older than reset_hours."""
    now = time.time()
    stale_after = max(0.0, float(reset_hours)) * 3600.0
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM channel_state WHERE channel_id = ? AND name = ?",
                (channel_id, name),
            ).fetchone()
            value = float(row["value"]) if row else 0.0
            updated = float(row["updated_at"]) if row else 0.0
            if stale_after and updated and (now - updated) > stale_after:
                value = 0.0
            value += float(amount)
            with conn:
                conn.execute(
                    """
                    INSERT INTO channel_state (channel_id, name, value, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id, name) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (channel_id, name, value, now),
                )
            return value


def add_audit(
    actor: str, action: str, target: str = "", detail: str = "", db_path: str = DB_PATH
) -> int:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                cur = conn.execute(
                    "INSERT INTO audit_log (actor, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                    (actor or "", action or "", target or "", detail or "", time.time()),
                )
                return int(cur.lastrowid)


def get_audit(limit: int = 10, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


def add_appeal(
    user_id: str, ledger_event_id: int, reason: str = "", db_path: str = DB_PATH
) -> int:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO appeals (user_id, ledger_event_id, reason, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'open', ?, ?)
                    """,
                    (user_id, int(ledger_event_id), reason or "", time.time(), time.time()),
                )
                return int(cur.lastrowid)


def get_appeals(status: str = "open", db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM appeals WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM appeals ORDER BY id").fetchall()
            return [dict(r) for r in rows]


def resolve_appeal(
    appeal_id: int, status: str = "resolved", note: str = "", db_path: str = DB_PATH
) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
            if not row:
                return None
            with conn:
                conn.execute(
                    "UPDATE appeals SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                    (status, note or "", time.time(), appeal_id),
                )
            return dict(row)


def create_report(
    kind: str,
    week_key: str,
    payload_json: str,
    channel_id: str = "",
    db_path: str = DB_PATH,
) -> int:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO pond_reports (kind, week_key, channel_id, payload, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (kind, week_key, channel_id, payload_json, time.time(), time.time()),
                )
                return int(cur.lastrowid)


def get_reports(status: str = "pending", db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM pond_reports WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM pond_reports ORDER BY id").fetchall()
            return [dict(r) for r in rows]


def get_report(report_id: int, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute("SELECT * FROM pond_reports WHERE id = ?", (report_id,)).fetchone()
            return dict(row) if row else None


def set_report_status(
    report_id: int, status: str, message_ts: str = "", db_path: str = DB_PATH
) -> None:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                conn.execute(
                    "UPDATE pond_reports SET status = ?, message_ts = ?, updated_at = ? WHERE id = ?",
                    (status, message_ts or "", time.time(), report_id),
                )


def save_snapshot(week_key: str, entries: List[Dict[str, Any]], db_path: str = DB_PATH) -> int:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                for entry in entries:
                    conn.execute(
                        """
                        INSERT INTO crumbs_snapshots (week_key, user_id, crumbs, level, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (week_key, entry["user_id"], float(entry.get("crumbs", 0.0)),
                         int(entry.get("level", 1)), time.time()),
                    )
                return len(entries)


def consume_voice_quota(
    user_id: str, hour_key: str, limit: int, db_path: str = DB_PATH
) -> bool:
    """Increment the per-person hourly voice counter; return True when still under the limit."""
    if limit <= 0:
        return False
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute(
                "SELECT count FROM voice_usage WHERE user_id = ? AND hour_key = ?",
                (user_id, hour_key),
            ).fetchone()
            current = int(row["count"]) if row else 0
            if current >= limit:
                return False
            with conn:
                conn.execute(
                    """
                    INSERT INTO voice_usage (user_id, hour_key, count) VALUES (?, ?, 1)
                    ON CONFLICT(user_id, hour_key) DO UPDATE SET count = voice_usage.count + 1
                    """,
                    (user_id, hour_key),
                )
            return True


def backup_database(db_path: str = DB_PATH, backup_dir: str = "") -> Optional[str]:
    """Copy the SQLite database to a timestamped file using the online backup API."""
    resolved = os.path.abspath(db_path)
    if not os.path.exists(resolved):
        return None
    target_dir = backup_dir or os.path.join(os.path.dirname(resolved), "backups")
    os.makedirs(target_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    dest = os.path.join(target_dir, f"{os.path.basename(resolved)}.{stamp}.bak")
    with _DB_LOCK:
        source = sqlite3.connect(resolved)
        try:
            destination = sqlite3.connect(dest)
            try:
                with destination:
                    source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    return dest
