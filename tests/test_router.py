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

    def __init__(self, enabled=True, allow=True, available=True, deny_reason="limit"):
        self._enabled = enabled
        self.allow = allow
        self.avail = available
        self.deny_reason = deny_reason
        self.recorded = []

    def enabled(self):
        return self._enabled

    def available(self, now):
        return (self.avail, "" if self.avail else "breaker_open")

    def allow_person(self, user_id, now):
        return (True, "") if self.allow else (False, self.deny_reason)

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


def join(ctx, *uids, show=True, consent=None):
    """Join people with the current privacy notice (or the given consent version)."""
    version = str(ctx.config.voice.notice_version) if consent is None else consent
    for uid in uids:
        ctx.users.join(uid, ctx.clock.now(), consent_version=version)
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


def test_explicit_goose_gag_still_works_with_four_words(ctx, ev, clock):
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> you are a goose", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert "duck" in [a.emoji for a in res.actions if a.kind == "reaction"]
    assert ctx.mood.heat(clock.now()) > 0.0


@pytest.mark.parametrize("text", [
    "hey goose, how do we write this?",
    "goose help me write this",
    "goose-lee, how do we write this?",
])
def test_explicit_goose_address_with_a_question_is_chat_not_the_gag(make_ctx, ev, text):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text=text, user="U1"))
    assert len(res.jobs) == 1 and res.jobs[0].mode == "chat"
    assert ctx.mood.heat(ctx.clock.now()) == 0.0


def test_incidental_goose_word_stays_ambient(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="a goose stole my bread", user="U1"))
    assert res.jobs == []
    assert ctx.mood.heat(ctx.clock.now()) == 0.0


def test_voice_person_gap_returns_a_short_canned_reply(make_ctx, ev):
    voice = FakeVoiceClient(allow=False, deny_reason="gap")
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> quick follow up", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == []
    assert "post" in kinds(res)


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

def test_ping_by_joined_user_queues_a_voice_job_without_an_auto_reaction(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> what is a duck anyway", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert "duck" not in [a.emoji for a in res.actions if a.kind == "reaction"]
    assert len(res.jobs) == 1
    job = res.jobs[0]
    assert job.kind == "addressed" and job.mode == "chat"
    assert job.request["v"] == 1 and job.request["persona"]["max_chars"] == ctx.config.voice.reply_max_chars
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


# ------------------------------------------------------------ gate order, distress, sad (fix list 1 to 3)

def test_orry_mention_with_distress_gets_the_safety_reply_and_no_voice(make_ctx, ev, cfg):
    from goose.core.safety import safety_reply
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> orry I want to die", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == [] and voice.recorded == []
    eph = [a for a in res.actions if a.kind == "ephemeral"]
    assert len(eph) == 1 and eph[0].text == safety_reply(cfg)
    alerts = [a for a in res.actions if a.kind == "organizer_post"]
    assert len(alerts) == 1 and alerts[0].channel == cfg.channels.organizer


def test_ambient_distress_alerts_organizers_only_without_the_text(ctx, ev, cfg):
    join(ctx, "U1")
    res = route(ctx, ev(text="honestly I want to die some days", user="U1", message_ts="1760000500.000001"))
    assert [a.kind for a in res.actions] == ["organizer_post"]
    alert = res.actions[0]
    assert alert.channel == cfg.channels.organizer
    assert "1760000500.000001" in alert.text and "C1" in alert.text
    assert "want to die" not in alert.text
    assert res.jobs == []


def test_sad_only_addressed_message_goes_to_voice_without_german_roast_or_reaction(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> Bruce Lee died in 1973, right?", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert "reaction" not in kinds(res)
    assert "organizer_post" not in kinds(res)
    assert "ephemeral" not in kinds(res)
    assert len(res.jobs) == 1
    job = res.jobs[0]
    assert job.allow_german is False and job.allow_roast is False
    assert job.request["persona"]["german_allowed"] is False
    assert job.request["persona"]["roast_allowed"] is False
    assert voice.recorded == ["U1"]


# ------------------------------------------------------------ freeze, re-credit (fix list 4 and 5)

def test_frozen_person_messages_and_reactions_are_not_counted(ctx, ev, clock):
    join(ctx, "U1", "U2")
    ctx.users.freeze("U1", clock.now() + 3600, "cooling off", "UORG1", clock.now())
    route(ctx, ev(text=LONG, user="U1"))
    assert facts(ctx, "U1", "msg") == []
    msg_ts = "1760000600.000001"
    route(ctx, ev(text="this is a helpful long message", user="U2", message_ts=msg_ts))
    ctx.users.freeze("U2", clock.now() + 3600, "cooling off", "UORG1", clock.now())
    route(ctx, ev("reaction_added", user="U2", reaction="duck", item_user="U1", message_ts=msg_ts))
    assert facts(ctx, "U1", "thanks") == []
    ctx.users.unfreeze("U1", "UORG1", clock.now())
    ctx.users.unfreeze("U2", "UORG1", clock.now())
    clock.advance(60)
    route(ctx, ev(text=LONG, user="U1"))
    assert len(facts(ctx, "U1", "msg")) == 1
    route(ctx, ev("reaction_added", user="U2", reaction="duck", item_user="U1", message_ts=msg_ts))
    assert len(facts(ctx, "U1", "thanks")) == 1


def test_remove_then_readd_reaction_counts_once_and_a_live_duplicate_adds_nothing(ctx, ev, clock):
    join(ctx, "U1", "U2")
    week = ctx.clock.week_id(clock.now())
    msg_ts = "1760000700.000001"
    route(ctx, ev(text="this is a helpful long message", user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_removed", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    rows = facts(ctx, "U2", "thanks")
    assert sorted(r["state"] for r in rows) == ["pending", "voided"]
    assert ctx.ledger.week_breakdown("U2", week, include_pending=True).n_thanks == 1
    route(ctx, ev("reaction_added", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    assert len(facts(ctx, "U2", "thanks")) == 2                       # the live duplicate added nothing
    assert ctx.ledger.week_breakdown("U2", week, include_pending=True).n_thanks == 1
    route(ctx, ev("reaction_removed", user="U1", reaction="duck", item_user="U2", message_ts=msg_ts))
    assert ctx.ledger.week_breakdown("U2", week, include_pending=True).n_thanks == 0


# ------------------------------------------------------------ voice consent, thread context, redaction (11 to 13)

def test_old_consent_person_gets_no_voice_job_until_they_rejoin(make_ctx, ev, clock):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1", consent="1")                                    # accepted an older notice version
    res = route(ctx, ev(text="<@UBOT> hello there friend", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert res.jobs == [] and voice.recorded == []
    assert "post" in kinds(res)
    route(ctx, ev("command", user="U1", command="join", args=""))   # accepts the current notice
    assert ctx.users.get("U1")["consent_version"] == str(ctx.config.voice.notice_version)
    clock.advance(60)
    res = route(ctx, ev(text="<@UBOT> hello again there friend", user="U1", bot_mentioned=True, mentions=[BOT]))
    assert len(res.jobs) == 1 and voice.recorded == ["U1"]


def test_thread_context_is_six_redacted_items_of_200_characters_oldest_first(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    items = ["item %d " % i + "word " * 60 for i in range(8)]
    items[-1] = "my key is xoxb-1234567890-abcdefghij ok"
    res = route(ctx, ev(text="<@UBOT> what do you think", user="U1", bot_mentioned=True, mentions=[BOT],
                        thread_excerpt=items))
    thread = res.jobs[0].request["input"]["thread"]
    assert len(thread) == 6
    assert thread[0].startswith("item 2 ")                            # the six most recent, oldest first
    assert all(len(t) <= 200 for t in thread)
    assert sum(len(t) for t in thread) <= 1200
    assert "xoxb-" not in thread[-1] and "[redacted]" in thread[-1]


def test_thread_context_respects_the_total_character_limit(make_ctx, ev):
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    items = ["word " * 40] * 6                                      # about 200 characters each, with spaces
    res = route(ctx, ev(text="<@UBOT> thoughts please", user="U1", bot_mentioned=True, mentions=[BOT],
                        thread_excerpt=items))
    thread = res.jobs[0].request["input"]["thread"]
    assert len(thread) == 6
    assert 1100 <= sum(len(t) for t in thread) <= 1200


def test_redaction_replaces_slack_and_api_tokens(make_ctx, ev):
    from goose.core.router import _redact
    assert _redact("token xoxb-1234567890-abcdefghij here", 500) == "token [redacted] here"
    voice = FakeVoiceClient()
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")
    res = route(ctx, ev(text="<@UBOT> my token is xoxb-1234567890-abcdefghij", user="U1", bot_mentioned=True,
                        mentions=[BOT]))
    text = res.jobs[0].request["input"]["message"]["text"]
    assert "xoxb" not in text and "[redacted]" in text


# ------------------------------------------------------------ level-up milestone (14)

def test_jump_from_one_to_thirteen_sends_one_head_duck_note(ctx, ev):
    join(ctx, "U1")
    ctx.ledger.add_fact(idem_key="jump:star", user_id="U1", kind="star", ts=ctx.clock.now(), channel="C1",
                        message_ts="jump1", state="final", amount=float(ctx.levels.threshold(13)))
    res = Router(ctx).level_up_notes(["U1", "U1"])
    assert [a.kind for a in res.actions] == ["ephemeral"]
    assert res.actions[0].text in _rendered(ctx, "level_12", 13)   # the Head Duck moment, one note only
    assert ctx.users.get("U1")["announced_level"] == 13
    assert Router(ctx).level_up_notes(["U1"]).actions == []


def test_ordinary_level_up_below_twelve_uses_the_normal_moment(ctx, ev):
    join(ctx, "U1")
    ctx.ledger.add_fact(idem_key="plain:star", user_id="U1", kind="star", ts=ctx.clock.now(), channel="C1",
                        message_ts="plain1", state="final", amount=float(ctx.levels.threshold(2)))
    res = Router(ctx).level_up_notes(["U1"])
    assert len(res.actions) == 1
    assert res.actions[0].text in _rendered(ctx, "level_up", 2)
    assert res.actions[0].text not in _rendered(ctx, "level_12", 2)


def test_over_the_message_limit_the_duck_says_one_line_per_hour(make_ctx, ev, clock):
    voice = FakeVoiceClient(allow=False)
    ctx = make_ctx(voice=voice)
    join(ctx, "U1")

    def ping():
        return route(ctx, ev(text="<@UBOT> hello again friend", user="U1", bot_mentioned=True, mentions=[BOT]))

    first = ping()
    assert "post" in kinds(first) and first.jobs == []
    clock.advance(60)
    assert "post" not in kinds(ping())                       # quiet for the rest of the hour
    clock.advance(3600)
    assert "post" in kinds(ping())                           # the hour rolled over


def _rendered(ctx, moment, level):
    """Every line of a moment, filled in and expanded, for the level given."""
    title = ctx.levels.title(level)
    return {ctx.lines.expand(ctx.lines.render(line.text, {"n": level, "title": title}))
            for line in ctx.lines.lines(moment)}
