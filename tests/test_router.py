"""Router tests (docs/PLAN.md sections 3, 4, 10.3, 10.10 and 11).

The router depends on modules other agents are writing in parallel. Until those land, the fixtures here skip the
test with a clear reason (NotImplementedError from a stub, or config/lines.yaml not written yet).
"""
from __future__ import annotations

import os

import pytest

from goose.core.router import Router
from goose.types import Result

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")
BOT = "UBOT"


class FakeVoiceClient:
    """Enough of goose.gateway.voice_client.VoiceClient for the router."""

    def __init__(self, enabled=True, allow=True, available=True):
        self._enabled = enabled
        self.allow = allow
        self.avail = available
        self.recorded = []

    def enabled(self):
        return self._enabled

    def available(self, now):
        return (self.avail, "" if self.avail else "breaker_open")

    def allow_person(self, user_id, now):
        return (True, "") if self.allow else (False, "limit")

    def record_person(self, user_id, now):
        self.recorded.append(user_id)


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
def make_ctx(cfg, clock, rng, tmp_path, lines_bank):
    def make(voice=None, config=None):
        from goose.core.context import build_context
        try:
            return build_context(config or cfg, db_path=":memory:", audit_path=str(tmp_path / "audit.log"),
                                 lines=lines_bank, clock=clock, rng=rng, voice_client=voice, bot_user_id=BOT)
        except NotImplementedError as exc:
            pytest.skip("other core modules not implemented yet: %s" % exc)
    return make


@pytest.fixture
def ctx(make_ctx):
    c = make_ctx()
    yield c
    c.db.close()


def join(ctx, *uids, show=True):
    for uid in uids:
        ctx.users.join(uid, ctx.clock.now())
        if show:
            ctx.users.set_show(uid, True)


def route(ctx, event) -> Result:
    return Router(ctx).handle(event)


def facts(ctx, user_id, kind=None):
    sql = "SELECT id, kind, state, user_id FROM facts WHERE user_id = ?"
    params = [user_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return [dict(r) for r in ctx.db.query(sql, params)]


def kinds(result):
    return [a.kind for a in result.actions]


LONG = "this is a perfectly ordinary chat message"


# ------------------------------------------------------------ dedupe, bots, scope

def test_bot_events_are_ignored(ctx, ev):
    join(ctx, "U1")
    assert route(ctx, ev(text=LONG, user="U1", is_bot=True)) == Result()
    assert route(ctx, ev(text=LONG, user=BOT)) == Result()
    assert facts(ctx, "U1") == []


def test_double_delivery_message_and_app_mention_handled_once(ctx, ev):
    join(ctx, "U1")
    ts = "1760000001.000100"
    first = ev(text=LONG, user="U1", message_ts=ts)
    twin = ev(text=LONG, user="U1", message_ts=ts)          # different event_id, same channel and ts
    route(ctx, first)
    again = route(ctx, twin)
    assert again.actions == [] and again.jobs == []
    assert len(facts(ctx, "U1", "msg")) == 1


def test_messages_outside_scored_channels_are_ignored(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text=LONG, user="U1", channel="COTHER"))
    assert res == Result()
    assert facts(ctx, "U1") == []


# ------------------------------------------------------------ scoring of messages

def test_unjoined_sender_gets_no_fact(ctx, ev):
    route(ctx, ev(text=LONG, user="U9"))
    assert facts(ctx, "U9") == []


def test_qualifying_message_creates_one_pending_msg_fact(ctx, ev):
    join(ctx, "U1")
    route(ctx, ev(text=LONG, user="U1"))
    rows = facts(ctx, "U1", "msg")
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"


def test_short_message_creates_no_fact(ctx, ev):
    join(ctx, "U1")
    route(ctx, ev(text="ok", user="U1"))
    assert facts(ctx, "U1", "msg") == []


def test_near_duplicate_message_creates_no_fact(ctx, ev, clock):
    join(ctx, "U1")
    route(ctx, ev(text=LONG, user="U1"))
    clock.advance(120)                                       # past the flood limit
    route(ctx, ev(text=LONG, user="U1"))
    assert len(facts(ctx, "U1", "msg")) == 1


def test_flood_limit_skips_a_second_message_within_30_seconds(ctx, ev, clock):
    join(ctx, "U1")
    route(ctx, ev(text=LONG, user="U1"))
    clock.advance(5)
    route(ctx, ev(text="a different sentence with enough words here", user="U1"))
    assert len(facts(ctx, "U1", "msg")) == 1


def test_message_deleted_voids_the_fact(ctx, ev):
    join(ctx, "U1")
    ts = "1760000050.000100"
    route(ctx, ev(text=LONG, user="U1", message_ts=ts))
    route(ctx, ev("message_deleted", user="U1", deleted_ts=ts, message_ts=ts))
    rows = facts(ctx, "U1", "msg")
    assert [r["state"] for r in rows] == ["voided"]


# ------------------------------------------------------------ reactions

def test_duck_reaction_by_joined_reactor_thanks_the_author(ctx, ev):
    join(ctx, "U1", "U2")
    msg_ts = "1760000100.000001"
    route(ctx, ev(text="this is a helpful long message", user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    thanks = facts(ctx, "U2", "thanks")
    assert len(thanks) == 1 and thanks[0]["state"] == "pending"


def test_duck_reaction_on_own_message_creates_nothing(ctx, ev):
    join(ctx, "U1")
    msg_ts = "1760000110.000001"
    route(ctx, ev(text="this is a helpful long message", user="U1", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U1", message_ts=msg_ts))
    assert facts(ctx, "U1", "thanks") == []


def test_duck_reaction_on_bot_message_creates_nothing(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user=BOT,
                        message_ts="1760000120.000001"))
    assert res == Result()
    assert facts(ctx, "U1") == []


def test_white_check_mark_by_thread_parent_author_adds_helpful_fact(ctx, ev):
    join(ctx, "U2", "U3")
    parent_ts = "1760000200.000001"
    reply_ts = "1760000200.000500"
    route(ctx, ev(text="parent question is here now", user="U2", message_ts=parent_ts))
    route(ctx, ev(text="here is my helpful answer", user="U3", message_ts=reply_ts,
                  thread_ts=parent_ts, thread_parent_user="U2"))
    route(ctx, ev("reaction_added", user="U2", reaction="white_check_mark", item_user="U3",
                  message_ts=reply_ts, thread_ts=parent_ts, thread_parent_user="U2"))
    helpful = facts(ctx, "U3", "helpful")
    assert len(helpful) == 1 and helpful[0]["state"] == "pending"


def test_reaction_removed_voids_the_thanks_fact(ctx, ev):
    join(ctx, "U1", "U2")
    msg_ts = "1760000300.000001"
    route(ctx, ev(text="this is a helpful long message", user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_removed", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    assert [r["state"] for r in facts(ctx, "U2", "thanks")] == ["voided"]


# ------------------------------------------------------------ the goose gag

def test_goose_gag_one_person_gets_duck_reaction_and_small_heat(ctx, ev, clock):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> you are a goose", user="U1", bot_mentioned=True, mentions=[BOT]))
    emojis = [a.emoji for a in res.actions if a.kind == "reaction"]
    assert "duck" in emojis
    assert res.jobs == []
    assert 0.0 < ctx.mood.heat(clock.now()) < 3.0


def test_goose_gag_four_people_raise_the_heat(ctx, ev, clock):
    join(ctx, "U1", "U2", "U3", "U4")
    for uid in ("U1", "U2", "U3", "U4"):
        route(ctx, ev(text="<@UBOT> goose", user=uid, bot_mentioned=True, mentions=[BOT]))
    assert ctx.mood.heat(clock.now()) >= 3.0
    assert ctx.mood.stage(clock.now()) in ("annoyed", "angry", "timeout")


def test_goose_call_naming_another_person_adds_no_heat(ctx, ev, clock):
    join(ctx, "U1", "U2")
    route(ctx, ev(text="<@UBOT> <@U2> you are a goose", user="U1", bot_mentioned=True, mentions=[BOT, "U2"]))
    assert ctx.mood.heat(clock.now()) == 0.0


# ------------------------------------------------------------ gates for addressed messages

def test_ping_by_joined_user_reacts_and_queues_a_voice_job(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> what is a duck anyway", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert "duck" in [a.emoji for a in res.actions if a.kind == "reaction"]
    assert len(res.jobs) == 1
    job = res.jobs[0]
    assert job.kind == "addressed" and job.mode == "chat"
    assert job.request["v"] == 1 and job.request["persona"]["max_chars"] == 300
    assert "card" in job.request and "input" in job.request
    text = job.request["input"]["message"]["text"]
    assert "UBOT" not in text and "someone" in text
    assert job.request["input"]["thread"] == []
    assert job.ephemeral is False and job.user == "U1"
    assert voice.recorded == ["U1"]


def test_ping_with_voice_off_gets_a_canned_ack_and_no_job(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> hello", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == []
    assert "post" in kinds(res)


def test_ack_repeats_stay_silent_within_the_gap(ctx, ev, clock):
    join(ctx, "U1")
    first = route(ctx, ev(text="<@UBOT> hello", user="U1", bot_mentioned=True, mentions=[BOT]))
    clock.advance(30)
    second = route(ctx, ev(text="<@UBOT> hello again", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert "post" in kinds(first)
    assert "post" not in kinds(second)


def test_program_question_gets_redirect_and_no_job(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> when does loadout launch", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == []
    assert voice.recorded == []
    assert "post" in kinds(res) and "ephemeral" not in kinds(res)


def test_distress_gets_safety_reply_and_organizer_alert_only(make_ctx, ev, cfg):
    from goose.core.safety import safety_reply
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> I want to die", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == []
    assert voice.recorded == []
    assert "reaction" not in kinds(res)
    eph = [a for a in res.actions if a.kind == "ephemeral"]
    assert len(eph) == 1 and eph[0].text == safety_reply(cfg)
    alerts = [a for a in res.actions if a.kind == "organizer_post"]
    assert len(alerts) == 1 and alerts[0].channel == cfg.channels.organizer


def test_rank_question_top_ten_gets_the_number_privately(make_ctx, ev, clock):
    """Build a confirmed week where U01..U11 rank 1..11, then ask from U01 and U11."""
    ctx = make_ctx()
    users = ["U%02d" % i for i in range(1, 12)]
    join(ctx, *users)
    now = clock.now()
    prev = ctx.clock.previous_week_id(ctx.clock.week_id(now))
    prev_start = ctx.clock.week_start_from_id(prev)
    for i, uid in enumerate(users):
        for n in range(20 - i):
            ctx.ledger.add_fact(idem_key="seed:%s:%d" % (uid, n), user_id=uid, kind="msg",
                                ts=prev_start + 2 * 86400 + n * 60, channel="C1",
                                message_ts="seed%s%d" % (uid, n), state="final")
    ctx.weekly.close_week(prev, now)
    ctx.weekly.confirm_week(prev, "UORG1", now)

    top = route(ctx, ev(text="<@UBOT> what's my rank?", user="U01", bot_mentioned=True, mentions=[BOT]))
    eph = [a for a in top.actions if a.kind == "ephemeral"]
    assert len(eph) == 1 and any(ch.isdigit() for ch in eph[0].text)

    below = route(ctx, ev(text="<@UBOT> what's my rank?", user="U11", bot_mentioned=True, mentions=[BOT]))
    eph = [a for a in below.actions if a.kind == "ephemeral"]
    assert len(eph) == 1
    assert not any(ch.isdigit() for ch in eph[0].text)
    assert "post" not in kinds(below)


def test_rank_question_about_the_duck_is_answered_privately(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> what is your rank?", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert [a.kind for a in res.actions if a.kind != "reaction"] == ["ephemeral"]
    assert res.jobs == []


def test_thread_reply_asking_for_meaning_gets_the_explanation(ctx, ev):
    join(ctx, "U1")
    de_ids = ctx.lines.german_ids()
    assert de_ids, "lines.yaml must carry approved German ids"
    duck_ts = "1760000400.000001"
    ctx.explain.record("C1", duck_ts, [de_ids[0]], ctx.clock.now())
    res = route(ctx, ev(text="what does that mean?", user="U1", thread_ts=duck_ts,
                        thread_parent_user=BOT, message_ts="1760000400.000900"))
    posts = [a for a in res.actions if a.kind == "post"]
    assert len(posts) == 1 and posts[0].thread_ts == duck_ts
    assert res.jobs == []


# ------------------------------------------------------------ pause and level-up

def test_paused_bot_ignores_everything(ctx, ev):
    join(ctx, "U1")
    ctx.db.set_setting("paused", "1")
    assert route(ctx, ev(text="<@UBOT> hello there", user="U1", bot_mentioned=True, mentions=[BOT])) == Result()
    assert route(ctx, ev(text=LONG, user="U1")) == Result()
    assert route(ctx, ev("command", user="U1", command="join", args="")) == Result()
    assert facts(ctx, "U1", "msg") == []


def test_level_up_note_is_private_and_announced_once(ctx, ev):
    join(ctx, "U1")
    i = 0
    while ctx.ledger.lifetime("U1") < ctx.levels.threshold(2) and i < 200:
        i += 1
        ctx.ledger.add_fact(idem_key="lvl:%d" % i, user_id="U1", kind="msg", ts=ctx.clock.now() - 86400 * (i % 5),
                            channel="C1", message_ts="lvl%d" % i, state="final")
    res = Router(ctx).level_up_notes(["U1"])
    assert [a.kind for a in res.actions] == ["ephemeral"]
    assert res.actions[0].user == "U1"
    again = Router(ctx).level_up_notes(["U1"])
    assert again.actions == []


# ------------------------------------------------------------ dedupe and single-reply guarantees

def test_same_event_id_delivered_twice_is_handled_once(ctx, ev):
    join(ctx, "U1")
    route(ctx, ev(text=LONG, user="U1", message_ts="1760000700.000100", event_id="Evdup1"))
    twin = ev(text="a different ordinary chat message", user="U1",
              message_ts="1760000700.000200", event_id="Evdup1")
    again = route(ctx, twin)
    assert again.actions == [] and again.jobs == []
    assert len(facts(ctx, "U1", "msg")) == 1


def test_reaction_redelivery_without_event_id_is_claimed_once(ctx, ev):
    join(ctx, "U1", "U2")
    msg_ts = "1760000710.000001"
    route(ctx, ev(text="this is a helpful long message", user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts, event_id=""))
    again = route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts,
                          event_id=""))
    assert again == Result()
    assert len(facts(ctx, "U2", "thanks")) == 1
    assert ctx.db.one("SELECT COUNT(*) AS n FROM processed WHERE key LIKE 'react:%'")["n"] == 1


def test_member_command_outside_a_scored_channel_is_ignored(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev("command", user="U1", command="me", args="", channel="COTHER"))
    assert res == Result()


def test_ping_gets_one_duck_reaction_and_one_canned_reply(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> hello there", user="U1", bot_mentioned=True, mentions=[BOT]))
    reacts = [a for a in res.actions if a.kind == "reaction"]
    posts = [a for a in res.actions if a.kind == "post"]
    assert len(reacts) == 1 and reacts[0].emoji == "duck"
    assert len(posts) == 1
    assert res.jobs == []


def test_name_without_ping_gets_a_reaction_and_no_reply(make_ctx, ev, cfg):
    cfg.vibes.name_reaction_chance = 1.0
    session = make_ctx(config=cfg)
    join(session, "U1")
    res = route(session, ev(text="hey goose-lee, how is the pond", user="U1"))
    assert kinds(res) == ["reaction"]
    assert res.actions[0].emoji == "duck"
    assert res.jobs == []


def test_mention_shortcut_me_replies_privately_with_the_page(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> me", user="U1", bot_mentioned=True, mentions=[BOT]))
    eph = [a for a in res.actions if a.kind == "ephemeral"]
    assert len(eph) == 1 and eph[0].user == "U1"
    assert "Pond level 1" in eph[0].text
    assert res.jobs == []


def test_mention_shortcut_top_replies_privately(ctx, ev):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> top", user="U1", bot_mentioned=True, mentions=[BOT]))
    eph = [a for a in res.actions if a.kind == "ephemeral"]
    assert len(eph) == 1 and eph[0].user == "U1"
    assert "confirmed" in eph[0].text.lower()
    assert res.jobs == []

