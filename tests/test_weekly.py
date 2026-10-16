"""Weekly and all-time results: eligibility, ranking and tie-breaks, closing, confirmations, disqualification,
skips, all-time and final rules."""
from __future__ import annotations

import datetime as dt

import pytest

UTC = dt.timezone.utc
DAY = 86400.0


def at(y, m, d, h=0, mi=0, s=0):
    return dt.datetime(y, m, d, h, mi, s, tzinfo=UTC).timestamp()


WEEK = "2025-10-06"          # Monday 2025-10-06 .. Sunday 2025-10-12
NEXT = "2025-10-13"
SNAP = at(2025, 10, 13, 0, 15)   # Monday 00:15, when the week is snapshotted
START_DAY = at(2025, 10, 7, 9)


class Feed:
    """Adds facts with unique idem keys."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.n = 0

    def _key(self):
        self.n += 1
        return "w%06d" % self.n

    def msgs(self, user, count, ts=START_DAY, state="final"):
        for i in range(count):
            self.ledger.add_fact(idem_key=self._key(), user_id=user, kind="msg", ts=ts + i, state=state,
                                 channel="C1", message_ts="%d.%d" % (self.n, i))

    def thanks(self, user, giver, ts=START_DAY, state="final"):
        self.ledger.add_fact(idem_key=self._key(), user_id=user, kind="thanks", counterparty=giver, ts=ts,
                             state=state)

    def star(self, user, amount=None, kind="star", ts=START_DAY, state="final"):
        self.ledger.add_fact(idem_key=self._key(), user_id=user, kind=kind, ts=ts, state=state,
                             actor="UORG1", reason="reason", amount=amount)


def join_shown(users, clock, *ids, ts=None):
    for uid in ids:
        users.join(uid, clock.now() if ts is None else ts)
        users.set_show(uid, True)


@pytest.fixture
def feed(ledger):
    return Feed(ledger)


def ids(rows):
    return [r.user_id for r in rows]


# ---------------------------------------------------------------- eligibility

def test_eligibility_rules(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    users.join("HIDDEN", clock.now())                 # show stays off
    join_shown(users, clock, "UORG1")                  # organizers never rank
    join_shown(users, clock, "UOWNER")
    now = clock.now()
    assert weekly.eligible("U1", WEEK, now)
    assert not weekly.eligible("HIDDEN", WEEK, now)
    assert not weekly.eligible("UORG1", WEEK, now)
    assert not weekly.eligible("UOWNER", WEEK, now)
    assert not weekly.eligible("NOBODY", WEEK, now)


def test_freeze_blocks_only_while_frozen(weekly, users, clock):
    join_shown(users, clock, "U1")
    now = clock.now()
    users.freeze("U1", now + 600, "reason", "UORG1", now)
    assert not weekly.eligible("U1", WEEK, now + 1)
    assert weekly.eligible("U1", WEEK, now + 601)


def test_week_disqualification_is_per_week_and_all_is_everywhere(weekly, users, clock):
    join_shown(users, clock, "U1", "U2")
    now = clock.now()
    users.add_flag("U1", "disqualify", WEEK, "spam", "UORG1", now)
    assert not weekly.eligible("U1", WEEK, now)
    assert weekly.eligible("U1", NEXT, now)
    users.add_flag("U2", "disqualify", "all", "spam", "UORG1", now)
    assert not weekly.eligible("U2", WEEK, now)
    assert not weekly.eligible("U2", NEXT, now)


def test_note_flags_do_not_block(weekly, users, clock):
    join_shown(users, clock, "U1")
    users.add_flag("U1", "note", "", "watching", "UORG1", clock.now())
    assert weekly.eligible("U1", WEEK, clock.now())


# ---------------------------------------------------------------- ranking

def test_ranking_sorts_by_total_and_skips_zero(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2", "U3")
    feed.msgs("U1", 40)
    feed.msgs("U2", 5)
    rows = weekly.ranking(WEEK, now=SNAP)
    assert ids(rows) == ["U1", "U2"]                 # U3 has no facts, so total 0 is skipped
    assert rows[0].total > rows[1].total > 0


def test_ranking_limit_and_row_fields(weekly, users, feed, clock):
    names = ["U%02d" % i for i in range(12)]
    join_shown(users, clock, *names)
    for i, name in enumerate(names):
        feed.msgs(name, 2 + i)
    feed.thanks("U11", "U00")
    rows = weekly.ranking(WEEK, limit=10, now=SNAP)
    assert len(rows) == 10
    top = rows[0]
    assert top.user_id == "U11"
    assert top.recognizers == 1
    assert top.breakdown.n_msgs == 13
    assert top.joined_ts == clock.now()
    assert top.stars == []
    assert top.flags == []


def test_ranking_pending_only_when_asked(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 5, state="pending")
    assert weekly.ranking(WEEK, now=SNAP) == []
    assert ids(weekly.ranking(WEEK, include_pending=True, now=SNAP)) == ["U1"]


def test_tie_break_more_stars_first(weekly, users, feed, clock):
    join_shown(users, clock, "UA", "UB")
    feed.msgs("UA", 2)
    feed.msgs("UB", 2)
    feed.star("UB", amount=0.0)   # adds a star but no points, so totals stay equal
    rows = weekly.ranking(WEEK, now=SNAP)
    assert rows[0].total == pytest.approx(rows[1].total)
    assert ids(rows) == ["UB", "UA"]


def test_tie_break_more_recognizers_next(weekly, users, feed, clock):
    join_shown(users, clock, "UA", "UB")
    feed.thanks("UA", "U9")
    feed.thanks("UA", "U9")
    feed.thanks("UB", "U9")
    feed.thanks("UB", "U8")
    rows = weekly.ranking(WEEK, now=SNAP)
    assert rows[0].total == pytest.approx(rows[1].total)
    assert ids(rows) == ["UB", "UA"]


def test_tie_break_earlier_first_fact_next(weekly, users, feed, clock):
    join_shown(users, clock, "UA", "UB")
    feed.msgs("UB", 2, ts=at(2025, 10, 7, 9, 30))
    feed.msgs("UA", 2, ts=at(2025, 10, 7, 9))
    assert ids(weekly.ranking(WEEK, now=SNAP)) == ["UA", "UB"]


def test_tie_break_user_id_last(weekly, users, feed, clock):
    join_shown(users, clock, "UB1", "UA1")   # joined in the other order on purpose
    feed.msgs("UB1", 2)
    feed.msgs("UA1", 2)
    assert ids(weekly.ranking(WEEK, now=SNAP)) == ["UA1", "UB1"]


def test_organizers_and_hidden_never_rank(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "UORG1")
    users.join("HIDDEN", clock.now())
    feed.msgs("UORG1", 300)
    feed.msgs("HIDDEN", 300)
    feed.msgs("U1", 3)
    assert ids(weekly.ranking(WEEK, now=SNAP)) == ["U1"]


def test_week_boundary_in_ranking(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 2, ts=at(2025, 10, 12, 23, 59))   # Sunday night, week 1
    feed.msgs("U1", 2, ts=at(2025, 10, 13, 0, 0))     # Monday, week 2
    assert weekly.ranking(WEEK, now=SNAP)[0].breakdown.n_msgs == 2
    assert weekly.ranking(NEXT, now=SNAP + 7 * DAY)[0].breakdown.n_msgs == 2


# ---------------------------------------------------------------- closing

def test_close_week_stores_snapshot_and_finalizes(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2")
    feed.msgs("U1", 10)
    feed.msgs("U2", 3, state="pending")
    status = weekly.close_week(WEEK, SNAP)
    assert status["state"] == "provisional"
    assert status["version"] == 1
    assert [r["user_id"] for r in status["snapshot"]] == ["U1", "U2"]
    row = status["snapshot"][0]
    assert {"user_id", "total", "breakdown", "stars", "recognizers", "joined_ts", "flags"} <= set(row)
    assert row["breakdown"]["n_msgs"] == 10
    assert weekly.snapshot(WEEK) == status["snapshot"]
    # the pending facts of the week were finalized by the close, even though hold has not passed
    assert [r["user_id"] for r in weekly.snapshot(WEEK)][1] == "U2"
    assert weekly.ranking(WEEK, now=SNAP)[1].breakdown.n_msgs == 3


def test_close_week_is_idempotent(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 4)
    first = weekly.close_week(WEEK, SNAP)
    feed.msgs("U1", 50)                       # a late fact must not change the stored snapshot
    second = weekly.close_week(WEEK, SNAP + 3600)
    assert second == first
    assert second["snapshot"][0]["breakdown"]["n_msgs"] == 4


def test_snapshot_keeps_only_top_ten(weekly, users, feed, clock):
    names = ["U%02d" % i for i in range(13)]
    join_shown(users, clock, *names)
    for i, name in enumerate(names):
        feed.msgs(name, i + 1)
    assert len(weekly.close_week(WEEK, SNAP)["snapshot"]) == 10


def test_status_and_snapshot_of_unknown_week(weekly):
    assert weekly.status(WEEK) is None
    assert weekly.snapshot(WEEK) == []


def test_closable_weeks(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    assert weekly.closable_weeks(SNAP - 1) == []
    assert weekly.closable_weeks(SNAP) == [WEEK]
    weekly.close_week(WEEK, SNAP)
    assert weekly.closable_weeks(SNAP + 7 * DAY) == []   # the next week has no facts and no members


def test_members_without_facts_make_their_week_closable(weekly, users, clock):
    users.join("QUIET", at(2025, 10, 7, 12))
    assert weekly.closable_weeks(SNAP) == [WEEK]


# ---------------------------------------------------------------- confirmations

def test_confirm_with_one_organizer(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2", "U3", "U4")
    for i, name in enumerate(["U1", "U2", "U3", "U4"]):
        feed.msgs(name, 20 - i * 4)
    weekly.close_week(WEEK, SNAP)
    result, info = weekly.confirm_week(WEEK, "UORG1", SNAP + 100)
    assert result == "confirmed"
    assert info["top"] == ["U1", "U2", "U3"]
    status = weekly.status(WEEK)
    assert status["state"] == "confirmed"
    assert status["version"] == 2
    assert status["confirmed_ts"] == SNAP + 100
    podium = weekly.winners()["weeks"][0]
    assert podium["week_id"] == WEEK
    assert [r["user_id"] for r in podium["top3"]] == ["U1", "U2", "U3"]
    assert [r["rank"] for r in podium["top3"]] == [1, 2, 3]
    assert weekly.last_confirmed()["top"] == ["U1", "U2", "U3"]
    assert weekly.last_confirmed()["version"] == 2


def test_confirm_again_is_already(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    weekly.confirm_week(WEEK, "UORG1", SNAP)
    result, info = weekly.confirm_week(WEEK, "UORG2", SNAP)
    assert result == "already"
    assert info["top"] == ["U1"]
    assert weekly.status(WEEK)["version"] == 2


def test_two_confirmations_required(weekly, users, feed, clock, cfg):
    cfg.people.confirmations_required = 2
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    assert weekly.confirm_week(WEEK, "UORG1", SNAP) == ("need_more", {"week_id": WEEK, "have": 1, "need": 2})
    result, info = weekly.confirm_week(WEEK, "UORG1", SNAP)
    assert result == "error" and info["reason"] == "already_confirmed"
    assert weekly.status(WEEK)["state"] == "provisional"
    assert weekly.status(WEEK)["confirmers"] == ["UORG1"]
    result, _ = weekly.confirm_week(WEEK, "UORG2", SNAP + 1)
    assert result == "confirmed"
    assert weekly.status(WEEK)["confirmers"] == ["UORG1", "UORG2"]


def test_confirm_errors(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    assert weekly.confirm_week(WEEK, "UORG1", SNAP) == ("error", {"reason": "unknown_week"})
    weekly.close_week(WEEK, SNAP)
    assert weekly.confirm_week(WEEK, "U1", SNAP) == ("error", {"reason": "not_organizer"})


def test_confirm_skipped_week_is_refused(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    assert weekly.skip_week(WEEK, "quiet week", "UORG1", SNAP)
    assert weekly.confirm_week(WEEK, "UORG1", SNAP) == ("error", {"reason": "skipped"})


def test_last_confirmed_none_until_confirmed(weekly, users, feed, clock):
    assert weekly.last_confirmed() is None
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    assert weekly.last_confirmed() is None
    weekly.confirm_week(WEEK, "UORG1", SNAP)
    info = weekly.last_confirmed()
    assert info["week_id"] == WEEK
    assert [r["user_id"] for r in info["rows"]] == ["U1"]


def test_winners_with_fewer_than_three(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    weekly.confirm_week(WEEK, "UORG1", SNAP)
    assert [r["user_id"] for r in weekly.winners()["weeks"][0]["top3"]] == ["U1"]


# ---------------------------------------------------------------- disqualify

def test_disqualify_provisional_removes_from_snapshot(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2", "U3")
    feed.msgs("U1", 30)
    feed.msgs("U2", 20)
    feed.msgs("U3", 10)
    weekly.close_week(WEEK, SNAP)
    assert weekly.disqualify(WEEK, "U1", "spam", "UORG1", SNAP + 1)
    status = weekly.status(WEEK)
    assert [r["user_id"] for r in status["snapshot"]] == ["U2", "U3"]
    assert status["state"] == "provisional"
    assert status["version"] == 1
    assert ids(weekly.ranking(WEEK, now=SNAP)) == ["U2", "U3"]


def test_disqualify_pulls_in_the_next_person_when_truncated(weekly, users, feed, clock):
    names = ["U%02d" % i for i in range(11)]
    join_shown(users, clock, *names)
    for i, name in enumerate(names):
        feed.msgs(name, 60 - i * 3)
    weekly.close_week(WEEK, SNAP)
    assert len(weekly.snapshot(WEEK)) == 10
    assert "U10" not in [r["user_id"] for r in weekly.snapshot(WEEK)]
    weekly.disqualify(WEEK, "U00", "spam", "UORG1", SNAP + 1)
    after = [r["user_id"] for r in weekly.snapshot(WEEK)]
    assert len(after) == 10
    assert "U00" not in after
    assert "U10" in after


def test_disqualify_confirmed_week_rewrites_results(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2", "U3", "U4")
    for i, name in enumerate(["U1", "U2", "U3", "U4"]):
        feed.msgs(name, 40 - i * 5)
    weekly.close_week(WEEK, SNAP)
    weekly.confirm_week(WEEK, "UORG1", SNAP)
    assert [r["user_id"] for r in weekly.winners()["weeks"][0]["top3"]] == ["U1", "U2", "U3"]
    weekly.disqualify(WEEK, "U1", "spam", "UORG1", SNAP + 5)
    podium = weekly.winners()["weeks"][0]["top3"]
    assert [r["user_id"] for r in podium] == ["U2", "U3", "U4"]
    assert weekly.status(WEEK)["version"] == 3
    assert weekly.last_confirmed()["top"] == ["U2", "U3", "U4"]


def test_disqualify_person_outside_the_board_changes_nothing(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2", "QUIET")      # QUIET has no facts, so is not on the board
    feed.msgs("U1", 10)
    feed.msgs("U2", 2)
    weekly.close_week(WEEK, SNAP)
    before = weekly.snapshot(WEEK)
    assert weekly.disqualify(WEEK, "QUIET", "x", "UORG1", SNAP)
    after = weekly.status(WEEK)
    assert after["snapshot"] == before
    assert after["version"] == 1
    assert "QUIET" not in [r["user_id"] for r in before]


def test_disqualify_all_only_flags(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2")
    feed.msgs("U1", 10)
    feed.msgs("U2", 5)
    weekly.close_week(WEEK, SNAP)
    before = weekly.snapshot(WEEK)
    assert weekly.disqualify("all", "U1", "spam", "UORG1", SNAP)
    assert weekly.snapshot(WEEK) == before
    assert "U1" not in ids(weekly.alltime_ranking(now=SNAP))


def test_disqualify_refusals(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    assert weekly.disqualify(WEEK, "U1", "   ", "UORG1", SNAP) is False
    assert weekly.disqualify(WEEK, "NOTJOINED", "reason", "UORG1", SNAP) is False
    with pytest.raises(ValueError):
        weekly.disqualify("not-a-week", "U1", "reason", "UORG1", SNAP)


def test_disqualify_before_close_is_a_flag_only(weekly, users, feed, clock):
    join_shown(users, clock, "U1", "U2")
    feed.msgs("U1", 10)
    feed.msgs("U2", 5)
    assert weekly.disqualify(WEEK, "U1", "spam", "UORG1", SNAP)
    assert weekly.status(WEEK) is None
    assert [r["user_id"] for r in weekly.close_week(WEEK, SNAP)["snapshot"]] == ["U2"]


# ---------------------------------------------------------------- skips and marks

def test_skip_week_rules(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    assert weekly.skip_week(WEEK, "", "UORG1", SNAP) is False
    assert weekly.skip_week(WEEK, "holiday", "UORG1", SNAP) is True
    status = weekly.status(WEEK)
    assert status["state"] == "skipped"
    assert status["skipped_reason"] == "holiday"
    assert weekly.skip_week(WEEK, "again", "UORG1", SNAP) is False


def test_skip_refused_for_confirmed_week(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    weekly.confirm_week(WEEK, "UORG1", SNAP)
    assert weekly.skip_week(WEEK, "too late", "UORG1", SNAP) is False


def test_mark_flags_persist(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    weekly.mark_organizer_posted(WEEK)
    weekly.mark_reminded(WEEK)
    weekly.mark_announced(WEEK)
    status = weekly.status(WEEK)
    assert (status["organizer_posted"], status["reminded"], status["announced"]) == (True, True, True)


def test_closable_weeks_excludes_closed(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    weekly.close_week(WEEK, SNAP)
    assert WEEK not in weekly.closable_weeks(SNAP + 30 * DAY)


# ---------------------------------------------------------------- all-time

def test_alltime_uses_lifetime_totals(weekly, users, feed, clock, ledger):
    join_shown(users, clock, "U1", "U2")
    feed.msgs("U1", 30)
    feed.msgs("U2", 10)
    feed.msgs("U2", 25, ts=at(2025, 10, 14, 9))   # next week
    rows = weekly.alltime_ranking(now=SNAP + 30 * DAY)
    expected = sorted(["U1", "U2"], key=lambda u: -ledger.lifetime(u))
    assert ids(rows) == expected
    assert rows[0].total == pytest.approx(ledger.lifetime(rows[0].user_id))


def test_alltime_ignores_week_disqualifications(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 10)
    weekly.disqualify(WEEK, "U1", "spam", "UORG1", clock.now())
    assert ids(weekly.alltime_ranking(now=clock.now())) == ["U1"]


def test_alltime_limit(weekly, users, feed, clock):
    names = ["U%02d" % i for i in range(12)]
    join_shown(users, clock, *names)
    for i, name in enumerate(names):
        feed.msgs(name, i + 1)
    assert len(weekly.alltime_ranking(limit=10, now=clock.now())) == 10


# ---------------------------------------------------------------- final

def test_final_rules_and_confirmation(weekly, users, feed, clock, cfg, ledger):
    cfg.schedule.end_date = "2026-01-05"
    early = at(2025, 10, 9)
    late = at(2025, 12, 30)
    join_shown(users, clock, "U1", "U2", "U4", ts=early)
    join_shown(users, clock, "U3", ts=late)
    for uid, stars in (("U1", 3), ("U2", 2), ("U3", 5), ("U4", 4)):
        feed.msgs(uid, 10)
        for _ in range(stars):
            feed.star(uid)
    now = at(2026, 1, 6)
    # U2 has too few stars, U3 joined too late; U4 has more stars than U1 so ranks first
    assert ids(weekly.final_ranking(now)) == ["U4", "U1"]

    assert weekly.confirm_final("UORG1", now) == ("error", {"reason": "not_owner"})
    result, info = weekly.confirm_final("UOWNER", now)
    assert result == "confirmed"
    assert {r["user_id"] for r in info["top"]} == {"U1", "U4"}
    assert weekly.confirm_final("UOWNER", now)[0] == "already"
    assert {r["user_id"] for r in weekly.winners()["final"]} == {"U1", "U4"}


def test_final_without_candidates(weekly, users, clock):
    assert weekly.confirm_final("UOWNER", clock.now()) == ("error", {"reason": "no_candidates"})


# ---------------------------------------------------------------- program dates

def test_end_ts_sources(weekly, cfg, db, clock):
    assert weekly.end_ts() is None
    cfg.schedule.launch_date = "2025-10-06"
    assert weekly.end_ts() == clock.date_to_ts("2025-10-06") + 13 * 7 * DAY
    cfg.schedule.end_date = "2026-01-05"
    assert weekly.end_ts() == clock.date_to_ts("2026-01-05")
    weekly.set_end("2026-02-01", "UOWNER")
    assert weekly.end_ts() == clock.date_to_ts("2026-02-01")
    assert db.get_setting("end_date_actor") == "UOWNER"


def test_set_end_rejects_bad_dates(weekly):
    with pytest.raises(ValueError):
        weekly.set_end("next tuesday", "UOWNER")


def test_skip_refuses_a_week_whose_end_is_still_in_the_future(weekly, users, feed, clock):
    join_shown(users, clock, "U1")
    feed.msgs("U1", 3)
    during = at(2025, 10, 9, 12)                             # Thursday, inside the week 2025-10-06
    assert weekly.skip_week(WEEK, "holiday", "UORG1", during) is False
    assert weekly.status(WEEK) is None
    assert weekly.skip_week(WEEK, "holiday", "UORG1", SNAP) is True   # after Monday 00:00 the week has ended
