"""Organizer commands. Uses the real modules when they have landed; skips with a reason while another agent's module is
still a stub (build_context needs every core module)."""
from __future__ import annotations

import csv
import io
import os

import pytest

from goose.core.commands_organizer import NOT_ORGANIZER, OWNER_ONLY, PLAIN_HINT, handle, pond_report
from goose.types import Event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")
ORG = "UORG1"
ORG2 = "UORG2"
OWNER = "UOWNER"
MEMBER = "U5"


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


def cmd(ctx, user, command, args="", channel="C1", mentions=None):
    return Event(kind="command", ts=ctx.clock.now(), channel=channel, user=user, command=command, args=args,
                 text=("%s %s" % (command, args)).strip(), mentions=list(mentions or []))


def run(ctx, user, command, args="", **kw):
    return handle(ctx, cmd(ctx, user, command, args, **kw))


def kinds(result, kind):
    return [a for a in result.actions if a.kind == kind]


def private_text(result):
    """The organizer's own private reply (replies to members, such as notices, are skipped)."""
    eph = kinds(result, "ephemeral")
    assert len(eph) >= 1, "expected a private reply"
    organizer_replies = [e for e in eph if e.user.startswith(("UORG", "UOWNER"))]
    return (organizer_replies or eph)[0].text


def join(ctx, *uids, show=True):
    now = ctx.clock.now()
    for uid in uids:
        ctx.users.join(uid, now)
        ctx.users.set_show(uid, show)


def add_msgs(ctx, uid, n, ts=None):
    base = ctx.clock.now() if ts is None else ts
    for i in range(n):
        ctx.ledger.add_fact(idem_key="msg:C1:%s:%s:%d" % (uid, base, i), user_id=uid, kind="msg", ts=base + i,
                            channel="C1", message_ts="%.6f" % (base + i))


# ------------------------------------------------------------------------------------------------ access control

def test_non_organizer_is_refused_and_nothing_changes(octx):
    join(octx, "U2")
    res = run(octx, MEMBER, "star", "<@U2|sam> active great work")
    assert private_text(res) == NOT_ORGANIZER
    assert kinds(res, "organizer_post") == []
    assert octx.ledger.all_facts(kinds=("star", "bigstar")) == []
    assert octx.audit.tail(5) == []


def test_owner_only_commands_refuse_organizers(octx):
    res = run(octx, ORG, "set-end", "2030-01-01")
    assert private_text(res) == OWNER_ONLY
    res = run(octx, ORG, "confirm-final")
    assert private_text(res) == OWNER_ONLY
    assert octx.weekly.end_ts() != octx.clock.date_to_ts("2030-01-01")


def test_unknown_command_gets_a_short_hint(octx):
    assert "Unknown organizer command" in private_text(run(octx, ORG, "frobnicate"))


# --------------------------------------------------------------------------------------------------------- stars

def test_star_needs_a_reason(octx):
    join(octx, "U2")
    text = private_text(run(octx, ORG, "star", "<@U2|sam> active"))
    assert "reason is required" in text
    assert octx.ledger.all_facts(kinds=("star",)) == []


def test_star_refuses_a_plain_at_name_with_a_hint(octx):
    join(octx, "U2")
    assert private_text(run(octx, ORG, "star", "@sam active nice")) == PLAIN_HINT


def test_star_needs_a_joined_recipient_and_a_known_kind(octx):
    assert "not joined" in private_text(run(octx, ORG, "star", "<@U2|sam> active nice"))
    join(octx, "U2")
    assert "Kinds:" in private_text(run(octx, ORG, "star", "<@U2|sam> sparkly nice"))
    assert "yourself" in private_text(run(octx, ORG, "star", "<@%s|me> active nice" % ORG))


def test_star_awards_final_crumbs_writes_audit_and_reacts_on_the_slack_link(octx):
    join(octx, "U2")
    res = run(octx, ORG, "star", "<@U2|sam> help thanks for the fix https://acme.slack.com/archives/C7/p1760000000123456")
    facts = octx.ledger.all_facts(kinds=("star",))
    assert len(facts) == 1
    fact = facts[0]
    assert fact["user_id"] == "U2" and fact["state"] == "final"
    assert fact["amount"] == octx.config.scoring.star_points
    assert fact["reason"] == "help: thanks for the fix"
    assert fact["link"].endswith("p1760000000123456")
    reaction = kinds(res, "reaction")
    assert len(reaction) == 1
    assert reaction[0].channel == "C7" and reaction[0].message_ts == "1760000000.123456" and reaction[0].emoji == "star"
    alert = kinds(res, "organizer_post")[0]
    assert alert.channel == octx.config.channels.organizer
    assert "<@U2>" in alert.text and "25" in alert.text
    assert octx.audit.tail(1)[0]["action"] == "star"
    assert octx.audit.verify()


def test_star_without_a_slack_link_adds_no_reaction(octx):
    join(octx, "U2")
    res = run(octx, ORG, "star", "<@U2|sam> made a neat thing")
    assert kinds(res, "reaction") == []
    assert len(octx.ledger.all_facts(kinds=("star",))) == 1


def test_star_escapes_the_reason_in_the_organizer_alert(octx):
    join(octx, "U2")
    res = run(octx, ORG, "star", "<@U2|sam> idea <b>cool</b> & more")
    alert = kinds(res, "organizer_post")[0].text
    assert "&lt;b&gt;cool&lt;/b&gt; &amp; more" in alert


def test_bigstar_needs_two_different_organizers(octx):
    join(octx, "U2")
    first = run(octx, ORG, "bigstar", "<@U2|sam> hosted a great session")
    assert octx.ledger.all_facts(kinds=("bigstar",)) == []
    assert "second organizer" in kinds(first, "organizer_post")[0].text
    again = run(octx, ORG, "bigstar", "<@U2|sam> hosted a great session")
    assert "already proposed" in private_text(again)
    assert octx.ledger.all_facts(kinds=("bigstar",)) == []
    second = run(octx, ORG2, "bigstar", "<@U2|sam> hosted a great session")
    facts = octx.ledger.all_facts(kinds=("bigstar",))
    assert len(facts) == 1 and facts[0]["amount"] == octx.config.scoring.bigstar_points
    assert facts[0]["state"] == "final"
    assert "Big star" in kinds(second, "organizer_post")[0].text


def test_stars_listing_shows_recent_stars(octx):
    join(octx, "U2")
    run(octx, ORG, "star", "<@U2|sam> active keeps the pond clean")
    text = private_text(run(octx, ORG2, "stars", "all <@U2|sam>"))
    assert "keeps the pond clean" in text
    assert "<@U2>" in text


# -------------------------------------------------------------------------------------------------------- revoke

def test_revoke_needs_a_reason_then_voids_the_row(octx):
    join(octx, "U2")
    run(octx, ORG, "star", "<@U2|sam> active nice work")
    fid = octx.ledger.all_facts(kinds=("star",))[0]["id"]
    assert "reason is required" in private_text(run(octx, ORG, "revoke", str(fid)))
    assert octx.ledger.get_fact(fid)["state"] == "final"
    res = run(octx, ORG, "revoke", "%d mistaken duplicate" % fid)
    assert octx.ledger.get_fact(fid)["state"] == "voided"
    assert octx.ledger.get_fact(fid)["void_reason"] == "mistaken duplicate"
    assert any(a.kind == "ephemeral" and a.user == "U2" for a in res.actions)
    assert "already voided" in private_text(run(octx, ORG, "revoke", "%d again" % fid))


# ---------------------------------------------------------------------------------------- freeze and disqualify

def test_freeze_bounds_reason_and_unfreeze(octx):
    join(octx, "U2")
    assert "1 to 72" in private_text(run(octx, ORG, "freeze", "<@U2|sam> 0 reason"))
    assert "1 to 72" in private_text(run(octx, ORG, "freeze", "<@U2|sam> 73 reason"))
    assert "reason is required" in private_text(run(octx, ORG, "freeze", "<@U2|sam> 2"))
    run(octx, ORG, "freeze", "<@U2|sam> 2 too loud in the pond")
    assert octx.users.is_frozen("U2", octx.clock.now())
    run(octx, ORG, "unfreeze", "<@U2|sam>")
    assert not octx.users.is_frozen("U2", octx.clock.now())
    assert "not frozen" in private_text(run(octx, ORG, "unfreeze", "<@U2|sam>"))


def test_disqualify_needs_a_reason_and_a_scope(octx):
    join(octx, "U2")
    assert "reason is required" in private_text(run(octx, ORG, "disqualify", "<@U2|sam> all"))
    assert "Disqualified for 2001-01-01" in private_text(
        run(octx, ORG, "disqualify", "<@U2|sam> 2001-01-01 no such week"))
    res = run(octx, ORG, "disqualify", "<@U2|sam> all banned from the pond")
    assert "Disqualified for all" in private_text(res)
    assert any(a.kind == "ephemeral" and a.user == "U2" for a in res.actions)


# ------------------------------------------------------------------------------------------- owner, end date

def test_set_end_is_owner_only_validated_and_audited(octx):
    assert "Usage" in private_text(run(octx, OWNER, "set-end", "31/12/2030"))
    run(octx, OWNER, "set-end", "2030-12-31")
    assert octx.weekly.end_ts() == octx.clock.date_to_ts("2030-12-31")
    assert octx.audit.tail(1)[0]["action"] == "set_end"


# ------------------------------------------------------------------------------------------------ announce

def test_announce_refuses_reward_words_while_unannounced(octx):
    assert octx.config.rewards.announced is False
    assert "Reward wording" in private_text(run(octx, ORG, "announce", "A big prize for everyone"))
    assert kinds(run(octx, ORG, "announce", "Quack, the big prize is coming"), "post") == []


def test_announce_refuses_broadcast_mentions_and_escapes_the_text(octx):
    assert "Broadcast" in private_text(run(octx, ORG, "announce", "hey @here look"))
    res = run(octx, ORG, "announce", "Hello <@U9> & friends")
    post = kinds(res, "post")[0]
    assert post.channel == "C1"
    assert post.text == "Hello &lt;@U9&gt; &amp; friends"


# -------------------------------------------------------------------------------------- pause, voice, config

def test_pause_and_resume_flip_the_setting(octx):
    run(octx, ORG, "pause")
    assert octx.db.get_setting("paused", "0") == "1"
    run(octx, ORG, "resume")
    assert octx.db.get_setting("paused", "0") == "0"


def test_voice_commands_work_with_the_voice_absent(octx):
    assert "not wired" in private_text(run(octx, ORG, "voice", "on"))
    assert "not wired" in private_text(run(octx, ORG, "voice", "status"))


class FakeVoice:
    def __init__(self):
        self.enabled_flag = False

    def enabled(self):
        return self.enabled_flag

    def set_enabled(self, value):
        self.enabled_flag = bool(value)

    def status(self, now):
        return {"enabled": self.enabled_flag, "breaker": "closed", "calls_today": 0}


def test_voice_on_off_and_status_with_a_voice_client(octx):
    octx.voice_client = FakeVoice()
    run(octx, ORG, "voice", "on")
    assert octx.voice_client.enabled_flag is True
    text = private_text(run(octx, ORG, "voice", "status"))
    assert "breaker: closed" in text
    run(octx, ORG, "voice", "off")
    assert octx.voice_client.enabled_flag is False


def test_config_show_has_no_secrets(octx, monkeypatch):
    monkeypatch.setenv("VOICE_SHARED_SECRET", "super-secret-value-123")
    text = private_text(run(octx, ORG, "config", "show"))
    assert "super-secret-value-123" not in text
    assert "Rewards announced: no" in text
    assert "Scored channels: C1" in text


# ------------------------------------------------------------------------------------------- reports, prompt

def test_reports_resolve_and_prompt(octx):
    octx.db.execute("INSERT INTO reports (user_id, kind, text, opened_ts, due_ts) VALUES (?,?,?,?,?)",
                    ("U7", "report", "someone was rude", octx.clock.now(), octx.clock.now() + 3600))
    rid = octx.db.query("SELECT id FROM reports")[0]["id"]
    assert "someone was rude" in private_text(run(octx, ORG, "reports"))
    assert "note is required" in private_text(run(octx, ORG, "resolve", str(rid)))
    run(octx, ORG, "resolve", "%d checked and handled" % rid)
    assert octx.db.query("SELECT status FROM reports WHERE id=?", (rid,))[0]["status"] == "resolved"
    assert "No open report" in private_text(run(octx, ORG, "resolve", "%d again" % rid))
    run(octx, ORG, "prompt", "Share your favourite pond snack")
    assert octx.db.get_setting("prompt") == "Share your favourite pond snack"
    assert "300 characters" in private_text(run(octx, ORG, "prompt", "x" * 301))


# ------------------------------------------------------------------------------------------------- export

def test_export_escapes_formula_cells_and_produces_a_file(octx):
    join(octx, "U2")
    octx.ledger.add_fact(idem_key="manual:1", user_id="U2", kind="msg", ts=octx.clock.now(), channel="C1",
                         reason="=HYPERLINK(\"http://x\")", state="final")
    res = run(octx, ORG, "export", "all")
    files = kinds(res, "file")
    assert len(files) == 1
    assert files[0].channel == octx.config.channels.organizer
    rows = list(csv.reader(io.StringIO(files[0].content)))
    assert rows[0][0] == "id"
    reasons = [r[rows[0].index("reason")] for r in rows[1:]]
    assert "'=HYPERLINK(\"http://x\")" in reasons
    assert all(not r.startswith("=") for r in reasons)
    assert octx.audit.tail(1)[0]["action"] == "export"


def test_export_rejects_an_unknown_target(octx):
    assert "Usage" in private_text(run(octx, ORG, "export", "everything"))


# ------------------------------------------------------------------------------------------------ roster, audit

def test_roster_and_audit_listings(octx):
    join(octx, "U2")
    text = private_text(run(octx, ORG, "roster"))
    assert "<@U2>" in text and "show:yes" in text
    run(octx, ORG, "pause")
    assert "pause" in private_text(run(octx, ORG, "audit", "3"))
    assert "pause" in private_text(run(octx, ORG, "audit"))


# ----------------------------------------------------------------------------- weekly confirm, skip, Pond Report

def _close_current(octx):
    """Close the clock's current week (the test clock starts mid-week, so facts go in this week)."""
    now = octx.clock.now()
    wid = octx.clock.week_id(now)
    octx.weekly.close_week(wid, now + 1000)
    return wid


def test_confirm_week_posts_the_pond_report_with_shown_names_only(octx):
    join(octx, "U1", "U2")
    join(octx, "U3", show=False)
    add_msgs(octx, "U1", 6)
    add_msgs(octx, "U2", 4)
    add_msgs(octx, "U3", 9)
    wid = _close_current(octx)
    octx.db.set_setting("prompt", "Post a build you are proud of")
    res = run(octx, ORG, "confirm-week", wid)
    posts = [a for a in kinds(res, "post") if a.channel == "C1"]
    assert len(posts) == 1
    text = posts[0].text
    assert "<@U1>" in text and "<@U2>" in text
    assert "<@U3>" not in text
    assert "Post a build you are proud of" in text
    assert octx.weekly.status(wid)["state"] == "confirmed"


def test_pond_report_omits_the_busiest_duck_when_they_are_quiet(octx):
    join(octx, "U1")
    add_msgs(octx, "U1", 5)
    wid = _close_current(octx)
    octx.users.set_quiet("U1", True)
    actions = pond_report(octx, wid)
    assert actions and actions[0].kind == "post"
    assert "Busiest duck" not in actions[0].text


def test_confirm_week_reports_errors_and_already_confirmed(octx):
    join(octx, "U1")
    add_msgs(octx, "U1", 3)
    wid = _close_current(octx)
    run(octx, ORG, "confirm-week", wid)
    assert "already confirmed" in private_text(run(octx, ORG2, "confirm-week", wid))
    assert "cannot be confirmed" in private_text(run(octx, ORG, "confirm-week", "2001-01-01"))


def test_skip_week_needs_a_reason(octx):
    join(octx, "U1")
    add_msgs(octx, "U1", 3)
    wid = _close_current(octx)
    assert "reason is required" in private_text(run(octx, ORG, "skip-week", wid))
    run(octx, ORG, "skip-week", wid + " the pond was frozen that week")
    assert octx.weekly.status(wid)["state"] == "skipped"


def test_confirm_final_by_the_owner_gets_a_reply(octx):
    res = run(octx, OWNER, "confirm-final")
    assert res.actions
    assert any(a.kind == "ephemeral" and a.user == OWNER for a in res.actions)


# ----------------------------------------------------------------------------------------- results and winners

def test_results_week_and_all_time_render_breakdowns(octx):
    join(octx, "U1")
    add_msgs(octx, "U1", 4)
    text = private_text(run(octx, ORG, "results", "week"))
    assert "<@U1>" in text and "crumbs" in text
    assert "live, not closed yet" in text
    assert "All-time top 10" in private_text(run(octx, ORG, "results", "all"))
    assert "from 1 to 50" in private_text(run(octx, ORG, "results", "all 99"))


def test_winners_with_nothing_confirmed(octx):
    assert "No confirmed weekly winners" in private_text(run(octx, ORG, "winners"))
