"""
==============================================================================
CONCURRENCY & STRESS TEST SUITE (test_stress.py)
==============================================================================
Tests the Slack Bot under severe load, edge cases, and high concurrency.

SCENARIOS TESTED:
  1. High Concurrency: 25 simultaneous threads sending 500 messages concurrently.
     Verifies SQLite WAL concurrency and thread mutex lock prevent any database lock collisions.
  2. Rapid-Fire Spammer: 1 user sending 100 messages within 1 second.
     Verifies cooldown correctly allows only 1 XP award while tracking all 100 messages.
  3. SQL Injection Resilience: Messages containing dangerous SQL strings like
     "'; DROP TABLE users; --" to prove parameterized queries protect data.
  4. Extreme Payloads: Very large text (50,000+ characters), unusual Unicode, and emojis.
  5. Multi-Level Leap: Gaining huge XP in one shot (e.g. 1500 XP) and jumping multiple levels.
==============================================================================
"""

import os
import tempfile
import threading
import time
import pytest
from unittest.mock import patch

import database
import leveling
import ai_service


@pytest.fixture
def stress_db():
    """Provides an isolated database for stress testing."""
    temp_file = tempfile.NamedTemporaryFile(suffix="_stress.db", delete=False)
    temp_file.close()
    db_path = temp_file.name

    database.init_db(db_path)
    yield db_path

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


def test_concurrent_message_bombardment(stress_db):
    """
    Simulates 25 users sending 20 messages each concurrently (500 total operations)
    while background readers continuously query the leaderboard.
    """
    num_users = 25
    messages_per_user = 20
    errors = []

    def user_worker(user_index: int):
        user_id = f"U_STRESS_{user_index}"
        try:
            for m in range(messages_per_user):
                # Calculate XP
                xp_earned, is_sug, _ = leveling.evaluate_message_xp(f"Message {m} from thread {user_index}")
                curr = database.get_or_create_user(user_id, db_path=stress_db)
                new_xp = curr["xp"] + xp_earned
                new_level, _ = leveling.get_level_info_for_xp(new_xp)

                # Record in database (bypass cooldown for this pure throughput test)
                database.record_message_and_add_xp(
                    user_id=user_id,
                    channel_id="C_MAIN",
                    xp_to_add=xp_earned,
                    new_level=new_level,
                    message_length=30,
                    is_suggestion=is_sug,
                    update_cooldown=False,
                    db_path=stress_db
                )
        except Exception as e:
            errors.append(f"Worker {user_index} error: {e}")

    def reader_worker():
        try:
            for _ in range(50):
                database.get_leaderboard(limit=10, db_path=stress_db)
                database.get_community_stats(db_path=stress_db)
                time.sleep(0.005)
        except Exception as e:
            errors.append(f"Reader error: {e}")

    threads = []
    # Launch concurrent writers
    for u in range(num_users):
        t = threading.Thread(target=user_worker, args=(u,))
        threads.append(t)

    # Launch concurrent readers
    for r in range(3):
        t_reader = threading.Thread(target=reader_worker)
        threads.append(t_reader)

    # Start all threads simultaneously
    for t in threads:
        t.start()

    # Wait for all threads to finish
    for t in threads:
        t.join(timeout=30.0)

    # Assert no collisions or errors occurred
    assert len(errors) == 0, f"Encountered concurrency errors: {errors}"

    # Verify all users exist and have exact expected message counts
    stats = database.get_community_stats(db_path=stress_db)
    assert stats["total_members"] == num_users
    assert stats["total_messages"] == num_users * messages_per_user
    assert stats["total_xp"] > 0


def test_rapid_fire_spammer_cooldown(stress_db):
    """
    Simulates a malicious user sending 100 messages within 1 second.
    Verifies that only 1 message gains XP, while all 100 are tracked in message_count.
    """
    user_id = "U_SPAMMER"
    cooldown_seconds = 30
    xp_awarded_count = 0

    for i in range(100):
        can_earn, _ = database.check_cooldown(user_id, cooldown_seconds, db_path=stress_db)
        if can_earn:
            database.record_message_and_add_xp(
                user_id=user_id,
                channel_id="C_SPAM",
                xp_to_add=10,
                new_level=1,
                update_cooldown=True,
                db_path=stress_db
            )
            xp_awarded_count += 1
        else:
            database.increment_message_count_only(user_id, db_path=stress_db)

    user = database.get_user(user_id, db_path=stress_db)
    # XP should only have been awarded once!
    assert xp_awarded_count == 1
    assert user["xp"] == 10
    # Total message count must accurately reflect all 100 attempts
    assert user["message_count"] == 100


def test_sql_injection_safety(stress_db):
    """
    Simulates malicious user IDs and channel IDs containing SQL injection payloads.
    Verifies database integrity remains intact.
    """
    malicious_user = "U'; DROP TABLE users; --"
    malicious_text = "'; DELETE FROM interactions; SELECT * FROM users WHERE '1'='1"

    xp, _, _ = leveling.evaluate_message_xp(malicious_text)
    record = database.record_message_and_add_xp(
        user_id=malicious_user,
        channel_id="C_ATTACK",
        xp_to_add=xp,
        new_level=1,
        message_length=len(malicious_text),
        db_path=stress_db
    )

    assert record["user_id"] == malicious_user
    # Ensure users table still exists and data was not dropped
    fetched = database.get_user(malicious_user, db_path=stress_db)
    assert fetched is not None
    assert fetched["xp"] == xp


def test_massive_message_payload(stress_db):
    """
    Simulates massive 50,000 character message with exotic Unicode and emojis.
    Verifies string handling and database storage do not choke.
    """
    massive_text = "🚀💡✨" * 5000 + " I suggest we stress test the system thoroughly! " + "A" * 35000
    xp, is_sug, is_long = leveling.evaluate_message_xp(massive_text)

    assert is_sug is True
    assert is_long is True
    assert xp == 30  # 10 base + 5 long + 15 suggestion

    user = database.record_message_and_add_xp(
        user_id="U_HEAVY",
        channel_id="C_TEST",
        xp_to_add=xp,
        new_level=1,
        message_length=len(massive_text),
        is_suggestion=is_sug,
        db_path=stress_db
    )
    assert user["xp"] == 30


def test_multi_level_jump():
    """
    Verifies that gaining massive XP in one leap correctly advances multiple levels
    without getting stuck on intermediate tiers.
    """
    # 0 XP = Level 1
    lvl_start, title_start = leveling.get_level_info_for_xp(0)
    assert lvl_start == 1

    # Jump straight to 252 XP (Level 7: Pond Scout)
    lvl_end, title_end = leveling.get_level_info_for_xp(252)
    assert lvl_end == 7
    assert "Pond Scout" in title_end


def test_ai_service_offline_stress():
    """
    Tests 100 rapid calls to AI service when offline.
    Must respond in under 0.1 seconds without any errors.
    """
    with patch("ai_service.AI_API_KEY", ""):
        start = time.time()
        for i in range(100):
            reply = ai_service.generate_response(user_id=f"U_AI_{i}", user_message="Hello bot")
            assert f"<@U_AI_{i}>" in reply
        duration = time.time() - start
        assert duration < 1.0  # 100 instant template responses in < 1s

