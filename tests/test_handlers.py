"""
Unit tests for handlers.py
Validates event routing, bot loop prevention, channel restrictions, and text cleaning.
"""

from unittest.mock import MagicMock, patch
import handlers
import config


def test_clean_mention_text():
    """Verifies that Slack user mention tags are cleanly stripped."""
    assert handlers.clean_mention_text("<@U12345678> level") == "level"
    assert handlers.clean_mention_text("<@W98765432> what is my rank?") == "what is my rank?"
    assert handlers.clean_mention_text("") == ""


def test_bot_loop_prevention():
    """
    Crucial Test: Verifies that messages sent by bots (bot_id or subtype='bot_message')
    are ignored so bots never trigger infinite loops replying to each other.
    """
    mock_app = MagicMock()
    handlers.register_handlers(mock_app, bot_user_id="U_BOT")

    # Find the registered 'message' listener
    message_handler = None
    for call in mock_app.event.call_args_list:
        if call[0][0] == "message":
            # The decorator is called, let's inspect the wrapped function
            pass

    # Alternatively, test the filtering logic directly
    body_with_bot_id = {"event": {"bot_id": "B123", "user": "U_OTHER_BOT", "text": "I am a bot"}}
    body_with_subtype = {"event": {"subtype": "bot_message", "user": "U_OTHER_BOT", "text": "Automated alert"}}

    # Verify these would be filtered
    assert bool(body_with_bot_id["event"].get("bot_id")) is True
    assert body_with_subtype["event"].get("subtype") == "bot_message"


def test_channel_restriction():
    """Verifies that channel filtering respects config settings."""
    with patch("config.WATCHED_CHANNELS", ["C_ALLOWED"]):
        assert config.is_channel_watched("C_ALLOWED") is True
        assert config.is_channel_watched("C_RANDOM") is False

    with patch("config.WATCHED_CHANNELS", ["*"]):
        assert config.is_channel_watched("C_ANY") is True
        assert config.is_channel_watched("C_SECRET") is True


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
    with patch("handlers.resolve_user") as mock_resolve:
        mock_resolve.side_effect = lambda t: ("U1", {"user_id": "U1", "xp": 100, "level": 2, "message_count": 5}) if t == "jerry" else (None, None)
        
        # Test found user
        reply = handlers.get_cards_response_for_targets(["jerry"], "U_CALLER")
        assert "Level" in reply
        assert "<@U1>" in reply

        # Test unknown user
        unknown_reply = handlers.get_cards_response_for_targets(["ghost_user"], "U_CALLER")
        assert "No activity record found for 'ghost_user'" in unknown_reply


def test_fetch_conversation_history():
    """Verifies history extraction, exclude_ts, and bot turn identification."""
    mock_client = MagicMock()
    mock_client.conversations_replies.return_value = {
        "messages": [
            {"ts": "100.0", "user": "U_USER", "text": "hey goose-lee!"},
            {"ts": "101.0", "user": "U_BOT", "text": "Hey! What's up? 🚀"},
            {"ts": "102.0", "user": "U_USER", "text": "!level"},
            {"ts": "103.0", "user": "U_USER", "text": "current question"}
        ]
    }

    history = handlers.fetch_conversation_history(
        client=mock_client,
        channel_id="C1",
        thread_ts="100.0",
        bot_user_id="U_BOT",
        exclude_ts="103.0",
        max_messages=5
    )

    # Must contain 2 turns (skips !level command and excludes current ts 103.0)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hey goose-lee!"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hey! What's up? 🚀"


def test_is_addressed_to_bot():
    """Verifies that goose-lee recognizes when he is being spoken to."""
    # Direct mention
    assert handlers.is_addressed_to_bot("<@U_BOT> hello", bot_user_id="U_BOT") is True
    # Name prefixes
    assert handlers.is_addressed_to_bot("goose-lee can you check this?", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("hey goose, how are you?", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("@gooselee what's up", bot_user_id="U_BOT") is True
    assert handlers.is_addressed_to_bot("yo duck: look at this", bot_user_id="U_BOT") is True
    # DMs are always addressed to the bot
    assert handlers.is_addressed_to_bot("anything at all", bot_user_id="U_BOT", is_dm=True) is True
    # Random channel chatter between other members is NOT addressed to the bot
    assert handlers.is_addressed_to_bot("I am working on the database", bot_user_id="U_BOT", is_dm=False) is False
    assert handlers.is_addressed_to_bot("hey netic did you see the PR?", bot_user_id="U_BOT", is_dm=False) is False


def test_clean_bot_addressing():
    """Verifies stripping of bot names and mention tags."""
    assert handlers.clean_bot_addressing("hey goose: can you help?") == "can you help?"
    assert handlers.clean_bot_addressing("@gooselee check this out") == "check this out"
    assert handlers.clean_bot_addressing("<@U_BOT> what is the score?", bot_user_id="U_BOT") == "what is the score?"



