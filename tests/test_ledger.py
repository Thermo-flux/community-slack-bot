"""Ledger: idempotent facts, voids, pending vs final, week boundaries, breakdowns and anonymization."""
from __future__ import annotations

import datetime as dt
import re

import pytest

from goose.core.ledger import helpful_key, msg_key, star_key, thanks_key, tombstone

UTC = dt.timezone.utc


def at(y, m, d, h=0, mi=0, s=0):
    return dt.datetime(y, m, d, h, mi, s, tzinfo=UTC).timestamp()


# Monday 2025-10-06 is the week; Monday 2025-10-13 is the next one.
MON = at(2025, 10, 6)
WEEK = "2025-10-06"
NEXT_WEEK = "2025-10-13"


class Facts:
    """Tiny helper: unique idem keys, sensible defaults."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.n = 0

    def add(self, user="U1", kind="msg", ts=None, state="final", **kw):
        self.n += 1
        key = kw.pop("idem_key", "k%06d" % self.n)
        return self.ledger.add_fact(idem_key=key, user_id=user, kind=kind,
                                    ts=ts if ts is not None else at(2025, 10, 7, 12), state=state, **kw)

    def msg(self, user="U1", ts=None, state="final", channel="C1", message_ts="", **kw):
        return self.add(user=user, kind="msg", ts=ts, state=state, channel=channel,
                        message_ts=message_ts or ("%.6f" % (ts or at(2025, 10, 7, 12))), **kw)


@pytest.fixture
def facts(ledger):
    return Facts(ledger)


# ---------------------------------------------------------------- writes and idempotency

def test_add_fact_returns_id_and_derives_week(ledger, db, facts):
    fid = facts.add(user="U1", kind="msg", ts=at(2025, 10, 8, 9))
    assert isinstance(fid, int)
    row = ledger.get_fact(fid)
    assert row["week_id"] == WEEK
    assert row["state"] == "final"


def test_duplicate_idem_key_is_ignored(ledger, db):
    first = ledger.add_fact(idem_key="msg:C1:1.0", user_id="U1", kind="msg", ts=MON + 10)
    second = ledger.add_fact(idem_key="msg:C1:1.0", user_id="U1", kind="msg", ts=MON + 10)
    assert first is not None
    assert second is None
    assert db.one("SELECT COUNT(*) AS n FROM facts")["n"] == 1


def test_idem_key_stays_taken_after_void(ledger, facts):
    facts.add(idem_key="thanks:C1:1.0:U2", user="U1", kind="thanks", counterparty="U2")
    assert ledger.void_fact(idem_key="thanks:C1:1.0:U2", actor="UORG1", reason="oops")
    assert ledger.add_fact(idem_key="thanks:C1:1.0:U2", user_id="U1", kind="thanks", ts=MON + 1) is None


def test_unknown_kind_is_refused(ledger):
    with pytest.raises(ValueError):
        ledger.add_fact(idem_key="x", user_id="U1", kind="bogus", ts=MON)


def test_bad_state_and_empty_key_are_refused(ledger):
    with pytest.raises(ValueError):
        ledger.add_fact(idem_key="x", user_id="U1", kind="msg", ts=MON, state="voided")
    with pytest.raises(ValueError):
        ledger.add_fact(idem_key="", user_id="U1", kind="msg", ts=MON)


def test_star_amounts_default_from_config(ledger, facts, cfg):
    s = facts.add(user="U1", kind="star", ts=MON + 5, actor="UORG1", reason="helpful")
    b = facts.add(user="U1", kind="bigstar", ts=MON + 6, actor="UORG1", reason="big")
    custom = facts.add(user="U1", kind="star", ts=MON + 7, amount=3.5, actor="UORG1", reason="x")
    assert ledger.get_fact(s)["amount"] == cfg.scoring.star_points
    assert ledger.get_fact(b)["amount"] == cfg.scoring.bigstar_points
    assert ledger.get_fact(custom)["amount"] == 3.5


# ---------------------------------------------------------------- voids

def test_voided_fact_never_counts(ledger, facts):
    keep = facts.msg(user="U1", ts=MON + 100)
    gone = facts.msg(user="U1", ts=MON + 200)
    assert ledger.void_fact(fact_id=gone, actor="UORG1", reason="spam", now=MON + 300)
    bd = ledger.week_breakdown("U1", WEEK)
    assert bd.n_msgs == 1
    assert ledger.get_fact(gone)["state"] == "voided"
    assert ledger.get_fact(gone)["void_reason"] == "spam"
    assert ledger.get_fact(keep)["state"] == "final"


def test_void_twice_and_unknown_return_false(ledger, facts):
    fid = facts.msg(user="U1", ts=MON + 1)
    assert ledger.void_fact(fact_id=fid, actor="UORG1", reason="x")
    assert ledger.void_fact(fact_id=fid, actor="UORG1", reason="x") is False
    assert ledger.void_fact(fact_id=99999, actor="UORG1") is False


def test_void_needs_an_identifier(ledger):
    with pytest.raises(ValueError):
        ledger.void_fact(actor="UORG1")


def test_voided_fact_takes_no_place_on_the_curve(ledger, facts):
    for i in range(5):
        facts.msg(user="U1", ts=MON + 10 + i)
    fid = facts.msg(user="U1", ts=MON + 50)
    before = ledger.week_breakdown("U1", WEEK).chat
    ledger.void_fact(fact_id=fid, actor="UORG1", reason="x")
    after = ledger.week_breakdown("U1", WEEK).chat
    assert after < before
    assert ledger.week_breakdown("U1", WEEK).n_msgs == 5


def test_find_by_message_skips_voided(ledger, facts):
    fid = facts.msg(user="U1", ts=MON + 1, channel="C1", message_ts="111.1")
    assert [f["id"] for f in ledger.find_by_message("C1", "111.1")] == [fid]
    ledger.void_fact(fact_id=fid, actor="UORG1", reason="deleted")
    assert ledger.find_by_message("C1", "111.1") == []


def test_find_by_message_with_no_kinds_is_empty(ledger, facts):
    facts.msg(user="U1", ts=MON + 1, channel="C1", message_ts="111.1")
    assert ledger.find_by_message("C1", "111.1", kinds=()) == []


def test_all_facts_keeps_voided_rows(ledger, facts):
    fid = facts.msg(user="U1", ts=MON + 1)
    ledger.void_fact(fact_id=fid, actor="UORG1", reason="x")
    rows = ledger.all_facts(week_id=WEEK)
    assert [r["state"] for r in rows] == ["voided"]


# ---------------------------------------------------------------- pending and final

def test_pending_becomes_final_after_hold(ledger, facts, clock, cfg):
    hold = cfg.scoring.hold_seconds
    fid = facts.msg(user="U1", ts=MON + 1000, state="pending")
    assert ledger.finalize_due(MON + 1000 + hold - 1) == []
    assert ledger.get_fact(fid)["state"] == "pending"
    assert ledger.finalize_due(MON + 1000 + hold) == ["U1"]  # exactly at the boundary
    assert ledger.get_fact(fid)["state"] == "final"


def test_finalize_due_reports_only_changed_people(ledger, facts, cfg):
    facts.msg(user="U2", ts=MON + 1, state="pending")
    facts.msg(user="U1", ts=MON + 10 ** 6, state="pending")
    assert ledger.finalize_due(MON + cfg.scoring.hold_seconds + 10) == ["U2"]


def test_pending_counts_only_when_asked(ledger, facts):
    facts.msg(user="U1", ts=MON + 1, state="final")
    facts.msg(user="U1", ts=MON + 2, state="pending")
    assert ledger.week_breakdown("U1", WEEK).n_msgs == 1
    assert ledger.week_breakdown("U1", WEEK, include_pending=True).n_msgs == 2
    assert ledger.week_breakdowns(WEEK).get("U1").n_msgs == 1
    assert ledger.week_breakdowns(WEEK, include_pending=True)["U1"].n_msgs == 2


def test_finalize_week_ignores_the_hold(ledger, facts):
    facts.msg(user="U1", ts=MON + 1, state="pending")
    facts.msg(user="U2", ts=MON + 2, state="pending")
    facts.msg(user="U3", ts=MON + 1 + 7 * 86400, state="pending")  # next week, untouched
    assert ledger.finalize_week(WEEK) == ["U1", "U2"]
    assert ledger.week_breakdown("U1", WEEK).n_msgs == 1
    assert ledger.week_breakdown("U3", NEXT_WEEK).n_msgs == 0


# ---------------------------------------------------------------- weeks

def test_week_boundary_sunday_night_vs_monday_morning(ledger, facts):
    sunday = facts.msg(user="U1", ts=at(2025, 10, 12, 23, 59, 59))
    monday = facts.msg(user="U1", ts=at(2025, 10, 13, 0, 0, 0))
    assert ledger.get_fact(sunday)["week_id"] == WEEK
    assert ledger.get_fact(monday)["week_id"] == NEXT_WEEK
    assert ledger.week_breakdown("U1", WEEK).n_msgs == 1
    assert ledger.week_breakdown("U1", NEXT_WEEK).n_msgs == 1


def test_weeks_with_facts_ignores_voided_only_weeks(ledger, facts):
    fid = facts.msg(user="U1", ts=MON + 1)
    facts.msg(user="U1", ts=MON + 8 * 86400)
    ledger.void_fact(fact_id=fid, actor="UORG1", reason="x")
    assert ledger.weeks_with_facts("U1") == [NEXT_WEEK]
    assert ledger.weeks_with_facts() == [NEXT_WEEK]


# ---------------------------------------------------------------- breakdowns

def test_active_days_counts_distinct_local_days(ledger, facts):
    facts.msg(user="U1", ts=at(2025, 10, 7, 9))
    facts.msg(user="U1", ts=at(2025, 10, 7, 21))   # same day
    facts.msg(user="U1", ts=at(2025, 10, 9, 9))
    bd = ledger.week_breakdown("U1", WEEK)
    assert bd.n_msgs == 3
    assert bd.active_days == 2


def test_thanks_and_helpful_go_to_the_recipient(ledger, facts):
    facts.add(user="U1", kind="thanks", counterparty="U2", ts=MON + 1)
    facts.add(user="U1", kind="thanks", counterparty="U3", ts=MON + 2)
    facts.add(user="U1", kind="helpful", counterparty="U4", ts=MON + 3)
    bd = ledger.week_breakdown("U1", WEEK)
    assert bd.n_thanks == 2
    assert bd.n_helpful == 1
    assert ledger.week_breakdown("U2", WEEK).n_thanks == 0


def test_stars_sum_and_count(ledger, facts):
    facts.add(user="U1", kind="star", ts=MON + 1, actor="UORG1", reason="a")
    facts.add(user="U1", kind="bigstar", ts=MON + 2, actor="UORG2", reason="b")
    bd = ledger.week_breakdown("U1", WEEK)
    assert bd.stars == 25 + 60
    assert bd.star_count == 2
    assert bd.total == pytest.approx(85.0)


def test_week_breakdowns_only_lists_people_with_counted_facts(ledger, facts):
    facts.msg(user="U1", ts=MON + 1)
    facts.msg(user="U2", ts=MON + 2, state="pending")
    assert set(ledger.week_breakdowns(WEEK)) == {"U1"}
    assert set(ledger.week_breakdowns(WEEK, include_pending=True)) == {"U1", "U2"}


def test_lifetime_sums_weekly_totals(ledger, facts):
    facts.msg(user="U1", ts=MON + 1)
    facts.msg(user="U1", ts=MON + 8 * 86400 + 1)
    w1 = ledger.week_breakdown("U1", WEEK).total
    w2 = ledger.week_breakdown("U1", NEXT_WEEK).total
    assert ledger.lifetime("U1") == pytest.approx(w1 + w2)
    assert ledger.lifetime_totals()["U1"] == pytest.approx(w1 + w2)
    assert ledger.lifetime_breakdown("U1").n_msgs == 2


def test_lifetime_for_unknown_person_is_zero(ledger):
    assert ledger.lifetime("UNKNOWN") == 0.0
    assert ledger.lifetime_totals() == {}


def test_stars_for_filters_and_skips_voided(ledger, facts):
    keep = facts.add(user="U1", kind="star", ts=MON + 1, actor="UORG1", reason="kept", link="https://x")
    gone = facts.add(user="U1", kind="star", ts=MON + 2, actor="UORG1", reason="gone")
    facts.add(user="U1", kind="bigstar", ts=MON + 8 * 86400, actor="UORG2", reason="next week")
    ledger.void_fact(fact_id=gone, actor="UORG1", reason="typo")
    week_one = ledger.stars_for("U1", WEEK)
    assert [s["id"] for s in week_one] == [keep]
    assert week_one[0]["reason"] == "kept"
    assert week_one[0]["link"] == "https://x"
    assert week_one[0]["kind"] == "star"
    assert len(ledger.stars_for("U1")) == 2
    assert ledger.star_count("U1") == 2


def test_stars_for_can_skip_pending(ledger, facts):
    facts.add(user="U1", kind="star", ts=MON + 1, state="pending", actor="UORG1", reason="new")
    assert len(ledger.stars_for("U1", WEEK)) == 1
    assert ledger.stars_for("U1", WEEK, include_pending=False) == []


def test_recognizers_are_distinct_and_skip_voided(ledger, facts):
    facts.add(user="U1", kind="thanks", counterparty="U2", ts=MON + 1)
    facts.add(user="U1", kind="thanks", counterparty="U2", ts=MON + 2)
    fid = facts.add(user="U1", kind="helpful", counterparty="U3", ts=MON + 3)
    facts.add(user="U1", kind="helpful", counterparty="U9", ts=MON + 4)
    ledger.void_fact(fact_id=ledger.get_fact(fid)["id"], actor="UORG1", reason="x")
    facts.add(user="U1", kind="msg", ts=MON + 5)  # messages are not recognition
    assert ledger.recognizers("U1", WEEK) == 2
    assert ledger.recognizers("U1") == 2


def test_first_fact_ts(ledger, facts):
    assert ledger.first_fact_ts("U1", WEEK) == 0.0
    facts.msg(user="U1", ts=MON + 500)
    facts.msg(user="U1", ts=MON + 100)
    assert ledger.first_fact_ts("U1", WEEK) == MON + 100


def test_message_days_and_last_channel(ledger, facts):
    facts.msg(user="U1", ts=at(2025, 10, 7, 9), channel="C1")
    facts.msg(user="U1", ts=at(2025, 10, 9, 9), channel="C2")
    assert ledger.message_days("U1") == {"2025-10-07", "2025-10-09"}
    assert ledger.last_channel("U1") == "C2"
    assert ledger.last_channel("NOBODY") == ""


# ---------------------------------------------------------------- leave: anonymization

def test_anonymize_removes_own_facts_and_tombstones_references(ledger, facts, db):
    facts.msg(user="LEAVER", ts=MON + 1)
    facts.add(user="U1", kind="thanks", counterparty="LEAVER", ts=MON + 2)
    facts.add(user="U1", kind="star", ts=MON + 3, actor="LEAVER", reason="x")
    touched = ledger.anonymize_user("LEAVER")
    assert touched >= 3
    stone = tombstone("LEAVER")
    assert stone.startswith("deleted-") and len(stone) == len("deleted-") + 6
    assert db.one("SELECT COUNT(*) AS n FROM facts WHERE user_id = 'LEAVER'")["n"] == 0
    assert db.one("SELECT counterparty FROM facts WHERE kind = 'thanks'")["counterparty"] == stone
    assert db.one("SELECT actor FROM facts WHERE kind = 'star'")["actor"] == stone


def test_tombstone_is_stable_and_distinct():
    assert tombstone("U1") == tombstone("U1")
    assert tombstone("U1") != tombstone("U2")
    assert re.fullmatch(r"deleted-[0-9a-f]{6}", tombstone("U1"))


# ---------------------------------------------------------------- idempotency key shapes and replays

def test_idempotency_key_shapes():
    assert msg_key("C1", "1.2") == "msg:C1:1.2"
    assert thanks_key("C1", "1.2", "U2") == "thanks:C1:1.2:U2"
    assert helpful_key("C1", "1.2", "U3") == "helpful:C1:1.2:U3"
    assert re.fullmatch(r"star:[0-9a-f]{32}", star_key())
    assert star_key("deadbeef") == "star:deadbeef"


def test_idempotent_replay_is_ignored_for_every_source(ledger, db):
    ts = MON + 5
    msg = msg_key("C1", "10.5")
    assert ledger.add_fact(idem_key=msg, user_id="U1", kind="msg", ts=ts, channel="C1",
                           message_ts="10.5") is not None
    assert ledger.add_fact(idem_key=msg, user_id="U1", kind="msg", ts=ts, channel="C1",
                           message_ts="10.5") is None

    thanks = thanks_key("C1", "10.5", "U2")
    assert ledger.add_fact(idem_key=thanks, user_id="U1", kind="thanks", ts=ts, counterparty="U2") is not None
    assert ledger.add_fact(idem_key=thanks, user_id="U1", kind="thanks", ts=ts, counterparty="U2") is None

    helpful = helpful_key("C1", "10.5", "U3")
    assert ledger.add_fact(idem_key=helpful, user_id="U1", kind="helpful", ts=ts, counterparty="U3") is not None
    assert ledger.add_fact(idem_key=helpful, user_id="U1", kind="helpful", ts=ts, counterparty="U3") is None

    star = star_key("abc123")
    assert ledger.add_fact(idem_key=star, user_id="U1", kind="star", ts=ts, actor="UORG1",
                           reason="x") is not None
    assert ledger.add_fact(idem_key=star, user_id="U1", kind="star", ts=ts, actor="UORG1", reason="x") is None
    assert db.one("SELECT COUNT(*) AS n FROM facts")["n"] == 4


def test_replayed_award_is_ignored_after_it_was_voided(ledger):
    key = star_key("cafe01")
    assert ledger.add_fact(idem_key=key, user_id="U1", kind="star", ts=MON + 1, actor="UORG1",
                           reason="x") is not None
    assert ledger.void_fact(idem_key=key, actor="UORG1", reason="oops")
    assert ledger.add_fact(idem_key=key, user_id="U1", kind="star", ts=MON + 2, actor="UORG1",
                           reason="x") is None


# ---------------------------------------------------------------- recompute and reconcile

def test_recompute_heals_week_id_drift_and_is_idempotent(ledger, facts, db):
    fid = facts.msg(user="U1", ts=MON + 100)
    db.execute("UPDATE facts SET week_id = '1999-01-04' WHERE id = ?", (fid,))
    assert ledger.recompute() == 1
    assert ledger.get_fact(fid)["week_id"] == WEEK
    assert ledger.recompute() == 0


def test_reconcile_finalizes_due_and_is_idempotent(ledger, facts, cfg):
    facts.msg(user="U1", ts=MON + 1000, state="pending")
    when = MON + 1000 + cfg.scoring.hold_seconds
    first = ledger.reconcile(when)
    assert first["finalized"] == ["U1"]
    assert first["recomputed"] == 0
    assert first["window_start"] == when - 48 * 3600
    assert ledger.week_breakdown("U1", WEEK).n_msgs == 1
    second = ledger.reconcile(when)
    assert second["finalized"] == []
    assert second["recomputed"] == 0


def test_reconcile_heals_week_id_drift_and_reports_no_finalize(ledger, facts, db):
    fid = facts.msg(user="U1", ts=MON + 100)
    db.execute("UPDATE facts SET week_id = 'wrong' WHERE id = ?", (fid,))
    out = ledger.reconcile(MON + 100)
    assert out["finalized"] == []
    assert out["recomputed"] == 1
    assert ledger.get_fact(fid)["week_id"] == WEEK
