"""Tests for goose/gateway/voice_client.py: message limit, breakers, junk, queue, slow mode, usage and secrets."""
from __future__ import annotations

import logging
import socket

import pytest

from goose.gateway.voice_client import VoiceClient

SECRET = "voice-secret-VALUE-9876"
HOUR = 3600


@pytest.fixture
def config(cfg):
    cfg.voice.enabled = True
    cfg.voice.url = "http://voice.test:8081"
    return cfg


class Fake:
    """Stands in for the HTTP post. Each call pops the next scripted outcome, or repeats the last one."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.hook = None

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        if self.hook:
            self.hook()
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Echo(Fake):
    """Answers every request with a valid response that carries the request's own id."""

    def __init__(self):
        super().__init__()

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        if self.hook:
            self.hook()
        return _ok(body["request_id"])


def _ok(request_id, tin=10, tout=5, action="speak"):
    return (200, {"v": 1, "request_id": request_id, "action": action, "text": "hi", "reaction": None,
                  "flags": {"roasted": False, "mentions_rank": False, "uses_german": False, "numbers_used": []},
                  "usage": {"tokens_in": tin, "tokens_out": tout}})


def _junk(request_id, tin=4, tout=2):
    return (502, {"error": "junk", "usage": {"tokens_in": tin, "tokens_out": tout}})


def _req(rid="r-1", kind="addressed"):
    return {"v": 1, "request_id": rid, "kind": kind, "mode": "chat" if kind == "addressed" else "event",
            "persona": {"humor_level": 2, "german_allowed": True, "roast_allowed": False},
            "card": {"v": 1}, "input": {"message": {"text": "hi"}, "thread": []} if kind == "addressed"
            else {"facts": {"band": "active"}}}


def _client(config, db, clock, post):
    return VoiceClient(config, db, clock, secret=SECRET, http_post=post)


class Mono:
    """A patchable monotonic clock for latency tests."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ----- message limit -------------------------------------------------------------------------------------------

def test_twelve_messages_an_hour_then_the_thirteenth_is_limited(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    for _ in range(12):
        assert c.allow_person("U1", clock.now()) == (True, "")
        c.record_person("U1", clock.now())
        clock.advance(5)
    assert c.allow_person("U1", clock.now()) == (False, "limit")


def test_the_limit_is_per_person(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    for _ in range(12):
        c.record_person("U1", clock.now())
        clock.advance(5)
    assert c.allow_person("U2", clock.now()) == (True, "")


def test_the_window_rolls_after_an_hour(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    start = clock.now()
    for _ in range(12):
        c.record_person("U1", clock.now())
        clock.advance(5)
    assert c.allow_person("U1", clock.now()) == (False, "limit")
    clock.set(start + HOUR + 1)
    assert c.allow_person("U1", clock.now()) == (True, "")


def test_five_second_gap_between_messages(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    c.record_person("U1", clock.now())
    clock.advance(2)
    assert c.allow_person("U1", clock.now()) == (False, "gap")
    clock.advance(3)
    assert c.allow_person("U1", clock.now()) == (True, "")


def test_allow_person_does_not_record(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    for _ in range(20):
        assert c.allow_person("U1", clock.now()) == (True, "")
    assert db.one("SELECT COUNT(*) AS n FROM voice_person")["n"] == 0


def test_limit_hits_are_counted_for_status(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("x")))
    for _ in range(12):
        c.record_person("U1", clock.now())
        clock.advance(5)
    c.allow_person("U1", clock.now())
    c.allow_person("U1", clock.now())
    assert c.status(clock.now())["limit_hits_last_hour"] == 2


# ----- kill switch and availability --------------------------------------------------------------------------

def test_disabled_by_config_and_runtime_switch(config, db, clock):
    config.voice.enabled = False
    post = Fake(_ok("r-1"))
    c = _client(config, db, clock, post)
    assert c.enabled() is False
    assert c.available(clock.now()) == (False, "disabled")
    assert post.calls == []
    result = c.call(_req())
    assert result.ok is False and result.reason == "disabled"


def test_runtime_switch_overrides_config_and_survives_a_new_client(config, db, clock):
    config.voice.enabled = False
    c = _client(config, db, clock, Fake(_ok("r-1")))
    c.set_enabled(True)
    assert c.enabled() is True
    assert _client(config, db, clock, Fake()).enabled() is True
    c.set_enabled(False)
    assert c.enabled() is False


def test_call_succeeds_and_strips_usage(config, db, clock):
    post = Fake(_ok("r-1", tin=10, tout=5))
    c = _client(config, db, clock, post)
    result = c.call(_req("r-1"), user_id="U1")
    assert result.ok is True
    assert result.reason == ""
    assert result.response["action"] == "speak"
    assert "usage" not in result.response
    assert (result.tokens_in, result.tokens_out) == (10, 5)
    assert post.calls[0]["url"] == "http://voice.test:8081/v1/voice"
    assert post.calls[0]["timeout"] == config.voice.timeout_s
    row = db.one("SELECT user_id, kind, action, ok, tokens_in, tokens_out, reason FROM voice_usage")
    assert (row["user_id"], row["kind"], row["action"], row["ok"], row["tokens_in"], row["tokens_out"]) == (
        "U1", "addressed", "speak", 1, 10, 5)
    assert c.tokens_today(clock.now()) == 15


def test_secret_goes_in_the_header_and_never_in_logs(config, db, clock, caplog):
    caplog.set_level(logging.DEBUG)
    post = Fake(socket.timeout("timed out " + SECRET), _ok("r-2"), _junk("r-3"))
    c = _client(config, db, clock, post)
    assert c.call(_req("r-1")).reason == "timeout"
    assert post.calls[0]["headers"]["X-Voice-Secret"] == SECRET
    c.call(_req("r-2"))
    c.call(_req("r-3"))
    text = "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
    assert SECRET not in text
    assert SECRET not in repr(c.status(clock.now()))


def test_timeout_is_handled_without_raising_and_is_not_retried(config, db, clock):
    post = Fake(socket.timeout("slow"), _ok("r-9"))
    c = _client(config, db, clock, post)
    result = c.call(_req("r-1"))
    assert result.ok is False
    assert result.reason == "timeout"
    assert len(post.calls) == 1


def test_network_error_is_handled_without_raising(config, db, clock):
    c = _client(config, db, clock, Fake(ConnectionRefusedError("refused")))
    result = c.call(_req())
    assert result.ok is False and result.reason == "network"


def test_late_replies_are_dropped(config, db, clock):
    mono = Mono()
    post = Fake(_ok("r-1"))

    def slow():
        mono.t += config.voice.timeout_s + 1

    post.hook = slow
    c = _client(config, db, clock, post)
    c._monotonic = mono
    result = c.call(_req("r-1"))
    assert result.ok is False and result.reason == "timeout"


def test_http_errors_map_to_reasons(config, db, clock):
    c = _client(config, db, clock, Fake((401, {"error": "unauthorized"})))
    assert c.call(_req()).reason == "http_401"
    c2 = _client(config, db, clock, Fake((502, {"error": "http_429"})))
    assert c2.call(_req()).reason == "http_429"


def test_server_reported_junk_is_junk(config, db, clock):
    c = _client(config, db, clock, Fake(_junk("r-1")))
    result = c.call(_req("r-1"))
    assert result.ok is False and result.reason == "junk"
    assert result.tokens_in == 4


def test_response_for_a_different_request_is_junk(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("other-id")))
    result = c.call(_req("r-1"))
    assert result.ok is False and result.reason == "junk"


# ----- circuit breaker -----------------------------------------------------------------------------------------

def test_breaker_opens_after_three_failures_then_probes_once_and_closes(config, db, clock):
    post = Fake((500, {"error": "boom"}), (500, {"error": "boom"}), (500, {"error": "boom"}), _ok("r-probe"))
    c = _client(config, db, clock, post)
    for i in range(3):
        assert c.call(_req("r-%d" % i)).ok is False
    assert c.available(clock.now()) == (False, "breaker_open")
    assert c.call(_req("blocked")).reason == "breaker_open"
    assert len(post.calls) == 3

    clock.advance(config.voice.breaker_open_s)
    assert c.available(clock.now()) == (True, "")

    seen = []
    post.hook = lambda: seen.append(c.available(clock.now()))
    result = c.call(_req("r-probe"))
    assert result.ok is True
    assert seen == [(False, "breaker_open")]   # while the probe is in flight, nothing else gets through
    assert c.status(clock.now())["breaker"] == "closed"
    assert c.available(clock.now()) == (True, "")


def test_failed_probe_opens_the_breaker_again(config, db, clock):
    post = Fake((500, {"error": "x"}), (500, {"error": "x"}), (500, {"error": "x"}), (500, {"error": "x"}))
    c = _client(config, db, clock, post)
    for i in range(3):
        c.call(_req("r-%d" % i))
    clock.advance(config.voice.breaker_open_s)
    assert c.call(_req("probe")).ok is False
    assert c.available(clock.now()) == (False, "breaker_open")
    clock.advance(config.voice.breaker_open_s)
    assert c.available(clock.now()) == (True, "")


def test_failures_outside_the_window_do_not_open_the_breaker(config, db, clock):
    post = Fake((500, {"error": "x"}), (500, {"error": "x"}), (500, {"error": "x"}))
    c = _client(config, db, clock, post)
    c.call(_req("r-0"))
    c.call(_req("r-1"))
    clock.advance(config.voice.breaker_window_s + 1)
    c.call(_req("r-2"))
    assert c.available(clock.now()) == (True, "")


def test_client_errors_do_not_trip_the_breaker(config, db, clock):
    post = Fake((400, {"error": "bad_request"}))
    c = _client(config, db, clock, post)
    for i in range(5):
        assert c.call(_req("r-%d" % i)).ok is False
    assert c.available(clock.now()) == (True, "")


# ----- queue ---------------------------------------------------------------------------------------------------

def test_queue_full_refuses_without_calling(config, db, clock):
    post = Fake(_ok("r-1"))
    c = _client(config, db, clock, post)
    c._active = config.voice.concurrency + config.voice.queue_size
    assert c.available(clock.now()) == (False, "queue_full")
    result = c.call(_req())
    assert result.ok is False and result.reason == "queue_full"
    assert post.calls == []
    c._active = 0
    assert c.call(_req("r-1")).ok is True


def test_concurrency_slots_are_released_after_each_call(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("a"), _ok("b"), socket.timeout("x"), _ok("c")))
    for i, rid in enumerate(["a", "b", "c", "d"]):
        c.call(_req(rid))
    assert c.status(clock.now())["inflight"] == 0


# ----- junk breaker --------------------------------------------------------------------------------------------

def test_junk_breaker_turns_the_voice_off_for_an_hour(config, db, clock):
    post = Fake(*[_ok("r-%d" % i) for i in range(10)])
    c = _client(config, db, clock, post)
    for i in range(10):
        assert c.call(_req("r-%d" % i)).ok is True
    for i in range(3):
        c.mark_junk("r-%d" % i)
    assert c.available(clock.now()) == (False, "junk_off")
    assert c.call(_req("later")).reason == "junk_off"
    clock.advance(config.voice.junk_off_s)
    assert c.available(clock.now()) == (True, "")


def test_junk_below_the_ratio_does_not_trip(config, db, clock):
    post = Fake(*[_ok("r-%d" % i) for i in range(20)])
    c = _client(config, db, clock, post)
    for i in range(20):
        c.call(_req("r-%d" % i))
    c.mark_junk("r-0")
    c.mark_junk("r-1")
    assert c.available(clock.now()) == (True, "")
    assert c.status(clock.now())["junk_ratio"] == 0.1


def test_server_junk_counts_toward_the_junk_breaker(config, db, clock):
    outcomes = [_ok("g-%d" % i) for i in range(7)] + [_junk("j-%d" % i) for i in range(3)]
    post = Fake(*outcomes)
    c = _client(config, db, clock, post)
    for i in range(7):
        c.call(_req("g-%d" % i))
    for i in range(3):
        c.call(_req("j-%d" % i))
    assert c.available(clock.now()) == (False, "junk_off")


def test_junk_ratio_in_status(config, db, clock):
    post = Fake(_ok("a"))
    c = _client(config, db, clock, post)
    assert c.status(clock.now())["junk_ratio"] == 0.0
    c.call(_req("a"))
    c.mark_junk("a")
    assert c.status(clock.now())["junk_ratio"] == 1.0


# ----- slow mode ------------------------------------------------------------------------------------------------

def test_slow_mode_sends_events_to_canned_and_lets_addressed_through(config, db, clock):
    mono = Mono()
    post = Echo()

    def slowish():
        mono.t += 3.5

    post.hook = slowish
    c = _client(config, db, clock, post)
    c._monotonic = mono
    for i in range(6):
        c.call(_req("r-%d" % i))
    assert c.status(clock.now())["p95_latency_s"] == 3.5
    assert c.available(clock.now()) == (True, "")
    clock.advance(config.voice.slow_for_s)
    assert c.status(clock.now())["slow"] is True
    assert c.available(clock.now()) == (True, "slow")
    assert c.available(clock.now(), "event") == (False, "slow")
    assert c.call(_req("ev", kind="event")).reason == "slow"
    assert c.call(_req("again")).ok is True


def test_fast_calls_clear_slow_mode(config, db, clock):
    mono = Mono()
    post = Echo()
    c = _client(config, db, clock, post)
    c._monotonic = mono
    c._latencies.extend([4.0] * 10)
    c._slow_since = clock.now()
    clock.advance(config.voice.slow_for_s)
    assert c.status(clock.now())["slow"] is True
    c._latencies.clear()
    c._latencies.extend([0.1] * 10)
    c.call(_req("fast"))
    assert c.status(clock.now())["slow"] is False


def test_p95_needs_enough_samples(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("a")))
    c._latencies.extend([9.0, 9.0])
    assert c.status(clock.now())["p95_latency_s"] is None


# ----- visibility ----------------------------------------------------------------------------------------------

def test_status_reports_calls_tokens_and_limits(config, db, clock):
    post = Fake(_ok("a", tin=100, tout=50), _ok("b", tin=1, tout=1))
    c = _client(config, db, clock, post)
    c.call(_req("a"), user_id="U1")
    c.call(_req("b"), user_id="U1")
    status = c.status(clock.now())
    assert status["enabled"] is True
    assert status["breaker"] == "closed"
    assert status["calls_today"] == 2
    assert status["tokens_today"] == 152
    assert status["calls_month"] == 2
    assert status["tokens_month"] == 152
    assert status["alert_tokens_per_day"] is None
    assert status["over_alert_tokens_per_day"] is False


def test_alert_threshold_is_visibility_only(config, db, clock):
    config.voice.alert_tokens_per_day = 10
    post = Fake(_ok("a", tin=6, tout=6))
    c = _client(config, db, clock, post)
    assert c.call(_req("a")).ok is True
    status = c.status(clock.now())
    assert status["over_alert_tokens_per_day"] is True
    assert c.available(clock.now()) == (True, "")


def test_usage_counts_only_the_current_day_for_today(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("a", tin=3, tout=3)))
    c.call(_req("a"))
    clock.advance(2 * 24 * HOUR)
    status = c.status(clock.now())
    assert status["tokens_today"] == 0
    assert status["tokens_month"] == 6


def test_tokens_today_matches_status(config, db, clock):
    c = _client(config, db, clock, Fake(_ok("a", tin=7, tout=8)))
    c.call(_req("a"))
    assert c.tokens_today(clock.now()) == 15 == c.status(clock.now())["tokens_today"]


def test_bad_request_type_is_refused_without_raising(config, db, clock):
    post = Fake(_ok("a"))
    c = _client(config, db, clock, post)
    result = c.call("not a dict")
    assert result.ok is False and result.reason == "bad_request"
    assert post.calls == []
