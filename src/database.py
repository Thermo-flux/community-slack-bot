"""
==============================================================================
DATABASE MODULE (src/database.py)
==============================================================================
Thread-safe SQLite storage with WAL journal mode and reentrant thread locking.
==============================================================================
"""

import re
import sqlite3
import time
import threading
import logging
from typing import Optional, Dict, Any, List, Tuple
from config import DB_PATH

logger = logging.getLogger("SlackBotDatabase")

# Reentrant lock avoids deadlock when helper methods are called internally
_DB_LOCK = threading.RLock()


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    from pathlib import Path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    with _DB_LOCK:
        conn = get_connection(db_path)
        try:
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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_users_xp ON users(xp DESC);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_user ON user_aliases(user_id);")

                # Seed default known aliases so anyone can query by name immediately
                now = time.time()
                default_aliases = {
                    "netic": "U0C7811N0CT",
                    "jerry": "U0896R8U9FT",
                    "gooselee": "U0C9JMMGDDH"
                }
                for alias, uid in default_aliases.items():
                    conn.execute("""
                        INSERT OR IGNORE INTO user_aliases (alias, user_id, created_at)
                        VALUES (?, ?, ?)
                    """, (alias.lower(), uid, now))
            logger.info("Database initialized successfully.")
        finally:
            conn.close()


def get_user(user_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_or_create_user(user_id: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    user = get_user(user_id, db_path)
    if user:
        return user

    with _DB_LOCK:
        user = get_user(user_id, db_path)
        if user:
            return user

        now = time.time()
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute("""
                    INSERT INTO users (user_id, xp, level, message_count, last_message_ts, first_seen_ts, updated_at)
                    VALUES (?, 0, 1, 0, 0.0, ?, ?)
                """, (user_id, now, now))
            return {
                "user_id": user_id,
                "xp": 0,
                "level": 1,
                "message_count": 0,
                "last_message_ts": 0.0,
                "first_seen_ts": now,
                "updated_at": now
            }
        finally:
            conn.close()


def check_cooldown(user_id: str, cooldown_seconds: int, db_path: str = DB_PATH) -> Tuple[bool, int]:
    if cooldown_seconds <= 0:
        return True, 0
    user = get_user(user_id, db_path)
    if not user:
        return True, 0

    now = time.time()
    elapsed = now - user["last_message_ts"]
    if elapsed >= cooldown_seconds:
        return True, 0
    return False, int(cooldown_seconds - elapsed)


def record_message_and_add_xp(
    user_id: str,
    channel_id: str,
    xp_to_add: int,
    new_level: int,
    message_length: int = 0,
    is_suggestion: bool = False,
    update_cooldown: bool = True,
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    with _DB_LOCK:
        get_or_create_user(user_id, db_path)
        now = time.time()
        conn = get_connection(db_path)
        try:
            with conn:
                if update_cooldown:
                    conn.execute("""
                        UPDATE users
                        SET xp = xp + ?,
                            level = ?,
                            message_count = message_count + 1,
                            last_message_ts = ?,
                            updated_at = ?
                        WHERE user_id = ?
                    """, (xp_to_add, new_level, now, now, user_id))
                else:
                    conn.execute("""
                        UPDATE users
                        SET xp = xp + ?,
                            level = ?,
                            message_count = message_count + 1,
                            updated_at = ?
                        WHERE user_id = ?
                    """, (xp_to_add, new_level, now, user_id))

                conn.execute("""
                    INSERT INTO interactions (user_id, channel_id, xp_gained, message_length, is_suggestion, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, channel_id, xp_to_add, message_length, 1 if is_suggestion else 0, now))

                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                return dict(cursor.fetchone())
        finally:
            conn.close()


def increment_message_count_only(user_id: str, db_path: str = DB_PATH) -> None:
    with _DB_LOCK:
        get_or_create_user(user_id, db_path)
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute("""
                    UPDATE users
                    SET message_count = message_count + 1,
                        updated_at = ?
                    WHERE user_id = ?
                """, (time.time(), user_id))
        finally:
            conn.close()


def get_leaderboard(limit: int = 10, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, xp, level, message_count
            FROM users
            ORDER BY xp DESC, message_count DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_community_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total_members,
                COALESCE(SUM(message_count), 0) as total_messages,
                COALESCE(SUM(xp), 0) as total_xp
            FROM users
        """)
        row = cursor.fetchone()
        return dict(row) if row else {"total_members": 0, "total_messages": 0, "total_xp": 0}
    finally:
        conn.close()


def reset_user(user_id: str, db_path: str = DB_PATH) -> None:
    with _DB_LOCK:
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM interactions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM user_aliases WHERE user_id = ?", (user_id,))
        finally:
            conn.close()


def register_user_alias(alias: str, user_id: str, db_path: str = DB_PATH) -> bool:
    """Registers or updates a friendly username or nickname alias for a user ID."""
    clean_alias = (alias or "").strip().lower().lstrip("@")
    if not clean_alias or not user_id:
        return False
    with _DB_LOCK:
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO user_aliases (alias, user_id, created_at)
                    VALUES (?, ?, ?)
                """, (clean_alias, user_id.strip().upper(), time.time()))
            logger.info(f"Registered alias '{clean_alias}' -> '{user_id}'")
            return True
        finally:
            conn.close()


def get_user_by_alias(alias: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Looks up a user record by friendly username or nickname alias."""
    clean_alias = (alias or "").strip().lower().lstrip("@")
    if not clean_alias:
        return None
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM user_aliases WHERE alias = ?", (clean_alias,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("SELECT user_id FROM user_aliases WHERE alias LIKE ? LIMIT 1", (f"%{clean_alias}%",))
            row = cursor.fetchone()
        if row:
            return get_or_create_user(row["user_id"], db_path)
        return None
    finally:
        conn.close()


def get_aliases_for_user(user_id: str, db_path: str = DB_PATH) -> List[str]:
    """Returns all known aliases for a given user ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT alias FROM user_aliases WHERE user_id = ? ORDER BY alias", (user_id.upper(),))
        return [row["alias"] for row in cursor.fetchall()]
    finally:
        conn.close()


def resolve_user(identifier: str, db_path: str = DB_PATH) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Robustly resolves any identifier (Slack mention, raw user ID, username, or alias)
    into a canonical (user_id, user_data) tuple.
    
    Returns (user_id, user_data) if resolved, or (None, None) if not found.
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
        user = get_or_create_user(uid, db_path)
        return uid, user

    # 2. Raw Slack user ID (e.g. U0C7811N0CT, W012345678)
    if re.match(r"^[UW][A-Z0-9]{8,14}$", raw, re.IGNORECASE):
        uid = raw.upper()
        user = get_or_create_user(uid, db_path)
        return uid, user

    # 3. Check alias or friendly name
    clean_name = raw.lower().lstrip("@").strip()
    if clean_name:
        user = get_user_by_alias(clean_name, db_path)
        if user:
            return user["user_id"], user

    return None, None


def get_all_users_summary(limit: int = 25, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Returns a rich summary of community members with stats and aliases for AI context."""
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                u.user_id, 
                u.xp, 
                u.level, 
                u.message_count,
                GROUP_CONCAT(a.alias, ', ') as aliases
            FROM users u
            LEFT JOIN user_aliases a ON u.user_id = a.user_id
            GROUP BY u.user_id
            ORDER BY u.xp DESC, u.message_count DESC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

