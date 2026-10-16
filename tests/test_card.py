"""The context card: bands, streaks, standing, the confirmed week block, and proof that no ids or names leak."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from goose.core.card import activity_band, build_card, display_names_for_slots

UTC = dt.timezone.utc
DAY = 86400.0


def at(y, m, d, h=0, mi=0, s=0):
    return dt.datetime(y, m, d, h, mi, s, tzinfo=UTC).timestamp()


WEEK = "2025-10-06"
NEXT = "2025-10-13"
SNAP = at(2025, 10, 13, 0, 15)
THU = at(2025, 10, 16, 12)        # a Thursday in the NEXT week, the "now" for most card tests
IDS = ["UALICE1", "UBOB22", "UCAROL3", "UDAN444", "UEVE5555", "UFRANK66"]


@pytest.fixture
def ctx(cfg, clock, db, ledger, users, levels, weekly):
    # A plain namespace with the same attributes as AppContext, built from the real components.
    return SimpleNamespace(config=cfg, clock=clock, db=db, ledger=ledger, users=users, levels=levels, weekly=weekly)


def msgs(ledger, user, count, ts=at(2025, 10, 7, 9), state="final", tag="m"):
    for i in range(count):
        ledger.add_fact(idem_key="%s:%s:%d" % (tag, user, i), user_id=user, kind="msg", ts=ts + i, state=state,
                        channel="C1", message_ts="%s.%d" % (tag, i))


def join_shown(users, ts, *ids):
    for uid in ids:
        users.join(uid, ts)
        users.set_show(uid, True)


def confirm_board(ctx, ids_with_counts, now=SNAP):
    """Join, add facts in WEEK, close and confirm WEEK with one organizer."""
    for uid, count in ids_with_counts:
        join_shown(ctx.users, now - 30 * DAY, uid)
        msgs(ctx.ledger, uid, count, tag="b" + uid)
    ctx.weekly.close_week(WEEK, now)
    ctx.weekly.confirm_week(WEEK, "UORG1", now)


# ---------------------------------------------------------------- activity bands

def test_band_thresholds():
    assert activity_band(None, 15, 50, 100) is None
    assert activity_band(0, 15, 50, 100) == "quiet"
    assert activity_band(14.9, 15, 50, 100) == "quiet"
    assert activity_band(15, 15, 50, 100) == "steady"
    assert activity_band(49.9, 15, 50, 100) == "steady"
    assert activity_band(50, 15, 50, 100) == "active"
    assert activity_band(99.9, 15, 50, 100) == "active"
    assert activity_band(100, 15, 50, 100) == "very_active"


def test_band_is_none_until_the_previous_week_is_closed(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    msgs(ctx.ledger, "U1", 40)
    card = build_card(ctx, "U1", private=False, now=THU)
    assert card["you"]["activity_band"] is None
    ctx.weekly.close_week(WEEK, SNAP)
    assert build_card(ctx, "U1", private=False, now=THU)["you"]["activity_band"] == "steady"


def test_band_follows_last_week_crumbs(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "QUIET", "BUSY")
    msgs(ctx.ledger, "QUIET", 1, tag="q")
    msgs(ctx.ledger, "BUSY", 40, tag="z")
    ctx.weekly.close_week(WEEK, SNAP)
    assert build_card(ctx, "QUIET", False, THU)["you"]["activity_band"] == "quiet"
    assert build_card(ctx, "BUSY", False, THU)["you"]["activity_band"] == "steady"


def test_resting_people_have_no_band(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    msgs(ctx.ledger, "U1", 40)
    ctx.weekly.close_week(WEEK, SNAP)
    ctx.users.set_rest("U1", True)
    card = build_card(ctx, "U1", private=False, now=THU)
    assert card["you"]["activity_band"] is None
    assert card["you"]["resting"] is True


# ---------------------------------------------------------------- levels, streaks, newcomers

def test_pond_level_and_title_come_from_lifetime_crumbs(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    msgs(ctx.ledger, "U1", 40)                        # about 19 crumbs: level 2
    you = build_card(ctx, "U1", False, THU)["you"]
    assert you["pond_level"] == 2
    assert you["pond_title"] == "Duckling"


def test_level_floor_is_respected(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    ctx.users.set_level_floor("U1", 5)
    you = build_card(ctx, "U1", False, THU)["you"]
    assert you["pond_level"] == 5
    assert you["pond_title"] == "Mallard"


def test_streak_counts_consecutive_days_ending_today(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 14, 9), tag="s1")   # Tue
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 15, 9), tag="s2")   # Wed
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 16, 9), tag="s3", state="pending")   # today, still pending
    assert build_card(ctx, "U1", False, THU)["you"]["streak_days"] == 3


def test_streak_stops_at_a_gap_and_is_zero_without_today(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 13, 9), tag="g1")   # Mon, then a gap on Tue
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 15, 9), tag="g2")   # Wed
    assert build_card(ctx, "U1", False, THU)["you"]["streak_days"] == 0   # Thursday has no message yet
    msgs(ctx.ledger, "U1", 1, ts=at(2025, 10, 16, 9), tag="g3")
    assert build_card(ctx, "U1", False, THU)["you"]["streak_days"] == 2   # Wed and Thu, not Mon


def test_new_member_flag(ctx):
    join_shown(ctx.users, THU - 2 * DAY, "NEW")
    join_shown(ctx.users, THU - 40 * DAY, "OLD")
    assert build_card(ctx, "NEW", False, THU)["you"]["new_member"] is True
    assert build_card(ctx, "OLD", False, THU)["you"]["new_member"] is False


# ---------------------------------------------------------------- the confirmed week block

def test_no_confirmed_week_gives_empty_week_block(ctx):
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    card = build_card(ctx, "U1", private=True, now=THU)
    assert card["week"] == {}
    assert card["version"] == 0
    assert card["v"] == 1
    assert card["rewards_announced"] is False


def test_confirmed_top_three_fills_the_week_block(ctx):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20), ("UA3", 10)])
    card = build_card(ctx, "UA1", private=False, now=THU)
    assert card["week"] == {"id": WEEK, "state": "confirmed", "has_top3": True, "slots": ["p1", "p2", "p3"]}
    assert card["version"] == 2


def test_fewer_than_three_winners_has_no_slots(ctx):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20)])
    card = build_card(ctx, "UA1", private=False, now=THU)
    assert card["week"]["has_top3"] is False
    assert card["week"]["slots"] == []


def test_rewards_flag_follows_config(ctx, cfg):
    cfg.rewards.announced = True
    join_shown(ctx.users, THU - 30 * DAY, "U1")
    assert build_card(ctx, "U1", False, THU)["rewards_announced"] is True


# ---------------------------------------------------------------- standing

def test_standing_only_when_private(ctx):
    confirm_board(ctx, [("UA1", 60), ("UA2", 50), ("UA3", 40), ("UA4", 30), ("UA5", 20)])
    private = build_card(ctx, "UA1", private=True, now=THU)["you"]
    public = build_card(ctx, "UA1", private=False, now=THU)["you"]
    assert private["standing"] == "top3"
    assert "standing" not in public


def test_standing_bands(ctx, users):
    confirm_board(ctx, [("UA%d" % i, 60 - i * 2) for i in range(1, 13)])
    snapshot = [r["user_id"] for r in ctx.weekly.snapshot(WEEK)]
    assert len(snapshot) == 10
    assert build_card(ctx, snapshot[2], True, THU)["you"]["standing"] == "top3"
    assert build_card(ctx, snapshot[3], True, THU)["you"]["standing"] == "top10"
    assert build_card(ctx, snapshot[9], True, THU)["you"]["standing"] == "top10"
    join_shown(users, THU - 30 * DAY, "LURKER")
    assert build_card(ctx, "LURKER", True, THU)["you"]["standing"] == "other"


def test_standing_uses_the_confirmed_snapshot_not_live_facts(ctx):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20), ("UA3", 10)])
    join_shown(ctx.users, THU - 30 * DAY, "UNEW")
    msgs(ctx.ledger, "UNEW", 500, tag="live")       # a live leader who is not confirmed yet
    assert build_card(ctx, "UNEW", True, THU)["you"]["standing"] == "other"


# ---------------------------------------------------------------- privacy: no ids, no names, no others' numbers

def _all_cards(ctx, user_ids):
    cards = []
    for uid in user_ids:
        cards.append(build_card(ctx, uid, private=True, now=THU))
        cards.append(build_card(ctx, uid, private=False, now=THU))
    return cards


def test_card_contains_no_user_ids(ctx):
    confirm_board(ctx, [(uid, 30 - i * 3) for i, uid in enumerate(IDS)])
    dump = json.dumps(_all_cards(ctx, IDS))
    for uid in IDS:
        assert uid not in dump


def test_card_keys_are_an_allowlist(ctx):
    confirm_board(ctx, [(uid, 30 - i * 3) for i, uid in enumerate(IDS)])
    for card in _all_cards(ctx, IDS):
        assert set(card) == {"v", "version", "you", "week", "rewards_announced"}
        assert set(card["you"]) <= {"pond_level", "pond_title", "activity_band", "streak_days", "new_member",
                                    "resting", "standing"}
        assert set(card["week"]) <= {"id", "state", "has_top3", "slots"}


def test_private_card_has_no_numbers_about_others(ctx):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20), ("UA3", 10)])
    card = build_card(ctx, "UA3", private=True, now=THU)
    serialized = json.dumps(card)
    assert "UA1" not in serialized and "UA2" not in serialized
    assert "crumbs" not in serialized and "total" not in serialized


# ---------------------------------------------------------------- display names for slots

def test_display_names_map_top_three_who_chose_show(ctx, users):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20), ("UA3", 10)])
    users.set_show("UA2", False)
    names = display_names_for_slots(ctx, None)
    assert names == {"top1": "<@UA1>", "top3": "<@UA3>"}


def test_display_names_accepts_rows_or_ids(ctx):
    confirm_board(ctx, [("UA1", 30), ("UA2", 20), ("UA3", 10)])
    rows = ctx.weekly.snapshot(WEEK)
    assert display_names_for_slots(ctx, rows) == {"top1": "<@UA1>", "top2": "<@UA2>", "top3": "<@UA3>"}
    assert display_names_for_slots(ctx, ["UA3"]) == {"top1": "<@UA3>"}


def test_display_names_empty_without_confirmed_week(ctx):
    assert display_names_for_slots(ctx, None) == {}
