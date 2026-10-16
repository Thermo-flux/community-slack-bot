"""The voice service: one endpoint, no Slack, no database. OWNER: agent D (voice). Stdlib http.server.

POST /v1/voice   headers X-Goose-Timestamp (integer seconds) and X-Goose-Signature (hex HMAC-SHA256 of
                 timestamp + "." + the exact body bytes, keyed with VOICE_SHARED_SECRET). JSON body = request.
                 401 for a bad or stale signature, 400 for a replayed request_id.
GET  /health     {"ok": true, "model": "..."}   (no secrets)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from goose.voice.prompt import APPROVED_REACTIONS, ELABORATE_CAP, REPLY_CAP, build_messages, build_parts, max_tokens_for
from goose.voice.provider import Provider, ProviderError

PROTOCOL_VERSION = 1
SIGNATURE_SKEW_S = 60          # a signed timestamp may be at most this far from the server's clock
REPLAY_WINDOW_S = 300          # a request_id seen in this window is refused
REPLAY_MAX_IDS = 10000         # bound on the replay memory; the oldest id is dropped first
MAX_BODY_BYTES = 16 * 1024
MAX_INPUT_CHARS = 500
MAX_THREAD_ITEMS = 6
MAX_THREAD_ITEM_CHARS = 200
MAX_THREAD_TOTAL_CHARS = 1200
MAX_CARD_CHARS = 4000
MAX_FACTS = 20
ACTIONS = ("speak", "silent", "redirect", "decline")
KINDS = ("addressed", "event")
MODES = ("chat", "event")
REQUEST_KEYS = {"v", "request_id", "kind", "mode", "persona", "card", "input"}
PERSONA_REQUIRED = {"humor_level", "german_allowed", "roast_allowed"}
PERSONA_OPTIONAL = {"max_chars", "elaborate"}
INPUT_KEYS_ADDRESSED = {"message", "thread"}
INPUT_KEYS_EVENT = {"facts"}
OUTPUT_KEYS = {"action", "text", "reaction", "flags"}
OUTPUT_OPTIONAL_KEYS = {"v", "request_id"}
FLAG_KEYS = {"roasted", "mentions_rank", "uses_german", "numbers_used"}
_FACT_KEY = re.compile(r"^[a-z_]{1,32}$")
_REASON_CODE = re.compile(r"^[a-z_0-9]{1,24}$")
_TIMESTAMP = re.compile(r"[0-9]{1,12}")


class _BadRequest(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_request(body: Any) -> None:
    """Raise _BadRequest with a short detail when the request does not match the contract."""
    if not isinstance(body, dict):
        raise _BadRequest("body must be a JSON object")
    unknown = set(body) - REQUEST_KEYS
    if unknown:
        raise _BadRequest("unknown top-level key")
    if not _is_int(body.get("v")) or body.get("v") != PROTOCOL_VERSION:
        raise _BadRequest("v must be 1")
    rid = body.get("request_id")
    if not isinstance(rid, str) or not (1 <= len(rid) <= 64):
        raise _BadRequest("request_id must be text of 1 to 64 characters")
    kind = body.get("kind")
    if kind not in KINDS:
        raise _BadRequest("kind must be addressed or event")
    if "mode" in body and body["mode"] not in MODES:
        raise _BadRequest("mode must be chat or event")
    persona = body.get("persona")
    if not isinstance(persona, dict):
        raise _BadRequest("persona must be an object")
    if not PERSONA_REQUIRED.issubset(persona) or set(persona) - PERSONA_REQUIRED - PERSONA_OPTIONAL:
        raise _BadRequest("persona has missing or unknown keys")
    if not _is_int(persona["humor_level"]) or not 1 <= persona["humor_level"] <= 3:
        raise _BadRequest("persona.humor_level must be 1 to 3")
    if not isinstance(persona["german_allowed"], bool) or not isinstance(persona["roast_allowed"], bool):
        raise _BadRequest("persona flags must be booleans")
    if "max_chars" in persona and (not _is_int(persona["max_chars"]) or not 20 <= persona["max_chars"] <= ELABORATE_CAP):
        raise _BadRequest("persona.max_chars out of range")
    if "elaborate" in persona and not isinstance(persona["elaborate"], bool):
        raise _BadRequest("persona.elaborate must be a boolean")
    card = body.get("card")
    if not isinstance(card, dict):
        raise _BadRequest("card must be an object")
    if len(json.dumps(card, separators=(",", ":"))) > MAX_CARD_CHARS:
        raise _BadRequest("card too large")
    input_ = body.get("input")
    if not isinstance(input_, dict):
        raise _BadRequest("input must be an object")
    if kind == "event":
        if set(input_) - INPUT_KEYS_EVENT or "facts" not in input_:
            raise _BadRequest("event input holds facts only")
        _check_facts(input_["facts"])
    else:
        if set(input_) - INPUT_KEYS_ADDRESSED or "message" not in input_:
            raise _BadRequest("addressed input needs message and optional thread")
        _check_message(input_["message"], input_.get("thread", []))


def _check_facts(facts: Any) -> None:
    if not isinstance(facts, dict) or len(facts) > MAX_FACTS:
        raise _BadRequest("facts must be a short object")
    for key, value in facts.items():
        if not isinstance(key, str) or not _FACT_KEY.match(key):
            raise _BadRequest("fact names must be short lowercase words")
        if isinstance(value, str):
            if len(value) > 40:
                raise _BadRequest("fact value too long")
        elif not isinstance(value, (int, float, bool)) and value is not None:
            raise _BadRequest("fact values must be numbers, booleans or short text")


def _check_message(message: Any, thread: Any) -> None:
    if not isinstance(message, dict) or set(message) - {"handle", "text"}:
        raise _BadRequest("message must hold text and an optional handle")
    text = message.get("text")
    if not isinstance(text, str) or len(text) > MAX_INPUT_CHARS:
        raise _BadRequest("message text must be text of at most 500 characters")
    if "handle" in message and (not isinstance(message["handle"], str) or len(message["handle"]) > 32):
        raise _BadRequest("message handle out of range")
    if not isinstance(thread, list) or len(thread) > MAX_THREAD_ITEMS:
        raise _BadRequest("thread must be a list of at most 6 items")
    total = 0
    for item in thread:
        if not isinstance(item, str) or len(item) > MAX_THREAD_ITEM_CHARS:
            raise _BadRequest("thread items must be text of at most 200 characters")
        total += len(item)
    if total > MAX_THREAD_TOTAL_CHARS:
        raise _BadRequest("thread too long")


def _parse_model_output(text: str) -> Optional[Dict[str, Any]]:
    """Exactly one JSON object. A single fenced code block is tolerated. Any prose around the object is junk."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.split("\n")
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            return None
        first = lines[0].strip()
        if first not in ("```", "```json", "```JSON"):
            return None
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        obj = json.loads(candidate)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _validate_output(obj: Dict[str, Any], request_id: str, cap: int) -> Optional[Dict[str, Any]]:
    """Return a clean response fragment (action, text, reaction, flags) or None for junk."""
    if set(obj) - (OUTPUT_KEYS | OUTPUT_OPTIONAL_KEYS) or not {"action", "text", "flags"}.issubset(obj):
        return None
    if "v" in obj and (not _is_int(obj["v"]) or obj["v"] != PROTOCOL_VERSION):
        return None
    if "request_id" in obj and obj["request_id"] != request_id:
        return None
    action = obj["action"]
    if action not in ACTIONS:
        return None
    text = obj["text"]
    if not isinstance(text, str) or len(text) > cap:
        return None
    if action == "speak" and not text.strip():
        return None
    reaction = obj.get("reaction")
    if reaction is not None and reaction not in APPROVED_REACTIONS:
        return None
    flags = obj["flags"]
    if not isinstance(flags, dict) or set(flags) - FLAG_KEYS:
        return None
    clean_flags: Dict[str, Any] = {}
    for key in ("roasted", "mentions_rank", "uses_german"):
        value = flags.get(key, False)
        if not isinstance(value, bool):
            return None
        clean_flags[key] = value
    numbers = flags.get("numbers_used", [])
    if not isinstance(numbers, list) or len(numbers) > 20:
        return None
    clean_numbers: List[str] = []
    for item in numbers:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            return None
        s = str(item)
        if len(s) > 16:
            return None
        clean_numbers.append(s)
    clean_flags["numbers_used"] = clean_numbers
    return {"action": action, "text": text, "reaction": reaction, "flags": clean_flags}


def _cap_for(body: Dict[str, Any]) -> int:
    persona = body.get("persona") or {}
    cap = ELABORATE_CAP if persona.get("elaborate") else REPLY_CAP
    if "max_chars" in persona:
        cap = min(cap, int(persona["max_chars"]))
    return cap


def handle_request(body: Dict[str, Any], provider: Provider) -> Tuple[int, Dict[str, Any]]:
    """Pure function used by the server and by tests.

    Validates the request schema (v == 1, request_id, kind in addressed|event, persona, card, input; text lengths within
    the limits), builds the prompt, calls the provider, parses the model output as ONE JSON object and returns
    (200, response) with keys v, request_id, action, text, reaction, flags. Model output that is not exactly that shape
    returns (502, {"error": "junk"}) so the gateway counts it, and is never silently turned into "silent".
    Bad request returns (400, {"error": "bad_request", "detail": ...}). Provider failure returns (502, {"error": <code>}).
    Never echoes the prompt or the key.

    Token counts travel in a "usage" key on the 200 and junk responses only, so the gateway can count them in
    voice_usage. The gateway client removes "usage" before the post-filter sees the response."""
    try:
        _check_request(body)
    except _BadRequest as exc:
        return 400, {"error": "bad_request", "detail": exc.detail}

    request_id = body["request_id"]
    try:
        result = provider.complete_usage(
            build_messages(body), max_tokens_for(body, default=100, elaborate=200), timeout=4.0,
            parts=build_parts(body))
        text, tokens_in, tokens_out = result.text, result.tokens_in, result.tokens_out
    except ProviderError as exc:
        reason = exc.args[0] if exc.args and _REASON_CODE.match(str(exc.args[0])) else "network"
        return 502, {"error": reason}

    usage = {"tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "cached_in": int(result.cached_in)}
    obj = _parse_model_output(text)
    clean = _validate_output(obj, request_id, _cap_for(body)) if obj is not None else None
    if clean is None:
        return 502, {"error": "junk", "usage": usage}
    response: Dict[str, Any] = {"v": PROTOCOL_VERSION, "request_id": request_id}
    response.update(clean)
    response["usage"] = usage
    return 200, response


def sign_request(secret: str, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 over str(timestamp) + "." + the exact body bytes. The gateway client signs with the same formula."""
    message = str(int(timestamp)).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _signature_ok(timestamp: Optional[str], signature: Optional[str], body: bytes, secret: str, now: float) -> bool:
    """True only for a fresh timestamp (within SIGNATURE_SKEW_S of now) and a matching signature (constant-time)."""
    if not secret or timestamp is None or signature is None:
        return False
    if not _TIMESTAMP.fullmatch(timestamp):
        return False
    ts = int(timestamp)
    if abs(float(now) - ts) > SIGNATURE_SKEW_S:
        return False
    expected = sign_request(secret, ts, body)
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8"))


class ReplayGuard:
    """Remembers request_ids for REPLAY_WINDOW_S seconds. Pruned on every admit; bounded to REPLAY_MAX_IDS entries."""

    def __init__(self, window_s: float = REPLAY_WINDOW_S, max_ids: int = REPLAY_MAX_IDS):
        self._window = float(window_s)
        self._max = max(1, int(max_ids))
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def admit(self, request_id: str, now: float) -> bool:
        """True the first time a request_id is seen inside the window, False for a replay."""
        with self._lock:
            cutoff = float(now) - self._window
            for key in [k for k, seen in self._seen.items() if seen <= cutoff]:
                del self._seen[key]
            if request_id in self._seen:
                return False
            while len(self._seen) >= self._max:
                del self._seen[next(iter(self._seen))]     # dict order is insertion order, so this is the oldest
            self._seen[request_id] = float(now)
            return True


def make_server(host: str, port: int, provider: Provider, secret: str,
                clock: Optional[Callable[[], float]] = None):
    """Return a ThreadingHTTPServer ready for serve_forever(). `clock` returns the server's time in seconds (default
    time.time); tests pass a fixed one."""
    now_fn: Callable[[], float] = clock or time.time
    guard = ReplayGuard()

    class Handler(BaseHTTPRequestHandler):
        server_version = "goose-lee-voice"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # request lines and bodies are never logged
            return

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/health":
                self._send(200, {"ok": True, "model": provider.model})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/voice":
                self._send(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"error": "bad_request", "detail": "content length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(400, {"error": "bad_request", "detail": "body size"})
                return
            raw = self.rfile.read(length)
            # The signature covers the exact bytes read here, so it is checked before the JSON is parsed.
            if not _signature_ok(self.headers.get("X-Goose-Timestamp"), self.headers.get("X-Goose-Signature"),
                                 raw, secret, now_fn()):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "bad_request", "detail": "not JSON"})
                return
            rid = body.get("request_id") if isinstance(body, dict) else None
            if isinstance(rid, str) and not guard.admit(rid, now_fn()):
                self._send(400, {"error": "bad_request", "detail": "replay"})
                return
            status, payload = handle_request(body, provider)
            self._send(status, payload)

    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    """Read env (VOICE_HOST default 0.0.0.0, VOICE_PORT default 8081, VOICE_SHARED_SECRET required, LLM_* via
    Provider.from_env) and serve. Refuses to start without a secret or key, or with a model not on the allowlist."""
    env = dict(os.environ)
    secret = env.get("VOICE_SHARED_SECRET", "")
    if not secret:
        sys.stderr.write("refusing to start: VOICE_SHARED_SECRET is not set\n")
        sys.exit(2)
    try:
        provider = Provider.from_env(env)
    except ProviderError as exc:
        sys.stderr.write("refusing to start: %s\n" % exc.args[0])
        sys.exit(2)
    host = env.get("VOICE_HOST", "0.0.0.0")
    try:
        port = int(env.get("VOICE_PORT", "8081"))
    except ValueError:
        sys.stderr.write("refusing to start: VOICE_PORT must be a number\n")
        sys.exit(2)
    server = make_server(host, port, provider, secret)
    sys.stdout.write("voice service listening on %s:%d (model %s)\n" % (host, port, provider.model))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
