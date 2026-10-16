"""
==============================================================================
DATABASE MODULE (src/database.py)
==============================================================================
Thread-safe SQLite storage with WAL journal mode and reentrant thread locking.
==============================================================================
"""

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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_users_xp ON users(xp DESC);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id);")
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
        finally:
            conn.close()
