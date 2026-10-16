"""The gateway wiring: CLI, token checks, heartbeat lock, handler wiring with fakes, and the Slack manifest.

Nothing here opens a network connection or needs slack_bolt. The handler tests use a fake router and fake client.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from goose.config import default_config
from goose.core.db import Database
from goose.gateway import app
from goose.types import Action, Result, VoiceJob, VoiceResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "slack", "manifest.json")
REAL_CONFIG = os.path.join(ROOT, "config", "goose.yaml")
REAL_LINES = os.path.join(ROOT, "config", "lines.yaml")


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))

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

    def call(self, request):
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


def test_check_config_real_files_print_ok(capsys):
    if not os.path.exists(REAL_LINES):
        pytest.skip("config/lines.yaml is not written yet (agent B)")
    assert app.main(["--config", REAL_CONFIG, "--check-config"]) == 0
    assert capsys.readouterr().out.strip() == "OK"


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


def test_bot_own_message_still_reaches_the_router_flagged_as_bot():
    router = FakeRouter()
    handlers = app.build_handlers(_ctx(), router, FakeClient())
    handlers["message"](_envelope(dict(MESSAGE, bot_id="B1")))
    assert router.events[0].is_bot is True


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
        def call(self, request):
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
