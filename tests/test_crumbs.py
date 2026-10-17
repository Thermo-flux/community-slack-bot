"""
Unit tests for crumbs.py - the deterministic Pond Level progression system.
"""

import math
import os
import tempfile
import time
import pytest
import database
import crumbs


@pytest.fixture
def temp_db():
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_file.close()
    db_path = temp_file.name
    database.init_db(db_path)
    yield db_path
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


def test_thresholds_match_the_spec():
    assert [crumbs.crumbs_to_reach(n) for n in range(1, 13)] == [
        0, 10, 25, 50, 100, 155, 235, 330, 445, 580, 735, 890
    ]
    # +20% per level above 12, rounded to the nearest 5
    assert crumbs.crumbs_to_reach(13) == 1070
    assert crumbs.crumbs_to_reach(14) == 1280
    assert crumbs.crumbs_to_reach(15) == 1540
    assert crumbs.crumbs_to_reach(16) == 1845


def test_titles():
    assert crumbs.title_for_level(1) == "Hatchling"
    assert crumbs.title_for_level(5) == "Mallard"
    assert crumbs.title_for_level(12) == "Head Duck"
    assert crumbs.title_for_level(13) is None


def test_star_gate_caps_at_level_5_without_a_star():
    assert crumbs.level_for_crumbs(0, has_star=False) == 1
    assert crumbs.level_for_crumbs(100, has_star=False) == 5
    # Huge crumb total but no star still caps at PL.5
    assert crumbs.level_for_crumbs(5000, has_star=False) == 5
    # With a star, higher levels unlock
    assert crumbs.level_for_crumbs(155, has_star=True) == 6
    assert crumbs.level_for_crumbs(890, has_star=True) == 12
    assert crumbs.level_for_crumbs(1070, has_star=True) == 13


def test_weekly_curves():
    assert crumbs.weekly_crumbs(messages=0) == 0.0
    expected = 8 * math.log(1 + 5 / 5.0)
    assert abs(crumbs.weekly_crumbs(messages=5) - expected) < 1e-9
    # active-day cap is 10.5 per week
    assert abs(crumbs.weekly_crumbs(active_days=20) - 10.5) < 1e-9
    assert abs(crumbs.weekly_crumbs(stars=2) - 50.0) < 1e-9
    assert abs(crumbs.weekly_crumbs(bigstars=1) - 60.0) < 1e-9


def test_qualification_rules():
    assert crumbs.is_qualified_message("hey team I finished the sensor build today")
    assert not crumbs.is_qualified_message("hi")            # too short / too few words
    assert not crumbs.is_qualified_message("ok thanks :duck:")  # stripped to < 3 words
    assert not crumbs.is_qualified_message("<@U123> https://x.com :sparkles:")  # no real words


def test_default_star_gate_and_opt_in_flow(temp_db):
    uid = "U_ALICE"
    status = crumbs.get_pond_status(uid, db_path=temp_db)
    assert status["level"] == 1
    assert status["crumbs"] == 0
    assert status["opted_in"] is False

    crumbs.set_opted_in(uid, db_path=temp_db)
    assert crumbs.is_opted_in(uid, db_path=temp_db)
    assert crumbs.get_pond_status(uid, db_path=temp_db)["opted_in"] is True


def test_star_revocation_demotes_and_floors_at_five(temp_db):
    uid = "U_BOB"
    crumbs.set_opted_in(uid, db_path=temp_db)
    # Plenty of crumbs from messages across several weeks/days, above the PL.5 threshold
    now = time.time()
    for wk in range(8):
        base = now - wk * 7 * 86400
        for d in range(5):
            for _ in range(4):
                crumbs.add_ledger_event(uid, "message", source_ts=base - d * 86400, db_path=temp_db)
    # Without a star, still capped at PL.5
    assert crumbs.get_pond_status(uid, db_path=temp_db)["level"] == 5

    star_id = crumbs.add_ledger_event(uid, "star", amount=crumbs.STAR_CRUMBS, db_path=temp_db)
    with_star = crumbs.get_pond_status(uid, db_path=temp_db)
    assert with_star["has_star"] is True
    assert with_star["level"] >= 6

    # Revoke the star -> recomputed level drops back to the PL.5 cap
    crumbs.void_ledger_event(star_id, "test revoke", db_path=temp_db)
    after = crumbs.get_pond_status(uid, db_path=temp_db)
    assert after["has_star"] is False
    assert after["level"] == 5


def test_level_floor_prevents_demotion(temp_db):
    uid = "U_CAROL"
    crumbs.set_opted_in(uid, db_path=temp_db)
    crumbs.set_level_floor(uid, 8, db_path=temp_db)
    status = crumbs.get_pond_status(uid, db_path=temp_db)
    assert status["level_floor"] == 8
    assert status["level"] == 8  # floor keeps them at 8 despite 0 crumbs


def test_record_qualified_message_updates_and_flood_limits(temp_db):
    uid = "U_DAVE"
    crumbs.set_opted_in(uid, db_path=temp_db)
    crumbs.record_qualified_message(
        uid, "C_LOADOUT", "I shipped the flash wear leveling fix today", "1.0", db_path=temp_db
    )
    assert crumbs.get_pond_status(uid, db_path=temp_db)["crumbs"] > 0

    # Immediately again -> flood limited, no new event
    before = crumbs.get_pond_status(uid, db_path=temp_db)["crumbs"]
    again = crumbs.record_qualified_message(
        uid, "C_LOADOUT", "another totally different message here", "2.0", db_path=temp_db
    )
    assert again is None
    assert crumbs.get_pond_status(uid, db_path=temp_db)["crumbs"] == before
