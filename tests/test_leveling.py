"""
Unit tests for leveling.py
Validates level tier mapping, progress bars, XP calculations, and card formatters.
"""

import pytest
import leveling


def test_level_info_for_xp():
    """Tests that XP correctly maps to configured levels and titles."""
    level, title = leveling.get_level_info_for_xp(0)
    assert level == 1
    assert "Hatchling" in title

    level, title = leveling.get_level_info_for_xp(10)
    assert level == 2
    assert "Duckling" in title

    level, title = leveling.get_level_info_for_xp(25)
    assert level == 3
    assert "Paddler" in title

    level, title = leveling.get_level_info_for_xp(50)
    assert level == 4
    assert "Dabbler" in title

    level, title = leveling.get_level_info_for_xp(10000)
    assert level >= 12


def test_progress_to_next_level():
    """Tests percentage and remaining XP math."""
    # User at 5 XP (halfway between Level 1 [0 XP] and Level 2 [10 XP])
    prog = leveling.get_progress_to_next_level(5)
    assert prog["level"] == 1
    assert prog["next_level"] == 2
    assert prog["xp_needed"] == 5
    assert prog["percent"] == 50
    assert prog["is_max_level"] is False


def test_max_level_handling():
    """Tests that reaching the highest tier does not crash or produce invalid numbers."""
    prog = leveling.get_progress_to_next_level(999999)
    assert prog["is_max_level"] is True
    assert prog["next_level"] is None
    assert prog["xp_needed"] == 0
    assert prog["percent"] == 100


def test_render_progress_bar():
    """Tests visual progress bar string rendering."""
    bar_0 = leveling.render_progress_bar(0, bar_length=10)
    assert bar_0 == "[░░░░░░░░░░]"

    bar_50 = leveling.render_progress_bar(50, bar_length=10)
    assert bar_50 == "[█████░░░░░]"

    bar_100 = leveling.render_progress_bar(100, bar_length=10)
    assert bar_100 == "[██████████]"


def test_evaluate_message_xp_base():
    """Tests standard short message awards base XP."""
    xp, is_sug, is_long = leveling.evaluate_message_xp("Hey team, good morning!")
    assert xp == 10
    assert is_sug is False
    assert is_long is False


def test_evaluate_message_xp_suggestion():
    """Tests message containing suggestion keywords awards suggestion bonus."""
    text = "I have a suggestion for improving our channel workflow."
    xp, is_sug, is_long = leveling.evaluate_message_xp(text)
    assert is_sug is True
    assert xp == 25  # 10 base + 15 suggestion


def test_evaluate_message_xp_long_and_suggestion():
    """Tests thoughtful, long suggestion message awards all bonuses."""
    text = (
        "Here is a detailed proposal and idea: we should automate our test runner and deploy "
        "the bot with Docker compose so everyone on the team can use it smoothly."
    )
    xp, is_sug, is_long = leveling.evaluate_message_xp(text)
    assert is_sug is True
    assert is_long is True
    assert xp == 30  # 10 base + 5 long + 15 suggestion


def test_format_rank_card():
    """Tests that rank card template produces valid markdown without errors."""
    user_data = {
        "user_id": "U_TESTER",
        "xp": 75,
        "message_count": 8
    }
    card = leveling.format_rank_card(user_data)
    assert "<@U_TESTER>" in card
    assert "Level 4" in card
    assert "Dabbler" in card
    assert "75 XP" in card


def test_format_level_up_message():
    """Tests level-up announcement template."""
    msg = leveling.format_level_up_message("U_HERO", 3, "⭐ Active Thinker")
    assert "<@U_HERO>" in msg
    assert "Level 3: ⭐ Active Thinker" in msg


def test_format_leaderboard():
    """Tests leaderboard formatting with entries and empty states."""
    # Non-empty
    entries = [
        {"user_id": "U1", "xp": 200, "message_count": 20},
        {"user_id": "U2", "xp": 100, "message_count": 10}
    ]
    board = leveling.format_leaderboard(entries)
    assert "<@U1>" in board
    assert "<@U2>" in board
    assert "Leaderboard" in board

    # Empty
    empty_board = leveling.format_leaderboard([])
    assert "No contributions recorded yet" in empty_board
