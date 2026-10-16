"""Members: join, leave (with anonymization), settings, freezes and flags."""
from __future__ import annotations

import pytest

from goose.core.ledger import tombstone

DAY = 86400.0


def add_msg(ledger, user, ts, key):
    return ledger.add_fact(idem_key=key, user_id=user, kind="msg", ts=ts, state="final", channel="C1",
                           message_ts="%s.1" % key)


def test_join_once_and_write_consent(users, db, clock):
    assert users.join("U1", clock.now(), channel="C1") is True
    assert users.join("U1", clock.now()) is False
    assert users.is_joined("U1")
    assert db.one("SELECT COUNT(*) AS n FROM consents WHERE user_id = 'U1'")["n"] == 1
    assert users.get("U1")["last_channel"] == "C1"


def test_join_needs_an_id(users, clock):
    with pytest.raises(ValueError):
        users.join("", clock.now())


def test_defaults_after_join(users, clock):
    users.join("U1", clock.now())
    row = users.get("U1")
    assert row["show"] is False          # hidden until the person chooses show
    assert row["rest"] is False
    assert row["quiet"] is False
    assert row["level_floor"] == 1
    assert row["frozen_until"] == 0


def test_unknown_person(users):
    assert users.get("UX") is None
    assert users.is_joined("UX") is False
    assert users.is_frozen("UX", 0) is False
    assert users.is_newcomer("UX", 0) is False


def test_settings_toggle(users, clock):
    users.join("U1", clock.now())
    users.set_show("U1", True)
    users.set_rest("U1", True)
    users.set_quiet("U1", True)
    users.set_level_floor("U1", 4)
    users.set_last_channel("U1", "C9")
    users.set_announced_level("U1", 3)
    row = users.get("U1")
    assert (row["show"], row["rest"], row["quiet"]) == (True, True, True)
    assert row["level_floor"] == 4
    assert row["last_channel"] == "C9"
    assert row["announced_level"] == 3
    users.set_show("U1", False)
    assert users.get("U1")["show"] is False


def test_level_floor_never_below_one(users, clock):
    users.join("U1", clock.now())
    users.set_level_floor("U1", -3)
    assert users.get("U1")["level_floor"] == 1


def test_roster_lists_members_with_open_flag_counts(users, clock):
    users.join("U2", clock.now())
    users.join("U1", clock.now() + 5)
    users.set_show("U1", True)
    users.add_flag("U1", "note", "", "check", "UORG1", clock.now())
    roster = users.roster()
    assert [r["user_id"] for r in roster] == ["U2", "U1"]
    by_id = {r["user_id"]: r for r in roster}
    assert by_id["U1"]["show"] is True
    assert by_id["U1"]["open_flags"] == 1
    assert by_id["U2"]["open_flags"] == 0
    assert set(by_id["U1"]) == {"user_id", "joined_ts", "show", "rest", "quiet", "frozen_until", "open_flags"}


def test_organizers_and_owner_come_from_config(users):
    assert users.is_organizer("UORG1")
    assert users.is_organizer("UOWNER")       # the owner also counts as an organizer
    assert users.is_owner("UOWNER")
    assert not users.is_owner("UORG1")
    assert not users.is_organizer("U1")


def test_newcomer_window(users, cfg, clock):
    start = clock.now()
    users.join("U1", start)
    assert users.is_newcomer("U1", start + DAY)
    assert users.is_newcomer("U1", start + cfg.vibes.newcomer_days * DAY - 1)
    assert not users.is_newcomer("U1", start + cfg.vibes.newcomer_days * DAY)


# ---------------------------------------------------------------- leave

def test_leave_not_joined_is_false(users, clock):
    assert users.leave("U1", clock.now()) is False


def test_leave_refused_while_a_flag_is_open(users, clock):
    users.join("U1", clock.now())
    flag = users.add_flag("U1", "note", "", "check", "UORG1", clock.now())
    assert users.leave("U1", clock.now()) is False
    assert users.is_joined("U1")
    assert users.close_flag(flag, clock.now())
    assert users.leave("U1", clock.now()) is True


def test_leave_deletes_person_and_anonymizes_references(users, ledger, db, clock):
    users.join("LEAVER", clock.now())
    users.join("OTHER", clock.now())
    now = clock.now()
    add_msg(ledger, "LEAVER", now, "a1")
    ledger.add_fact(idem_key="t1", user_id="OTHER", kind="thanks", ts=now, counterparty="LEAVER")
    ledger.add_fact(idem_key="t2", user_id="LEAVER", kind="thanks", ts=now, counterparty="OTHER")
    ledger.add_fact(idem_key="s1", user_id="OTHER", kind="star", ts=now, actor="LEAVER", reason="x")
    db.execute("INSERT INTO gloss_seen (user_id, de_id) VALUES ('LEAVER', 'ente_gut')")
    db.execute("INSERT INTO voice_person (user_id, ts) VALUES ('LEAVER', ?)", (now,))
    db.execute("INSERT INTO remark_log (user_id, ts, kind) VALUES ('LEAVER', ?, 'rank')", (now,))
    db.execute("INSERT INTO reaction_log (user_id, ts, kind) VALUES ('LEAVER', ?, 'duck')", (now,))
    db.execute("INSERT INTO seen_hash (user_id, hash, ts) VALUES ('LEAVER', 'h', ?)", (now,))

    assert users.leave("LEAVER", now + 1) is True

    assert users.get("LEAVER") is None
    assert db.one("SELECT COUNT(*) AS n FROM facts WHERE user_id = 'LEAVER'")["n"] == 0
    stone = tombstone("LEAVER")
    assert db.one("SELECT counterparty FROM facts WHERE idem_key = 't1'")["counterparty"] == stone
    assert db.one("SELECT actor FROM facts WHERE idem_key = 's1'")["actor"] == stone
    assert users.get("OTHER") is not None
    for table in ("gloss_seen", "voice_person", "remark_log", "reaction_log", "consents", "seen_hash"):
        assert db.one("SELECT COUNT(*) AS n FROM %s WHERE user_id = 'LEAVER'" % table)["n"] == 0


def test_leave_leaves_no_trace_of_the_id_in_any_table(users, ledger, db, clock):
    """Scan every text column after leave: the old user id must not appear anywhere."""
    users.join("GHOSTUSER", clock.now())
    users.join("OTHER", clock.now())
    now = clock.now()
    add_msg(ledger, "GHOSTUSER", now, "g1")
    ledger.add_fact(idem_key="g2", user_id="OTHER", kind="helpful", ts=now, counterparty="GHOSTUSER")
    users.leave("GHOSTUSER", now + 1)
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for row in db.query("SELECT * FROM %s" % table):
            for value in dict(row).values():
                assert value != "GHOSTUSER", table
                if isinstance(value, str):
                    assert "GHOSTUSER" not in value, table


def test_leave_tombstones_closed_flags(users, db, clock):
    users.join("U1", clock.now())
    flag = users.add_flag("U1", "disqualify", "2025-10-06", "reason", "UORG1", clock.now())
    users.close_flag(flag, clock.now())
    assert users.leave("U1", clock.now())
    row = db.one("SELECT user_id FROM flags WHERE id = ?", (flag,))
    assert row["user_id"] == tombstone("U1")


def test_leave_without_ledger_still_deletes_own_facts(db, cfg, clock):
    from goose.core.ledger import Ledger
    from goose.core.users import Users

    ledger = Ledger(db, cfg, clock)
    bare = Users(db, cfg, clock)  # no ledger passed
    bare.join("U1", clock.now())
    add_msg(ledger, "U1", clock.now(), "b1")
    assert bare.leave("U1", clock.now())
    assert db.one("SELECT COUNT(*) AS n FROM facts WHERE user_id = 'U1'")["n"] == 0


# ---------------------------------------------------------------- freezes and flags

def test_freeze_and_unfreeze(users, clock):
    users.join("U1", clock.now())
    now = clock.now()
    users.freeze("U1", now + 3600, "too many links", "UORG1", now)
    assert users.is_frozen("U1", now + 10)
    assert not users.is_frozen("U1", now + 3600)
    assert [f["kind"] for f in users.open_flags("U1")] == ["freeze"]
    assert users.unfreeze("U1", "UORG1", now + 20) is True
    assert not users.is_frozen("U1", now + 30)
    assert users.open_flags("U1") == []
    assert users.unfreeze("U1", "UORG1", now + 40) is False


def test_freeze_twice_keeps_one_open_freeze_flag(users, clock):
    users.join("U1", clock.now())
    now = clock.now()
    users.freeze("U1", now + 100, "first", "UORG1", now)
    users.freeze("U1", now + 200, "second", "UORG1", now + 1)
    flags = users.open_flags("U1")
    assert len(flags) == 1
    assert flags[0]["reason"] == "second"


def test_freeze_needs_a_member(users, clock):
    with pytest.raises(ValueError):
        users.freeze("UNKNOWN", clock.now() + 60, "x", "UORG1", clock.now())


def test_flags_validate_kind_and_close_once(users, clock):
    users.join("U1", clock.now())
    with pytest.raises(ValueError):
        users.add_flag("U1", "ban", "", "x", "UORG1", clock.now())
    flag = users.add_flag("U1", "note", "", "watch", "UORG1", clock.now())
    assert users.close_flag(flag, clock.now()) is True
    assert users.close_flag(flag, clock.now()) is False
    assert users.open_flags("U1") == []


def test_is_frozen_reads_frozen_until_only(users, clock):
    users.join("U1", clock.now())
    users.freeze("U1", clock.now() + DAY, "x", "UORG1", clock.now())
    assert users.is_frozen("U1", clock.now())


def test_leave_scrubs_published_results_and_snapshots(db, users, ledger, weekly, clock):
    """A departed member's id must not survive in stored snapshots, confirmers or results."""
    import json
    users.join("UGONE", clock.now())
    users.set_show("UGONE", True)
    ledger.add_fact(idem_key="s1", user_id="UGONE", kind="msg", ts=clock.now(), state="final")
    week = clock.week_id(clock.now())
    db.execute("INSERT INTO week_status(week_id, state, snapshot_json, confirmers_json) VALUES(?,?,?,?)",
               (week, "confirmed", json.dumps([{"user_id": "UGONE", "total": 5.0}]), json.dumps(["UGONE"])))
    db.execute("INSERT INTO results(scope, week_id, rank, user_id, total) VALUES('week', ?, 1, 'UGONE', 5.0)", (week,))
    assert users.leave("UGONE", clock.now())
    row = db.one("SELECT snapshot_json, confirmers_json FROM week_status WHERE week_id=?", (week,))
    assert "UGONE" not in row["snapshot_json"] and "UGONE" not in row["confirmers_json"]
    assert db.one("SELECT 1 FROM results WHERE user_id='UGONE'") is None
    assert db.one("SELECT 1 FROM results WHERE rank=1") is not None


def test_leave_deletes_the_reports_the_person_wrote(users, db, clock):
    users.join("UREPORTER", clock.now())
    users.join("UOTHER", clock.now())
    now = clock.now()
    db.execute("INSERT INTO reports (user_id, kind, text, opened_ts, due_ts) VALUES ('UREPORTER', 'report', 'x', ?, ?)",
               (now, now + DAY))
    db.execute("INSERT INTO reports (user_id, kind, text, opened_ts, due_ts) VALUES ('UOTHER', 'report', 'y', ?, ?)",
               (now, now + DAY))
    assert users.leave("UREPORTER", now + 1) is True
    assert db.one("SELECT COUNT(*) AS n FROM reports WHERE user_id = 'UREPORTER'")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM reports WHERE user_id = 'UOTHER'")["n"] == 1


def test_leave_blanks_the_person_in_voice_usage_and_keeps_the_counts(users, db, clock):
    users.join("UVOICE", clock.now())
    users.join("UKEEP", clock.now())
    now = clock.now()
    db.execute("INSERT INTO voice_usage (ts, user_id, kind, action, ok, tokens_in, tokens_out) "
               "VALUES (?, 'UVOICE', 'addressed', 'speak', 1, 40, 12)", (now,))
    db.execute("INSERT INTO voice_usage (ts, user_id, kind, action, ok, tokens_in, tokens_out) "
               "VALUES (?, 'UKEEP', 'addressed', 'speak', 1, 5, 6)", (now,))
    assert users.leave("UVOICE", now + 1)
    rows = db.query("SELECT user_id, tokens_in FROM voice_usage ORDER BY tokens_in")
    assert [(r["user_id"], r["tokens_in"]) for r in rows] == [("UKEEP", 5), ("", 40)]
