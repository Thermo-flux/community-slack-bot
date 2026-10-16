"""The gateway's client for the voice service. OWNER: agent D (voice). Behavior: docs/PLAN.md sections 10.4 and 10.8.

There are NO spend caps. Controls are: the per-person message limit, reply length, timeout, queue, circuit breaker,
junk breaker, slow mode and the kill switch. The core never awaits this: it is only called from worker threads.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.types import VoiceResult

log = logging.getLogger("goose.voice_client")

HOUR_S = 3600
LATENCY_WINDOW = 50
MIN_LATENCY_SAMPLE = 5
MIN_JUNK_SAMPLE = 10
BREAKER_FAILURE_REASONS = ("timeout", "network", "junk")
_CODE = re.compile(r"^[a-z_0-9]{1,24}$")

PostFn = Callable[[str, Dict[str, Any], Dict[str, str], float], Tuple[int, Dict[str, Any]]]


def _default_post(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = int(e.code)
        try:
            raw = e.read()
        except Exception:
            raw = b""
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class VoiceClient:
    def __init__(self, config: Config, db: Database, clock: Clock, secret: str = "",
                 http_post: Optional[PostFn] = None):
        """http_post(url, json_body, headers, timeout) -> (status, json) is injectable for tests; the default uses
        urllib.request. The shared secret goes in the header X-Voice-Secret and is never logged."""
        self._config = config
        self._db = db
        self._clock = clock
        self._secret = secret or ""
        self._http_post: PostFn = http_post or _default_post
        self._monotonic: Callable[[], float] = time.monotonic   # patchable in tests
        self._lock = threading.RLock()
        self._semaphore = threading.BoundedSemaphore(max(1, config.voice.concurrency))
        self._active = 0                                          # admitted calls: running plus queued
        self._breaker = "closed"                                  # closed | open | half_open
        self._opened_at = 0.0
        self._failures: Deque[float] = collections.deque()
        self._probe_inflight = False
        self._junk_off_until = 0.0
        self._outputs: Deque[Tuple[str, bool]] = collections.deque(maxlen=max(1, config.voice.junk_window))
        self._latencies: Deque[float] = collections.deque(maxlen=LATENCY_WINDOW)
        self._slow_since: Optional[float] = None
        self._limit_hits: Deque[float] = collections.deque()

    # ----- kill switch -------------------------------------------------------------------------------------------

    def enabled(self) -> bool:
        """voice.enabled in config, overridable at runtime by set_enabled (stored in settings 'voice_enabled')."""
        value = self._db.get_setting("voice_enabled", "")
        if value == "":
            return bool(self._config.voice.enabled)
        return value == "1"

    def set_enabled(self, value: bool) -> None:
        self._db.set_setting("voice_enabled", "1" if value else "0")

    # ----- message limit -----------------------------------------------------------------------------------------

    def allow_person(self, user_id: str, now: float) -> Tuple[bool, str]:
        """The message limit: at most config.voice.messages_per_person_per_hour addressed messages per person per hour
        (table voice_person) and at least person_gap_s between them. Returns (True, "") or (False, "limit"|"gap").
        Does NOT record; call record_person for a message that really goes to the voice."""
        voice = self._config.voice
        rows = self._db.query(
            "SELECT ts FROM voice_person WHERE user_id=? AND ts>? ORDER BY ts", (user_id, now - HOUR_S))
        if len(rows) >= voice.messages_per_person_per_hour:
            with self._lock:
                self._limit_hits.append(now)
            return False, "limit"
        if rows and now - float(rows[-1]["ts"]) < voice.person_gap_s:
            return False, "gap"
        return True, ""

    def record_person(self, user_id: str, now: float) -> None:
        self._db.execute("INSERT INTO voice_person(user_id, ts) VALUES(?,?)", (user_id, now))
        self._db.execute("DELETE FROM voice_person WHERE ts <= ?", (now - HOUR_S,))

    # ----- availability ------------------------------------------------------------------------------------------

    def _capacity(self) -> int:
        return max(1, self._config.voice.concurrency) + max(0, self._config.voice.queue_size)

    def _state_locked(self, now: float) -> str:
        if self._breaker == "open" and now - self._opened_at >= self._config.voice.breaker_open_s:
            return "half_open"
        return self._breaker

    def _slow_locked(self, now: float) -> bool:
        return self._slow_since is not None and now - self._slow_since >= self._config.voice.slow_for_s

    def _p95_locked(self) -> Optional[float]:
        if len(self._latencies) < MIN_LATENCY_SAMPLE:
            return None
        values = sorted(self._latencies)
        index = max(0, int(math.ceil(0.95 * len(values))) - 1)
        return values[index]

    def _check_locked(self, now: float, kind: str) -> Tuple[bool, str, bool]:
        """(ok, reason, is_probe). Caller holds the lock. Pure apart from reading the kill switch."""
        if not self.enabled():
            return False, "disabled", False
        if now < self._junk_off_until:
            return False, "junk_off", False
        state = self._state_locked(now)
        probe = False
        if state == "open":
            return False, "breaker_open", False
        if state == "half_open":
            if self._probe_inflight:
                return False, "breaker_open", False
            probe = True
        if self._active >= self._capacity():
            return False, "queue_full", False
        if self._slow_locked(now):
            if kind == "event":
                return False, "slow", False
            return True, "slow", probe
        return True, "", probe

    def available(self, now: float, kind: str = "addressed") -> Tuple[bool, str]:
        """(True, "") or (False, reason) with reason in disabled | breaker_open | junk_off | queue_full.
        Slow mode returns (True, "slow") for addressed use and (False, "slow") for kind "event"."""
        with self._lock:
            ok, reason, _probe = self._check_locked(now, kind)
        return ok, reason

    # ----- the call ----------------------------------------------------------------------------------------------

    def call(self, request: Dict[str, Any], user_id: str = "") -> VoiceResult:
        """POST the request to {voice.url}/v1/voice with config.voice.timeout_s, no retries. Enforces the queue
        (concurrency 2, queue 5), breaker (3 failures in 5 minutes -> open 15 minutes, then one probe), junk breaker,
        slow mode (events become canned; addressed still tries). Writes voice_usage. Never raises: returns
        VoiceResult(ok=False, reason=...) on any problem. Late replies are dropped."""
        if not isinstance(request, dict):
            return VoiceResult(ok=False, reason="bad_request")
        now = self._clock.now()
        kind = str(request.get("kind") or "addressed")
        with self._lock:
            ok, reason, probe = self._check_locked(now, kind)
            if not ok:
                return VoiceResult(ok=False, reason=reason)
            if probe:
                self._probe_inflight = True
                self._breaker = "half_open"
            self._active += 1
        try:
            if not self._semaphore.acquire(timeout=float(self._config.voice.timeout_s)):
                return VoiceResult(ok=False, reason="queue_full")
            try:
                return self._attempt(request, user_id, kind, probe)
            finally:
                self._semaphore.release()
        except Exception:  # never raise into a worker: reason code only
            log.warning("voice call failed reason=internal")
            return VoiceResult(ok=False, reason="internal")
        finally:
            with self._lock:
                self._active -= 1
                if probe:
                    self._probe_inflight = False

    def _attempt(self, request: Dict[str, Any], user_id: str, kind: str, probe: bool) -> VoiceResult:
        voice = self._config.voice
        rid = str(request.get("request_id") or "")
        url = voice.url.rstrip("/") + "/v1/voice"
        headers = {"Content-Type": "application/json", "X-Voice-Secret": self._secret}
        started = self._monotonic()
        status: Optional[int] = None
        data: Any = None
        error = ""
        try:
            status, data = self._http_post(url, request, headers, float(voice.timeout_s))
        except Exception as exc:  # classified by kind only; the message is never logged
            error = "timeout" if _is_timeout(exc) else "network"
        latency = max(0.0, self._monotonic() - started)
        if not error and latency > float(voice.timeout_s):
            error = "timeout"   # a late reply is dropped

        response: Optional[Dict[str, Any]] = None
        action = ""
        junk = False
        tokens_in = 0
        tokens_out = 0
        if not isinstance(data, dict):
            data = {}
        data = dict(data)
        usage = data.pop("usage", None)
        if isinstance(usage, dict):
            tokens_in = usage.get("tokens_in") if _is_int(usage.get("tokens_in")) else 0
            tokens_out = usage.get("tokens_out") if _is_int(usage.get("tokens_out")) else 0
        if not error:
            if status == 200 and data.get("v") == 1 and data.get("request_id") == rid and isinstance(data.get("action"), str):
                response = data
                action = data["action"]
            elif status == 502 and data.get("error") == "junk":
                error, junk = "junk", True
            elif status == 502 and isinstance(data.get("error"), str) and _CODE.match(data["error"]):
                error = str(data["error"])
            elif status == 200:
                error, junk = "junk", True
            else:
                error = "http_%s" % status

        is_failure = error in BREAKER_FAILURE_REASONS or (status is not None and status >= 500)
        now = self._clock.now()
        with self._lock:
            self._latencies.append(latency)
            p95 = self._p95_locked()
            if p95 is not None and p95 > voice.slow_p95_s:
                if self._slow_since is None:
                    self._slow_since = now
            else:
                self._slow_since = None
            if response is not None:
                self._record_output_locked(rid, False, now)
            elif junk:
                self._record_output_locked(rid, True, now)
            self._finish_breaker_locked(now, ok=response is not None, failure=is_failure, probe=probe)

        self._write_usage(now, user_id, kind, action, response is not None, tokens_in, tokens_out, latency, error)
        if response is None:
            log.warning("voice call failed reason=%s", error)
            return VoiceResult(ok=False, reason=error, latency=latency, tokens_in=tokens_in, tokens_out=tokens_out)
        return VoiceResult(ok=True, response=response, reason="", latency=latency,
                           tokens_in=tokens_in, tokens_out=tokens_out)

    def _finish_breaker_locked(self, now: float, ok: bool, failure: bool, probe: bool) -> None:
        if probe:
            if ok:
                self._breaker = "closed"
                self._failures.clear()
            else:
                self._open_locked(now)
            return
        if ok or not failure:
            return
        self._failures.append(now)
        window = float(self._config.voice.breaker_window_s)
        while self._failures and self._failures[0] <= now - window:
            self._failures.popleft()
        if len(self._failures) >= self._config.voice.breaker_failures:
            self._open_locked(now)

    def _open_locked(self, now: float) -> None:
        self._breaker = "open"
        self._opened_at = now
        self._failures.clear()
        log.warning("voice breaker opened")

    # ----- junk ---------------------------------------------------------------------------------------------------

    def _record_output_locked(self, request_id: str, junk: bool, now: float) -> None:
        self._outputs.append((request_id, junk))
        self._check_junk_locked(now)

    def _check_junk_locked(self, now: float) -> None:
        total = len(self._outputs)
        if total < MIN_JUNK_SAMPLE:
            return
        junk_count = sum(1 for _rid, j in self._outputs if j)
        if junk_count / float(total) > self._config.voice.junk_ratio:
            self._junk_off_until = now + float(self._config.voice.junk_off_s)
            self._outputs.clear()
            log.warning("voice junk breaker opened, voice off for a while")

    def mark_junk(self, request_id: str) -> None:
        """The post-filter rejected a reply. Counts toward the junk breaker (over junk_ratio of the last junk_window)."""
        now = self._clock.now()
        with self._lock:
            for index in range(len(self._outputs) - 1, -1, -1):
                rid, junk = self._outputs[index]
                if rid == request_id:
                    if not junk:
                        self._outputs[index] = (rid, True)
                    break
            else:
                self._outputs.append((str(request_id), True))
            self._check_junk_locked(now)

    # ----- visibility ---------------------------------------------------------------------------------------------

    def _day_start(self, now: float) -> float:
        return self._clock.date_to_ts(self._clock.day_key(now))

    def _month_start(self, now: float) -> float:
        first = self._clock.local(now).date().replace(day=1)
        return self._clock.date_to_ts(first.isoformat())

    def _usage_sum(self, since: float) -> Tuple[int, int]:
        row = self._db.one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens "
            "FROM voice_usage WHERE ts >= ?", (since,))
        if row is None:
            return 0, 0
        return int(row["calls"]), int(row["tokens"])

    def _write_usage(self, now: float, user_id: str, kind: str, action: str, ok: bool,
                     tokens_in: int, tokens_out: int, latency: float, reason: str) -> None:
        try:
            self._db.execute(
                "INSERT INTO voice_usage(ts, user_id, kind, action, ok, tokens_in, tokens_out, latency, reason) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now, user_id or "", kind, action, 1 if ok else 0, tokens_in, tokens_out, latency, reason))
        except Exception:
            log.warning("voice usage write failed")

    def tokens_today(self, now: float) -> int:
        return self._usage_sum(self._day_start(now))[1]

    def status(self, now: float) -> Dict[str, Any]:
        """For `/goose voice status`: enabled, breaker state, calls and tokens today and this month, message-limit hits
        in the last hour, junk ratio, p95 latency. Visibility only."""
        voice = self._config.voice
        with self._lock:
            breaker = self._state_locked(now)
            junk_off = now < self._junk_off_until
            slow = self._slow_locked(now)
            p95 = self._p95_locked()
            outputs = list(self._outputs)
            while self._limit_hits and self._limit_hits[0] <= now - HOUR_S:
                self._limit_hits.popleft()
            limit_hits = len(self._limit_hits)
            inflight = self._active
        junk_ratio = (sum(1 for _rid, j in outputs if j) / float(len(outputs))) if outputs else 0.0
        calls_today, tokens_today = self._usage_sum(self._day_start(now))
        calls_month, tokens_month = self._usage_sum(self._month_start(now))
        alert = voice.alert_tokens_per_day
        return {
            "enabled": self.enabled(),
            "breaker": breaker,
            "junk_off": junk_off,
            "slow": slow,
            "inflight": inflight,
            "calls_today": calls_today,
            "tokens_today": tokens_today,
            "calls_month": calls_month,
            "tokens_month": tokens_month,
            "limit_hits_last_hour": limit_hits,
            "junk_ratio": round(junk_ratio, 3),
            "p95_latency_s": round(p95, 3) if p95 is not None else None,
            "alert_tokens_per_day": alert,
            "over_alert_tokens_per_day": alert is not None and tokens_today >= alert,
        }
