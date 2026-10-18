"""Tests for the reliability/personality features: state, distress, privacy, voice cap,
goose meter, weekly report, config validation, and voice post-filter."""

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ai_service
import config
import crumbs
import database
import handlers


@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database.init_db(db_path)
    yield db_path
    if os.path.exists(db_path):
        os.remove(db_path)


# --- default-path safety (regression) -------------------------------------

def test_managed_connection_treats_none_as_default(monkeypatch, temp_db):
    monkeypatch.setattr(database, "DB_PATH", temp_db)
    with database._managed_connection(None) as conn:
        assert conn is not None


def test_crumbs_functions_work_without_explicit_db_path(monkeypatch, temp_db):
    monkeypatch.setattr(database, "DB_PATH", temp_db)
    crumbs.set_opted_in("U_DEF")
    assert crumbs.is_opted_in("U_DEF") is True
    crumbs.add_ledger_event("U_DEF", "star", amount=25)
    assert crumbs.get_pond_status("U_DEF")["crumbs"] == 25
    assert any(e["user_id"] == "U_DEF" for e in crumbs.get_pond_leaderboard(public=False))


# --- pause vs freeze ------------------------------------------------------

def test_pause_and_freeze_are_distinct(monkeypatch):
    store = {}
    monkeypatch.setattr(handlers, "get_flag", lambda key, default=False: store.get(key, default))
    monkeypatch.setattr(handlers, "set_flag", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(handlers, "add_audit", lambda *a, **k: 0)
    monkeypatch.setattr(handlers, "_STATE_CACHE", {}, raising=False)

    handlers._set_paused(True)
    assert handlers._is_paused() is True
    assert handlers._is_frozen() is False

    handlers._set_frozen(True)
    assert handlers._is_paused() is True and handlers._is_frozen() is True

    handlers._set_paused(False)
    assert handlers._is_paused() is False and handlers._is_frozen() is True

    handlers._set_frozen(False)
    assert handlers._is_frozen() is False


def test_freeze_and_pause_block_scoring(monkeypatch):
    monkeypatch.setattr(handlers, "_is_paused", lambda: True)
    monkeypatch.setattr(handlers, "_is_frozen", lambda: False)
    with patch("handlers.crumbs.record_qualified_message") as record:
        handlers._score_loadout_activity("U1", handlers.OFFICIAL_LOADOUT_CHANNEL_ID, {"ts": "1"}, MagicMock(), "hello there friend", {})
        record.assert_not_called()

    monkeypatch.setattr(handlers, "_is_paused", lambda: False)
    monkeypatch.setattr(handlers, "_is_frozen", lambda: True)
    with patch("handlers.crumbs.record_qualified_message") as record:
        handlers._score_loadout_activity("U1", handlers.OFFICIAL_LOADOUT_CHANNEL_ID, {"ts": "1"}, MagicMock(), "hello there friend", {})
        record.assert_not_called()


# --- distress -------------------------------------------------------------

def test_distress_sends_safe_reply_and_alerts_organizers(monkeypatch):
    client = MagicMock()
    say = MagicMock()
    monkeypatch.setattr(ai_service, "format_distress_reply", lambda: "SAFE REPLY")
    alerts = []
    monkeypatch.setattr(handlers, "_alert_organizers", lambda c, text: alerts.append(text))

    handlers._distress_reply_and_alert(client, say, "C1", "U1", "t1")
    client.chat_postEphemeral.assert_called_once()
    assert client.chat_postEphemeral.call_args.kwargs.get("user") == "U1"
    assert client.chat_postEphemeral.call_args.kwargs.get("text") == "SAFE REPLY"
    assert alerts


def test_detect_distress_flags_and_ignores():
    assert ai_service.detect_distress("I want to die") is True
    assert ai_service.detect_distress("thinking about suicide") is True
    assert ai_service.detect_distress("great build, love the finish") is False


# --- privacy --------------------------------------------------------------

def test_privacy_controls_public_board(temp_db):
    crumbs.set_opted_in("U_SHOW", db_path=temp_db)
    crumbs.set_opted_in("U_HIDE", db_path=temp_db)
    crumbs.add_ledger_event("U_SHOW", "star", amount=25, db_path=temp_db)
    crumbs.add_ledger_event("U_HIDE", "star", amount=60, db_path=temp_db)
    crumbs.set_privacy_show("U_SHOW", True, db_path=temp_db)
    crumbs.set_privacy_show("U_HIDE", False, db_path=temp_db)

    public = crumbs.get_pond_leaderboard(public=True, db_path=temp_db)
    ids = {e["user_id"] for e in public}
    assert "U_SHOW" in ids and "U_HIDE" not in ids

    full = crumbs.get_pond_leaderboard(public=False, db_path=temp_db)
    assert {"U_SHOW", "U_HIDE"} <= {e["user_id"] for e in full}


def test_public_board_is_visible_by_default_and_not_capped(temp_db):
    ids = [f"U{i}" for i in range(5)]
    for amount, uid in enumerate(ids, start=1):
        crumbs.set_opted_in(uid, db_path=temp_db)
        crumbs.add_ledger_event(uid, "star", amount=25 + amount, db_path=temp_db)

    public = crumbs.get_pond_leaderboard(public=True, db_path=temp_db)
    assert {e["user_id"] for e in public} == set(ids)


# --- voice cap ------------------------------------------------------------

def test_voice_hourly_cap(temp_db):
    assert database.consume_voice_quota("U1", "2026-01-01T10", 2, db_path=temp_db) is True
    assert database.consume_voice_quota("U1", "2026-01-01T10", 2, db_path=temp_db) is True
    assert database.consume_voice_quota("U1", "2026-01-01T10", 2, db_path=temp_db) is False
    assert database.consume_voice_quota("U1", "2026-01-01T11", 2, db_path=temp_db) is True


# --- goose meter ----------------------------------------------------------

def test_goose_meter_is_match_gated(monkeypatch):
    calls = []
    monkeypatch.setattr(handlers, "GOOSE_METER", {"enabled": True, "reset_hours": 24})
    monkeypatch.setattr(handlers, "touch_channel_counter", lambda *a, **k: calls.append(a) or 1)
    assert handlers._bump_goose_meter("C1", "nice build today") == 0
    assert handlers._bump_goose_meter("C1", "you goose, that's a goose move") == 1
    assert len(calls) == 1


def test_goose_meter_counter_persists_and_increments(temp_db):
    assert database.touch_channel_counter("C1", "goose_meter", 1.0, 24, db_path=temp_db) == 1.0
    assert database.touch_channel_counter("C1", "goose_meter", 1.0, 24, db_path=temp_db) == 2.0
    assert database.get_channel_counter("C1", "goose_meter", db_path=temp_db) == 2.0


# --- weekly report --------------------------------------------------------

def test_weekly_report_posts_once(monkeypatch):
    state = {}
    monkeypatch.setattr(handlers, "_is_paused", lambda: False)
    monkeypatch.setattr(handlers, "_is_frozen", lambda: False)
    monkeypatch.setattr(handlers, "ORGANIZER_CHANNEL_ID", "C_ORG")
    monkeypatch.setattr(handlers, "get_state", lambda k, d="": state.get(k, d))
    monkeypatch.setattr(handlers, "set_state", lambda k, v: state.__setitem__(k, v))
    monkeypatch.setattr(handlers.crumbs, "get_weekly_leaderboard", lambda wk, limit=10: [
        {"user_id": "U1", "level": 3, "title": "Paddler", "week_crumbs": 12.0}
    ])
    monkeypatch.setattr(handlers.crumbs, "format_weekly_report", lambda wk, entries: "REPORT")
    monkeypatch.setattr(handlers.crumbs, "snapshot_week", lambda wk: 0)
    monkeypatch.setattr(handlers, "backup_database", lambda: None)

    client = MagicMock()
    handlers.run_scheduled_maintenance(client, force=True)
    handlers.run_scheduled_maintenance(client, force=True)
    assert client.chat_postMessage.call_count == 1
    assert state.get("last_weekly_report")


# --- config validation ----------------------------------------------------

def test_config_validation_catches_bad_config(monkeypatch):
    monkeypatch.setattr(config, "CONFIG", {
        "official_loadout_channel_id": "not-a-channel",
        "owner_user_id": "bad",
        "level_tiers": [],
    })
    problems = config.validate_config()
    assert problems
    with pytest.raises(config.ConfigValidationError):
        config.assert_valid_config()


def test_config_validation_passes_for_defaults():
    assert config.validate_config() == []


# --- voice post-filter + persona blocks -----------------------------------

def test_post_filter_voice_blocks_reward_language(monkeypatch):
    monkeypatch.setattr(ai_service, "REWARDS", {"announced": False, "words": ["prize", "money"]})
    assert ai_service.post_filter_voice("you could win a prize for that") == ""
    assert ai_service.post_filter_voice("nice work on the build") == "nice work on the build"


def test_parse_pond_command_leaderboard_and_season_aliases():
    assert handlers.parse_pond_command("goose leaderboard") == ("leaderboard", [])
    assert handlers.parse_pond_command("goose set-end 1700000000") == ("set-end", ["1700000000"])
    assert handlers.parse_pond_command("goose season") == ("season", [])


def test_pond_leaderboard_is_posted_publicly(monkeypatch):
    posted = {}

    def fake_say(say, **kwargs):
        posted.update(kwargs)

    entries = [{"user_id": f"U{i}"} for i in range(5)]
    monkeypatch.setattr(handlers, "_safe_say", fake_say)
    monkeypatch.setattr(handlers, "_is_organizer", lambda uid: False)
    monkeypatch.setattr(
        handlers.crumbs, "get_pond_leaderboard", lambda **kwargs: entries
    )
    monkeypatch.setattr(
        handlers.crumbs, "format_pond_leaderboard", lambda items, limit=10: f"{len(items)} entries"
    )

    handled = handlers._handle_pond_command(
        "leaderboard", [], MagicMock(), MagicMock(), "C1", "U1", "T1"
    )

    assert handled is True
    assert posted == {"text": "5 entries", "thread_ts": "T1"}


def test_season_line_and_set_end(monkeypatch):
    state = {}
    monkeypatch.setattr(handlers, "get_state", lambda k, d="": state.get(k, d))
    monkeypatch.setattr(handlers, "set_state", lambda k, v: state.__setitem__(k, v))
    monkeypatch.setattr(handlers, "_is_organizer", lambda uid: True)
    monkeypatch.setattr(handlers, "_post_private", lambda *a, **k: None)
    monkeypatch.setattr(handlers, "SEASON", {"end_ts": 0})

    assert "open-ended" in handlers._season_line()
    handlers._handle_pond_command("set-end", ["1700000000"], MagicMock(), MagicMock(), "C1", "U1", None)
    assert "2023-11-14" in handlers._season_line()
    handlers._handle_pond_command("set-end", ["off"], MagicMock(), MagicMock(), "C1", "U1", None)
    assert "open-ended" in handlers._season_line()


def test_loadout_questions_deflect_to_orry():
    instructions = ai_service.build_system_instructions()
    assert "Orry" in instructions
    assert "silly goose" in instructions.lower()
    assert "Stay fresh" in instructions


def test_knowledge_block_deflects_instead_of_answering():
    instructions = ai_service.build_system_instructions(knowledge="Ore price for X is 100.")
    assert "LOADOUT PROGRAM KNOWLEDGE" in instructions
    assert "Orry is the authority" in instructions
    assert "Quote exact figures" not in instructions
    assert "Use it as the source of truth for this LOADOUT question" not in instructions


def test_persona_blocks_are_conditional():
    base = ai_service.build_system_instructions()
    assert "ROAST POLICY (a rare bit" not in base
    assert "GOOSE METER (the channel" not in base

    with_roast = ai_service.build_system_instructions(roast_allowed=True)
    assert "ROAST POLICY (a rare bit" in with_roast

    with_meter = ai_service.build_system_instructions(goose_meter=7)
    assert "GOOSE METER (the channel" in with_meter
