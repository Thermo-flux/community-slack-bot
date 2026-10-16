"""Scheduler: closing weeks, reminders, monthly and final posts, pause, catch-up, the run loop.

Uses the real modules when they have landed; skips with a reason while another agent's module is still a stub.
"""
from __future__ import annotations

import os
import threading

import pytest

from goose.core.clock import FakeClock
from goose.gateway.scheduler import Scheduler
from goose.types import Result

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")


class FakeRouter:
    def __init__(self):
        self.notes = []

    def level_up_notes(self, user_ids):
        self.notes.append(list(user_ids))
        return Result()


class FakeStop:
    """A stop flag whose wait() returns at once, so run() loops without sleeping."""

    def __init__(self):
        self._set = False
        self.waits = 0

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits += 1
        return self._set


@pytest.fixture
def octx(cfg, clock, rng, tmp_path):
    from goose.core.context import build_context
    from goose.core.lines import LineBank

    if not os.path.exists(LINES_PATH):
        pytest.skip("config/lines.yaml is not written yet (agent B)")
    try:
        bank = LineBank.from_file(LINES_PATH)
        c = build_context(cfg, db_path=":memory:", audit_path=str(tmp_path / "audit.log"), lines=bank, clock=clock,
                          rng=rng, bot_user_id="UBOT")
    except NotImplementedError as exc:
        pytest.skip("waiting for another agent's module to land: %s" % (exc or "NotImplementedError"))
    yield c
    c.db.close()


def _at(clock: FakeClock, iso_date: str, seconds_into_day: int = 0) -> float:
    """Local UTC time of a date plus seconds. Test config uses UTC."""
    return clock.date_to_ts(iso_date) + seconds_into_day


def _organizer_texts(result: Result, phrase: str = ""):
    """Organizer posts, optionally only those containing `phrase`."""
    return [a.text for a in result.actions if a.kind == "organizer_post" and phrase in a.text]


def _join(ctx, *uids, show=True):
    """Join people during the previous week (2025-09-29), so a member's join week is not a second closable week."""
    clock = ctx.clock
    real_now = clock.now()
    clock.set(clock.date_to_ts("2025-09-29") + 12 * 3600)
    try:
        for uid in uids:
            ctx.users.join(uid, clock.now())
            ctx.users.set_show(uid, show)
    finally:
        clock.set(real_now)


def _msgs(ctx, uid, ts, n):
    for i in range(n):
        ctx.ledger.add_fact(idem_key="msg:C1:%s:%d" % (uid, int(ts) + i), user_id=uid, kind="msg", ts=ts + i,
                            channel="C1", message_ts="%.6f" % (ts + i))


def _setup_previous_week(ctx, clock):
    """Two people, active on Tuesday 2025-09-30. That week (id 2025-09-29) closes on Monday 2025-10-06 at 00:15."""
    _join(ctx, "U1", "U2")
    ts = _at(clock, "2025-09-30", 3600)
    _msgs(ctx, "U1", ts, 5)
    _msgs(ctx, "U2", ts, 3)


def test_week_closes_at_monday_0015_not_before(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-06", 900 - 1))
    assert _organizer_texts(sched.tick(clock.now())) == []
    clock.set(_at(clock, "2025-10-06", 900))
    texts = _organizer_texts(sched.tick(clock.now()), "is closed")
    assert len(texts) == 1
    assert "Week 2025-09-29 is closed" in texts[0]
    assert "/goose confirm-week 2025-09-29" in texts[0]
    assert octx.weekly.status("2025-09-29")["state"] == "provisional"


def test_tick_is_idempotent_and_posts_once(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-06", 1000))
    first = _organizer_texts(sched.tick(clock.now()), "is closed")
    second = _organizer_texts(sched.tick(clock.now()), "is closed")
    third = _organizer_texts(sched.tick(clock.now() + 60), "is closed")
    assert len(first) == 1
    assert second == [] and third == []


def test_catch_up_after_downtime_closes_every_missed_week(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _join(octx, "U1")
    _msgs(octx, "U1", _at(clock, "2025-09-23", 3600), 2)
    _msgs(octx, "U1", _at(clock, "2025-09-30", 3600), 2)
    clock.set(_at(clock, "2025-10-06", 2000))
    texts = " ".join(_organizer_texts(sched.tick(clock.now())))
    assert "Week 2025-09-22 is closed" in texts
    assert "Week 2025-09-29 is closed" in texts


def test_tuesday_reminder_after_noon_once(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-06", 1000))
    sched.tick(clock.now())
    clock.set(_at(clock, "2025-10-07", 12 * 3600 - 1))
    assert _organizer_texts(sched.tick(clock.now()), "not confirmed yet") == []
    clock.set(_at(clock, "2025-10-07", 12 * 3600))
    reminders = _organizer_texts(sched.tick(clock.now()), "not confirmed yet")
    assert len(reminders) == 1 and "2025-09-29" in reminders[0]
    clock.set(_at(clock, "2025-10-07", 15 * 3600))
    assert _organizer_texts(sched.tick(clock.now()), "not confirmed yet") == []


def test_no_confirmation_means_no_public_post(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-08", 0))
    res = sched.tick(clock.now())
    assert all(a.kind != "post" for a in res.actions)
    assert octx.weekly.status("2025-09-29")["state"] == "provisional"


def test_all_time_post_once_on_the_first_monday_of_the_month(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-06", 1000))
    sched.tick(clock.now())
    clock.set(_at(clock, "2025-11-03", 900 - 1))
    assert not _organizer_texts(sched.tick(clock.now()), "All-time 10")
    clock.set(_at(clock, "2025-11-03", 900))
    posts = [t for t in _organizer_texts(sched.tick(clock.now())) if "All-time" in t]
    assert len(posts) == 1 and "2025-11" in posts[0]
    clock.set(_at(clock, "2025-11-03", 5000))
    assert not [t for t in _organizer_texts(sched.tick(clock.now())) if "All-time" in t]


def test_final_post_once_at_the_end_date(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    octx.weekly.set_end("2025-10-20", "UOWNER")
    end = octx.weekly.end_ts()
    assert end == _at(clock, "2025-10-20")
    clock.set(end - 1)
    assert not [t for t in _organizer_texts(sched.tick(clock.now())) if "end date" in t]
    clock.set(end)
    finals = [t for t in _organizer_texts(sched.tick(clock.now())) if "end date" in t]
    assert len(finals) == 1 and "confirm-final" in finals[0]
    clock.set(end + 3600)
    assert not [t for t in _organizer_texts(sched.tick(clock.now())) if "end date" in t]


def test_paused_only_finalizes_pending_facts(octx, clock):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    octx.db.set_setting("paused", "1")
    clock.set(_at(clock, "2025-10-06", 1000))
    res = sched.tick(clock.now())
    assert res.actions == []
    assert octx.weekly.status("2025-09-29") is None
    # The pending facts became final while paused (hold is 600 seconds), so the ledger already counts them.
    assert octx.ledger.week_breakdown("U1", "2025-09-29").total > 0
    octx.db.set_setting("paused", "0")
    texts = _organizer_texts(sched.tick(clock.now()), "is closed")
    assert len(texts) == 1


def test_level_up_notes_are_asked_for_the_people_who_became_final(octx, clock):
    router = FakeRouter()
    sched = Scheduler(octx, router)
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-09-30", 3600 + 700))
    sched.tick(clock.now())
    assert router.notes and "U1" in router.notes[0]


def test_run_delivers_results_and_survives_a_failing_tick(octx, clock, monkeypatch):
    sched = Scheduler(octx, FakeRouter())
    _setup_previous_week(octx, clock)
    clock.set(_at(clock, "2025-10-06", 1000))
    original = octx.weekly.closable_weeks
    calls = {"n": 0}

    def flaky(now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database busy")
        return original(now)

    monkeypatch.setattr(octx.weekly, "closable_weeks", flaky)
    stop = FakeStop()
    delivered = []

    def deliver(result):
        delivered.append(result)
        stop.set()

    sched.run(stop, deliver)
    assert calls["n"] == 2
    assert len(delivered) == 1
    assert any(a.kind == "organizer_post" for a in delivered[0].actions)


def test_run_returns_at_once_when_already_stopped(octx):
    sched = Scheduler(octx, FakeRouter())
    stop = FakeStop()
    stop.set()
    called = []
    sched.run(stop, lambda r: called.append(r))
    assert called == []
    assert stop.waits == 0


def test_threading_event_works_as_the_stop_flag(octx):
    sched = Scheduler(octx, FakeRouter())
    stop = threading.Event()
    stop.set()
    sched.run(stop, lambda r: None)
