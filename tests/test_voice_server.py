"""Tests for goose/voice/server.py: contract validation, output parsing, HTTP paths and the voice import boundary."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import urllib.error
import urllib.request

import pytest

from goose.voice.provider import ProviderError, Usage
from goose.voice.server import (REPLAY_WINDOW_S, ReplayGuard, handle_request, make_server, sign_request)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_DIR = os.path.join(ROOT, "goose", "voice")
SECRET = "shared-secret-for-tests-0123"
START = 1_760_000_000
NOW = [float(START)]          # the server's clock in these tests; tests move it with NOW[0] = ...

GOOD_OUTPUT = {"action": "speak", "text": "Short answer: yes. The duck is sure.", "reaction": "duck",
               "flags": {"roasted": False, "mentions_rank": False, "uses_german": False, "numbers_used": []}}


class FakeProvider:
    """Stands in for the provider. `reply` is the model text; `error` makes complete() raise."""

    model = "fake-model"

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.seen = []

    def complete_usage(self, messages, max_tokens, timeout=4.0, parts=None):
        self.seen.append((messages, max_tokens, timeout))
        if self.error:
            raise ProviderError(self.error)
        return Usage(text=self.reply, tokens_in=21, tokens_out=9, cached_in=0)


def _text(obj):
    return obj if isinstance(obj, str) else json.dumps(obj)


def _request(**overrides):
    req = {
        "v": 1,
        "request_id": "abc-123",
        "kind": "addressed",
        "mode": "chat",
        "persona": {"humor_level": 2, "german_allowed": True, "roast_allowed": False, "max_chars": 300},
        "card": {"v": 1, "version": 0, "you": {"pond_level": 2, "activity_band": "steady"}},
        "input": {"message": {"handle": "you", "text": "who is Bruce Lee?"}, "thread": []},
    }
    req.update(overrides)
    return req


def _event_request():
    return {
        "v": 1, "request_id": "ev-1", "kind": "event", "mode": "event",
        "persona": {"humor_level": 2, "german_allowed": False, "roast_allowed": False},
        "card": {"v": 1, "you": {"pond_level": 4}},
        "input": {"facts": {"band": "very_active", "streak": 4}},
    }


# ----- success and parsing -------------------------------------------------------------------------------------

def test_valid_request_returns_the_response_with_version_and_request_id():
    provider = FakeProvider(reply=_text(GOOD_OUTPUT))
    status, body = handle_request(_request(), provider)
    assert status == 200
    assert body["v"] == 1
    assert body["request_id"] == "abc-123"
    assert body["action"] == "speak"
    assert body["text"] == GOOD_OUTPUT["text"]
    assert body["reaction"] == "duck"
    assert body["flags"]["numbers_used"] == []
    assert body["usage"] == {"tokens_in": 21, "tokens_out": 9, "cached_in": 0}


def test_fenced_json_block_is_accepted():
    fenced = "```json\n" + json.dumps(GOOD_OUTPUT) + "\n```"
    status, body = handle_request(_request(), FakeProvider(reply=fenced))
    assert status == 200
    assert body["action"] == "speak"


def test_prose_before_the_json_is_junk():
    status, body = handle_request(_request(), FakeProvider(reply="Sure! " + json.dumps(GOOD_OUTPUT)))
    assert status == 502
    assert body["error"] == "junk"
    assert "Sure" not in json.dumps(body)


def test_two_objects_are_junk_and_so_is_not_json():
    assert handle_request(_request(), FakeProvider(reply=json.dumps(GOOD_OUTPUT) * 2))[0] == 502
    assert handle_request(_request(), FakeProvider(reply="just words"))[0] == 502
    assert handle_request(_request(), FakeProvider(reply="[1,2]"))[0] == 502


def test_text_over_the_cap_is_junk_and_elaborate_allows_700():
    too_long = dict(GOOD_OUTPUT, text="x" * 301)
    assert handle_request(_request(), FakeProvider(reply=_text(too_long)))[0] == 502
    elaborate = _request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": False,
                                  "max_chars": 700, "elaborate": True})
    assert handle_request(elaborate, FakeProvider(reply=_text(dict(GOOD_OUTPUT, text="x" * 500))))[0] == 200
    assert handle_request(elaborate, FakeProvider(reply=_text(dict(GOOD_OUTPUT, text="x" * 701))))[0] == 502


def test_max_chars_in_the_persona_lowers_the_cap():
    req = _request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": False, "max_chars": 80})
    assert handle_request(req, FakeProvider(reply=_text(dict(GOOD_OUTPUT, text="x" * 81))))[0] == 502
    assert handle_request(req, FakeProvider(reply=_text(dict(GOOD_OUTPUT, text="x" * 80))))[0] == 200


def test_output_shape_is_strict():
    cases = [
        dict(GOOD_OUTPUT, extra="nope"),
        {k: v for k, v in GOOD_OUTPUT.items() if k != "text"},
        dict(GOOD_OUTPUT, action="explode"),
        dict(GOOD_OUTPUT, reaction="star"),
        dict(GOOD_OUTPUT, reaction="white_check_mark"),
        dict(GOOD_OUTPUT, flags={"roasted": "yes", "mentions_rank": False, "uses_german": False, "numbers_used": []}),
        dict(GOOD_OUTPUT, flags={"roasted": False, "unknown": True}),
        dict(GOOD_OUTPUT, flags={"numbers_used": "7"}),
        dict(GOOD_OUTPUT, v=2),
        dict(GOOD_OUTPUT, request_id="someone-else"),
        dict(GOOD_OUTPUT, text="   "),
    ]
    for case in cases:
        assert handle_request(_request(), FakeProvider(reply=_text(case)))[0] == 502, case


def test_null_reaction_and_silent_action_are_fine():
    silent = {"action": "silent", "text": "", "reaction": None,
              "flags": {"roasted": False, "mentions_rank": False, "uses_german": False, "numbers_used": []}}
    status, body = handle_request(_request(), FakeProvider(reply=_text(silent)))
    assert status == 200 and body["action"] == "silent" and body["reaction"] is None


def test_numbers_used_list_is_validated_and_stringified():
    out = copy.deepcopy(GOOD_OUTPUT)
    out["flags"]["numbers_used"] = [7, "1940"]
    status, body = handle_request(_request(), FakeProvider(reply=_text(out)))
    assert status == 200
    assert body["flags"]["numbers_used"] == ["7", "1940"]


def test_provider_errors_become_reason_codes():
    for code in ("http_429", "timeout", "bad_json", "network"):
        status, body = handle_request(_request(), FakeProvider(error=code))
        assert status == 502
        assert body == {"error": code}


def test_provider_error_detail_is_never_echoed():
    status, body = handle_request(_request(), FakeProvider(error="http 500 Authorization secret-ish text"))
    assert status == 502
    assert body["error"] == "network"


def test_bad_requests_are_rejected_with_400():
    bad = [
        _request(v=2),
        _request(request_id=""),
        _request(request_id="x" * 65),
        _request(kind="ambient"),
        _request(extra_top="x"),
        _request(persona="nope"),
        _request(persona={"humor_level": 2, "german_allowed": True}),
        _request(persona={"humor_level": 9, "german_allowed": True, "roast_allowed": False}),
        _request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": False, "name": "x"}),
        _request(card=[]),
        _request(input={"message": {"text": "x" * 501}, "thread": []}),
        _request(input={"message": {"text": "ok"}, "thread": ["x"] * 7}),
        _request(input={"message": {"text": "ok"}, "thread": ["x" * 201]}),
        _request(input={"message": {"text": "ok"}, "thread": ["x" * 200] * 6 + ["y"]}),
        _request(input={"message": {"text": "ok"}, "thread": [], "names": ["sam"]}),
        _request(mode="ambient"),
    ]
    for body in bad:
        provider = FakeProvider(reply=_text(GOOD_OUTPUT))
        status, payload = handle_request(body, provider)
        assert status == 400, body
        assert payload["error"] == "bad_request"
        assert provider.seen == []


def test_thread_total_of_1200_is_the_limit():
    ok = _request(input={"message": {"text": "ok"}, "thread": ["x" * 200] * 6})
    assert handle_request(ok, FakeProvider(reply=_text(GOOD_OUTPUT)))[0] == 200


def test_event_requests_carry_facts_only():
    status, body = handle_request(_event_request(), FakeProvider(reply=_text(GOOD_OUTPUT)))
    assert status == 200
    bad = _event_request()
    bad["input"]["message"] = {"text": "a chat line must never reach an event"}
    assert handle_request(bad, FakeProvider(reply=_text(GOOD_OUTPUT)))[0] == 400
    bad_fact = _event_request()
    bad_fact["input"]["facts"] = {"name": "x" * 41}
    assert handle_request(bad_fact, FakeProvider(reply=_text(GOOD_OUTPUT)))[0] == 400
    bad_key = _event_request()
    bad_key["input"]["facts"] = {"Bad Key!": 1}
    assert handle_request(bad_key, FakeProvider(reply=_text(GOOD_OUTPUT)))[0] == 400


def test_the_prompt_and_the_user_text_reach_the_provider_redacted():
    provider = FakeProvider(reply=_text(GOOD_OUTPUT))
    req = _request(input={"message": {"text": "ping <@U12345678|sam>"}, "thread": ["U12345678 said hi"]})
    handle_request(req, provider)
    messages, max_tokens, timeout = provider.seen[0]
    joined = "\n".join(m["content"] for m in messages)
    assert "U12345678" not in joined
    assert max_tokens == 100
    assert timeout == 4.0


# ----- HTTP ----------------------------------------------------------------------------------------------------

@pytest.fixture
def running():
    NOW[0] = float(START)
    provider = FakeProvider(reply=_text(GOOD_OUTPUT))
    server = make_server("127.0.0.1", 0, provider, SECRET, clock=lambda: NOW[0])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    yield base, provider
    server.shutdown()
    server.server_close()


def _post(base, body, secret=SECRET, raw=None, headers=None, ts=None, sign=True):
    """Signs the exact bytes sent, with the timestamp `ts` (default: the test clock). secret=None sends no signature."""
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + "/v1/voice", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if sign and secret is not None:
        stamp = int(NOW[0]) if ts is None else int(ts)
        req.add_header("X-Goose-Timestamp", str(stamp))
        req.add_header("X-Goose-Signature", sign_request(secret, stamp, data))
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_health_reports_ok_and_model_without_secrets(running):
    base, _ = running
    with urllib.request.urlopen(base + "/health", timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload == {"ok": True, "model": "fake-model"}
    assert SECRET not in json.dumps(payload)


def test_post_without_or_with_wrong_secret_is_401(running):
    base, provider = running
    assert _post(base, _request(), secret=None)[0] == 401
    assert _post(base, _request(), secret="wrong")[0] == 401
    assert provider.seen == []


def test_the_old_plain_secret_header_is_no_longer_accepted(running):
    base, provider = running
    status, body = _post(base, _request(), secret=None, headers={"X-Voice-Secret": SECRET})
    assert status == 401 and body == {"error": "unauthorized"}
    assert provider.seen == []


def test_a_signature_over_different_bytes_is_401(running):
    base, provider = running
    signed = json.dumps(_request()).encode("utf-8")
    sent = json.dumps(_request(request_id="changed-id")).encode("utf-8")
    status, _ = _post(base, None, raw=sent, sign=False, headers={
        "X-Goose-Timestamp": str(START),
        "X-Goose-Signature": sign_request(SECRET, START, signed),
    })
    assert status == 401
    assert provider.seen == []


def test_stale_or_future_timestamps_are_401_and_60_seconds_is_fine(running):
    base, provider = running
    assert _post(base, _request(request_id="t-1"), ts=START - 61)[0] == 401
    assert _post(base, _request(request_id="t-2"), ts=START + 61)[0] == 401
    assert _post(base, _request(request_id="t-3"), ts=START - 60)[0] == 200
    assert _post(base, _request(request_id="t-4"), ts=START + 60)[0] == 200
    assert len(provider.seen) == 2


def test_non_integer_timestamp_is_401(running):
    base, _ = running
    data = json.dumps(_request(request_id="n-1")).encode("utf-8")
    status, _ = _post(base, None, raw=data, sign=False, headers={
        "X-Goose-Timestamp": "%d.5" % START,
        "X-Goose-Signature": sign_request(SECRET, START, data),
    })
    assert status == 401


def test_replayed_request_id_inside_five_minutes_is_400(running):
    base, provider = running
    assert _post(base, _request(request_id="dup-1"))[0] == 200
    NOW[0] += 30
    status, body = _post(base, _request(request_id="dup-1"))
    assert status == 400
    assert body["error"] == "bad_request"
    assert len(provider.seen) == 1


def test_request_id_is_accepted_again_after_the_window(running):
    base, provider = running
    assert _post(base, _request(request_id="again-1"))[0] == 200
    NOW[0] += REPLAY_WINDOW_S + 1
    assert _post(base, _request(request_id="again-1"))[0] == 200
    assert len(provider.seen) == 2


def test_replay_guard_is_bounded_and_prunes_old_ids():
    guard = ReplayGuard(window_s=300, max_ids=3)
    assert guard.admit("a", 0.0) and guard.admit("b", 1.0) and guard.admit("c", 2.0)
    assert not guard.admit("a", 3.0)
    assert guard.admit("d", 4.0)               # full: the oldest ("a") is dropped to make room
    assert guard.admit("a", 5.0)               # so "a" is admitted again (it was evicted, the window is still open)
    assert len(guard._seen) <= 3
    assert guard.admit("e", 400.0)             # everything older than 300 seconds is pruned on this request
    assert guard._seen.keys() == {"e"}


def test_replay_guard_refuses_a_repeat_in_the_window_only():
    guard = ReplayGuard()
    assert guard.admit("x", 100.0)
    assert not guard.admit("x", 100.0 + REPLAY_WINDOW_S - 1)
    assert guard.admit("x", 100.0 + REPLAY_WINDOW_S + 1)


def test_post_with_bad_json_is_400(running):
    base, _ = running
    status, body = _post(base, None, raw=b"{not json")
    assert status == 400
    assert body["error"] == "bad_request"


def test_post_with_unknown_top_level_key_is_400(running):
    base, _ = running
    status, body = _post(base, _request(surprise=True))
    assert status == 400


def test_post_valid_request_is_200(running):
    base, _ = running
    status, body = _post(base, _request())
    assert status == 200
    assert body["request_id"] == "abc-123"
    assert body["text"] == GOOD_OUTPUT["text"]


def test_post_provider_failure_is_502_with_reason(running):
    base, provider = running
    provider.error = "http_429"
    status, body = _post(base, _request())
    assert status == 502
    assert body == {"error": "http_429"}


def test_post_junk_is_502(running):
    base, provider = running
    provider.reply = "not json at all"
    status, body = _post(base, _request())
    assert status == 502
    assert body["error"] == "junk"


def test_unknown_paths_are_404(running):
    base, _ = running
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(base + "/nope", timeout=5)
    assert info.value.code == 404


def test_missing_content_length_is_400(running):
    base, _ = running
    status, body = _post(base, None, raw=b"")
    assert status == 400


def test_make_server_requires_nothing_else_than_provider_and_secret():
    server = make_server("127.0.0.1", 0, FakeProvider(reply="{}"), "s")
    try:
        assert server.server_address[1] > 0
    finally:
        server.server_close()


# ----- import boundary -----------------------------------------------------------------------------------------

FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(goose\.core|goose\.gateway|slack|slack_bolt|sqlite3)(?:\.|\s|$)",
                       re.MULTILINE)


def test_voice_package_imports_only_the_standard_library_and_itself():
    sources = [n for n in os.listdir(VOICE_DIR) if n.endswith(".py")]
    assert {"prompt.py", "provider.py", "server.py"}.issubset(sources)
    for name in sources:
        with open(os.path.join(VOICE_DIR, name), encoding="utf-8") as fh:
            text = fh.read()
        assert FORBIDDEN.search(text) is None, "forbidden import in goose/voice/%s" % name
