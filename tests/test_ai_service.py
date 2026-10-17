"""
Unit tests for ai_service.py
Validates personality loading, emoji rule injection, multi-provider handling, and offline fallback.
"""

from unittest.mock import patch, MagicMock
import ai_service
import pytest


def _mock_json_response(payload):
    body = ai_service.json.dumps(payload).encode("utf-8")

    class FakeSocket:
        def settimeout(self, _value):
            return None

    class FakeRaw:
        def __init__(self):
            self._fp = MagicMock()
            self._fp.fp.raw._sock = FakeSocket()
            self._body = body

        def read(self, size, decode_content=False):
            chunk, self._body = self._body[:size], self._body[size:]
            return chunk

    class FakeResponse:
        status_code = 200
        headers = {
            "Content-Encoding": "identity",
            "Content-Length": str(len(body)),
        }

        def __init__(self):
            self.raw = FakeRaw()

        def close(self):
            return None

    return FakeResponse()


def test_offline_fallback():
    """Verifies that without an API key, the bot returns a friendly template instead of crashing."""
    with patch("ai_service.AI_API_KEY", ""):
        response = ai_service.generate_response(user_id="U_GUEST", user_message="Hello bot!")
        assert "<@U_GUEST>" in response
        assert "can't reach my answer service" in response



def test_load_soul_prompt():
    """Verifies soul.md is loaded or a safe default is provided."""
    soul = ai_service.load_soul_prompt()
    assert len(soul) > 0
    assert "community duck" in soul


def test_build_system_instructions():
    """Verifies emoji rules from config are appended to system instructions."""
    instructions = ai_service.build_system_instructions()
    assert "EMOJI RULES:" in instructions
    assert "most replies use none" in instructions.lower()
    assert ":duck:" in instructions
    assert "current tracked members" not in instructions.lower()
    assert "direct messages, clear goose name calls" in instructions.lower()
    assert "explicit opinion question that names goose, goose lee, or orry" in instructions.lower()


def test_soul_has_a_dry_bruce_lee_easter_egg_without_forced_hype():
    soul = ai_service.load_soul_prompt().lower()
    assert "bruce lee" in soul
    assert "jackie chan" in soul
    assert "avoid canned hype" in soul
    assert "high-energy" not in soul


def test_assistance_limits_forbid_code_and_bulk_work():
    """The persona and system prompt must forbid code output and general-assistant use."""
    soul = ai_service.load_soul_prompt().lower()
    instructions = ai_service.build_system_instructions().lower()

    for text in (soul, instructions):
        assert "never write, complete, review, debug, or refactor code" in text
        assert "code blocks" in text
        assert "general-purpose assistant" in text
        assert "refuse homework, essays, translations" in text
        assert "generic chatbot" in text


def test_fallback_prompt_keeps_assistance_limits(monkeypatch):
    """Assistance limits survive even when soul.md cannot be read."""
    monkeypatch.setattr(ai_service, "SOUL_PATH", MagicMock(exists=lambda: False))
    instructions = ai_service.build_system_instructions().lower()
    assert "assistance limits" in instructions
    assert "never write, complete, review, debug, or refactor code" in instructions

def test_fallback_prompt_keeps_privacy_and_bruce_lee_rules(monkeypatch):
    monkeypatch.setattr(ai_service, "SOUL_PATH", MagicMock(exists=lambda: False))
    instructions = ai_service.build_system_instructions().lower()
    assert "jackie chan" in instructions
    assert "direct messages, clear goose name calls" in instructions
    assert "explicit opinion question that names goose, goose lee, or orry" in instructions


def test_error_resilience_on_network_failure():
    """Simulates API network exception to verify the bot catches it and falls back safely."""
    with patch("ai_service.AI_API_KEY", "fake-key-for-test"), \
         patch("ai_service.AI_PROVIDER", "gemini"), \
         patch("requests.post", side_effect=Exception("Connection timed out")):
        response = ai_service.generate_response(user_id="U_FAIL", user_message="Test")
        # Should NOT raise exception, must return graceful fallback
        assert "<@U_FAIL>" in response


def test_gemini_success_mock():
    """Tests successful Gemini API parsing."""
    mock_response = _mock_json_response({
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Great idea! Keep contributing to the team!"}]
                }
            }
        ]
    })

    with patch("ai_service.AI_API_KEY", "fake-key"), \
         patch("ai_service.AI_PROVIDER", "gemini"), \
         patch("requests.post", return_value=mock_response):
        reply = ai_service.generate_response(user_id="U_HAPPY", user_message="What should we build?")
        assert "Great idea!" in reply


def test_clean_ai_output():
    """Verifies that think tags and scratchpads are stripped cleanly."""
    raw = "<think>I should be cheerful and encouraging.</think>Hey there! Great to meet you! 🎉"
    cleaned = ai_service.clean_ai_output(raw)
    assert cleaned == "Hey there! Great to meet you! :sparkles:"
    assert "<think>" not in cleaned

    scratchpad = "Thinking Process:\n1. User asked for advice.\n\nKeep up the great work! 🚀"
    assert ai_service.clean_ai_output(scratchpad) == "Keep up the great work! :rocket:"
    assert "🔥" not in ai_service.clean_ai_output("A small fire 🔥")

    code_block = "Use this:\n```python\n# comment\ndef fn(**kwargs):\n    pass\n```"
    cleaned_code = ai_service.clean_ai_output(code_block)
    assert "# comment" in cleaned_code
    assert "**kwargs" in cleaned_code


def test_ai_replies_are_capped_at_configured_character_limit():
    with patch("ai_service.AI_MAX_REPLY_CHARS", 40):
        reply = ai_service.clean_ai_output("A direct answer with enough extra words to exceed the configured limit.")
    assert len(reply) <= 40
    assert reply.endswith("…")


def test_build_messages_payload_without_context_contains_only_the_direct_mention():
    """An unthreaded, context-free request contains only its direct mention."""
    payload = ai_service.build_messages_payload("system instruction", "<@UGOOSE> cool, thanks!")
    assert len(payload) == 2
    assert payload[0]["role"] == "system"
    assert payload[1]["role"] == "user"
    assert payload[1]["content"] == "<@UGOOSE> cool, thanks!"


def test_build_messages_payload_includes_explicitly_selected_context():
    payload = ai_service.build_messages_payload(
        "system instruction",
        "<@UGOOSE> what about that?",
        context_messages=[{"role": "user", "content": "Should we use SQLite?"}],
    )
    assert payload == [
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "Should we use SQLite?"},
        {"role": "user", "content": "<@UGOOSE> what about that?"},
    ]


def test_clean_ai_output_enforces_shortcode_allowlist_and_low_frequency():
    cleaned = ai_service.clean_ai_output(":tada: :duck: :duck: :trophy: hello 🦆 🚀")
    assert cleaned == ":duck: hello"


def test_clean_ai_output_can_disable_emojis(monkeypatch):
    monkeypatch.setattr(ai_service, "CONFIG", {"emoji_settings": {"frequency": "none", "allowed_emojis": [":duck:"]}})
    assert ai_service.clean_ai_output("Hello :duck: 🦆") == "Hello"


def test_openai_never_leaks_reasoning():
    """Verifies that internal model reasoning is NEVER leaked to the user even if content is empty."""
    mock_response = _mock_json_response({
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Internal scratchpad: user wants something."
                }
            }
        ]
    })

    with patch("ai_service.AI_API_KEY", "fake-key"), \
         patch("ai_service.AI_PROVIDER", "custom"), \
         patch("requests.post", return_value=mock_response):
        reply = ai_service.generate_response(user_id="U_TEST", user_message="test message")
        assert "Internal scratchpad" not in reply
        assert "I could not get a useful answer" in reply


def test_openai_timeout_is_configurable_and_not_retried():
    timeout_error = ai_service.requests.exceptions.Timeout("provider stalled")
    with patch("ai_service.AI_API_KEY", "fake-key"), \
         patch("ai_service.AI_PROVIDER", "custom"), \
         patch("ai_service.AI_MODEL", "x-ai/grok-4.7"), \
         patch("ai_service.AI_BASE_URL", "https://ai.example/v1"), \
         patch("ai_service.AI_TIMEOUT_SECONDS", 12), \
         patch("ai_service.requests.post", side_effect=timeout_error) as post:
        reply = ai_service.generate_response(user_id="U_TIMEOUT", user_message="Who wins?", timeout=None)

    assert post.call_count == 1
    assert post.call_args.kwargs["timeout"] == 12
    assert post.call_args.kwargs["json"]["reasoning_effort"] == "low"
    assert "<@U_TIMEOUT>" in reply


def test_ai_response_stream_obeys_a_total_deadline(monkeypatch):
    class FakeSocket:
        timeout = None

        def settimeout(self, value):
            self.timeout = value

    class FakeRaw:
        def __init__(self):
            self._fp = MagicMock()
            self._fp.fp.raw._sock = FakeSocket()
            self.elapsed = 0.0

        def read(self, size, decode_content=False):
            if self._fp.fp.raw._sock.timeout < 0.04:
                raise ai_service.requests.exceptions.Timeout("deadline")
            self.elapsed += 0.04
            return b"x"

    class FakeResponse:
        status_code = 200
        headers = {"Content-Encoding": "identity", "Content-Length": "100"}

        def __init__(self):
            self.raw = FakeRaw()

        def close(self):
            return None

    response = FakeResponse()
    clock = [0.0]
    monkeypatch.setattr(ai_service.time, "monotonic", lambda: clock[0])
    original_read = response.raw.read

    def advancing_read(size, decode_content=False):
        clock[0] += 0.04
        return original_read(size, decode_content=decode_content)

    response.raw.read = advancing_read
    monkeypatch.setattr(ai_service.requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(ai_service.requests.exceptions.Timeout):
        ai_service._post_ai_json("https://ai.example/v1", {}, {}, timeout=0.1)


