"""Member slash command tests (docs/PLAN.md section 8). Replies are private unless stated.

Like test_router.py, these skip with a clear reason until the other core modules are implemented.
"""
from __future__ import annotations

import os

import pytest

from goose.core import commands_member
from goose.core.commands_member import PRIVACY_LINE
from goose.types import Result

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")
BOT = "UBOT"


@pytest.fixture
def lines_bank():
    from goose.core.lines import LineBank
    if not os.path.exists(LINES_PATH):
        pytest.skip("config/lines.yaml is not written yet (agent B)")
    try:
        return LineBank.from_file(LINES_PATH)
    except NotImplementedError as exc:
        pytest.skip("core/lines.py not implemented yet: %s" % exc)


@pytest.fixture
def ctx(cfg, clock, rng, tmp_path, lines_bank):
    from goose.core.context import build_context
    try:
        c = build_context(cfg, db_path=":memory:", audit_path=str(tmp_path / "audit.log"), lines=lines_bank,
                          clock=clock, rng=rng, bot_user_id=BOT)
    except NotImplementedError as exc:
        pytest.skip("other core modules not implemented yet: %s" % exc)
    yield c
    c.db.close()


def join(ctx, *uids, show=True):
    for uid in uids:
        ctx.users.join(uid, ctx.clock.now())
        if show:
            ctx.users.set_show(uid, True)


def cmd(ctx, ev, user, command, args=""):
    return commands_member.handle(ctx, ev("command", user=user, command=command, args=args))


def only_private(result: Result, user: str) -> str:
    """Assert exactly one ephemeral action for `user` and no public or organizer actions. Returns its text."""
    assert result.jobs == []
    assert [a.kind for a in result.actions] == ["ephemeral"]
    assert result.actions[0].user == user
    return result.actions[0].text


# ------------------------------------------------------------ help and unknown commands

def test_help_lists_commands_and_the_privacy_line(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "help"), "U1")
    for word in ("join", "leave", "me", "top", "all", "show", "hide", "rest", "quiet", "explain", "report", "appeal"):
        assert word in text
    assert PRIVACY_LINE in text


def test_unknown_subcommand_returns_help(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "dance"), "U1")
    assert "/goose join" in text and PRIVACY_LINE in text


# ------------------------------------------------------------ join and leave

def test_join_opts_in_and_replies_privately(ctx, ev):
    only_private(cmd(ctx, ev, "U1", "join"), "U1")
    assert ctx.users.is_joined("U1")


def test_second_join_is_harmless(ctx, ev):
    cmd(ctx, ev, "U1", "join")
    only_private(cmd(ctx, ev, "U1", "join"), "U1")
    assert ctx.users.is_joined("U1")


def test_leave_removes_the_person(ctx, ev):
    join(ctx, "U1")
    only_private(cmd(ctx, ev, "U1", "leave"), "U1")
    assert not ctx.users.is_joined("U1")


def test_leave_when_not_joined_is_a_private_not_joined_line(ctx, ev):
    only_private(cmd(ctx, ev, "U9", "leave"), "U9")
    assert not ctx.users.is_joined("U9")


# ------------------------------------------------------------ show, hide, rest, quiet

def test_show_and_hide_toggle_the_board_flag(ctx, ev):
    join(ctx, "U1", show=False)
    only_private(cmd(ctx, ev, "U1", "show"), "U1")
    assert ctx.users.get("U1")["show"]
    only_private(cmd(ctx, ev, "U1", "hide"), "U1")
    assert not ctx.users.get("U1")["show"]


def test_rest_on_and_off(ctx, ev):
    join(ctx, "U1")
    cmd(ctx, ev, "U1", "rest")
    assert ctx.users.get("U1")["rest"]
    cmd(ctx, ev, "U1", "rest", "off")
    assert not ctx.users.get("U1")["rest"]


def test_quiet_on_and_off(ctx, ev):
    join(ctx, "U1")
    cmd(ctx, ev, "U1", "quiet", "on")
    assert ctx.users.get("U1")["quiet"]
    cmd(ctx, ev, "U1", "quiet", "off")
    assert not ctx.users.get("U1")["quiet"]


def test_quiet_without_on_or_off_shows_usage_and_changes_nothing(ctx, ev):
    join(ctx, "U1")
    text = only_private(cmd(ctx, ev, "U1", "quiet", "maybe"), "U1")
    assert "quiet on" in text
    assert not ctx.users.get("U1")["quiet"]


def test_show_from_someone_not_joined_is_refused_privately(ctx, ev):
    only_private(cmd(ctx, ev, "U9", "show"), "U9")
    assert not ctx.users.is_joined("U9")


# ------------------------------------------------------------ me

def test_me_shows_level_crumbs_and_pending(ctx, ev):
    join(ctx, "U1")
    ctx.ledger.add_fact(idem_key="me:1", user_id="U1", kind="msg", ts=ctx.clock.now(), channel="C1",
                        message_ts="1.1", state="pending")
    text = only_private(cmd(ctx, ev, "U1", "me"), "U1")
    assert "Pond level 1" in text
    assert "This week" in text and "Waiting to count" in text
    assert "Chat:" in text and "Thanks:" in text and "Helpful:" in text and "Stars:" in text
    assert "Not counted" in text


def test_me_for_someone_else_is_refused_for_members(ctx, ev):
    join(ctx, "U1", "U2")
    text = only_private(cmd(ctx, ev, "U1", "me", "<@U2>"), "U1")
    assert "organizers" in text.lower()
    assert "Lifetime" not in text


def test_me_for_someone_else_works_for_organizers(ctx, ev):
    join(ctx, "U2")
    text = only_private(cmd(ctx, ev, "UORG1", "me", "<@U2>"), "UORG1")
    assert "Pond level" in text


def test_me_for_someone_not_joined_is_refused(ctx, ev):
    text = only_private(cmd(ctx, ev, "U9", "me"), "U9")
    assert "join" in text.lower()


# ------------------------------------------------------------ top and all

def test_top_without_a_confirmed_week_says_so(ctx, ev):
    join(ctx, "U1")
    text = only_private(cmd(ctx, ev, "U1", "top"), "U1")
    assert "confirmed" in text.lower()


def test_all_lists_all_time_top_three_as_mentions_only_for_shown_people(ctx, ev, clock):
    join(ctx, "U1", "U2", "U3", "U4")
    join(ctx, "U5", show=False)
    now = clock.now()
    for uid, n in (("U1", 30), ("U2", 20), ("U3", 10), ("U4", 5), ("U5", 60)):
        for i in range(n):
            ctx.ledger.add_fact(idem_key="all:%s:%d" % (uid, i), user_id=uid, kind="msg", ts=now - 60 * (i + 1),
                                channel="C1", message_ts="%s.%d" % (uid, i), state="final")
    text = only_private(cmd(ctx, ev, "U4", "all"), "U4")
    assert "<@U1>" in text and "<@U2>" in text and "<@U3>" in text
    assert "<@U5>" not in text
    assert "Your place" in text


def test_top_after_confirmation_lists_mentions_and_own_place(ctx, ev, clock):
    join(ctx, "U1", "U2", "U3")
    now = clock.now()
    prev = ctx.clock.previous_week_id(ctx.clock.week_id(now))
    start = ctx.clock.week_start_from_id(prev)
    for uid, n in (("U1", 12), ("U2", 8), ("U3", 4)):
        for i in range(n):
            ctx.ledger.add_fact(idem_key="top:%s:%d" % (uid, i), user_id=uid, kind="msg",
                                ts=start + 2 * 86400 + i * 60, channel="C1", message_ts="%s.%d" % (uid, i),
                                state="final")
    ctx.weekly.close_week(prev, now)
    ctx.weekly.confirm_week(prev, "UORG1", now)
    text = only_private(cmd(ctx, ev, "U3", "top"), "U3")
    assert "<@U1>" in text and "<@U2>" in text and "<@U3>" in text
    assert "#3" in text


# ------------------------------------------------------------ explain, pun, quack

def test_explain_without_a_link_asks_for_one(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "explain", "what is this"), "U1")
    assert "link" in text.lower()


def test_explain_a_stored_duck_message_is_private(ctx, ev, clock):
    de_ids = ctx.lines.german_ids()
    assert de_ids
    ctx.explain.record("C1", "1760000500.000100", [de_ids[0]], clock.now())
    link = "<https://x.slack.com/archives/C1/p1760000500000100|link>"
    text = only_private(cmd(ctx, ev, "U1", "explain", link), "U1")
    assert text and "{de:" not in text


def test_explain_with_program_words_gets_the_redirect(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "explain", "how much ore per hour"), "U1")
    assert "Orry" in text


def test_pun_is_private_and_has_an_explanation(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "pun"), "U1")
    assert "\n" in text and "{de:" not in text


def test_quack_is_private(ctx, ev):
    text = only_private(cmd(ctx, ev, "U1", "quack"), "U1")
    assert text and "{de:" not in text


# ------------------------------------------------------------ report and appeal

def test_report_inserts_a_row_and_alerts_organizers(ctx, ev, cfg):
    join(ctx, "U1")
    res = cmd(ctx, ev, "U1", "report", "someone posted a bad link")
    kinds = [a.kind for a in res.actions]
    assert kinds == ["ephemeral", "organizer_post"]
    assert res.actions[0].user == "U1"
    assert res.actions[1].channel == cfg.channels.organizer
    row = ctx.db.one("SELECT kind, text, due_ts, opened_ts FROM reports WHERE user_id = ?", ("U1",))
    assert row["kind"] == "report" and row["text"] == "someone posted a bad link"
    assert abs((row["due_ts"] - row["opened_ts"]) - 72 * 3600) < 1


def test_appeal_is_stored_with_its_own_kind(ctx, ev):
    res = cmd(ctx, ev, "U1", "appeal", "please look again at my week")
    assert res.actions[0].kind == "ephemeral"
    row = ctx.db.one("SELECT kind FROM reports WHERE user_id = ?", ("U1",))
    assert row["kind"] == "appeal"
    assert "7 days" in res.actions[0].text


def test_report_text_with_program_words_is_still_filed(ctx, ev):
    res = cmd(ctx, ev, "U1", "report", "the ore numbers look wrong")
    assert [a.kind for a in res.actions] == ["ephemeral", "organizer_post"]
    assert ctx.db.one("SELECT COUNT(*) AS n FROM reports")["n"] == 1


# ------------------------------------------------------------ hidden people on top and all (fix list 6)

def _confirm_last_week_with_three(ctx, clock):
    now = clock.now()
    prev = ctx.clock.previous_week_id(ctx.clock.week_id(now))
    start = ctx.clock.week_start_from_id(prev)
    for uid, n in (("U1", 12), ("U2", 8), ("U3", 4)):
        for i in range(n):
            ctx.ledger.add_fact(idem_key="hide:%s:%d" % (uid, i), user_id=uid, kind="msg",
                                ts=start + 2 * 86400 + i * 60, channel="C1", message_ts="%s.%d" % (uid, i),
                                state="final")
    ctx.weekly.close_week(prev, now)
    ctx.weekly.confirm_week(prev, "UORG1", now)


def test_top_leaves_out_someone_who_hid_after_the_week_closed(ctx, ev, clock):
    join(ctx, "U1", "U2", "U3", "U4")
    _confirm_last_week_with_three(ctx, clock)
    ctx.users.set_show("U2", False)                              # hid after the snapshot was taken
    text = only_private(cmd(ctx, ev, "U4", "top"), "U4")
    assert "<@U1>" in text and "<@U3>" in text
    assert "<@U2>" not in text


def test_all_leaves_out_someone_who_hid_after_ranking(ctx, ev, clock):
    join(ctx, "U1", "U2", "U3", "U4")
    now = clock.now()
    for uid, n in (("U1", 30), ("U2", 20), ("U3", 10), ("U4", 5)):
        for i in range(n):
            ctx.ledger.add_fact(idem_key="hideall:%s:%d" % (uid, i), user_id=uid, kind="msg", ts=now - 60 * (i + 1),
                                channel="C1", message_ts="h%s.%d" % (uid, i), state="final")
    ctx.users.set_show("U1", False)
    text = only_private(cmd(ctx, ev, "U4", "all"), "U4")
    assert "<@U1>" not in text and "<@U2>" in text


# ------------------------------------------------------------ join and the privacy notice (fix list 11)

def test_join_stores_the_current_notice_version(ctx, ev, cfg):
    cmd(ctx, ev, "U1", "join")
    assert ctx.users.get("U1")["consent_version"] == str(cfg.voice.notice_version)


def test_rejoin_under_an_old_notice_accepts_the_current_one(ctx, ev, cfg):
    ctx.users.join("U1", ctx.clock.now(), consent_version="1")
    only_private(cmd(ctx, ev, "U1", "join"), "U1")
    assert ctx.users.get("U1")["consent_version"] == str(cfg.voice.notice_version)
    versions = [r["version"] for r in ctx.db.query("SELECT version FROM consents WHERE user_id = 'U1'")]
    assert sorted(versions) == sorted(["1", str(cfg.voice.notice_version)])
