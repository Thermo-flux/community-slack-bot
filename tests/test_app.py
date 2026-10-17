"""The gateway wiring: CLI, token checks, heartbeat lock, handler wiring with fakes, and the Slack manifest.

Nothing here opens a network connection or needs slack_bolt. The handler tests use a fake router and fake client.
"""
from __future__ import annotations

import json
import os
import sys
import types
import pytest

from goose.config import default_config
from goose.core.db import Database
from goose.gateway import app
from goose.types import Action, Event, Result, VoiceJob, VoiceResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "slack", "manifest.json")
REAL_CONFIG = os.path.join(ROOT, "config", "goose.yaml")
REAL_LINES = os.path.join(ROOT, "config", "lines.yaml")


class FakeClient:
    def __init__(self):
        self.calls = []
        self._next_ts = 1

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        self._next_ts += 1
        return {"ts": "%d.0" % self._next_ts}

    def chat_update(self, **kwargs):
        self.calls.append(("chat_update", kwargs))

    def chat_delete(self, **kwargs):
        self.calls.append(("chat_delete", kwargs))

    def chat_postEphemeral(self, **kwargs):
        self.calls.append(("chat_postEphemeral", kwargs))

    def reactions_add(self, **kwargs):
        self.calls.append(("reactions_add", kwargs))

    def files_upload_v2(self, **kwargs):
        self.calls.append(("files_upload_v2", kwargs))


class FakeRouter:
    """Records the events it gets and returns a scripted Result."""

    def __init__(self, result=None):
        self.events = []
        self.result = result

    def handle(self, event):
        self.events.append(event)
        return self.result or Result()


class FakeVoice:
    def __init__(self, response=None, ok=True, reason=""):
        self.response = response
        self.ok = ok
        self.reason = reason
        self.requests = []
        self.junk = []

    def call(self, request, user_id=""):
        self.requests.append(request)
        if not self.ok:
            return VoiceResult(ok=False, reason=self.reason)
        return VoiceResult(ok=True, response=self.response)

    def mark_junk(self, request_id):
        self.junk.append(request_id)


def _ctx(voice=None):
    return types.SimpleNamespace(bot_user_id="UBOT", organizer_channel="CORG", config=default_config(),
                                 voice_client=voice, lines=None,
                                 clock=types.SimpleNamespace(now=lambda: 1_760_000_000.0))


def _envelope(event, event_id="Ev1"):
    return {"type": "event_callback", "event_id": event_id, "event": event}


MESSAGE = {"type": "message", "channel": "C1", "user": "U1", "ts": "1760000000.000100", "text": "hello duck there"}


# ----------------------------------------------------------------------------------------------------- CLI and config

def test_version_flag_prints_the_version(capsys):
    assert app.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("goose-lee ")


def test_check_config_reports_a_typo_and_exits_one(tmp_path, capsys):
    bad = tmp_path / "goose.yaml"
    bad.write_text("scoring:\n  hold_secnds: 600\n", encoding="utf-8")
    assert app.main(["--config", str(bad), "--check-config"]) == 1
    out = capsys.readouterr().out
    assert "hold_secnds" in out
    assert "OK" not in out.split("\n")


def test_check_config_fails_closed_on_a_missing_file(tmp_path, capsys):
    assert app.main(["--config", str(tmp_path / "missing.yaml"), "--check-config"]) == 1
    assert "not found" in capsys.readouterr().out


def test_check_config_real_files_are_valid_but_not_configured_yet(capsys):
    """The shipped file validates, but its organizers, owner and channel ids are still placeholders: exit 0 with the list."""
    if not os.path.exists(REAL_LINES):
        pytest.skip("config/lines.yaml is not written yet (agent B)")
    assert app.main(["--config", REAL_CONFIG, "--check-config"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK, not configured yet:")
    assert "people.organizers" in out and "people.owner" in out
    assert "channels.scored" in out and "channels.organizer" in out


def test_check_config_prints_plain_ok_when_fully_set_up(tmp_path, capsys):
    text = (open(REAL_CONFIG, encoding="utf-8").read()
            .replace('owner: ""', 'owner: "U0OWNER001"')
            .replace("organizers: []", 'organizers: ["U0ORG00001"]')
            .replace('scored: ["C_LOADOUT"]', 'scored: ["C0LOADOUT1"]')
            .replace('organizer: "C_ORGANIZERS"', 'organizer: "C0ORGANIZ1"')
            .replace("lines: config/lines.yaml", "lines: %s" % REAL_LINES.replace("\\", "/")))
    path = tmp_path / "goose.yaml"
    path.write_text(text, encoding="utf-8")
    assert app.main(["--config", str(path), "--check-config"]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_check_config_empty_file_is_a_validation_error(tmp_path, capsys):
    path = tmp_path / "goose.yaml"
    path.write_text("# only a comment\n", encoding="utf-8")
    assert app.main(["--config", str(path), "--check-config"]) == 1
    assert "config file is empty" in capsys.readouterr().out


def test_start_refuses_without_tokens(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    assert app.main(["--config", REAL_CONFIG]) == 2


def test_token_prefixes_are_checked_without_echoing_tokens():
    assert app.check_tokens("xoxb-123", "xapp-456") == []
    problems = app.check_tokens("xoxp-wrong", "")
    assert "SLACK_BOT_TOKEN must start with xoxb-" in problems
    assert "SLACK_APP_TOKEN is missing" in problems
    assert app.check_tokens("xoxb-1", "xoxb-2") == ["SLACK_APP_TOKEN must start with xapp-"]
    assert not any("xoxp-wrong" in p for p in problems)


# ------------------------------------------------------------------------------------------------- heartbeat lock

def test_heartbeat_refuses_a_second_gateway_while_the_first_is_fresh():
    db = Database(":memory:")
    try:
        assert app.acquire_heartbeat(db, 1000.0) is True
        assert app.acquire_heartbeat(db, 1000.0 + 89) is False
        assert app.acquire_heartbeat(db, 1000.0 + 90) is True
    finally:
        db.close()


def test_heartbeat_beat_and_release():
    db = Database(":memory:")
    try:
        app.acquire_heartbeat(db, 500.0)
        app.beat_heartbeat(db, 560.0)
        assert app.acquire_heartbeat(db, 600.0) is False
        app.release_heartbeat(db)
        assert app.acquire_heartbeat(db, 601.0) is True
    finally:
        db.close()


def test_heartbeat_ignores_a_garbled_value():
    db = Database(":memory:")
    try:
        db.set_setting(app.HEARTBEAT_KEY, "not a number")
        assert app.acquire_heartbeat(db, 10.0) is True
    finally:
        db.close()


# ------------------------------------------------------------------------------------------------ handler wiring

def test_message_envelope_is_normalized_and_routed_with_its_event_id():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    handlers["message"](_envelope(MESSAGE, event_id="EvX"))
    assert len(router.events) == 1
    ev = router.events[0]
    assert ev.kind == "message" and ev.user == "U1" and ev.channel == "C1"
    assert ev.event_id == "EvX"


def test_bot_message_is_ignored_before_the_router():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    handlers["message"](_envelope(dict(MESSAGE, bot_id="B1")))
    assert router.events == []


def test_channel_join_is_dropped_before_the_router():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    handlers["message"](_envelope(dict(MESSAGE, subtype="channel_join")))
    assert router.events == []


def test_app_mention_and_message_for_the_same_post_share_a_message_ts():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    handlers["message"](_envelope(dict(MESSAGE, text="<@UBOT> hi")))
    handlers["app_mention"](_envelope({"type": "app_mention", "channel": "C1", "user": "U1",
                                       "ts": "1760000000.000100", "text": "<@UBOT> hi"}, event_id="EvM"))
    assert [e.message_ts for e in router.events] == ["1760000000.000100", "1760000000.000100"]
    assert router.events[1].bot_mentioned is True


def test_reactions_and_member_join_are_wired():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    reaction = {"type": "reaction_added", "user": "U2", "reaction": "duck", "item_user": "U1", "event_ts": "1.0",
                "item": {"type": "message", "channel": "C1", "ts": "1760000000.000100"}}
    handlers["reaction_added"](_envelope(reaction))
    handlers["reaction_removed"](_envelope(dict(reaction, type="reaction_removed")))
    handlers["member_joined_channel"](_envelope({"type": "member_joined_channel", "user": "U3", "channel": "C1",
                                                 "event_ts": "2.0"}))
    assert [e.kind for e in router.events] == ["reaction_added", "reaction_removed", "member_joined"]


def test_command_is_acknowledged_before_the_work_and_normalized():
    order = []
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())

    def ack():
        order.append("ack")

    def submit(fn, *args):
        order.append("work")
        fn(*args)

    handlers = app.build_handlers(_ctx(), router, FakeClient(), submit=submit)
    handlers["command"]({"text": "me", "user_id": "U9", "channel_id": "C1", "trigger_id": "T9"}, ack=ack)
    assert order == ["ack", "work"]
    ev = router.events[0]
    assert ev.kind == "command" and ev.command == "me" and ev.user == "U9"
    assert ev.event_id == "cmd:T9"


def test_router_actions_are_executed_on_the_client():
    client = FakeClient()
    router = FakeRouter(Result.of(Action(kind="ephemeral", channel="C1", user="U1", text="hi there")))
    handlers = app.build_handlers(_ctx(), router, client)
    handlers["message"](_envelope(MESSAGE))
    assert client.calls and client.calls[0][0] == "chat_postEphemeral"
    assert client.calls[0][1]["user"] == "U1"


def test_a_router_crash_is_logged_by_reason_only_and_does_not_escape(caplog):
    class Boom(FakeRouter):
        def handle(self, event):
            raise RuntimeError("secret inside the message text")

    handlers = app.build_handlers(_ctx(), Boom(), FakeClient())
    with caplog.at_level("ERROR"):
        handlers["message"](_envelope(MESSAGE))
    assert "RuntimeError" in caplog.text
    assert "secret inside" not in caplog.text


def test_voice_jobs_run_on_the_voice_pool():
    voice = FakeVoice(response={"v": 1, "request_id": "r1", "action": "speak", "text": "Quack to you.",
                                "reaction": None, "flags": {"roasted": False, "mentions_rank": False,
                                                            "uses_german": False, "numbers_used": []}})
    job = VoiceJob(request={"v": 1, "request_id": "r1", "kind": "addressed", "input": {"message": {"text": "hi"}}},
                   channel="C1", user="U1", thread_ts="1.0", message_ts="1.1", ephemeral=False,
                   fallback="Quack.", mode="chat", kind="addressed")
    client = FakeClient()
    pool_calls = []

    def voice_submit(fn, *args):
        pool_calls.append(fn.__name__)
        fn(*args)

    router = FakeRouter(Result(jobs=[job]))
    ctx = _ctx(voice)
    handlers = app.build_handlers(ctx, router, client, voice_submit=voice_submit)
    # The job runs through PostFilter, which needs a real line bank; without one the job logs and posts nothing.
    handlers["message"](_envelope(MESSAGE))
    assert pool_calls == ["run_voice_job"]
    assert [r["request_id"] for r in voice.requests] == ["r1"]


def test_voice_job_failure_posts_nothing_and_does_not_raise(caplog):
    class BrokenVoice(FakeVoice):
        def call(self, request, user_id=""):
            raise RuntimeError("timeout details must not be logged")

    job = VoiceJob(request={"v": 1, "request_id": "r2", "input": {"message": {"text": "hi"}}}, channel="C1",
                   user="U1", thread_ts="", message_ts="1.1", ephemeral=False, fallback="Quack.")
    client = FakeClient()
    ctx = _ctx(BrokenVoice())
    with caplog.at_level("ERROR"):
        app.run_voice_job(ctx, client, job)
    assert client.calls == []
    assert "timeout details must not be logged" not in caplog.text


def test_scheduler_deliver_sends_organizer_actions_to_the_organizer_channel():
    client = FakeClient()
    deliver = app.make_deliver(_ctx(), client)
    deliver(Result.of(Action(kind="organizer_post", channel="CORG", text="alert")))
    assert client.calls == [("chat_postMessage", {"channel": "CORG", "text": "alert", "thread_ts": None,
                                                  "parse": "none", "link_names": False, "unfurl_links": False,
                                                  "unfurl_media": False})]


# ----------------------------------------------------------------------------------------------------- manifest

def _manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def test_manifest_has_exactly_the_planned_scopes_and_events():
    m = _manifest()
    assert set(m["oauth_config"]["scopes"]["bot"]) == {
        "chat:write", "commands", "app_mentions:read", "channels:history", "reactions:read", "reactions:write",
        "users:read", "files:write", "channels:read", "groups:history",
    }
    assert len(m["oauth_config"]["scopes"]["bot"]) == 10
    assert set(m["settings"]["event_subscriptions"]["bot_events"]) == {
        "message.channels", "app_mention", "reaction_added", "reaction_removed", "member_joined_channel",
    }


def test_manifest_slash_command_socket_mode_and_switches():
    m = _manifest()
    commands = m["features"]["slash_commands"]
    assert [c["command"] for c in commands] == ["/goose"]
    assert commands[0]["should_escape"] is True
    assert commands[0]["usage_hint"]
    assert m["settings"]["socket_mode_enabled"] is True
    assert m["settings"]["interactivity"]["is_enabled"] is False
    assert m["settings"]["token_rotation_enabled"] is False
    assert m["display_information"]["name"] == "goose-lee"
    assert m["features"]["bot_user"]["display_name"] == "goose-lee"


def test_manifest_has_no_dashes_in_text():
    raw = open(MANIFEST, encoding="utf-8").read()
    assert chr(0x2014) not in raw and chr(0x2013) not in raw


# ------------------------------------------------------------------------------------------- voice gates and admission

from goose.core.lines import LineBank  # noqa: E402

LINES_OK = {"v": 1, "request_id": "r1", "action": "speak", "text": "Quack to you.", "reaction": None,
            "flags": {"roasted": False, "mentions_rank": False, "uses_german": False, "numbers_used": []}}


def _real_ctx(voice=None, db=None, queue_timeout_s=5.0):
    """A context with the real line bank and a real database, for jobs that go through PostFilter."""
    config = default_config()
    config.voice.queue_timeout_s = queue_timeout_s
    return types.SimpleNamespace(bot_user_id="UBOT", organizer_channel="CORG", config=config,
                                 voice_client=voice, lines=LineBank.from_file(REAL_LINES),
                                 db=db if db is not None else Database(":memory:"),
                                 clock=types.SimpleNamespace(now=lambda: 1_760_000_000.0))


def _job(request_id="r1", channel="C1", fallback="Quack."):
    return VoiceJob(request={"v": 1, "request_id": request_id, "kind": "addressed",
                             "input": {"message": {"text": "hi"}}},
                    channel=channel, user="U1", thread_ts="", message_ts="1.1", ephemeral=False, fallback=fallback)


def test_paused_skips_the_voice_and_posts_nothing():
    db = Database(":memory:")
    db.set_setting("paused", "1")
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    app.run_voice_job(_real_ctx(voice, db), client, _job())
    assert voice.requests == []
    assert client.calls == []


def test_resumed_voice_job_posts_again():
    db = Database(":memory:")
    db.set_setting("paused", "0")
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    app.run_voice_job(_real_ctx(voice, db), client, _job())
    assert len(voice.requests) == 1
    assert client.calls and client.calls[0][1]["text"] == "Quack to you."


def test_voice_filter_result_is_reused_and_successful_update_text_is_remembered(monkeypatch):
    real_filter = app.PostFilter
    checks = []

    class SpyPostFilter(real_filter):
        def check(self, *args, **kwargs):
            checks.append(kwargs.copy())
            return super().check(*args, **kwargs)

    monkeypatch.setattr(app, "PostFilter", SpyPostFilter)
    ctx = _real_ctx(FakeVoice(response=LINES_OK))
    job = _job()
    job.thinking_ts = "2.0"
    client = FakeClient()

    app.run_voice_job(ctx, client, job)

    assert len(checks) == 1
    assert checks[0]["private"] is False
    assert [call[0] for call in client.calls] == ["chat_update"]
    assert app._recent_texts(ctx, job.channel) == ["Quack to you."]


def test_ephemeral_voice_filter_receives_private_delivery(monkeypatch):
    real_filter = app.PostFilter
    checks = []

    class SpyPostFilter(real_filter):
        def check(self, *args, **kwargs):
            checks.append(kwargs.copy())
            return super().check(*args, **kwargs)

    monkeypatch.setattr(app, "PostFilter", SpyPostFilter)
    ctx = _real_ctx(FakeVoice(response=LINES_OK))
    job = _job()
    job.ephemeral = True

    app.run_voice_job(ctx, FakeClient(), job)

    assert len(checks) == 1
    assert checks[0]["private"] is True


def test_voice_job_that_returns_after_pause_does_not_post():
    db = Database(":memory:")

    class PausingVoice(FakeVoice):
        def call(self, request, user_id=""):
            db.set_setting("paused", "1")
            return VoiceResult(ok=True, response=self.response)

    client = FakeClient()
    job = _job()
    job.thinking_ts = "2.0"
    app.run_voice_job(_real_ctx(PausingVoice(response=LINES_OK), db), client, job)
    assert client.calls == [("chat_delete", {"channel": job.channel, "ts": job.thinking_ts})]


def test_job_older_than_queue_timeout_posts_the_fallback_without_a_voice_call():
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    job = _job(fallback="Quack.")
    job.created_ts = 100.0
    app.run_voice_job(_real_ctx(voice), client, job, now_fn=lambda: 105.5)
    assert voice.requests == []
    assert [c[1]["text"] for c in client.calls] == ["Quack."]


def test_job_exactly_at_queue_timeout_still_calls_the_voice():
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    job = _job()
    job.created_ts = 100.0
    app.run_voice_job(_real_ctx(voice), client, job, now_fn=lambda: 105.0)
    assert len(voice.requests) == 1
    assert client.calls[0][1]["text"] == "Quack to you."


def test_handlers_stamp_the_admission_time_and_expire_stale_jobs():
    now = {"t": 100.0}
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    deferred = []
    router = FakeRouter(Result(jobs=[_job()]))
    handlers = app.build_handlers(_real_ctx(voice), router, client,
                                  voice_submit=lambda fn, *a: deferred.append((fn, a)),
                                  now_fn=lambda: now["t"])
    handlers["message"](_envelope(MESSAGE))
    assert len(deferred) == 1
    fn, args = deferred[0]
    assert args[2].created_ts == 100.0, "args are (ctx, client, job, clock, admission)"
    now["t"] = 105.5
    fn(*args)
    assert voice.requests == []
    assert any(c[0] == "chat_update" and c[1]["text"] == "Quack." for c in client.calls)


def test_queue_age_starts_after_the_thinking_message_is_sent():
    now = {"t": 100.0}
    voice = FakeVoice(response=LINES_OK)

    class SlowThinkingClient(FakeClient):
        def chat_postMessage(self, **kwargs):
            response = super().chat_postMessage(**kwargs)
            now["t"] += 6.0
            return response

    client = SlowThinkingClient()
    deferred = []
    handlers = app.build_handlers(_real_ctx(voice), FakeRouter(Result(jobs=[_job()])), client,
                                  voice_submit=lambda fn, *args: deferred.append((fn, args)),
                                  now_fn=lambda: now["t"])
    handlers["message"](_envelope(MESSAGE))
    fn, args = deferred[0]
    assert args[2].created_ts == 106.0
    fn(*args)
    assert len(voice.requests) == 1


class SlackResponseLike:
    """The Slack SDK response exposes mapping methods without inheriting dict or Mapping."""

    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def get(self, key, default=None):
        return self.data.get(key, default)


def test_thinking_placeholder_ts_is_read_from_mapping_response():
    class ThinkingClient(FakeClient):
        def chat_postMessage(self, **kwargs):
            self.calls.append(("chat_postMessage", kwargs))
            return SlackResponseLike({"ts": "2.0"})

    deferred = []
    client = ThinkingClient()
    handlers = app.build_handlers(_real_ctx(None), FakeRouter(Result(jobs=[_job()])), client,
                                 voice_submit=lambda fn, *args: deferred.append((fn, args)))
    handlers["message"](_envelope(MESSAGE))
    assert deferred[0][1][2].thinking_ts == "2.0"


def test_voice_exception_resolves_thinking_placeholder_with_fallback():
    class BrokenVoice(FakeVoice):
        def call(self, request, user_id=""):
            raise RuntimeError("provider details stay private")

    job = _job(fallback="Quack.")
    job.thinking_ts = "2.0"
    client = FakeClient()
    app.run_voice_job(_real_ctx(BrokenVoice()), client, job)
    assert any(call[0] in ("chat_postMessage", "chat_update") and call[1]["text"] == "Quack."
               for call in client.calls)


def test_voice_submit_failure_resolves_placeholder():
    client = FakeClient()
    job = _job(fallback="Quack.")

    def reject_submit(*args):
        raise RuntimeError("executor is shutting down")

    handlers = app.build_handlers(_real_ctx(None), FakeRouter(Result(jobs=[job])), client,
                                 voice_submit=reject_submit)
    # The failed submission is handled inside admit after its placeholder is posted.
    handlers["message"](_envelope(MESSAGE))
    assert any(call[0] == "chat_update" and call[1]["text"] == "Quack."
               for call in client.calls)


class EchoVoice(FakeVoice):
    """Answers every request with a reply that echoes the request id."""

    def call(self, request, user_id=""):
        self.requests.append(request)
        return VoiceResult(ok=True, response=dict(LINES_OK, request_id=request["request_id"]))


def test_eighth_pending_voice_job_is_refused_with_the_default_settings():
    """Defaults: concurrency 2 and queue_size 5, so 7 jobs are admitted and the 8th gets its canned line at once."""
    voice = EchoVoice()
    client = FakeClient()
    deferred = []
    jobs = [_job(request_id="r%d" % i, fallback="Quack.") for i in range(8)]
    router = FakeRouter(Result(jobs=jobs))
    handlers = app.build_handlers(_real_ctx(voice), router, client,
                                  voice_submit=lambda fn, *a: deferred.append((fn, a)),
                                  now_fn=lambda: 50.0)
    handlers["message"](_envelope(MESSAGE))
    assert len(deferred) == 7
    initial_posts = [c[1]["text"] for c in client.calls if c[0] == "chat_postMessage"]
    assert initial_posts == ["_Thinking... :duck:_"] * 7 + ["Quack."], \
        "seven admitted jobs show a placeholder and the refused job gets its canned line"
    for fn, args in deferred:
        fn(*args)
    assert len(voice.requests) == 7
    assert len([c for c in client.calls if c[0] == "chat_update"]) == 7


def test_admission_slots_are_released_when_a_job_fails():
    class BrokenVoice(FakeVoice):
        def call(self, request, user_id=""):
            raise RuntimeError("boom")

    client = FakeClient()
    admission = app.VoiceAdmission(1)
    assert admission.try_admit() is True
    job = _job()
    app.run_voice_job(_real_ctx(BrokenVoice()), client, job, admission=admission)
    assert admission.pending == 0
    assert admission.try_admit() is True


def test_admission_refuses_at_the_limit_and_counts_down():
    admission = app.VoiceAdmission(2)
    assert admission.try_admit() and admission.try_admit()
    assert admission.try_admit() is False
    admission.release()
    assert admission.try_admit() is True
    admission.release()
    admission.release()
    admission.release()
    assert admission.pending == 0


def test_repeated_reply_is_refused_inside_24_hours_and_allowed_after(tmp_path):
    db = Database(":memory:")
    voice = FakeVoice(response=LINES_OK)
    client = FakeClient()
    ctx = _real_ctx(voice, db)

    def run(request_id):
        voice.response = dict(LINES_OK, request_id=request_id)   # the reply must echo the job's request id
        app.run_voice_job(ctx, client, _job(request_id=request_id))
        return client.calls[-1][1]["text"]

    assert run("r1") == "Quack to you."
    assert db.query("SELECT text FROM posted_texts")[0]["text"] == "Quack to you."
    assert run("r2") == "Quack.", "the same text is refused and the canned line is posted"
    assert len(db.query("SELECT text FROM posted_texts")) == 1

    db.execute("UPDATE posted_texts SET ts = ?", (1_760_000_000.0 - 90000,))
    assert run("r3") == "Quack to you.", "after 24 hours the text may come back"


def test_recent_texts_are_only_read_for_the_same_channel():
    db = Database(":memory:")
    db.execute("INSERT INTO posted_texts (channel, ts, text) VALUES ('C9', ?, 'elsewhere')", (1_760_000_000.0,))
    db.execute("INSERT INTO posted_texts (channel, ts, text) VALUES ('C1', ?, 'here')", (1_760_000_000.0,))
    ctx = _real_ctx(None, db)
    assert app._recent_texts(ctx, "C1") == ["here"]


# ------------------------------------------------------------------------------------------------ thread lookups

class ThreadClient(FakeClient):
    """Records every read call. `replies` and `history` are the responses; `fail` is an exception to raise."""

    def __init__(self, replies=None, history=None, fail=None):
        super().__init__()
        self.replies = replies if replies is not None else []
        self.history = history if history is not None else []
        self.fail = fail

    def conversations_replies(self, **kwargs):
        self.calls.append(("conversations_replies", kwargs))
        if self.fail is not None:
            raise self.fail
        return {"messages": list(self.replies)}

    def conversations_history(self, **kwargs):
        self.calls.append(("conversations_history", kwargs))
        if self.fail is not None:
            raise self.fail
        return {"messages": list(self.history)}


class SlackError(Exception):
    def __init__(self, code):
        super().__init__("slack says something with user text")
        self.response = {"error": code}


def _thread_event(message_ts="3.0", thread_ts="1.0"):
    return Event(kind="message", ts=3.0, channel="C1", user="U1", message_ts=message_ts, thread_ts=thread_ts,
                 text="hello duck")


def test_thread_reply_gets_parent_author_and_excerpt_without_the_current_message():
    replies = [
        {"ts": "1.0", "user": "UPARENT", "text": "parent text"},
        {"ts": "2.0", "user": "U2", "text": "first reply"},
        {"ts": "3.0", "user": "U1", "text": "hello duck"},
    ]
    client = ThreadClient(replies=replies)
    event = _thread_event()
    app.ThreadLookup(client).enrich(event)
    assert client.calls == [("conversations_replies", {"channel": "C1", "ts": "1.0", "limit": 7})]
    assert event.thread_parent_user == "UPARENT"
    assert event.thread_excerpt == ["parent text", "first reply"]


def test_thread_excerpt_keeps_the_newest_six_and_skips_missing_keys():
    replies = [{"ts": "1.0", "user": "UP", "text": "p"}]
    replies += [{"ts": "%d.0" % (2 + i), "user": "U2", "text": "t%d" % i} for i in range(6)]
    replies += [{"ts": "9.0"}, {"ts": "20.0", "user": "U1", "text": "current"}]
    client = ThreadClient(replies=replies)
    event = _thread_event(message_ts="20.0")
    app.ThreadLookup(client).enrich(event)
    assert event.thread_excerpt == ["t0", "t1", "t2", "t3", "t4", "t5"]
    assert event.thread_parent_user == "UP"
    no_user = ThreadClient(replies=[{"ts": "1.0"}])
    other = _thread_event()
    app.ThreadLookup(no_user).enrich(other)
    assert other.thread_parent_user == "" and other.thread_excerpt == []


def test_slack_error_leaves_fields_empty_and_logs_only_the_code(caplog):
    client = ThreadClient(fail=SlackError("ratelimited"))
    event = _thread_event()
    with caplog.at_level("WARNING"):
        app.ThreadLookup(client).enrich(event)
    assert event.thread_parent_user == "" and event.thread_excerpt == []
    assert "ratelimited" in caplog.text
    assert "user text" not in caplog.text


def test_reaction_on_a_thread_reply_sets_the_thread_and_parent_author():
    client = ThreadClient(
        history=[{"ts": "2.0", "thread_ts": "1.0", "user": "U2"}],
        replies=[{"ts": "1.0", "user": "UPARENT"}])
    event = Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0", item_user="U2",
                  reaction="white_check_mark")
    app.ThreadLookup(client).enrich(event)
    assert client.calls[0] == ("conversations_history",
                               {"channel": "C1", "latest": "2.0", "inclusive": True, "limit": 1})
    assert client.calls[1] == ("conversations_replies", {"channel": "C1", "ts": "1.0", "limit": 1})
    assert event.thread_ts == "1.0" and event.thread_parent_user == "UPARENT"


def test_reaction_on_a_plain_message_or_a_mismatched_history_changes_nothing():
    plain = ThreadClient(history=[{"ts": "2.0", "thread_ts": "2.0"}])
    event = Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0",
                  reaction="white_check_mark")
    app.ThreadLookup(plain).enrich(event)
    assert event.thread_ts == "" and len(plain.calls) == 1

    other = ThreadClient(history=[{"ts": "1.0", "thread_ts": "0.5"}])
    event2 = Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0")
    app.ThreadLookup(other).enrich(event2)
    assert event2.thread_ts == "" and event2.thread_parent_user == ""


def test_thread_lookups_are_cached_and_the_cache_is_bounded():
    client = ThreadClient(history=[{"ts": "2.0", "thread_ts": "1.0"}], replies=[{"ts": "1.0", "user": "UP"}])
    lookup = app.ThreadLookup(client)
    for _ in range(2):
        lookup.enrich(Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0",
                            reaction="white_check_mark"))
    assert [c[0] for c in client.calls].count("conversations_history") == 1

    small = app.ThreadLookup(ThreadClient(history=[{"ts": "9.0", "thread_ts": ""}]), max_entries=2)
    for ts in ("7.0", "8.0", "9.0"):
        small.enrich(Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts=ts,
                           reaction="white_check_mark"))
    assert len(small._cache) == 2


def test_non_helper_reactions_skip_thread_history_lookup():
    client = ThreadClient(history=[{"ts": "2.0", "thread_ts": "1.0"}])
    lookup = app.ThreadLookup(client)
    for reaction in ("duck", "question"):
        lookup.enrich(Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0",
                            reaction=reaction))
    assert client.calls == []
    lookup.enrich(Event(kind="reaction_added", ts=5.0, channel="C1", user="U3", message_ts="2.0",
                        reaction="white_check_mark"))
    assert [call[0] for call in client.calls].count("conversations_history") == 1


def test_repeated_thread_messages_use_fresh_event_context_without_refetching():
    client = ThreadClient(replies=[{"ts": "1.0", "user": "UP", "text": "parent"},
                                  {"ts": "2.0", "user": "U1", "text": "first reply"}])
    lookup = app.ThreadLookup(client, bot_user_id="UBOT")
    first = Event(kind="message", ts=2.0, channel="C1", user="U1", message_ts="2.0", thread_ts="1.0",
                  text="first reply")
    lookup.enrich(first)
    second = Event(kind="message", ts=3.0, channel="C1", user="U2", message_ts="3.0", thread_ts="1.0",
                   text="second reply")
    lookup.enrich(second)
    assert [call[0] for call in client.calls] == ["conversations_replies"]
    assert second.thread_parent_user == "UP"
    assert second.thread_excerpt == ["parent", "first reply"]


def test_handlers_fill_thread_fields_before_the_router_sees_the_event():
    client = ThreadClient(replies=[{"ts": "1.0", "user": "UP", "text": "parent"},
                                   {"ts": "2.0", "user": "U1", "text": "hello"}])
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, client)
    body = _envelope({"type": "message", "channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                      "text": "hello duck"})
    handlers["message"](body)
    assert router.events[0].thread_parent_user == "UP"
    assert router.events[0].thread_excerpt == ["parent"]


def test_own_bot_thread_message_is_ignored_before_slack_history_lookup():
    client = ThreadClient(replies=[{"ts": "1.0", "user": "UP", "text": "parent"}])
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, client)
    handlers["message"](_envelope({"type": "message", "channel": "C1", "user": "UBOT", "ts": "2.0",
                                   "thread_ts": "1.0", "text": "our own reply"}))
    assert client.calls == []
    assert router.events == []


# ------------------------------------------------------------------------------------------------- official channel

class ChannelClient:
    def __init__(self, info=None, fail=None):
        self.info = info
        self.fail = fail
        self.calls = []

    def conversations_info(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return {"channel": self.info}


def test_official_channel_must_be_public_and_named_loadout():
    good = ChannelClient({"name": "loadout", "is_private": False})
    assert app.verify_official_channel(good, "C0LOADOUT1") is True
    assert good.calls == [{"channel": "C0LOADOUT1"}]
    assert app.verify_official_channel(ChannelClient({"name": "loadout", "is_private": True}), "C0LOADOUT1") is False
    assert app.verify_official_channel(ChannelClient({"name": "general", "is_private": False}), "C0LOADOUT1") is False
    assert app.verify_official_channel(ChannelClient({"name": "loadout"}), "C0LOADOUT1") is False


def test_official_channel_refuses_wildcards_empty_ids_and_slack_errors(caplog):
    client = ChannelClient({"name": "loadout", "is_private": False})
    assert app.verify_official_channel(client, "*") is False
    assert app.verify_official_channel(client, "") is False
    assert client.calls == [], "no Slack call for a wildcard or an empty id"
    failing = ChannelClient(fail=SlackError("channel_not_found"))
    with caplog.at_level("ERROR"):
        assert app.verify_official_channel(failing, "C0LOADOUT1") is False
    assert "channel_not_found" in caplog.text and "user text" not in caplog.text


def test_start_refuses_when_a_scored_channel_is_not_the_official_one(tmp_path, monkeypatch):
    """The real start path, with slack_bolt replaced by a stub so no network is used."""
    class StubClient:
        def auth_test(self):
            return {"user_id": "UBOT"}

        def conversations_info(self, **kwargs):
            return {"channel": {"name": "random", "is_private": False}}

    class StubApp:
        def __init__(self, token):
            self.client = StubClient()

        def event(self, name):
            return lambda fn: fn

        def command(self, name):
            return lambda fn: fn

    class StubSocket:
        def __init__(self, *a, **k):
            raise AssertionError("must not connect to Slack")

    bolt = types.ModuleType("slack_bolt")
    bolt.App = StubApp
    adapter = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = StubSocket
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", socket_mode)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

    text = (open(REAL_CONFIG, encoding="utf-8").read()
            .replace('owner: ""', 'owner: "U0OWNER001"')
            .replace("organizers: []", 'organizers: ["U0ORG00001"]')
            .replace('scored: ["C_LOADOUT"]', 'scored: ["C0LOADOUT1"]')
            .replace('organizer: "C_ORGANIZERS"', 'organizer: "C0ORGANIZ1"')
            .replace("db: data/goose.db", "db: %s" % (tmp_path / "goose.db").as_posix())
            .replace("audit: data/audit.log", "audit: %s" % (tmp_path / "audit.log").as_posix())
            .replace("lines: config/lines.yaml", "lines: %s" % REAL_LINES.replace("\\", "/")))
    path = tmp_path / "goose.yaml"
    path.write_text(text, encoding="utf-8")
    assert app.run_gateway(str(path)) == 2


def test_start_refuses_while_setup_gaps_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    assert app.run_gateway(REAL_CONFIG) == 2
