"""Tests for goose/core/explain.py: stored German notes, explanation limits, gloss dial, unknown phrases, purge."""
from __future__ import annotations

import random

import pytest

from goose.core.explain import ExplainService

DAY = 86400
CH = "C1"


@pytest.fixture
def svc(db, cfg, clock, lines):
    return ExplainService(db, cfg, clock, lines, random.Random(4))


def _explain(lines, ids):
    return " ".join(lines.explain_text(i) for i in ids)


def test_record_and_lookup_round_trip(svc, clock):
    svc.record(CH, "100.1", ["quak", "gans_falsch", "quak"], clock.now())
    assert svc.lookup(CH, "100.1") == ["quak", "gans_falsch"]
    assert svc.lookup(CH, "100.2") == []
    assert svc.lookup("C2", "100.1") == []


def test_record_nothing_when_no_german(svc, clock):
    svc.record(CH, "100.1", [], clock.now())
    assert svc.lookup(CH, "100.1") == []
    assert svc.explanation(CH, "100.1", clock.now()) is None


def test_explanation_is_none_when_nothing_is_stored(svc, clock):
    assert svc.explanation(CH, "999.9", clock.now()) is None


def test_explanation_is_the_approved_text_only(svc, clock, lines):
    svc.record(CH, "100.1", ["gans_falsch"], clock.now())
    text = svc.explanation(CH, "100.1", clock.now())
    assert text == _explain(lines, ["gans_falsch"])
    assert "ganz falsch" in text
    assert "{" not in text and "http" not in text


def test_one_explanation_per_message_per_ten_minutes(svc, clock):
    svc.record(CH, "100.1", ["quaktuell"], clock.now())
    assert svc.explanation(CH, "100.1", clock.now()) is not None
    clock.advance(300)
    assert svc.explanation(CH, "100.1", clock.now()) is None
    clock.advance(301)                                       # 601 seconds after the first
    assert svc.explanation(CH, "100.1", clock.now()) is not None


def test_at_most_three_explanations_per_channel_per_ten_minutes(svc, clock):
    for i in range(4):
        svc.record(CH, "200.%d" % i, ["quak"], clock.now())
    got = [svc.explanation(CH, "200.%d" % i, clock.now()) for i in range(4)]
    assert [g is not None for g in got] == [True, True, True, False]
    # another channel is not affected
    svc.record("C2", "300.1", ["quak"], clock.now())
    assert svc.explanation("C2", "300.1", clock.now()) is not None
    clock.advance(601)
    svc.record(CH, "200.9", ["quak"], clock.now())
    assert svc.explanation(CH, "200.9", clock.now()) is not None


def test_explanation_joins_several_ids(svc, clock, lines):
    svc.record(CH, "400.1", ["quak", "mensch"], clock.now())
    assert svc.explanation(CH, "400.1", clock.now()) == _explain(lines, ["quak", "mensch"])


def test_unknown_reply_is_a_fixed_plain_line(svc):
    text = svc.unknown_reply()
    assert text and "{" not in text
    assert "not in my table" in text


def test_gloss_on_request_is_empty(svc, cfg):
    cfg.vibes.german_gloss = "on_request"
    assert svc.maybe_gloss("U1", ["quak"], random.Random(1)) == ""


def test_gloss_always_glosses_every_id(svc, cfg):
    cfg.vibes.german_gloss = "always"
    text = svc.maybe_gloss("U1", ["quak_mal", "mensch"], random.Random(1))
    assert "look" in text and "wow" in text
    assert svc.maybe_gloss("U1", ["quak"], random.Random(1)) != ""
    assert svc.maybe_gloss("U1", [], random.Random(1)) == ""


def test_gloss_first_time_once_per_person_and_phrase(svc, cfg, db):
    cfg.vibes.german_gloss = "first_time"
    rng = random.Random(2)
    first = svc.maybe_gloss("U1", ["quak"], rng)
    assert first != ""
    assert svc.maybe_gloss("U1", ["quak"], rng) == ""                 # seen now
    assert svc.maybe_gloss("U1", ["quak", "mensch"], rng).count("(") == 1   # only mensch is new
    assert svc.maybe_gloss("U2", ["quak"], rng) != ""                 # another person has not seen it
    seen = db.query("SELECT de_id FROM gloss_seen WHERE user_id='U1' ORDER BY de_id")
    assert [r["de_id"] for r in seen] == ["mensch", "quak"]


def test_lookup_ignores_notes_older_than_seven_days(svc, clock):
    svc.record(CH, "500.1", ["quak"], clock.now())
    clock.advance(7 * DAY + 1)
    assert svc.lookup(CH, "500.1") == []
    assert svc.explanation(CH, "500.1", clock.now()) is None


def test_purge_removes_old_notes_and_logs_only(svc, clock, db):
    t0 = clock.now()
    svc.record(CH, "600.1", ["quak"], t0)                    # will be 8 days old
    clock.set(t0 + 6 * DAY)
    svc.record(CH, "600.2", ["mensch"], clock.now())         # 6 days old at purge time
    svc.explanation(CH, "600.2", clock.now())                # writes explain_log rows
    clock.set(t0 + 8 * DAY)
    deleted = svc.purge(clock.now())
    assert deleted >= 2
    rows = [r["message_ts"] for r in db.query("SELECT message_ts FROM german_notes")]
    assert rows == ["600.2"]
    assert db.query("SELECT key FROM explain_log WHERE ts < ?", (clock.now() - DAY,)) == []


def test_purge_keeps_fresh_rows(svc, clock, db):
    svc.record(CH, "700.1", ["quak"], clock.now())
    svc.explanation(CH, "700.1", clock.now())
    assert svc.purge(clock.now()) == 0
    assert svc.lookup(CH, "700.1") == ["quak"]
    assert db.query("SELECT key FROM explain_log")


def test_explain_log_is_purged_after_a_day_but_notes_are_kept(svc, clock, db):
    svc.record(CH, "800.1", ["quak"], clock.now())
    svc.explanation(CH, "800.1", clock.now())
    clock.advance(2 * DAY)
    svc.purge(clock.now())
    assert db.query("SELECT key FROM explain_log") == []
    assert svc.lookup(CH, "800.1") == ["quak"]
