"""Slack payload normalizers and the action executor. Needs no other goose module."""
from __future__ import annotations

from goose.gateway import slack_io
from goose.types import Action

BOT = "UBOT"


class FakeSlackError(Exception):
    def __init__(self, error: str):
        super().__init__("slack error")
        self.response = {"error": error}


class FakeClient:
    """Records every call. `fail` maps a method name to an exception to raise."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def _call(self, _method, **kwargs):
        self.calls.append((_method, kwargs))
        if _method in self.fail:
            raise self.fail[_method]
        return {"ok": True}

    def chat_postMessage(self, **kwargs):
        return self._call("chat_postMessage", **kwargs)

    def chat_postEphemeral(self, **kwargs):
        return self._call("chat_postEphemeral", **kwargs)

    def reactions_add(self, **kwargs):
        return self._call("reactions_add", **kwargs)

    def files_upload_v2(self, **kwargs):
        return self._call("files_upload_v2", **kwargs)


# ---------------------------------------------------------------------------------------------- parse_command_text

def test_parse_command_text_splits_subcommand_and_mentions():
    parsed = slack_io.parse_command_text("star <@U123|sam> help thanks for the fix https://x.slack.com/archives/C1/p1")
    assert parsed["command"] == "star"
    assert parsed["args"] == "<@U123|sam> help thanks for the fix https://x.slack.com/archives/C1/p1"
    assert parsed["mentions"] == ["U123"]


def test_parse_command_text_blank_is_help_and_lowercases():
    assert slack_io.parse_command_text("   ") == {"command": "help", "args": "", "mentions": []}
    assert slack_io.parse_command_text("ME")["command"] == "me"


def test_parse_command_text_leaves_plain_at_names_alone():
    parsed = slack_io.parse_command_text("star @sam active nice")
    assert parsed["mentions"] == []
    assert parsed["args"] == "@sam active nice"


def test_parse_command_text_dedupes_mentions_and_drops_labels():
    parsed = slack_io.parse_command_text("top <@U1|a> <@U1|a> <@U2>")
    assert parsed["mentions"] == ["U1", "U2"]
    assert "|" not in " ".join(parsed["mentions"])


# ------------------------------------------------------------------------------------------------ normalize_message

def _msg(**kw):
    base = {"type": "message", "channel": "C1", "user": "U1", "ts": "1760000000.000100", "text": "hello there duck"}
    base.update(kw)
    return base


def test_plain_message_is_normalized():
    ev = slack_io.normalize_message({"event": _msg()}, BOT)
    assert ev.kind == "message"
    assert ev.user == "U1" and ev.channel == "C1"
    assert ev.message_ts == "1760000000.000100"
    assert ev.thread_ts == ""
    assert ev.is_bot is False and ev.bot_mentioned is False
    assert abs(ev.ts - 1760000000.0001) < 1e-6


def test_thread_reply_sets_thread_ts_but_parent_does_not():
    reply = slack_io.normalize_message(_msg(thread_ts="1759999999.000001"), BOT)
    assert reply.thread_ts == "1759999999.000001"
    parent = slack_io.normalize_message(_msg(thread_ts="1760000000.000100"), BOT)
    assert parent.thread_ts == ""


def test_bot_mention_and_mentions_from_text():
    ev = slack_io.normalize_message(_msg(text="hi <@UBOT> and <@U7|sam>"), BOT)
    assert ev.bot_mentioned is True
    assert ev.mentions == ["UBOT", "U7"]


def test_bot_messages_set_is_bot():
    by_subtype = slack_io.normalize_message(_msg(subtype="bot_message", user=None), BOT)
    assert by_subtype.is_bot is True
    by_bot_id = slack_io.normalize_message(_msg(bot_id="B1"), BOT)
    assert by_bot_id.is_bot is True


def test_thread_broadcast_and_file_share_are_plain_messages():
    for subtype in ("thread_broadcast", "file_share"):
        ev = slack_io.normalize_message(_msg(subtype=subtype), BOT)
        assert ev is not None and ev.kind == "message"


def test_message_changed_carries_both_texts():
    body = {"type": "message", "subtype": "message_changed", "channel": "C1", "event_ts": "1760000100.0",
            "message": {"type": "message", "user": "U1", "ts": "1760000000.000100", "text": "new words here"},
            "previous_message": {"type": "message", "user": "U1", "ts": "1760000000.000100", "text": "old words here"}}
    ev = slack_io.normalize_message({"event": body}, BOT)
    assert ev.kind == "message_changed"
    assert ev.text == "new words here"
    assert ev.previous_text == "old words here"
    assert ev.message_ts == "1760000000.000100"
    assert ev.user == "U1"


def test_message_deleted_uses_deleted_ts():
    body = {"type": "message", "subtype": "message_deleted", "channel": "C1", "deleted_ts": "1760000000.000100",
            "previous_message": {"type": "message", "user": "U1", "ts": "1760000000.000100", "text": "gone"}}
    ev = slack_io.normalize_message({"event": body}, BOT)
    assert ev.kind == "message_deleted"
    assert ev.deleted_ts == "1760000000.000100"
    assert ev.message_ts == "1760000000.000100"
    assert ev.user == "U1"


def test_channel_join_and_leave_are_ignored():
    assert slack_io.normalize_message(_msg(subtype="channel_join"), BOT) is None
    assert slack_io.normalize_message(_msg(subtype="channel_leave"), BOT) is None


def test_non_message_type_is_ignored():
    assert slack_io.normalize_message({"event": {"type": "reaction_added"}}, BOT) is None


# ----------------------------------------------------------------------------------------- app_mention / reactions

def test_app_mention_matches_message_ts_of_the_message_event():
    msg = slack_io.normalize_message({"event": _msg(text="<@UBOT> quack")}, BOT)
    mention = slack_io.normalize_app_mention(
        {"event": {"type": "app_mention", "channel": "C1", "user": "U1", "ts": "1760000000.000100",
                   "text": "<@UBOT> quack"}}, BOT)
    assert mention.kind == msg.kind == "message"
    assert mention.message_ts == msg.message_ts
    assert mention.bot_mentioned is True


def test_reaction_added_and_removed():
    body = {"type": "reaction_added", "user": "U2", "reaction": ":duck:", "item_user": "U1", "event_ts": "1760000200.0",
            "item": {"type": "message", "channel": "C1", "ts": "1760000000.000100"}}
    added = slack_io.normalize_reaction({"event": body}, True)
    assert added.kind == "reaction_added"
    assert added.reaction == "duck"
    assert added.user == "U2" and added.item_user == "U1"
    assert added.message_ts == "1760000000.000100" and added.channel == "C1"
    removed = slack_io.normalize_reaction({"event": dict(body, type="reaction_removed")}, False)
    assert removed.kind == "reaction_removed"


def test_reaction_on_a_file_is_ignored():
    body = {"type": "reaction_added", "user": "U2", "reaction": "duck", "item": {"type": "file", "file": "F1"}}
    assert slack_io.normalize_reaction({"event": body}, True) is None


# ------------------------------------------------------------------------------------- command and member joined

def test_normalize_command_fields():
    ev = slack_io.normalize_command({"command": "/goose", "text": "star <@U5|lee> help nice", "user_id": "U9",
                                     "channel_id": "C1", "trigger_id": "T1"}, now=123.0)
    assert ev.kind == "command"
    assert ev.command == "star"
    assert ev.args == "<@U5|lee> help nice"
    assert ev.mentions == ["U5"]
    assert ev.user == "U9" and ev.channel == "C1"
    assert ev.ts == 123.0
    assert ev.event_id == "cmd:T1"


def test_normalize_command_empty_text_is_help():
    ev = slack_io.normalize_command({"text": "", "user_id": "U9", "channel_id": "C1"}, now=1.0)
    assert ev.command == "help"
    assert ev.event_id == ""


def test_member_joined_channel():
    ev = slack_io.normalize_member_joined({"event": {"type": "member_joined_channel", "user": "U4",
                                                       "channel": "C1", "event_ts": "1760000000.0"}})
    assert ev.kind == "member_joined"
    assert ev.user == "U4" and ev.channel == "C1"
    assert slack_io.normalize_member_joined({"event": {"type": "message"}}) is None


# ---------------------------------------------------------------------------------------------------------- execute

def test_execute_maps_each_action_kind_to_the_right_call():
    client = FakeClient()
    actions = [
        Action(kind="post", channel="C1", text="hi", thread_ts="1.1"),
        Action(kind="ephemeral", channel="C1", user="U1", text="psst"),
        Action(kind="reaction", channel="C1", message_ts="1.1", emoji="duck"),
        Action(kind="organizer_post", channel="CORG", text="alert"),
        Action(kind="file", channel="CORG", filename="a.csv", content="x,y\n1,2\n", text="here"),
    ]
    assert slack_io.execute(client, actions, "CDEF") == []
    names = [c[0] for c in client.calls]
    assert names == ["chat_postMessage", "chat_postEphemeral", "reactions_add", "chat_postMessage", "files_upload_v2"]
    post = client.calls[0][1]
    assert post["channel"] == "C1" and post["text"] == "hi" and post["thread_ts"] == "1.1"
    assert post["parse"] == "none" and post["link_names"] is False
    assert post["unfurl_links"] is False and post["unfurl_media"] is False
    assert client.calls[1][1]["user"] == "U1"
    assert client.calls[2][1] == {"channel": "C1", "name": "duck", "timestamp": "1.1"}
    assert client.calls[3][1]["channel"] == "CORG"
    assert client.calls[4][1]["filename"] == "a.csv" and client.calls[4][1]["content"] == "x,y\n1,2\n"


def test_execute_uses_default_channel_when_action_has_none():
    client = FakeClient()
    slack_io.execute(client, [Action(kind="post", text="hi")], "CDEF")
    assert client.calls[0][1]["channel"] == "CDEF"


def test_execute_ignores_already_reacted():
    client = FakeClient(fail={"reactions_add": FakeSlackError("already_reacted")})
    errors = slack_io.execute(client, [Action(kind="reaction", channel="C1", message_ts="1.1", emoji="star")], "")
    assert errors == []


def test_one_failure_does_not_stop_the_others_and_only_the_code_is_returned(caplog):
    client = FakeClient(fail={"chat_postMessage": FakeSlackError("channel_not_found")})
    actions = [Action(kind="post", channel="C1", text="secret text must not be logged"),
               Action(kind="ephemeral", channel="C1", user="U1", text="still sent")]
    with caplog.at_level("WARNING"):
        errors = slack_io.execute(client, actions, "")
    assert errors == ["channel_not_found"]
    assert [c[0] for c in client.calls] == ["chat_postMessage", "chat_postEphemeral"]
    assert "channel_not_found" in caplog.text
    assert "secret text must not be logged" not in caplog.text


def test_execute_reports_exception_class_when_there_is_no_slack_code():
    client = FakeClient(fail={"chat_postMessage": RuntimeError("boom with text")})
    errors = slack_io.execute(client, [Action(kind="post", channel="C1", text="x")], "")
    assert errors == ["RuntimeError"]


def test_execute_skips_ephemeral_without_user_and_unknown_kind_is_an_error():
    client = FakeClient()
    errors = slack_io.execute(client, [Action(kind="ephemeral", channel="C1", text="no one")], "")
    assert errors == [] and client.calls == []
    errors = slack_io.execute(client, [Action(kind="nonsense", channel="C1", text="x")], "")
    assert errors == ["ValueError"]
