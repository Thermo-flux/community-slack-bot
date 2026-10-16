"""Tests for goose/core/mood.py: the goose meter, cases 1 to 8 of docs/APPENDIX_VOICE.md section B, plus persistence,
table isolation and the import rule.

Expectations are written from config.mood and the decay formula (heat halves every half_life_s), so a retune of the
defaults changes the numbers here without editing the tests. The story these tests pin down with the current defaults:
one person alone tops out at annoyed (the per-person cap is below angry_at), two people reach angry, three stay at
angry or below, and four people reach the self-timeout.

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


def decayed(cfg, amount, seconds):
    """What an amount of heat is worth after the given number of seconds (halves every half_life_s)."""
    return amount * 0.5 ** (seconds / float(cfg.mood.half_life_s))


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


def stagger_plan(people, between_s=60, within_s=40):
    """Each person makes three calls, within_s apart. Each person starts between_s after the one before.
    Returns (offset, user) pairs in time order, offsets counted from the start of the plan."""
    plan = []
    for i, user in enumerate(people):
        for k in range(3):
            plan.append((i * between_s + k * within_s, user))
    return sorted(plan)


def play(mood, clock, plan):
    """Register each (offset, user) call on the given meter, in order, from the clock's current time."""
    start = clock.now()
    results = []
    for offset, user in plan:
        clock.set(start + offset)
        results.append(mood.register(user, clock.now()))
    return results


# ---- case 1: one person, three calls in five minutes -------------------------------------------------------

def test_case1_three_calls_in_five_minutes_then_quiet_gag(m, clock, db, cfg):
    mc = cfg.mood
    assert mc.first_add + 2 * mc.repeat_add == pytest.approx(mc.person_cap)   # three quick calls hit the cap
    r1 = m.register(U1, clock.now())
    assert r1.counted and r1.heat == pytest.approx(mc.first_add)
    clock.advance(1)
    r2 = m.register(U1, clock.now())
    assert r2.heat == pytest.approx(decayed(cfg, mc.first_add, 1) + mc.repeat_add, abs=1e-6)  # repeat adds repeat_add
    clock.advance(1)
    r3 = m.register(U1, clock.now())
    # the third call completes three calls in five minutes: it starts the gag and adds nothing
    assert r3.gag and not r3.counted
    assert not r3.reply and not r3.react and r3.stage == "silent"
    assert r3.heat == pytest.approx(decayed(cfg, r2.heat, 1), abs=1e-6)
    assert m.stage(clock.now()) == "calm"                     # two counted calls never reach annoyed
    clock.advance(1)
    r4 = m.register(U1, clock.now())
    clock.advance(1)
    r5 = m.register(U1, clock.now())
    for r in (r4, r5):
        assert r.gag and not r.counted
        assert not r.reply and not r.react
        assert r.stage == "silent"
    assert r5.heat == pytest.approx(decayed(cfg, r2.heat, 3), abs=1e-6)
    assert m.heat(clock.now()) == pytest.approx(decayed(cfg, r2.heat, 3), abs=1e-6)


def test_gag_ends_after_thirty_minutes(m, clock):
    for _ in range(3):
        m.register(U1, clock.now())
        clock.advance(1)
    assert m.register(U1, clock.now()).gag
    clock.advance(1800)
    r = m.register(U1, clock.now())
    assert not r.gag and r.counted


def test_one_person_quick_calls_never_get_past_annoyed(m, clock, db, cfg):
    mc = cfg.mood
    assert mc.person_cap < mc.angry_at                        # one person's cap alone cannot reach angry
    stages = []
    max_heat = 0.0
    for _ in range(30):
        clock.advance(10)
        r = m.register(U1, clock.now())
        stages.append(r.stage)
        max_heat = max(max_heat, r.heat)
    # the two counted calls add first_add + repeat_add (2.25), below annoyed_at; the third call starts the gag
    assert "silent" in stages
    assert set(stages) <= {"calm", "silent"}
    assert max_heat <= mc.first_add + mc.repeat_add + 1e-9
    assert m.stage(clock.now()) == "calm"


# ---- case 2: two people reach the per-person cap -----------------------------------------------------------

def test_case2_two_capped_people_heat_is_the_decayed_sum_of_their_adds(m, clock, db, cfg):
    start = clock.now()
    # each person makes calls 20 seconds apart: two count (1.5 then 0.75), the third starts the gag and adds nothing,
    # and the fourth is silent too. U2 starts 80 seconds after U1.
    plan = sorted([(t, U1) for t in (0, 20, 40, 60)] + [(t + 80, U2) for t in (0, 20, 40, 60)])
    results = {}
    for offset, user in plan:
        clock.set(start + offset)
        results.setdefault(user, []).append(m.register(user, clock.now()))
    for user in (U1, U2):
        assert [r.counted for r in results[user]] == [True, True, False, False]
        raw = sum(amount for _, _, amount in event_rows(db, user))
        assert raw == pytest.approx(cfg.mood.first_add + cfg.mood.repeat_add)   # two counted calls, under the cap
    assert results[U2][2].stage == "silent" and results[U2][2].gag
    now = clock.now()
    expected = 0.0
    for ts, _user, amount in event_rows(db):
        expected += amount * 0.5 ** ((now - ts) / float(cfg.mood.half_life_s))
    assert m.heat(now) == pytest.approx(expected, abs=1e-6)
    assert m.heat(now) < 2 * (cfg.mood.first_add + cfg.mood.repeat_add)
    # one more call from a capped person adds nothing
    clock.advance(10)
    assert not m.register(U1, clock.now()).counted


def test_per_person_cap_stops_at_three_in_thirty_minutes(m, clock, db, cfg):
    mc = cfg.mood
    adds = []
    for _ in range(6):
        clock.advance(360)                                    # six minutes apart: no gag, all within 30 minutes
        adds.append(m.register(U1, clock.now()))
    assert [r.counted for r in adds] == [True, True, True, False, False, False]
    assert sum(amount for _, _, amount in event_rows(db, U1)) == pytest.approx(mc.person_cap)
    # once the first add is more than thirty minutes old, the person has room again
    clock.advance(1)
    assert m.register(U1, clock.now()).counted


# ---- case 3: three capped people, then a fourth calls once -------------------------------------------------

def test_case3_fourth_call_reaches_timeout_then_goose_calls_are_silent(m, clock, db, cfg):
    mc = cfg.mood
    set_heat(m, 7.5, clock.now())                             # what three people leave behind: angry, under the timeout
    before = m.heat(clock.now())
    assert mc.angry_at <= before < mc.timeout_at              # angry, just under the timeout line
    clock.advance(10)
    r = m.register(U4, clock.now())                           # the fourth person calls once
    assert r.stage == "timeout" and r.reply and r.react
    assert r.heat == pytest.approx(decayed(cfg, before, 10) + mc.first_add, abs=1e-6)
    before_silent = m.heat(clock.now())
    clock.advance(60)
    silent = m.register("U5", clock.now())                    # a fresh person, so only the timeout silence applies
    assert silent.stage == "silent" and not silent.counted and not silent.reply and not silent.react
    assert silent.heat == pytest.approx(decayed(cfg, before_silent, 60), abs=1e-6)   # no new heat
    clock.advance(mc.timeout_silent_s - 120)                  # nine minutes after the timeout: still silent
    assert m.register("U6", clock.now()).stage == "silent"


def test_gagged_third_calls_add_no_heat_so_four_stagger_people_stay_under_timeout(cfg, clock):
    """Each person makes three calls about 40 seconds apart. The third call starts the gag and adds nothing, so each
    person adds only first_add + repeat_add. Four people peak below the timeout line (the plan's old 'four people
    reach timeout' figure assumed the third call added heat)."""
    three_db = Database(":memory:")
    four_db = Database(":memory:")
    try:
        three = play(Mood(three_db, cfg, clock, random.Random(1)), clock,
                     stagger_plan([U1, U2, U3]))
        assert all(r.stage in ("calm", "annoyed", "angry", "silent") for r in three)
        assert max(r.heat for r in three) < cfg.mood.timeout_at
        four = play(Mood(four_db, cfg, clock, random.Random(1)), clock,
                    stagger_plan([U1, U2, U3, U4]))
        assert max(r.heat for r in four) < cfg.mood.timeout_at
        assert "timeout" not in [r.stage for r in four]
        gagged = [r for r in four if r.gag]
        assert len(gagged) == 4 and all(not r.counted for r in gagged)
    finally:
        three_db.close()
        four_db.close()


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
    mc = cfg.mood
    rng = FixedRng([0.0, 0.0])
    mood = Mood(db, cfg, clock, rng)
    set_heat(mood, mc.angry_at, clock.now())                  # plus one first add: angry, under the timeout
    first = mood.register(U1, clock.now())
    assert first.stage == "angry" and first.honk and first.reply
    clock.advance(mc.honk_gap_s // 2)                         # half the honk gap later: no roll
    set_heat(mood, mc.angry_at, clock.now())
    second = mood.register(U2, clock.now())
    assert second.stage == "angry" and second.reply and not second.honk
    clock.advance(mc.honk_gap_s + 300 - mc.honk_gap_s // 2)   # honk_gap_s + 5 minutes after the first honk
    set_heat(mood, mc.angry_at, clock.now())
    third = mood.register(U3, clock.now())
    assert third.honk


def test_anger_lines_are_at_least_two_minutes_apart(db, cfg, clock):
    mc = cfg.mood
    mood = Mood(db, cfg, clock, FixedRng([0.99] * 10))
    set_heat(mood, mc.annoyed_at, clock.now())               # plus one first add: annoyed
    first = mood.register(U1, clock.now())
    assert first.stage == "annoyed" and first.reply
    clock.advance(60)
    second = mood.register(U1, clock.now())                  # a repeat from the same person: still annoyed
    assert second.stage == "annoyed" and not second.reply
    clock.advance(mc.anger_line_gap_s + 10 - 60)             # two minutes after the first line
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

def test_case6_timeout_silence_cooling_then_calm(m, clock, db, cfg):
    mc = cfg.mood
    t0 = clock.now()
    set_heat(m, mc.timeout_at, t0)
    timeout = m.register(U1, t0)
    assert timeout.stage == "timeout"
    timeout_heat = mc.timeout_at + mc.first_add
    assert timeout.heat == pytest.approx(timeout_heat)
    clock.set(t0 + 300)
    first = m.register(U2, clock.now())
    assert first.stage == "silent" and not first.counted and not first.reply
    clock.set(t0 + 480)
    second = m.register(U2, clock.now())
    assert second.stage == "silent" and not second.counted
    assert second.heat == pytest.approx(m.heat(clock.now()))
    assert second.heat == pytest.approx(decayed(cfg, timeout_heat, 480), abs=1e-6)  # the silent calls added nothing
    clock.set(t0 + 720)                                       # twelve minutes in: silence is over
    cooling = m.register(U3, clock.now())
    assert cooling.stage == "cooling" and cooling.reply
    clock.set(t0 + 780)
    quiet_line = m.register(U4, clock.now())
    assert quiet_line.stage == "cooling" and not quiet_line.reply   # one cooling line per five minutes
    clock.set(t0 + mc.cooling_max_s)                          # the end of the cooling window: calm again
    assert m.stage(clock.now()) == "calm"


def test_cooling_resets_heat_after_thirty_minutes(m, clock, cfg):
    t0 = clock.now()
    set_heat(m, cfg.mood.timeout_at, t0)
    m.register(U1, t0)
    clock.set(t0 + cfg.mood.cooling_max_s + 100)
    r = m.register(U2, clock.now())                           # past the cooling window: heat resets, then this adds
    assert r.stage == "calm"
    assert m.heat(clock.now()) == pytest.approx(cfg.mood.first_add)


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


def test_canned_stage_line_is_available_for_each_result(m, clock, lines, cfg):
    set_heat(m, cfg.mood.annoyed_at, clock.now())
    r = m.register(U1, clock.now())
    assert lines.canned_for_stage(r.stage, random.Random(1)).moment == "anger_" + r.stage
