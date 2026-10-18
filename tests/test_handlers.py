"""
Unit tests for handlers.py
Validates event routing, bot loop prevention, channel restrictions, and text cleaning.
"""

from unittest.mock import MagicMock, patch
from collections import OrderedDict
import json
import pytest
import handlers
import config


@pytest.fixture(autouse=True)
def allow_test_channels(monkeypatch):
    monkeypatch.setattr(handlers, "is_channel_watched", lambda channel_id: channel_id != "C_UNWATCHED")
    monkeypatch.setattr(config, "WATCHED_CHANNELS_OVERRIDE", None, raising=False)
    monkeypatch.setattr(handlers, "_RECENT_REPLY_TRIGGERS", OrderedDict())


def test_clean_mention_text():
    """Verifies that Slack user mention tags are cleanly stripped."""
    assert handlers.clean_mention_text("<@U12345678> level") == "level"
    assert handlers.clean_mention_text("<@W98765432> what is my rank?") == "what is my rank?"
    assert handlers.clean_mention_text("") == ""


def test_bot_loop_prevention():
    """
    Crucial Test: Verifies that messages sent by bots (bot_id or subtype='bot_message')
    are ignored by both message and app_mention listeners so bots never trigger infinite loops.
    """
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U_BOT")

    body_with_bot_id = {"event": {"channel": "C1", "bot_id": "B123", "user": "U_OTHER_BOT", "text": "<@U_BOT> I am a bot"}}
    body_with_subtype = {"event": {"channel": "C1", "subtype": "bot_message", "user": "U_OTHER_BOT", "text": "<@U_BOT> Automated alert"}}
    body_from_self = {"event": {"channel": "C1", "user": "U_BOT", "text": "<@U_BOT> self message"}}

    say = MagicMock()
    client = MagicMock()
    with patch("handlers._queue_ai_reply") as queue, \
         patch("handlers._score_loadout_activity") as record:
        for payload in (body_with_bot_id, body_with_subtype, body_from_self):
            app.events["message"](body=payload, client=client, say=say)
            app.events["app_mention"](body=payload, say=say, client=client)

    queue.assert_not_called()
    record.assert_not_called()
    say.assert_not_called()


def test_channel_restriction():
    """Verifies that channel filtering respects config settings."""
    with patch("config.OFFICIAL_LOADOUT_CHANNEL_ID", "C_ALLOWED"), \
         patch("config.LOADOUT_DEVELOPMENT_CHANNEL_ID", "C_DEV"), \
         patch("config.WATCHED_CHANNELS_OVERRIDE", None), \
         patch("config.VERIFIED_CHANNEL_IDS", {"C_ALLOWED", "C_DEV"}):
        assert config.is_channel_watched("C_ALLOWED") is True
        assert config.is_channel_watched("C_DEV") is True
        assert config.is_channel_watched("C_RANDOM") is False
        assert config.is_channel_watched("D123456") is True

    with patch("config.OFFICIAL_LOADOUT_CHANNEL_ID", ""), \
         patch("config.LOADOUT_DEVELOPMENT_CHANNEL_ID", ""), \
         patch("config.WATCHED_CHANNELS_OVERRIDE", None), \
         patch("config.VERIFIED_CHANNEL_IDS", set()):
        assert config.is_channel_watched("C_ANY") is False
        assert config.is_channel_watched("D123456") is True


def test_both_channels_require_exact_public_name_verification(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL_LOADOUT_CHANNEL_ID", "C_LOADOUT")
    monkeypatch.setattr(config, "LOADOUT_DEVELOPMENT_CHANNEL_ID", "C_DEV")
    monkeypatch.setattr(config, "WATCHED_CHANNELS_OVERRIDE", None)
    monkeypatch.setattr(config, "VERIFIED_CHANNEL_IDS", set())
    monkeypatch.setattr(config, "WATCHED_CHANNELS", [])

    assert config.get_configured_channels() == [
        ("C_LOADOUT", "loadout"),
        ("C_DEV", "loadout-development"),
    ]
    assert not config.is_channel_watched("C_LOADOUT")
    assert not config.is_channel_watched("C_DEV")

    assert not config.set_watched_channel_verified("C_LOADOUT", "loadout-development", False)
    assert not config.set_watched_channel_verified("C_DEV", "loadout", False)
    assert not config.set_watched_channel_verified("C_LOADOUT", "loadout", True)
    assert config.set_watched_channel_verified("C_LOADOUT", "loadout", False)
    assert config.set_watched_channel_verified("C_DEV", "loadout-development", False)
    assert config.is_channel_watched("C_LOADOUT")
    assert config.is_channel_watched("C_DEV")
    assert config.WATCHED_CHANNELS == ["C_LOADOUT", "C_DEV"]


def test_duplicate_channel_ids_disable_both_public_channel_entries(monkeypatch):
    monkeypatch.setattr(config, "OFFICIAL_LOADOUT_CHANNEL_ID", "C_SAME")
    monkeypatch.setattr(config, "LOADOUT_DEVELOPMENT_CHANNEL_ID", "C_SAME")
    monkeypatch.setattr(config, "WATCHED_CHANNELS_OVERRIDE", None)
    assert config.get_configured_channels() == []


def test_official_channel_verification_fails_closed_for_wrong_or_private_channels():
    with patch("config.OFFICIAL_LOADOUT_CHANNEL_ID", "C_OFFICIAL"), \
         patch("config.LOADOUT_DEVELOPMENT_CHANNEL_ID", "C_DEV"), \
         patch("config.WATCHED_CHANNELS_OVERRIDE", None), \
         patch("config.VERIFIED_CHANNEL_IDS", set()):
        assert not config.set_official_loadout_channel_verified("C_OFFICIAL", "loadout-development", False)
        assert config.is_channel_watched("C_OFFICIAL") is False
        assert not config.set_official_loadout_channel_verified("C_OFFICIAL", "loadout", True)
        assert config.is_channel_watched("C_OFFICIAL") is False
        assert config.set_official_loadout_channel_verified("C_OFFICIAL", "loadout", False)
        assert config.is_channel_watched("C_OFFICIAL") is True


def test_watched_channels_override_defines_the_exact_watch_set(monkeypatch):
    """Explicit WATCHED_CHANNELS entries are authoritative and may be private channels."""
    monkeypatch.setattr(config, "WATCHED_CHANNELS_OVERRIDE", ["C0C68G3K0AJ"])
    monkeypatch.setattr(config, "VERIFIED_CHANNEL_IDS", set())
    monkeypatch.setattr(config, "WATCHED_CHANNELS", [])

    assert config.get_configured_channels() == [("C0C68G3K0AJ", None)]
    assert not config.is_channel_watched("C0C68G3K0AJ")
    # Explicitly listed channels are watched even when they are private,
    # but they are only enabled after a successful verification lookup.
    assert config.set_watched_channel_verified("C0C68G3K0AJ", "loadout-development", True)
    assert config.is_channel_watched("C0C68G3K0AJ")
    assert config.is_channel_watched("D123456")
    assert not config.is_channel_watched("C_OTHER")
    assert config.WATCHED_CHANNELS == ["C0C68G3K0AJ"]

    # A failed lookup must still be able to force-disable an explicitly configured channel.
    config.disable_watched_channel("C0C68G3K0AJ")
    assert not config.is_channel_watched("C0C68G3K0AJ")
    assert config.WATCHED_CHANNELS == []


def test_parse_watched_channels_rejects_wildcards_and_invalid_ids():
    assert config._parse_watched_channels("") is None
    assert config._parse_watched_channels("   ") is None
    assert config._parse_watched_channels("C123ABC456") == ["C123ABC456"]
    assert config._parse_watched_channels("C123, C456 C123") == ["C123", "C456"]
    assert config._parse_watched_channels("*") is None
    assert config._parse_watched_channels("D123 bad!") is None


def test_slack_manifest_enables_one_to_one_dm_events_and_history():
    manifest_path = config.PROJECT_DIR / "config" / "slack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "channels:read" in manifest["oauth_config"]["scopes"]["bot"]
    assert "channels:history" in manifest["oauth_config"]["scopes"]["bot"]
    assert "groups:history" in manifest["oauth_config"]["scopes"]["bot"]
    assert "im:history" in manifest["oauth_config"]["scopes"]["bot"]
    assert "message.channels" in manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "message.groups" in manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "message.im" in manifest["settings"]["event_subscriptions"]["bot_events"]


def test_parse_card_request_targets():
    """Verifies target parsing for self, mentions, and conversational queries."""
    # Self queries
    assert handlers.parse_card_request_targets("!level", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("/level", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("level", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("my rank", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what's my rank card? show me my stats", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("show me my stats", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what is my level", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what level am i", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what level am i at?", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("which level am i", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what rank am i", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("how much xp do i have", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("do i have any xp", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("gimme my rank", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("what's my current level", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets("show me my current stats", "U_CALLER") == ["U_CALLER"]
    assert handlers.parse_card_request_targets(
        "<@U_BOT> what level am i", "U_CALLER", bot_user_id="U_BOT"
    ) == ["U_CALLER"]

    # Conversational & multiple targets
    assert handlers.parse_card_request_targets("give me the card for X", "U_CALLER") == ["X"]
    assert handlers.parse_card_request_targets("show me the card for jerry and netic", "U_CALLER") == ["jerry", "netic"]
    assert handlers.parse_card_request_targets("<@U_BOT> show me the card for jerry and netic", "U_CALLER", bot_user_id="U_BOT") == ["jerry", "netic"]
    assert handlers.parse_card_request_targets("card for @netic", "U_CALLER") == ["@netic"]
    assert handlers.parse_card_request_targets("!level <@U0C7811N0CT>", "U_CALLER") == ["<@U0C7811N0CT>"]
    assert handlers.parse_card_request_targets("stats of jerry", "U_CALLER") == ["jerry"]

    # Non-card regular messages must return None
    assert handlers.parse_card_request_targets("hello there how are you?", "U_CALLER") is None
    assert handlers.parse_card_request_targets("I think the level of difficulty is high", "U_CALLER") is None
    assert handlers.parse_card_request_targets("who is netic", "U_CALLER") is None
    assert handlers.parse_card_request_targets("who is the bot", "U_CALLER") is None


def test_get_cards_response_for_targets():
    """Verifies card responses for known and unknown users."""
    fake_status = {
        "user_id": "U1", "opted_in": True, "crumbs": 15.0, "level": 2, "title": "Duckling",
        "next_level": 3, "next_title": "Paddler", "crumbs_to_next": 10, "next_threshold": 25,
        "has_star": False, "stars": 0, "bigstars": 0, "level_floor": 1, "weekly": [], "gated": False,
    }
    with patch("handlers.resolve_user") as mock_resolve, \
         patch("handlers.crumbs.get_pond_status", return_value=fake_status):
        mock_resolve.side_effect = lambda t, **k: ("U1", {"user_id": "U1"}) if t == "jerry" else (None, None)

        # Test found user
        reply = handlers.get_cards_response_for_targets(["jerry"], "U_CALLER")
        assert "Level" in reply
        assert "<@U1>" in reply

        # Test unknown user
        unknown_reply = handlers.get_cards_response_for_targets(["ghost_user"], "U_CALLER")
        assert "No activity record found for 'ghost_user'" in unknown_reply


def test_thread_context_reads_only_the_mentioned_thread_and_only_up_to_the_mention():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("cursor") == "page-2":
                return {
                    "messages": [
                        {"ts": "3.0", "user": "U_CURRENT", "text": "<@U_BOT> current question"},
                        {"ts": "4.0", "user": "U_OTHER", "text": "later message"},
                    ],
                    "response_metadata": {"next_cursor": ""},
                }
            return {
                "messages": [
                    {"ts": "1.0", "user": "U_OTHER", "text": "thread root"},
                    {"ts": "2.0", "user": "U_BOT", "text": "earlier Goose answer"},
                ],
                "response_metadata": {"next_cursor": "page-2"},
            }

    client = FakeClient()
    context = handlers.fetch_mentioned_thread_context(
        client=client,
        channel_id="C_THREAD",
        thread_ts="1.0",
        current_ts="3.0",
        bot_user_id="U_BOT",
    )

    assert context == [
        {"role": "user", "content": "thread root"},
        {"role": "assistant", "content": "earlier Goose answer"},
    ]
    assert len(client.calls) == 2
    assert client.calls[0]["channel"] == "C_THREAD"
    assert client.calls[0]["ts"] == "1.0"
    assert client.calls[0]["limit"] == 200
    assert client.calls[1]["cursor"] == "page-2"


def test_thread_context_fails_closed_without_a_valid_message_timestamp():
    client = MagicMock()
    assert handlers.fetch_mentioned_thread_context(client, "C1", "ROOT", None, "U_BOT") == []
    assert handlers._context_turns(
        [{"ts": "5.0", "user": "U1", "text": "later context"}],
        "U_BOT",
        "not-a-timestamp",
    ) == []
    client.conversations_replies.assert_not_called()


@pytest.mark.parametrize("current_ts", ["NaN", "Infinity", "-Infinity"])
def test_thread_context_rejects_non_finite_current_timestamps(current_ts):
    assert handlers._context_turns(
        [{"ts": "5.0", "user": "U1", "text": "should not be sent"}],
        "U_BOT",
        current_ts,
    ) == []


@pytest.mark.parametrize("message_ts", ["NaN", "Infinity", "-Infinity"])
def test_thread_context_rejects_non_finite_message_timestamps(message_ts):
    assert handlers._context_turns(
        [{"ts": message_ts, "user": "U1", "text": "should not be sent"}],
        "U_BOT",
        "5.0",
    ) == []


def test_channel_context_uses_recent_same_channel_messages_before_the_mention():
    class FakeClient:
        def __init__(self):
            self.call = None

        def conversations_history(self, **kwargs):
            self.call = kwargs
            return {
                "messages": [
                    {"ts": "3.0", "user": "U_CURRENT", "text": "<@U_BOT> hi"},
                    {"ts": "4.0", "user": "U_FUTURE", "text": "future message"},
                    {"ts": "1.0", "user": "U_OTHER", "text": "earlier channel context"},
                ]
            }

    client = FakeClient()
    context = handlers.fetch_mentioned_channel_context(client, "C1", "3.0", "U_BOT")

    assert context == [{"role": "user", "content": "earlier channel context"}]
    assert client.call == {
        "channel": "C1",
        "latest": "3.0",
        "inclusive": True,
        "limit": handlers.CHANNEL_CONTEXT_LIMIT,
    }


def test_ai_mention_queues_work_and_updates_the_placeholder():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    class DeferredExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))
            return None

    class FakeSlack:
        def __init__(self):
            self.calls = []

        def chat_postMessage(self, **kwargs):
            self.calls.append(("post", kwargs))
            return {"ok": True, "ts": "123.456"}

        def chat_update(self, **kwargs):
            self.calls.append(("update", kwargs))
            return {"ok": True, "ts": kwargs["ts"]}

        def conversations_replies(self, **kwargs):
            raise AssertionError("unthreaded mentions must not fetch thread replies")

        def conversations_history(self, **kwargs):
            self.calls.append(("history", kwargs))
            return {"messages": []}

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    executor = DeferredExecutor()
    client = FakeSlack()
    say = MagicMock()
    body = {"event": {"channel": "C_ASYNC", "user": "U_ASYNC", "text": "<@U123BOT> who wins?", "ts": "100.0"}}
    with patch("handlers._AI_REPLY_EXECUTOR", executor), \
         patch("handlers.ai_service.generate_response", return_value="Bruce Lee wins.") as generate:
        app.events["app_mention"](body=body, say=say, client=client)

        assert generate.call_count == 0
        assert len(executor.jobs) == 1
        assert client.calls == []

        fn, args, kwargs = executor.jobs[0]
        fn(*args, **kwargs)

    assert generate.call_count == 1
    assert client.calls[0][0] == "post"
    assert client.calls[1][0] == "history"
    assert client.calls[-1][0] == "update"
    assert client.calls[-1][1]["ts"] == "123.456"
    assert client.calls[-1][1]["text"] == "Bruce Lee wins."
    assert generate.call_args.kwargs["user_message"] == "who wins?"
    assert generate.call_args.kwargs["context_messages"] == []
    assert handlers._is_known_bot_thread("C_ASYNC", "100.0")
    with handlers._BOT_THREADS_LOCK:
        handlers._BOT_THREADS.pop(("C_ASYNC", "100.0"), None)
    # Replies are always threaded on the triggering message
    assert client.calls[0][1]["thread_ts"] == "100.0"
    say.assert_not_called()


def test_app_mentions_in_unwatched_channels_do_not_fetch_context_or_queue_ai():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    with patch("handlers.is_channel_watched", return_value=False), \
         patch("handlers._queue_ai_reply") as queue:
        app.events["app_mention"](
            body={"event": {"channel": "C_UNWATCHED", "user": "U1", "text": "<@U123BOT> hi"}},
            say=MagicMock(),
            client=MagicMock(),
        )

    queue.assert_not_called()


def test_thread_mention_uses_its_full_thread_context():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    class DeferredExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))

    class FakeSlack:
        def __init__(self):
            self.thread_calls = []

        def chat_postMessage(self, **kwargs):
            return {"ok": True, "ts": "placeholder-ts"}

        def chat_update(self, **kwargs):
            return {"ok": True, "ts": kwargs["ts"]}

        def conversations_replies(self, **kwargs):
            self.thread_calls.append(kwargs)
            return {
                "messages": [
                    {"ts": "1.0", "user": "U_OTHER", "text": "Should we use SQLite?"},
                    {"ts": "1.5", "user": "U_OTHER", "text": "The app is single-user."},
                    {"ts": "2.0", "user": "U_CURRENT", "text": "<@U123BOT> what about that?"},
                ]
            }

        def conversations_history(self, **kwargs):
            raise AssertionError("thread mentions must use the thread, not channel history")

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    executor = DeferredExecutor()
    client = FakeSlack()
    say = MagicMock()
    body = {"event": {
        "channel": "C_THREAD",
        "user": "U_CURRENT",
        "text": "<@U123BOT> what about that?",
        "ts": "2.0",
        "thread_ts": "1.0",
    }}
    with patch("handlers._AI_REPLY_EXECUTOR", executor), \
         patch("handlers.ai_service.generate_response", return_value="SQLite works here.") as generate:
        app.events["app_mention"](body=body, say=say, client=client)
        fn, args, kwargs = executor.jobs[0]
        fn(*args, **kwargs)

    assert client.thread_calls == [{"channel": "C_THREAD", "ts": "1.0", "limit": 200}]
    assert generate.call_args.kwargs["context_messages"] == [
        {"role": "user", "content": "Should we use SQLite?"},
        {"role": "user", "content": "The app is single-user."},
    ]


def test_uncalled_messages_score_crumbs_only_in_the_official_channel():
    """Ordinary chatter in #loadout scores crumbs for opted-in members; other channels do not."""
    with patch("handlers.OFFICIAL_LOADOUT_CHANNEL_ID", "C_LOADOUT"), \
         patch("handlers.crumbs.is_opted_in", return_value=True) as opted, \
         patch("handlers.crumbs.is_qualified_message", return_value=True), \
         patch("handlers.crumbs.record_qualified_message", return_value=None) as record:
        handlers._score_loadout_activity(
            "U1", "C_LOADOUT", {"text": "I shipped the wear leveling fix today", "ts": "1.0"},
            MagicMock(), "I shipped the wear leveling fix today", {},
        )
        record.assert_called_once()

        # A non-official channel must not score
        record.reset_mock()
        handlers._score_loadout_activity(
            "U1", "C_OTHER", {"text": "I shipped the wear leveling fix today", "ts": "2.0"},
            MagicMock(), "I shipped the wear leveling fix today", {},
        )
        record.assert_not_called()


def test_unmentioned_messages_never_trigger_an_ai_reply():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    client = MagicMock()
    with patch("handlers.check_cooldown", return_value=(False, 0)), \
         patch("handlers.increment_message_count_only"), \
         patch("handlers._queue_ai_reply") as queue:
        app.events["message"](
            body={"event": {"channel": "C1", "user": "U1", "text": "The build finished."}},
            client=client,
            say=MagicMock(),
        )

    queue.assert_not_called()
    client.conversations_history.assert_not_called()
    client.conversations_replies.assert_not_called()


def test_dm_messages_are_directly_addressed_without_a_name_mention():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    with patch("handlers._queue_ai_reply") as queue:
        app.events["message"](
            body={"event": {"channel": "D123", "channel_type": "im", "user": "U1", "text": "Can you help with xoxb-123456789-abcdef?"}},
            client=MagicMock(),
            say=MagicMock(),
        )

    queue.assert_called_once()
    assert queue.call_args.kwargs["message"] == "Can you help with [REDACTED_SLACK_TOKEN]?"


def test_bot_opened_threads_accept_followups_without_rechecking_slack_history():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    handlers._remember_bot_thread("C_THREAD", "ROOT_TS")
    client = MagicMock()
    try:
        with patch("handlers._queue_ai_reply") as queue:
            app.events["message"](
                body={"event": {"channel": "C_THREAD", "user": "U1", "text": "One more question", "ts": "2.0", "thread_ts": "ROOT_TS"}},
                client=client,
                say=MagicMock(),
            )

        queue.assert_called_once()
        assert queue.call_args.kwargs["context_thread_ts"] == "ROOT_TS"
        client.conversations_replies.assert_not_called()
    finally:
        with handlers._BOT_THREADS_LOCK:
            handlers._BOT_THREADS.pop(("C_THREAD", "ROOT_TS"), None)


def test_unprompted_opinion_questions_do_not_trigger_a_reply():
    """Questions that never call Goose by name are not addressed and must stay silent."""
    assert not handlers.is_addressed_to_bot(
        "What do you think about the goose sculpture?", bot_user_id="U_BOT"
    )

    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    with patch("handlers._queue_ai_reply") as queue, \
         patch("handlers.check_cooldown", return_value=(False, 0)), \
         patch("handlers.increment_message_count_only"):
        app.events["message"](
            body={"event": {"channel": "C1", "user": "U1", "text": "What do you think about the goose sculpture?", "ts": "3.0"}},
            client=MagicMock(),
            say=MagicMock(),
        )

    queue.assert_not_called()


def test_name_calls_in_vocative_positions_are_addressed():
    """Calling Goose by name as a vocative reaches it, but passing mentions do not."""
    assert handlers.is_addressed_to_bot("what do you think, goose?", bot_user_id="U_BOT")
    assert handlers.is_addressed_to_bot("yo goose can you check this", bot_user_id="U_BOT")
    assert handlers.is_addressed_to_bot("goose can you check this", bot_user_id="U_BOT")
    assert not handlers.is_addressed_to_bot("thoughts on this goose", bot_user_id="U_BOT")
    assert not handlers.is_addressed_to_bot("that goose is roasting you back bro", bot_user_id="U_BOT")
    assert not handlers.is_addressed_to_bot("the goose sculpture is huge", bot_user_id="U_BOT")
    assert not handlers.is_addressed_to_bot("I fed the ducks today", bot_user_id="U_BOT")

    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U123BOT")
    with patch("handlers._queue_ai_reply") as queue:
        app.events["message"](
            body={"event": {"channel": "C1", "user": "U1", "text": "goose can you check this", "ts": "4.0"}},
            client=MagicMock(),
            say=MagicMock(),
        )

    queue.assert_called_once()
    assert queue.call_args.kwargs["message"] == "can you check this"


def test_thread_followup_stays_silent_when_the_model_declines():
    class Slots:
        def release(self):
            return None

    client = MagicMock()
    with patch("handlers._AI_REPLY_SLOTS", Slots()), \
         patch("handlers.fetch_mentioned_channel_context", return_value=[]), \
         patch("handlers.ai_service.generate_response", return_value=handlers.ai_service.NO_REPLY_TOKEN):
        handlers._run_ai_reply(client, "C1", "U1", "yeah that sounds fun", None, None, "5.0", "U_BOT", True, None)

    client.chat_postMessage.assert_not_called()
    client.chat_update.assert_not_called()


def test_thread_followup_posts_one_reply_when_the_model_engages():
    class Slots:
        def release(self):
            return None

    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "FINAL_TS"}
    with patch("handlers._AI_REPLY_SLOTS", Slots()), \
         patch("handlers.fetch_mentioned_channel_context", return_value=[]), \
         patch("handlers.ai_service.generate_response", return_value="Sure, here you go.") as generate:
        handlers._run_ai_reply(client, "C1", "U1", "goose, thoughts?", None, None, "5.0", "U_BOT", True, None)

    assert generate.call_args.kwargs["may_decline"] is True
    assert client.chat_postMessage.call_count == 1
    assert client.chat_postMessage.call_args.kwargs["text"] == "Sure, here you go."
    client.chat_update.assert_not_called()


def test_duplicate_deliveries_of_one_message_yield_a_single_reply():
    """A message delivered as both app_mention and message events must reply only once."""
    class Slots:
        def acquire(self, blocking=False):
            return True

        def release(self):
            return None

    class DeferredExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))

    executor = DeferredExecutor()
    with patch("handlers._AI_REPLY_EXECUTOR", executor), \
         patch("handlers._AI_REPLY_SLOTS", Slots()), \
         patch("handlers._run_ai_reply") as run:
        for _ in range(2):
            handlers._queue_ai_reply(
                MagicMock(), MagicMock(), "C_DEDUPE", "U1", "hi", None, None, "9000.1", "U_BOT",
            )

    assert len(executor.jobs) == 1


def test_full_ai_queue_replies_immediately_instead_of_silently_dropping_the_mention():
    class FullSlots:
        def acquire(self, blocking=False):
            return False

    say = MagicMock()
    with patch("handlers._AI_REPLY_SLOTS", FullSlots()):
        handlers._queue_ai_reply(MagicMock(), say, "C1", "U1", "who wins?", None, None, None, None)

    say.assert_called_once()
    assert "try again in a moment" in say.call_args.kwargs["text"]


def test_ambiguous_placeholder_update_failure_does_not_create_a_duplicate_reply():
    client = MagicMock()
    client.chat_update.side_effect = TimeoutError("response may have been lost")

    delivered = handlers._deliver_ai_reply(client, "C1", None, "123.456", "Final answer")

    assert delivered is False
    client.chat_update.assert_called_once()
    client.chat_postMessage.assert_not_called()


def test_fallback_final_reply_registers_its_thread_and_queue_key():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "FINAL_TS"}
    queue_key = ("C1", "user", "U1")
    try:
        assert handlers._deliver_ai_reply(
            client, "C1", None, "", "Final answer", queue_key=queue_key
        )
        assert handlers._is_known_bot_thread("C1", "FINAL_TS")
        assert handlers._bot_thread_queue_key("C1", "FINAL_TS") == queue_key
    finally:
        with handlers._BOT_THREADS_LOCK:
            handlers._BOT_THREADS.pop(("C1", "FINAL_TS"), None)
            handlers._BOT_THREAD_QUEUE_KEYS.pop(("C1", "FINAL_TS"), None)


def test_replies_for_the_same_thread_are_dispatched_in_message_order():
    class DeferredExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))

    class Slots:
        def acquire(self, blocking=False):
            return True

        def release(self):
            return None

    executor = DeferredExecutor()
    slots = Slots()
    processed = []

    def run_job(_client, _channel, _user, message, *_rest):
        processed.append(message)

    with patch("handlers._AI_REPLY_EXECUTOR", executor), \
         patch("handlers._AI_REPLY_SLOTS", slots), \
         patch("handlers._run_ai_reply", side_effect=run_job):
        for text, ts in (("first", "2.0"), ("second", "3.0")):
            handlers._queue_ai_reply(
                MagicMock(), MagicMock(), "C1", "U1", text,
                "ROOT", "ROOT", ts, "U_BOT",
            )

        assert len(executor.jobs) == 1
        fn, args, kwargs = executor.jobs[0]
        fn(*args, **kwargs)

    assert processed == ["first", "second"]


def test_followup_to_a_new_placeholder_stays_on_the_original_user_queue():
    class DeferredExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))

    class Slots:
        def acquire(self, blocking=False):
            return True

        def release(self):
            return None

    executor = DeferredExecutor()
    slots = Slots()
    processed = []
    user_queue = ("C1", "user", "U1")
    handlers._remember_bot_thread("C1", "PLACEHOLDER", user_queue)
    try:
        with patch("handlers._AI_REPLY_EXECUTOR", executor), \
             patch("handlers._AI_REPLY_SLOTS", slots), \
             patch("handlers._run_ai_reply", side_effect=lambda *args: processed.append(args[3])):
            handlers._queue_ai_reply(
                MagicMock(), MagicMock(), "C1", "U1", "root request",
                None, None, "1.0", "U_BOT",
            )
            handlers._queue_ai_reply(
                MagicMock(), MagicMock(), "C1", "U1", "thread followup",
                "PLACEHOLDER", "PLACEHOLDER", "2.0", "U_BOT",
            )

            assert len(executor.jobs) == 1
            fn, args, kwargs = executor.jobs[0]
            fn(*args, **kwargs)

        assert processed == ["root request", "thread followup"]
    finally:
        with handlers._BOT_THREADS_LOCK:
            handlers._BOT_THREADS.pop(("C1", "PLACEHOLDER"), None)
            handlers._BOT_THREAD_QUEUE_KEYS.pop(("C1", "PLACEHOLDER"), None)


def test_is_addressed_to_bot():
    """Verifies that goose-lee recognizes when he is being spoken to."""
    # Direct mention
    assert handlers.is_addressed_to_bot("<@U_BOT> hello", bot_user_id="U_BOT") is True
    # Name prefixes
    assert handlers.is_addressed_to_bot("goose-lee can you check this?", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("hey goose, how are you?", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("@gooselee what's up", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("yo duck: look at this", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("goose-lee can you check this?", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("@gooselee what's up", bot_user_id="U_BOT") is True
    # DMs are always addressed to the bot
    assert handlers.is_addressed_to_bot("anything at all", bot_user_id="U_BOT", is_dm=True) is True
    # Random channel chatter between other members is NOT addressed to the bot
    assert handlers.is_addressed_to_bot("I am working on the database", bot_user_id="U_BOT", is_dm=False) is False
    assert handlers.is_addressed_to_bot("hey netic did you see the PR?", bot_user_id="U_BOT", is_dm=False) is False
    assert handlers.is_addressed_to_bot("What do you think about the goose sculpture?", bot_user_id="U_BOT") is False


def test_clean_bot_addressing():
    """Verifies stripping of bot names and mention tags while preserving other user mentions."""
    assert handlers.clean_bot_addressing("hey goose: can you help?") == "can you help?"
    assert handlers.clean_bot_addressing("@gooselee check this out") == "check this out"
    assert handlers.clean_bot_addressing("<@U_BOT> what is the score?", bot_user_id="U_BOT") == "what is the score?"
    assert handlers.clean_bot_addressing("!level <@U0C7811N0CT>", bot_user_id="U_BOT") == "!level <@U0C7811N0CT>"
    assert handlers.clean_bot_addressing("<@U_BOT> what did <@U0C7811N0CT> build?", bot_user_id="U_BOT") == "what did <@U0C7811N0CT> build?"


def test_parse_card_request_does_not_hijack_normal_sentences_with_substrings():
    """Ensures words like 'explain' (contains 'xp') or 'level design' are not hijacked as card requests."""
    assert handlers.parse_card_request_targets(
        "<@U_BOT> can you explain what <@U0C7811N0CT> built?",
        caller_id="U_CALLER",
        bot_user_id="U_BOT",
    ) is None
    assert handlers.parse_card_request_targets(
        "level design is hard to balance",
        caller_id="U_CALLER",
        bot_user_id="U_BOT",
    ) is None
    assert handlers.parse_card_request_targets(
        "!level <@U0C7811N0CT>",
        caller_id="U_CALLER",
        bot_user_id="U_BOT",
    ) == ["<@U0C7811N0CT>"]


def test_sanitize_context_text():
    """Verifies that secrets and Slack tokens in context are redacted before reaching AI."""
    sample_text = "Here is my bot token xoxb-123456789-abcdef and API key sk-proj1234567890abcdef1234 and Bearer my_secret_token"
    sanitized = handlers.sanitize_context_text(sample_text)
    assert "xoxb-123456789-abcdef" not in sanitized
    assert "[REDACTED_SLACK_TOKEN]" in sanitized
    assert "sk-proj1234567890abcdef1234" not in sanitized
    assert "[REDACTED_API_KEY]" in sanitized
    assert "Bearer [REDACTED_TOKEN]" in sanitized


def test_parse_pond_command_variants():
    assert handlers.parse_pond_command("goose join") == ("join", [])
    assert handlers.parse_pond_command("goose me") == ("me", [])
    assert handlers.parse_pond_command("!goose star <@U1> great work") == ("star", ["<@u1>", "great", "work"])
    assert handlers.parse_pond_command(".goose revoke 12 oops") == ("revoke", ["12", "oops"])
    assert handlers.parse_pond_command("goose") == ("help", [])
    assert handlers.parse_pond_command("goose nonsense") is None
    assert handlers.parse_pond_command("just chatting") is None


def test_reaction_added_thanks_scores_crumbs():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U_BOT")
    client = MagicMock()
    status = {"user_id": "U_AUTHOR", "level": 3, "title": "Paddler"}
    body = {"event": {
        "reaction": "duck", "user": "U_GIVER", "item_user": "U_AUTHOR",
        "item": {"type": "message", "channel": "C_LOADOUT", "ts": "5.0"},
    }}
    with patch("handlers.OFFICIAL_LOADOUT_CHANNEL_ID", "C_LOADOUT"), \
         patch("handlers.crumbs.is_opted_in", return_value=True), \
         patch("handlers.crumbs.get_pond_status", return_value=status), \
         patch("handlers.crumbs.add_ledger_event") as add:
        app.events["reaction_added"](body=body, client=client)
    add.assert_called_once()
    assert add.call_args.args[0] == "U_AUTHOR"
    assert add.call_args.args[1] == "thanks"


def test_reaction_added_ignores_self_and_non_official_channels():
    class ListenerApp:
        def __init__(self):
            self.events = {}

        def event(self, name):
            return lambda fn: self.events.setdefault(name, fn)

        def command(self, _name):
            return lambda fn: fn

    app = ListenerApp()
    handlers.register_handlers(app, bot_user_id="U_BOT")
    client = MagicMock()
    with patch("handlers.OFFICIAL_LOADOUT_CHANNEL_ID", "C_LOADOUT"), \
         patch("handlers.crumbs.is_opted_in", return_value=True), \
         patch("handlers.crumbs.add_ledger_event") as add:
        # Self-reaction
        app.events["reaction_added"](
            body={"event": {"reaction": "duck", "user": "U_AUTHOR", "item_user": "U_AUTHOR",
                            "item": {"type": "message", "channel": "C_LOADOUT", "ts": "1"}}},
            client=client,
        )
        # Other channel
        app.events["reaction_added"](
            body={"event": {"reaction": "duck", "user": "U_GIVER", "item_user": "U_AUTHOR",
                            "item": {"type": "message", "channel": "C_OTHER", "ts": "2"}}},
            client=client,
        )
    add.assert_not_called()




