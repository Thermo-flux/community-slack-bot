"""
Unit tests for ai_service.py
Validates personality loading, emoji rule injection, multi-provider handling, and offline fallback.
"""

from unittest.mock import patch, MagicMock
import ai_service


def test_offline_fallback():
    """Verifies that without an API key, the bot returns a friendly template instead of crashing."""
    with patch("ai_service.AI_API_KEY", ""):
        response = ai_service.generate_response(user_id="U_GUEST", user_message="Hello bot!")
        assert "<@U_GUEST>" in response
        assert "standby mode" in response or "tracking" in response



def test_load_soul_prompt():
    """Verifies soul.md is loaded or a safe default is provided."""
    soul = ai_service.load_soul_prompt()
    assert len(soul) > 0
    assert "Community" in soul or "Bot" in soul


def test_build_system_instructions():
    """Verifies emoji rules from config are appended to system instructions."""
    instructions = ai_service.build_system_instructions()
    assert "EMOJI RULES:" in instructions
    assert "frequency" in instructions.lower()


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
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Great idea! Keep contributing to the team!"}]
                }
            }
        ]
    }

    with patch("ai_service.AI_API_KEY", "fake-key"), \
         patch("ai_service.AI_PROVIDER", "gemini"), \
         patch("requests.post", return_value=mock_response):
        reply = ai_service.generate_response(user_id="U_HAPPY", user_message="What should we build?")
        assert "Great idea!" in reply


def test_clean_ai_output():
    """Verifies that think tags and scratchpads are stripped cleanly."""
    raw = "<think>I should be cheerful and encouraging.</think>Hey there! Great to meet you! 🎉"
    cleaned = ai_service.clean_ai_output(raw)
    assert cleaned == "Hey there! Great to meet you! 🎉"
    assert "<think>" not in cleaned

    scratchpad = "Thinking Process:\n1. User asked for advice.\n\nKeep up the great work! 🚀"
    assert ai_service.clean_ai_output(scratchpad) == "Keep up the great work! 🚀"


def test_build_messages_payload():
    """Verifies that conversation history is properly structured for the model."""
    history = [
        {"role": "user", "content": "can you use slang?"},
        {"role": "assistant", "content": "Sure, I can use casual phrases!"}
    ]
    payload = ai_service.build_messages_payload("system instruction", "cool, thanks!", history)
    assert len(payload) == 4
    assert payload[0]["role"] == "system"
    assert payload[1]["role"] == "user"
    assert payload[2]["role"] == "assistant"
    assert payload[3]["role"] == "user"
    assert payload[3]["content"] == "cool, thanks!"


def test_openai_never_leaks_reasoning():
    """Verifies that internal model reasoning is NEVER leaked to the user even if content is empty."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Internal scratchpad: user wants something."
                }
            }
        ]
    }

    with patch("ai_service.AI_API_KEY", "fake-key"), \
         patch("ai_service.AI_PROVIDER", "custom"), \
         patch("requests.post", return_value=mock_response):
        reply = ai_service.generate_response(user_id="U_TEST", user_message="test message")
        assert "Internal scratchpad" not in reply
        assert "I'm right here!" in reply


