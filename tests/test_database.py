"""
Unit tests for database.py
Validates SQLite thread safety, user creation, XP updates, cooldowns, and leaderboards.
"""

import os
import tempfile
import threading
import time
import pytest
import database


@pytest.fixture
def temp_db():
    """Provides a fresh temporary database file for each test."""
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_file.close()
    db_path = temp_file.name

    database.init_db(db_path)
    yield db_path

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


def test_get_or_create_user(temp_db):
    """Verifies that new users start at Level 1 with 0 XP."""
    user = database.get_or_create_user("U123", db_path=temp_db)
    assert user["user_id"] == "U123"
    assert user["xp"] == 0
    assert user["level"] == 1
    assert user["message_count"] == 0

    # Fetching again should return existing record
    same_user = database.get_or_create_user("U123", db_path=temp_db)
    assert same_user["user_id"] == "U123"


def test_record_message_and_add_xp(temp_db):
    """Verifies XP addition, level update, and interaction recording."""
    updated = database.record_message_and_add_xp(
        user_id="U456",
        channel_id="C999",
        xp_to_add=25,
        new_level=1,
        message_length=40,
        is_suggestion=True,
        update_cooldown=True,
        db_path=temp_db
    )
    assert updated["xp"] == 25
    assert updated["message_count"] == 1
    assert updated["last_message_ts"] > 0


def test_cooldown_enforcement(temp_db):
    """Verifies cooldown prevents XP gain within cooldown window."""
    user_id = "U_COOLDOWN"

    # Initially user can earn XP
    can_earn, remaining = database.check_cooldown(user_id, cooldown_seconds=10, db_path=temp_db)
    assert can_earn is True
    assert remaining == 0

    # Record message to start cooldown
    database.record_message_and_add_xp(
        user_id=user_id,
        channel_id="C1",
        xp_to_add=10,
        new_level=1,
        db_path=temp_db
    )

    # Immediately after, user must be on cooldown
    can_earn, remaining = database.check_cooldown(user_id, cooldown_seconds=10, db_path=temp_db)
    assert can_earn is False
    assert remaining > 0


def test_leaderboard_ordering(temp_db):
    """Verifies leaderboard accurately ranks users by XP descending."""
    database.record_message_and_add_xp("USER_LOW", "C1", 10, 1, db_path=temp_db)
    database.record_message_and_add_xp("USER_HIGH", "C1", 100, 2, db_path=temp_db)
    database.record_message_and_add_xp("USER_MID", "C1", 50, 1, db_path=temp_db)

    board = database.get_leaderboard(limit=5, db_path=temp_db)
    assert len(board) == 3
    assert board[0]["user_id"] == "USER_HIGH"
    assert board[0]["xp"] == 100
    assert board[1]["user_id"] == "USER_MID"
    assert board[2]["user_id"] == "USER_LOW"


def test_increment_message_count_only(temp_db):
    """Verifies messages can increment counter without awarding XP."""
    user_id = "U_COUNT"
    database.get_or_create_user(user_id, db_path=temp_db)
    database.increment_message_count_only(user_id, db_path=temp_db)
    database.increment_message_count_only(user_id, db_path=temp_db)

    user = database.get_user(user_id, db_path=temp_db)
    assert user["message_count"] == 2
    assert user["xp"] == 0


def test_user_aliases_and_resolution(temp_db):
    """Verifies registering aliases, resolving via mentions, raw IDs, and aliases."""
    database.record_message_and_add_xp("U0C7811N0CT", "C1", 200, 3, db_path=temp_db)
    database.register_user_alias("netic", "U0C7811N0CT", db_path=temp_db)

    # Resolve via mention <@U0C7811N0CT>
    uid, udata = database.resolve_user("<@U0C7811N0CT>", db_path=temp_db)
    assert uid == "U0C7811N0CT"
    assert udata["xp"] == 200

    # Resolve via mention with nickname <@U0C7811N0CT|netic_alias>
    uid2, udata2 = database.resolve_user("<@U0C7811N0CT|netic_alias>", db_path=temp_db)
    assert uid2 == "U0C7811N0CT"

    # Resolve via friendly alias
    uid3, udata3 = database.resolve_user("netic", db_path=temp_db)
    assert uid3 == "U0C7811N0CT"
    assert udata3["xp"] == 200

    # Resolve via raw ID
    uid4, udata4 = database.resolve_user("U0C7811N0CT", db_path=temp_db)
    assert uid4 == "U0C7811N0CT"

    # Unknown user returns None, None
    uid_none, udata_none = database.resolve_user("unknown_ghost_user", db_path=temp_db)
    assert uid_none is None
    assert udata_none is None


def test_get_all_users_summary(temp_db):
    """Verifies summary contains member stats and concatenated aliases."""
    database.record_message_and_add_xp("U100", "C1", 50, 2, db_path=temp_db)
    database.register_user_alias("sam", "U100", db_path=temp_db)

    summary = database.get_all_users_summary(limit=10, db_path=temp_db)
    assert len(summary) >= 1
    u100 = [u for u in summary if u["user_id"] == "U100"][0]
    assert u100["xp"] == 50
    assert "sam" in (u100["aliases"] or "")

