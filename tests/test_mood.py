"""Tests for goose/core/mood.py: the goose meter, cases 1 to 8 of docs/APPENDIX_VOICE.md section B, plus persistence,
table isolation and the import rule.

Caller duty (documented, not tested here): the router must only call Mood.register for valid goose calls from humans,
never for bot messages (the duck's own lines included).
"""
from __future__ import annotations

import inspect
import os
import random

import pytest

from goose.core import mood as mood_module
from goose.core.clock import FakeClock
from goose.core.db import Database
from goose.core.mood import Mood
from goose.core.rules import mentions_goose
from goose.core.vibes import Vibes
from goose.types import Event

U1, U2, U3, U4 = "U1", "U2", "U3", "U4"


class FixedRng:
    """Returns the given values from random() in order, then 0.999 (never a roll). Only random() is used by the meter."""

    def __init__(self, values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0) if self._values else 0.999


@pytest.fixture
def m(db, cfg, clock):
    return Mood(db, cfg, clock, random.Random(1234))


def set_heat(mood, value, now):
    """Put the meter at a given heat at a given time (what a run of calls would have produced)."""
    mood._store_heat(value, now)


def event_rows(db, user=None):
    if user is None:
        return [tuple(r) for r in db.query("SELECT ts, user_id, amount FROM mood_events ORDER BY id")]
    return [tuple(r) for r in db.query("SELECT ts, user_id, amount FROM mood_events WHERE user_id=? ORDER BY id",
                                       (user,))]


def table_snapshot(db):
    out = {}
    for table in ("users", "facts", "flags"):
        out[table] = [tuple(r) for r in db.query("SELECT * FROM %s ORDER BY 1" % table)]
    return out


# ---- case 1: one person, three calls in five minutes -------------------------------------------------------

def test_case1_three_calls_in_five_minutes_then_quiet_gag(m, clock, db):
    r1 = m.register(U1, clock.now())
    assert r1.counted and r1.heat == pytest.approx(1.0, abs=0.01)
    clock.advance(1)
    r2 = m.register(U1, clock.now())
    assert r2.heat == pytest.approx(1.5, abs=0.02)            # repeat within 10 minutes adds 0.5
    clock.advance(1)
    r3 = m.register(U1, clock.now())
    assert r3.heat == pytest.approx(2.0, abs=0.02)            # the third call still counts
    assert r3.stage == "calm" and not r3.gag
    clock.advance(1)
    r4 = m.register(U1, clock.now())
    clock.advance(1)
    r5 = m.register(U1, clock.now())
    for r in (r4, r5):
        assert r.gag and not r.counted
        assert not r.reply and not r.react
        assert r.stage == "silent"
    assert r5.heat == pytest.approx(2.0, abs=0.03)
    assert m.heat(clock.now()) == pytest.approx(2.0, abs=0.03)


def test_gag_ends_after_thirty_minutes(m, clock):
    for _ in range(3):
        m.register(U1, clock.now())
        clock.advance(1)
    assert m.register(U1, clock.now()).gag
    clock.advance(1800)
    r = m.register(U1, clock.now())
    assert not r.gag and r.counted


# ---- case 2: two people reach the per-person cap -----------------------------------------------------------

def test_case2_two_capped_people_heat_is_the_decayed_sum_of_their_adds(m, clock, db):
    start = clock.now()
    # each person calls five times, 2.5 minutes apart (never three in five minutes, so the gag stays off)
    plan = sorted([(t, U1) for t in (0, 150, 306, 456, 612)] + [(t + 30, U2) for t in (0, 150, 306, 456, 612)])
    results = {}
    for offset, user in plan:
        clock.set(start + offset)
        results.setdefault(user, []).append(m.register(user, clock.now()))
    for user in (U1, U2):
        adds = [r.counted for r in results[user]]
        assert adds == [True, True, True, True, True]
        raw = sum(amount for _, _, amount in event_rows(db, user))
        assert raw == pytest.approx(3.0)                      # the per-person cap
    now = clock.now()
    expected = 0.0
    for ts, _user, amount in event_rows(db):
        expected += amount * 0.5 ** ((now - (ts)) / 600.0)
    assert m.heat(now) == pytest.approx(expected, abs=1e-6)
    # the appendix says 6.0 for two capped people. Decay lowers it, so it reads below that.
    assert m.heat(now) < 6.0
    # one more call from a capped person adds nothing
    clock.advance(10)
    assert not m.register(U1, clock.now()).counted


def test_per_person_cap_stops_at_three_in_thirty_minutes(m, clock, db):
    adds = []
    for _ in range(6):
        clock.advance(360)                                    # six minutes apart: no gag, all within 30 minutes
        adds.append(m.register(U1, clock.now()))
    assert [r.counted for r in adds] == [True, True, True, True, True, False]
    assert sum(amount for _, _, amount in event_rows(db, U1)) == pytest.approx(3.0)


# ---- case 3: three capped people, then a fourth calls once -------------------------------------------------

def test_case3_fourth_call_reaches_timeout_then_goose_calls_are_silent(m, clock, db):
    set_heat(m, 9.0, clock.now())                             # three capped people, no decay (appendix figure)
    r = m.register(U4, clock.now())
    assert r.stage == "timeout" and r.reply and r.react
    assert r.heat == pytest.approx(10.0)
    before = m.heat(clock.now())
    clock.advance(60)
    silent = m.register(U2, clock.now())
    assert silent.stage == "silent" and not silent.counted and not silent.reply and not silent.react
    assert silent.heat == pytest.approx(before * 0.5 ** (60 / 600.0), abs=1e-6)   # no new heat
    clock.advance(480)                                        # still inside the 10 minute silence (total 9 min)
    assert m.register(U3, clock.now()).stage == "silent"


def test_timeout_is_not_repeated_within_thirty_minutes(m, clock):
    set_heat(m, 9.0, clock.now())
    assert m.register(U1, clock.now()).stage == "timeout"
    clock.advance(20 * 60)                                    # silence over, cooling still running
    set_heat(m, 9.5, clock.now())
    again = m.register(U2, clock.now())
    assert again.stage == "angry"                             # the gap between timeouts is 30 minutes
    assert again.heat >= 10.0


def test_timeout_is_at_most_once_a_day(m, clock):
    set_heat(m, 9.0, clock.now())
    assert m.register(U1, clock.now()).stage == "timeout"
    clock.advance(31 * 60)                                    # cooling ends (heat resets on the next call)
    assert m.register(U2, clock.now()).stage == "calm"
    set_heat(m, 9.5, clock.now())
    again = m.register(U3, clock.now())
    assert again.stage == "angry"                             # second timeout the same day is refused
    assert again.heat >= 10.0


# ---- case 4: an angry reply rolls a honk, and another roll 10 minutes later --------------------------------

def test_case4_honk_then_no_honk_within_twenty_minutes(db, cfg, clock):
    rng = FixedRng([0.0, 0.0])
    mood = Mood(db, cfg, clock, rng)
    set_heat(mood, 7.0, clock.now())
    first = mood.register(U1, clock.now())
    assert first.stage == "angry" and first.honk and first.reply
    clock.advance(600)
    set_heat(mood, 7.0, clock.now())
    second = mood.register(U2, clock.now())
    assert second.stage == "angry" and second.reply and not second.honk
    clock.advance(900)                                        # 25 minutes after the first honk
    set_heat(mood, 7.0, clock.now())
    third = mood.register(U3, clock.now())
    assert third.honk


def test_anger_lines_are_at_least_two_minutes_apart(db, cfg, clock):
    mood = Mood(db, cfg, clock, FixedRng([0.99] * 10))
    set_heat(mood, 4.0, clock.now())
    first = mood.register(U1, clock.now())
    assert first.stage == "annoyed" and first.reply
    clock.advance(60)
    second = mood.register(U2, clock.now())
    assert second.stage == "annoyed" and not second.reply
    clock.advance(70)
    third = mood.register(U3, clock.now())
    assert third.reply


# ---- case 5: decay, heat halves every ten minutes ----------------------------------------------------------

def test_case5_heat_halves_every_ten_minutes(m, clock):
    set_heat(m, 9.0, clock.now())
    clock.advance(600)
    assert m.heat(clock.now()) == pytest.approx(4.5)
    assert m.stage(clock.now()) == "annoyed"
    clock.advance(600)
    assert m.heat(clock.now()) == pytest.approx(2.25)
    assert m.stage(clock.now()) == "calm"


# ---- case 6: timeout, silence, cooling, calm ----------------------------------------------------------------

def test_case6_timeout_silence_cooling_then_calm(m, clock, db):
    t0 = clock.now()
    set_heat(m, 9.0, t0)
    assert m.register(U1, t0).stage == "timeout"
    clock.set(t0 + 300)
    first = m.register(U2, clock.now())
    assert first.stage == "silent" and not first.counted and not first.reply
    clock.set(t0 + 480)
    second = m.register(U2, clock.now())
    assert second.stage == "silent" and not second.counted
    assert second.heat == pytest.approx(m.heat(clock.now()))
    assert second.heat == pytest.approx(10.0 * 0.5 ** (480 / 600.0), abs=1e-6)  # the silent calls added nothing
    clock.set(t0 + 720)                                       # twelve minutes in: silence is over
    cooling = m.register(U3, clock.now())
    assert cooling.stage == "cooling" and cooling.reply
    clock.set(t0 + 780)
    quiet_line = m.register(U4, clock.now())
    assert quiet_line.stage == "cooling" and not quiet_line.reply   # one cooling line per five minutes
    clock.set(t0 + 1500)                                      # twenty-five minutes in, heat well under 3
    assert m.stage(clock.now()) == "calm"


def test_cooling_resets_heat_after_thirty_minutes(m, clock):
    t0 = clock.now()
    set_heat(m, 9.0, t0)
    m.register(U1, t0)
    clock.set(t0 + 1900)
    r = m.register(U2, clock.now())                           # past the cooling window: heat resets, then this adds 1
    assert r.stage == "calm"
    assert m.heat(clock.now()) == pytest.approx(1.0)


# ---- cases 7 and 8: unaddressed and ping-with-others never reach the meter ---------------------------------

def test_case7_unaddressed_goose_never_reaches_the_meter(m, clock, db, cfg, lines):
    text = "a goose stole my bread"
    assert mentions_goose(text)                               # the word is there
    vibes = Vibes(db, cfg, clock, lines, random.Random(1))
    assert vibes.classify_addressing(Event(kind="message", ts=clock.now(), text=text), "UBOT") == ""
    assert m.heat(clock.now()) == 0.0
    assert event_rows(db) == []


def test_case8_ping_with_another_person_is_not_registered(m, clock, db, cfg, lines):
    text = "<@UBOT> <@U2> you're a goose"
    vibes = Vibes(db, cfg, clock, lines, random.Random(1))
    event = Event(kind="message", ts=clock.now(), text=text, bot_mentioned=True, mentions=["U2"])
    assert vibes.classify_addressing(event, "UBOT") == "ping"
    # The caller ignores goose calls that name another person (mentions); the meter is not called, so nothing moves.
    assert m.heat(clock.now()) == 0.0
    assert event_rows(db) == []


# ---- further guarantees ------------------------------------------------------------------------------------

def test_meter_never_changes_users_facts_or_flags(db, cfg, clock):
    db.execute("INSERT INTO users(user_id, joined_ts, show) VALUES ('U1', 1.0, 1)")
    db.execute("INSERT INTO users(user_id, joined_ts, show) VALUES ('U2', 1.0, 0)")
    db.execute("INSERT INTO facts(idem_key, week_id, user_id, kind, ts, amount, state) "
               "VALUES ('msg:C1:1', '2025-10-06', 'U1', 'msg', 1.0, 1.0, 'final')")
    db.execute("INSERT INTO flags(user_id, kind, scope, reason, actor, opened_ts) "
               "VALUES ('U2', 'note', 'all', 'x', 'UORG1', 1.0)")
    before = table_snapshot(db)
    mood = Mood(db, cfg, clock, random.Random(99))
    for step in range(40):
        user = ("U1", "U2", "U3", "U4")[step % 4]
        mood.register(user, clock.now())
        clock.advance(20 if step % 5 else 200)
    set_heat(mood, 11.0, clock.now())
    mood.register("U1", clock.now())
    mood.register("U2", clock.now())
    assert table_snapshot(db) == before
    assert event_rows(db)                                    # the meter did write its own rows
    assert db.query("SELECT key FROM mood_state")


def test_meter_source_does_not_import_scoring_or_commands():
    source = inspect.getsource(mood_module)
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "ledger" not in line and "users" not in line and "router" not in line
            assert "weekly" not in line and "levels" not in line and "commands" not in line


def test_state_survives_a_restart(tmp_path, cfg):
    path = str(tmp_path / "mood.db")
    clock = FakeClock(start=1_760_000_000.0)
    db = Database(path)
    mood = Mood(db, cfg, clock, random.Random(5))
    set_heat(mood, 7.0, clock.now())
    mood.register(U1, clock.now())
    value = mood.heat(clock.now())
    expected_stage = mood.stage(clock.now())
    db.close()
    db2 = Database(path)
    try:
        again = Mood(db2, cfg, clock, random.Random(5))
        assert again.heat(clock.now()) == pytest.approx(value)
        assert again.stage(clock.now()) == expected_stage
    finally:
        db2.close()
    assert os.path.exists(path)


def test_reset_forgets_the_meter(m, clock):
    set_heat(m, 9.0, clock.now())
    m.register(U1, clock.now())
    m.reset()
    assert m.heat(clock.now()) == 0.0
    assert m.stage(clock.now()) == "calm"


def test_canned_stage_line_is_available_for_each_result(m, clock, lines):
    set_heat(m, 4.0, clock.now())
    r = m.register(U1, clock.now())
    assert lines.canned_for_stage(r.stage, random.Random(1)).moment == "anger_" + r.stage
