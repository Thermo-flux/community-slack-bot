"""Tests for goose/voice/provider.py: request shape, reason codes, key handling and the default HTTP path."""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from goose.voice.provider import Provider, ProviderError

KEY = "sk-test-KEY-123456789"
MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def _ok_body(text='{"action":"speak"}', prompt=11, completion=7):
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


class Recorder:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.result


def _provider(post=None, json_mode=False):
    return Provider("https://llm.example/v1/", KEY, "model-a", ["model-a", "model-b"], http_post=post,
                    json_mode=json_mode)


def test_missing_key_is_refused():
    with pytest.raises(ProviderError) as info:
        Provider("https://x", "", "model-a", ["model-a"])
    assert info.value.args[0] == "missing_key"


def test_model_not_on_allowlist_is_refused_and_empty_allowlist_too():
    with pytest.raises(ProviderError) as info:
        Provider("https://x", KEY, "model-z", ["model-a"])
    assert info.value.args[0] == "model_not_allowed"
    with pytest.raises(ProviderError) as info:
        Provider("https://x", KEY, "model-a", [])
    assert info.value.args[0] == "model_not_allowed"


def test_from_env_reads_the_settings_and_the_allowlist():
    env = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": KEY, "LLM_MODEL": "model-b",
           "LLM_MODEL_ALLOWLIST": "model-a, model-b ,"}
    p = Provider.from_env(env)
    assert p.model == "model-b"
    assert p.allowlist == ["model-a", "model-b"]
    assert p.base_url == "https://llm.example/v1"


def test_from_env_without_allowlist_refuses_to_start():
    with pytest.raises(ProviderError) as info:
        Provider.from_env({"LLM_API_KEY": KEY, "LLM_MODEL": "model-a"})
    assert info.value.args[0] == "model_not_allowed"


def test_complete_posts_the_chat_request_with_the_key_only_in_the_header():
    rec = Recorder(result=(200, _ok_body('{"action":"silent"}', 11, 7)))
    text, tin, tout = _provider(rec).complete(MESSAGES, max_tokens=100, timeout=4.0)
    assert (text, tin, tout) == ('{"action":"silent"}', 11, 7)
    call = rec.calls[0]
    assert call["url"] == "https://llm.example/v1/chat/completions"
    assert call["timeout"] == 4.0
    assert call["headers"]["Authorization"] == "Bearer " + KEY
    body = call["body"]
    assert body["model"] == "model-a"
    assert body["messages"] == MESSAGES
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.8
    assert body["stream"] is False
    assert "response_format" not in body
    assert KEY not in json.dumps(body)


def test_json_mode_is_off_by_default_and_on_when_asked():
    rec = Recorder(result=(200, _ok_body()))
    _provider(rec).complete(MESSAGES, max_tokens=10)
    assert "response_format" not in rec.calls[0]["body"]
    rec2 = Recorder(result=(200, _ok_body()))
    _provider(rec2, json_mode=True).complete(MESSAGES, max_tokens=10)
    assert rec2.calls[0]["body"]["response_format"] == {"type": "json_object"}


def test_http_error_becomes_a_reason_code_without_key_or_body():
    rec = Recorder(result=(429, {"error": {"message": "slow down " + KEY}}))
    with pytest.raises(ProviderError) as info:
        _provider(rec).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "http_429"
    assert KEY not in str(info.value)


def test_timeout_and_network_errors_are_classified():
    with pytest.raises(ProviderError) as info:
        _provider(Recorder(error=socket.timeout("read timed out " + KEY))).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "timeout"
    with pytest.raises(ProviderError) as info:
        _provider(Recorder(error=urllib.error.URLError(socket.timeout()))).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "timeout"
    with pytest.raises(ProviderError) as info:
        _provider(Recorder(error=ConnectionResetError("reset " + KEY))).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "network"
    assert KEY not in str(info.value)


def test_missing_content_is_bad_json():
    with pytest.raises(ProviderError) as info:
        _provider(Recorder(result=(200, {"choices": []}))).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "bad_json"
    with pytest.raises(ProviderError) as info:
        _provider(Recorder(result=(200, {"choices": [{"message": {"content": None}}]}))).complete(MESSAGES, max_tokens=10)
    assert info.value.args[0] == "bad_json"


def test_missing_usage_counts_as_zero_tokens():
    body = {"choices": [{"message": {"content": "x"}}]}
    assert _provider(Recorder(result=(200, body))).complete(MESSAGES, max_tokens=10) == ("x", 0, 0)


def test_repr_never_shows_the_key():
    assert KEY not in repr(_provider(Recorder(result=(200, _ok_body()))))


@pytest.mark.parametrize("url", ["http://llm.example/v1", "ftp://llm.example/v1", "llm.example/v1",
                                 "http://10.0.0.5:8080/v1", "http://localhost.evil.example/v1"])
def test_insecure_base_url_is_refused_at_construction(url):
    with pytest.raises(ProviderError) as info:
        Provider(url, KEY, "model-a", ["model-a"])
    assert info.value.args[0] == "insecure_url"
    assert KEY not in str(info.value)


@pytest.mark.parametrize("url", ["http://localhost:8080/v1", "http://127.0.0.1:9/v1", "https://llm.example/v1"])
def test_https_and_local_http_are_accepted(url):
    assert Provider(url, KEY, "model-a", ["model-a"]).base_url == url


def test_from_env_refuses_a_plain_http_remote_url():
    with pytest.raises(ProviderError) as info:
        Provider.from_env({"LLM_BASE_URL": "http://llm.example/v1", "LLM_API_KEY": KEY, "LLM_MODEL": "model-a",
                           "LLM_MODEL_ALLOWLIST": "model-a"})
    assert info.value.args[0] == "insecure_url"


class _Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") != "Bearer " + KEY:
            code, payload = 401, {"error": "no"}
        elif "/rate/" in self.path:
            code, payload = 429, {"error": "slow"}
        else:
            code, payload = 200, _ok_body("hello", 3, 4)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_default_http_path_against_a_local_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d/v1" % server.server_address[1]
        p = Provider(base, KEY, "model-a", ["model-a"])
        assert p.complete(MESSAGES, max_tokens=5, timeout=2.0) == ("hello", 3, 4)
        bad = Provider(base + "/rate", KEY, "model-a", ["model-a"])
        with pytest.raises(ProviderError) as info:
            bad.complete(MESSAGES, max_tokens=5, timeout=2.0)
        assert info.value.args[0] == "http_429"
        wrong = Provider(base, "other-key", "model-a", ["model-a"])
        with pytest.raises(ProviderError) as info:
            wrong.complete(MESSAGES, max_tokens=5, timeout=2.0)
        assert info.value.args[0] == "http_401"
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------------- universal formats and caching

from goose.voice.prompt import build_parts, STATIC_PREFIX  # noqa: E402

PARTS = {"static": STATIC_PREFIX, "dynamic": "REQUEST\nmode: chat", "user": "hi"}
MSGS = [{"role": "system", "content": STATIC_PREFIX + "\nREQUEST\nmode: chat"}, {"role": "user", "content": "hi"}]


class Capture:
    def __init__(self, body):
        self.body = body
        self.url = None
        self.headers = None
        self.sent = None

    def __call__(self, url, body, headers, timeout):
        self.url, self.sent, self.headers = url, body, headers
        return 200, self.body


def _anthropic_body(**usage):
    base = {"content": [{"type": "text", "text": '{"action":"speak"}'}],
            "usage": {"input_tokens": 0, "output_tokens": 9}}
    base["usage"].update(usage)
    return base


def test_anthropic_format_marks_only_the_static_block_for_caching():
    cap = Capture(_anthropic_body())
    p = Provider("https://api.anthropic.com/v1", "k", "m", ["m"], http_post=cap, api_format="anthropic")
    p.complete_usage(MSGS, max_tokens=100, parts=PARTS)
    system = cap.sent["system"]
    assert system[0] == {"type": "text", "text": STATIC_PREFIX, "cache_control": {"type": "ephemeral"}}
    assert system[1] == {"type": "text", "text": "REQUEST\nmode: chat"}      # per-request facts are not cached
    assert cap.url == "https://api.anthropic.com/v1/messages"
    assert cap.headers["x-api-key"] == "k" and cap.headers["anthropic-version"]
    assert "Authorization" not in cap.headers
    assert cap.sent["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_cache_off_sends_no_cache_marker():
    cap = Capture(_anthropic_body())
    p = Provider("https://api.anthropic.com/v1", "k", "m", ["m"], http_post=cap, api_format="anthropic", cache=False)
    p.complete_usage(MSGS, max_tokens=100, parts=PARTS)
    assert all("cache_control" not in block for block in cap.sent["system"])


def test_anthropic_usage_adds_cache_reads_and_writes_into_the_input_total():
    cap = Capture(_anthropic_body(input_tokens=40, cache_read_input_tokens=900, cache_creation_input_tokens=0))
    p = Provider("https://api.anthropic.com/v1", "k", "m", ["m"], http_post=cap, api_format="anthropic")
    u = p.complete_usage(MSGS, max_tokens=100, parts=PARTS)
    assert (u.tokens_in, u.tokens_out, u.cached_in) == (940, 9, 900)


def test_openai_format_sends_the_same_messages_with_no_cache_marker():
    cap = Capture({"choices": [{"message": {"content": "ok"}}],
                   "usage": {"prompt_tokens": 950, "completion_tokens": 4,
                             "prompt_tokens_details": {"cached_tokens": 800}}})
    p = Provider("https://api.openai.com/v1", "k", "m", ["m"], http_post=cap)
    u = p.complete_usage(MSGS, max_tokens=100, parts=PARTS)
    assert cap.url == "https://api.openai.com/v1/chat/completions"
    assert cap.sent["messages"] == MSGS
    assert "cache_control" not in str(cap.sent)
    assert (u.tokens_in, u.tokens_out, u.cached_in) == (950, 4, 800)


def test_openai_format_without_cache_details_counts_zero_cached():
    cap = Capture({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    u = Provider("https://api.example.com/v1", "k", "m", ["m"], http_post=cap).complete_usage(MSGS, max_tokens=10)
    assert u.cached_in == 0


def test_anthropic_response_without_a_text_block_is_bad_json():
    cap = Capture({"content": [{"type": "tool_use"}], "usage": {}})
    p = Provider("https://api.anthropic.com/v1", "k", "m", ["m"], http_post=cap, api_format="anthropic")
    with pytest.raises(ProviderError, match="bad_json"):
        p.complete_usage(MSGS, max_tokens=10, parts=PARTS)


def test_unknown_format_is_refused():
    with pytest.raises(ProviderError, match="bad_format"):
        Provider("https://x.example/v1", "k", "m", ["m"], api_format="gemini-native")


def test_default_base_url_follows_the_format_and_env_selects_it():
    p = Provider("", "k", "m", ["m"], api_format="anthropic")
    assert p.base_url == "https://api.anthropic.com/v1"
    env = {"LLM_API_FORMAT": "anthropic", "LLM_API_KEY": "k", "LLM_MODEL": "m", "LLM_MODEL_ALLOWLIST": "m",
           "LLM_CACHE": "off"}
    q = Provider.from_env(env)
    assert q.api_format == "anthropic" and q.cache is False and q.base_url == "https://api.anthropic.com/v1"
    assert Provider.from_env({"LLM_API_KEY": "k", "LLM_MODEL": "m", "LLM_MODEL_ALLOWLIST": "m"}).api_format == "openai"


def test_static_prefix_is_the_same_bytes_on_every_request():
    a = build_parts({"kind": "addressed", "mode": "chat", "card": {"you": {"pond_level": 3}}, "persona": {}})
    b = build_parts({"kind": "addressed", "mode": "chat", "card": {"you": {"pond_level": 9}}, "persona": {}})
    assert a["static"] == b["static"] == STATIC_PREFIX
    assert a["dynamic"] != b["dynamic"]
