"""The gateway process: owns the ONE Slack Socket Mode connection. OWNER: agent C2b (ops-wiring). Behavior: docs/PLAN.md section 10.

`slack_bolt` is imported lazily inside main() so the rest of the package and the tests work without it installed.
Logging never includes message text, tokens or secrets: only reason codes.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from goose import __version__
from goose.core.db import Database
from goose.gateway import slack_io
from goose.gateway.poster import FilterResult, PostFilter, finalize
from goose.types import Event, Result, VoiceJob, VoiceResult

log = logging.getLogger("goose.app")

HEARTBEAT_KEY = "gateway_heartbeat"
HEARTBEAT_STALE_S = 90
HEARTBEAT_EVERY_S = 30
DEFAULT_CONFIG = "config/goose.yaml"
BOT_PREFIX = "xoxb-"
APP_PREFIX = "xapp-"
SLASH_COMMAND = "/goose"
STALE_JOB_S = 45.0              # a voice job older than this (since admission) posts the canned line, no call
RECENT_TEXT_S = 86400           # posted texts remembered per channel for the no-repeat check
THREAD_CACHE_MAX = 500          # thread lookups remembered (oldest dropped)
THREAD_EXCERPT_MAX = 6          # earlier thread messages sent with an addressed message
OFFICIAL_CHANNEL_NAME = "loadout"


# ------------------------------------------------------------------------------------------------ heartbeat lock

def acquire_heartbeat(db: Database, now: float) -> bool:
    """Refuse a second gateway: True when no heartbeat younger than HEARTBEAT_STALE_S exists. Writes ours on success."""
    with db.tx():
        raw = db.get_setting(HEARTBEAT_KEY, "")
        if raw:
            try:
                last: Optional[float] = float(raw)
            except ValueError:
                last = None  # a garbled value counts as no heartbeat
            if last is not None and now - last < HEARTBEAT_STALE_S:
                return False
        db.set_setting(HEARTBEAT_KEY, "%.3f" % now)
    return True


def beat_heartbeat(db: Database, now: float) -> None:
    db.set_setting(HEARTBEAT_KEY, "%.3f" % now)


def release_heartbeat(db: Database) -> None:
    db.set_setting(HEARTBEAT_KEY, "0")


# ------------------------------------------------------------------------------------------------ token checks

def check_tokens(bot_token: str, app_token: str) -> List[str]:
    """Problems with the two Slack tokens, as reason codes (the tokens themselves are never printed)."""
    problems = []
    if not bot_token:
        problems.append("SLACK_BOT_TOKEN is missing")
    elif not bot_token.startswith(BOT_PREFIX):
        problems.append("SLACK_BOT_TOKEN must start with xoxb-")
    if not app_token:
        problems.append("SLACK_APP_TOKEN is missing")
    elif not app_token.startswith(APP_PREFIX):
        problems.append("SLACK_APP_TOKEN must start with xapp-")
    return problems


# ------------------------------------------------------------------------------------------------ official channel

def verify_official_channel(client: Any, channel_id: str, expected_name: str = OFFICIAL_CHANNEL_NAME) -> bool:
    """True only when Slack says the channel is named expected_name or loadout-development. Empty ids, wildcards and any Slack error
    give False; only the error code is logged."""
    if not channel_id or str(channel_id).strip() in ("", "*"):
        return False
    try:
        info = client.conversations_info(channel=channel_id)["channel"]
        name = str(info.get("name") or "")
        return (name in (expected_name, "loadout", "loadout-development")
                and info.get("is_private") is False)
    except Exception as exc:
        log.error("official channel check failed: %s", slack_io._error_code(exc))
        return False


# ------------------------------------------------------------------------------------------------ voice admission

class VoiceAdmission:
    """Counts voice jobs that are admitted and not yet finished (running or queued). A job is refused at the limit."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._pending = 0
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def try_admit(self) -> bool:
        with self._lock:
            if self._pending >= self.limit:
                return False
            self._pending += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._pending > 0:
                self._pending -= 1


def _admission_limit(ctx: Any) -> int:
    voice = ctx.config.voice
    return max(1, int(voice.concurrency)) + max(0, int(voice.queue_size))


# ------------------------------------------------------------------------------------------------ thread lookups

def _messages(resp: Any) -> List[Dict[str, Any]]:
    """The message dicts of a Slack response, or [] when the response has none."""
    try:
        raw = resp["messages"]
    except (KeyError, TypeError, IndexError):
        return []
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, dict)]


def _message_time(message: Dict[str, str]) -> float:
    try:
        return float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


class ThreadLookup:
    """Fills thread_parent_user and thread_excerpt for thread replies, and resolves reactions on thread replies.
    Any Slack error leaves the fields empty and is logged by error code only. Results are cached (oldest dropped)."""

    def __init__(self, client: Any, max_entries: int = THREAD_CACHE_MAX, bot_user_id: str = ""):
        self._client = client
        self._bot_user_id = bot_user_id
        self._max = max(1, int(max_entries))
        self._cache: "OrderedDict[Tuple[str, str, str], str]" = OrderedDict()
        self._thread_cache: "OrderedDict[Tuple[str, str], Tuple[str, bool, List[Dict[str, str]]]]" = OrderedDict()
        self._lock = threading.Lock()

    def _get(self, key: Tuple[str, str, str]) -> Optional[str]:
        with self._lock:
            return self._cache.get(key)

    def _put(self, key: Tuple[str, str, str], value: str) -> None:
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def _get_thread(self, key: Tuple[str, str]) -> Optional[Tuple[str, bool, List[Dict[str, str]]]]:
        with self._lock:
            state = self._thread_cache.get(key)
            if state is None:
                return None
            self._thread_cache.move_to_end(key)
            return state[0], state[1], [dict(item) for item in state[2]]

    def _put_thread(self, key: Tuple[str, str], state: Tuple[str, bool, List[Dict[str, str]]]) -> None:
        with self._lock:
            self._thread_cache[key] = state
            self._thread_cache.move_to_end(key)
            while len(self._thread_cache) > self._max:
                self._thread_cache.popitem(last=False)

    def _append_cached_event(self, event: Event) -> Optional[Tuple[str, bool, List[Dict[str, str]]]]:
        key = (event.channel, event.thread_ts)
        with self._lock:
            state = self._thread_cache.get(key)
            if state is None:
                return None
            parent, has_bot, messages = state
            messages = [dict(item) for item in messages]
            self._append_thread_event(messages, event)
            has_bot = has_bot or bool(self._bot_user_id and event.user == self._bot_user_id)
            updated = (parent, has_bot, messages)
            self._thread_cache[key] = updated
            self._thread_cache.move_to_end(key)
            return parent, has_bot, [dict(item) for item in messages]

    @staticmethod
    def _append_thread_event(messages: List[Dict[str, str]], event: Event) -> None:
        ts = str(event.message_ts or "")
        if ts and any(item.get("ts") == ts for item in messages):
            return
        messages.append({"ts": ts, "user": str(event.user or ""), "text": str(event.text or "")})
        messages.sort(key=_message_time)
        # Keep the parent plus the latest six replies, enough for the configured voice context.
        if len(messages) > THREAD_EXCERPT_MAX + 1:
            del messages[1:len(messages) - THREAD_EXCERPT_MAX]

    def observe(self, event: Event) -> None:
        """Update known thread context from a message event without making a Slack request."""
        if event.kind in ("message_changed", "message_deleted"):
            key_ts = event.thread_ts or event.message_ts
            with self._lock:
                self._thread_cache.pop((event.channel, key_ts), None)
            return
        if event.kind != "message" or not event.thread_ts:
            return
        self._append_cached_event(event)

    def enrich(self, event: Event) -> None:
        try:
            if event.kind == "message" and event.thread_ts:
                self._message(event)
            elif event.kind in ("message_changed", "message_deleted"):
                self.observe(event)
            elif (event.kind == "reaction_added" and event.reaction == "white_check_mark"
                  and event.message_ts):
                self._reaction(event)
        except Exception as exc:
            log.warning("thread lookup failed: %s", slack_io._error_code(exc))

    def _message(self, event: Event) -> None:
        key = (event.channel, event.thread_ts)
        state = self._append_cached_event(event)
        if state is None:
            raw = _messages(self._client.conversations_replies(channel=event.channel, ts=event.thread_ts, limit=7))
            if not raw:
                return
            parent_user = str(raw[0].get("user") or "")
            messages = [{"ts": str(m.get("ts") or ""), "user": str(m.get("user") or ""),
                         "text": str(m.get("text") or "")}
                        for m in raw]
            has_bot = bool(self._bot_user_id) and any(m["user"] == self._bot_user_id for m in messages)
            self._append_thread_event(messages, event)
            state = (parent_user, has_bot, messages)
            self._put_thread(key, state)
        else:
            parent_user, has_bot, messages = state
        texts = [m["text"] for m in messages if m["ts"] != event.message_ts and m["text"]]
        self._put(("parent", event.channel, event.thread_ts), parent_user)
        event.thread_parent_user = parent_user
        event.thread_excerpt = texts[-THREAD_EXCERPT_MAX:]
        event.thread_has_bot = has_bot

    def _reaction(self, event: Event) -> None:
        key = ("thread", event.channel, event.message_ts)
        thread_ts = self._get(key)
        if thread_ts is None:
            history = _messages(self._client.conversations_history(
                channel=event.channel, latest=event.message_ts, inclusive=True, limit=1))
            thread_ts = ""
            if history and str(history[0].get("ts") or "") == event.message_ts:
                candidate = str(history[0].get("thread_ts") or "")
                if candidate and candidate != event.message_ts:
                    thread_ts = candidate
            self._put(key, thread_ts)
        if not thread_ts:
            return
        parent = self._get(("parent", event.channel, thread_ts))
        if parent is None:
            replies = _messages(self._client.conversations_replies(channel=event.channel, ts=thread_ts, limit=1))
            parent = str(replies[0].get("user") or "") if replies else ""
            self._put(("parent", event.channel, thread_ts), parent)
        event.thread_ts = thread_ts
        event.thread_parent_user = parent


# ------------------------------------------------------------------------------------------------ wiring

def _inline(fn: Callable[..., Any], *args: Any) -> None:
    fn(*args)


def _envelope(body: Dict[str, Any], fn: Callable[..., Optional[Event]], *args: Any) -> Optional[Event]:
    """Normalize one event; keep the envelope's event_id (used for dedupe)."""
    event = fn(body, *args)
    if event is not None and isinstance(body, dict) and body.get("event_id"):
        event.event_id = str(body["event_id"])
    return event


def _names_for(ctx: Any, job: VoiceJob) -> Dict[str, str]:
    from goose.core.card import display_names_for_slots

    names: Dict[str, str] = {}
    try:
        confirmed = ctx.weekly.last_confirmed()
        if confirmed:
            names.update(display_names_for_slots(ctx, confirmed.get("rows") or []))
    except Exception as exc:
        log.warning("slot names unavailable: %s", type(exc).__name__)
    names["you"] = "<@%s>" % job.user if job.user else ""
    return names


def _is_paused(ctx: Any) -> bool:
    """The kill switch: settings 'paused' == '1'. Fails closed when the database cannot be read."""
    db = getattr(ctx, "db", None)
    if db is None:
        return False
    try:
        return db.get_setting("paused", "0") == "1"
    except Exception as exc:
        log.error("pause state unreadable: %s", type(exc).__name__)
        return True


def _recent_texts(ctx: Any, channel: str) -> List[str]:
    db = getattr(ctx, "db", None)
    if db is None:
        return []
    try:
        rows = db.query("SELECT text FROM posted_texts WHERE channel = ? AND ts > ? ORDER BY ts",
                        (channel, float(ctx.clock.now()) - RECENT_TEXT_S))
    except Exception as exc:
        log.warning("recent texts unavailable: %s", type(exc).__name__)
        return []
    return [str(row["text"]) for row in rows]


def _remember_post(ctx: Any, channel: str, text: str) -> None:
    db = getattr(ctx, "db", None)
    if db is None or not text:
        return
    try:
        db.execute("INSERT INTO posted_texts (channel, ts, text) VALUES (?, ?, ?)",
                   (channel, float(ctx.clock.now()), text))
    except Exception as exc:
        log.warning("posted text not remembered: %s", type(exc).__name__)


def run_voice_job(ctx: Any, client: Any, job: VoiceJob, now_fn: Optional[Callable[[], float]] = None,
                  admission: Optional[VoiceAdmission] = None) -> None:
    """Call the voice, post-filter, then post (or the canned fallback). Never raises. Posts nothing while paused.
    A job waiting longer than `voice.queue_timeout_s` skips the voice and posts the fallback. `now_fn` is the
    monotonic clock (injected in tests). `admission` is released when the job ends, also after an exception."""
    try:
        if _is_paused(ctx):
            _clear_thinking_placeholder(client, job)
            return
        _run_job(ctx, client, job, now_fn or time.monotonic)
    except Exception as exc:
        log.error("voice job failed: %s", type(exc).__name__)
        if _is_paused(ctx):
            _clear_thinking_placeholder(client, job)
        else:
            try:
                _post_canned_now(ctx, client, job, "voice")
            except Exception as fallback_exc:
                log.error("voice fallback failed: %s", type(fallback_exc).__name__)
    finally:
        if admission is not None:
            admission.release()


def _run_job(ctx: Any, client: Any, job: VoiceJob, clock_fn: Callable[[], float]) -> None:
    if _is_paused(ctx):
        _clear_thinking_placeholder(client, job)
        return
    voice = ctx.voice_client
    queue_timeout = float(getattr(ctx.config.voice, "queue_timeout_s", STALE_JOB_S))
    stale = bool(job.created_ts) and clock_fn() - job.created_ts > queue_timeout
    if voice is None:
        vr = VoiceResult(ok=False, reason="disabled")
    elif stale:
        vr = VoiceResult(ok=False, reason="stale")
    else:
        vr = voice.call(job.request, user_id=job.user)
    if _is_paused(ctx):
        _clear_thinking_placeholder(client, job)
        return
    names = _names_for(ctx, job)
    post_filter = PostFilter(ctx.config, ctx.lines)
    message = (job.request.get("input") or {}).get("message") or {}
    user_text = str(message.get("text") or "")
    recent = _recent_texts(ctx, job.channel)
    checked: Optional[FilterResult] = None
    if vr.ok and voice is not None:
        checked = post_filter.check(vr.response or {}, job.request, user_text=user_text, names=names,
                                    private=bool(job.ephemeral), recent_texts=recent)
        if not checked.ok:
            voice.mark_junk(str(job.request.get("request_id") or ""))
    actions = finalize(job, vr, post_filter, ctx.lines, names=names, recent_texts=recent,
                       checked_result=checked)
    if _is_paused(ctx):
        _clear_thinking_placeholder(client, job)
        return
    errors = slack_io.execute(client, actions, job.channel)
    if checked is not None and checked.ok and not errors:
        for action in actions:
            if action.kind in ("post", "update"):
                _remember_post(ctx, action.channel or job.channel, action.text)


def _clear_thinking_placeholder(client: Any, job: VoiceJob) -> None:
    """Remove a pending visual marker if a pause cancels its reply."""
    if not job.channel or not job.thinking_ts:
        return
    try:
        client.chat_delete(channel=job.channel, ts=job.thinking_ts)
    except Exception as exc:
        log.debug("thinking placeholder cleanup failed: %s", type(exc).__name__)


def _post_canned_now(ctx: Any, client: Any, job: VoiceJob, reason: str) -> None:
    names = _names_for(ctx, job)
    actions = finalize(job, VoiceResult(ok=False, reason=reason), PostFilter(ctx.config, ctx.lines), ctx.lines,
                       names=names)
    slack_io.execute(client, actions, job.channel)


def build_handlers(ctx: Any, router: Any, client: Any, *, submit: Optional[Callable[..., Any]] = None,
                   voice_submit: Optional[Callable[..., Any]] = None,
                   now_fn: Optional[Callable[[], float]] = None,
                   voice_limit: Optional[int] = None) -> Dict[str, Callable[..., None]]:
    """Plain callables for each Slack listener. Each one normalizes its payload, then hands the work to `submit`
    (a worker pool in production; a direct call in tests). Slack is acknowledged before submit runs. Returns a dict with
    keys: message, app_mention, reaction_added, reaction_removed, member_joined_channel, command (takes body, ack).
    Thread lookups fill the event before routing. Each voice job is admitted (counted) before it is submitted; when
    voice_limit (default concurrency + queue_size) jobs are pending, the job gets its canned line at once. `now_fn` is
    the monotonic clock used for admission times and stale checks."""
    submit_fn = submit or _inline
    voice_fn = voice_submit or _inline
    clock_fn = now_fn or time.monotonic
    bot_id = str(ctx.bot_user_id or "")
    admission = VoiceAdmission(voice_limit if voice_limit is not None else _admission_limit(ctx))
    lookup = ThreadLookup(client, bot_user_id=bot_id)

    def admit(job: VoiceJob) -> None:
        if _is_paused(ctx):
            return
        if not admission.try_admit():
            log.info("voice queue full, canned line posted")
            _post_canned_now(ctx, client, job, "queue_full")
            return
        # Immediate visual feedback: send a temporary thinking message into the thread/channel
        thinking_ts = ""
        if job.channel and not job.ephemeral:
            try:
                target_thread = job.thread_ts or job.message_ts
                resp = client.chat_postMessage(
                    channel=job.channel,
                    text="_Thinking... :duck:_",
                    thread_ts=target_thread or None,
                    parse="none",
                )
                ts_getter = getattr(resp, "get", None)
                if callable(ts_getter):
                    thinking_ts = str(ts_getter("ts") or "")
                elif resp is not None:
                    thinking_ts = str(resp["ts"] or "")
            except Exception as exc:
                log.debug("initial thinking message skipped: %s", type(exc).__name__)
        job.thinking_ts = thinking_ts
        job.created_ts = clock_fn()
        try:
            voice_fn(run_voice_job, ctx, client, job, clock_fn, admission)
        except BaseException as exc:
            admission.release()
            log.error("voice submission failed: %s", type(exc).__name__)
            if not _is_paused(ctx):
                try:
                    _post_canned_now(ctx, client, job, "queue_full")
                except Exception as fallback_exc:
                    log.error("voice fallback failed: %s", type(fallback_exc).__name__)

    def process(event: Event) -> None:
        try:
            if event.is_bot or (event.user and event.user == bot_id):
                lookup.observe(event)
                return
            lookup.enrich(event)
            result: Result = router.handle(event)
            slack_io.execute(client, result.actions, event.channel)
            for job in result.jobs:
                admit(job)
        except Exception as exc:
            log.error("event %s failed: %s", event.kind, type(exc).__name__)

    def dispatch(event: Optional[Event]) -> None:
        if event is not None:
            submit_fn(process, event)

    def on_message(body: Dict[str, Any], **_: Any) -> None:
        dispatch(_envelope(body, slack_io.normalize_message, bot_id))

    def on_app_mention(body: Dict[str, Any], **_: Any) -> None:
        dispatch(_envelope(body, slack_io.normalize_app_mention, bot_id))

    def on_reaction_added(body: Dict[str, Any], **_: Any) -> None:
        dispatch(_envelope(body, slack_io.normalize_reaction, True))

    def on_reaction_removed(body: Dict[str, Any], **_: Any) -> None:
        dispatch(_envelope(body, slack_io.normalize_reaction, False))

    def on_member_joined(body: Dict[str, Any], **_: Any) -> None:
        dispatch(_envelope(body, slack_io.normalize_member_joined))

    def on_command(body: Dict[str, Any], ack: Optional[Callable[[], Any]] = None, **_: Any) -> None:
        if ack is not None:
            ack()
        dispatch(slack_io.normalize_command(dict(body or {}), now=ctx.clock.now()))

    return {
        "message": on_message,
        "app_mention": on_app_mention,
        "reaction_added": on_reaction_added,
        "reaction_removed": on_reaction_removed,
        "member_joined_channel": on_member_joined,
        "command": on_command,
    }


def make_deliver(ctx: Any, client: Any) -> Callable[[Result], None]:
    """The scheduler's delivery: its actions go to Slack like any other action."""
    def deliver(result: Result) -> None:
        slack_io.execute(client, result.actions, ctx.organizer_channel)
    return deliver


# ------------------------------------------------------------------------------------------------ check and start

def check_config(path: str) -> List[str]:
    """Validation problems with the config file and lines.yaml (empty list when all is well). Needs no Slack tokens."""
    from goose.config import ConfigError, load_config
    from goose.core.lines import LineBank

    try:
        cfg = load_config(path)
    except ConfigError as exc:
        return [str(exc)]
    try:
        LineBank.from_file(cfg.paths.lines)
    except (ValueError, OSError) as exc:
        return ["lines: %s" % exc]
    return []


def setup_problems(path: str) -> List[str]:
    """Setup gaps (config.missing_setup) for a config that validates. Empty when the file does not load."""
    from goose.config import ConfigError, load_config, missing_setup

    try:
        cfg = load_config(path)
    except ConfigError:
        return []
    return missing_setup(cfg)


def run_gateway(config_path: str) -> int:
    """The real start path. Returns a process exit code."""
    from goose.config import ConfigError, load_config, missing_setup
    from goose.core.clock import Clock
    from goose.core.context import build_context
    from goose.core.lines import LineBank
    from goose.core.router import Router
    from goose.gateway.scheduler import Scheduler
    from goose.gateway.voice_client import VoiceClient

    problems = check_tokens(os.environ.get("SLACK_BOT_TOKEN", ""), os.environ.get("SLACK_APP_TOKEN", ""))
    if problems:
        for p in problems:
            log.error("%s", p)
        return 2
    try:
        cfg = load_config(config_path)
        lines = LineBank.from_file(cfg.paths.lines)
    except (ConfigError, ValueError, OSError) as exc:
        log.error("config problem: %s", type(exc).__name__)
        return 2
    gaps = missing_setup(cfg)
    if gaps:
        for gap in gaps:
            log.error("not configured yet: %s", gap)
        return 2

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        log.error("slack_bolt is not installed")
        return 2

    clock = Clock(cfg.schedule.timezone)
    ctx = build_context(cfg, db_path=cfg.paths.db, audit_path=cfg.paths.audit, lines=lines, clock=clock)
    if not acquire_heartbeat(ctx.db, clock.now()):
        log.error("another gateway is running (heartbeat is fresh); refusing to start")
        ctx.db.close()
        return 3

    bot_token = os.environ["SLACK_BOT_TOKEN"]
    app_token = os.environ["SLACK_APP_TOKEN"]
    secret = os.environ.get(cfg.voice.secret_env, "")
    ctx.voice_client = VoiceClient(cfg, ctx.db, clock, secret=secret)
    app = App(token=bot_token)
    try:
        ctx.bot_user_id = str(app.client.auth_test()["user_id"])
    except Exception as exc:
        log.error("auth.test failed: %s", type(exc).__name__)
        release_heartbeat(ctx.db)
        ctx.db.close()
        return 2
    for channel_id in cfg.channels.scored:
        if not verify_official_channel(app.client, channel_id):
            log.error("a scored channel is not the official public channel named %s; refusing to start",
                      OFFICIAL_CHANNEL_NAME)
            release_heartbeat(ctx.db)
            ctx.db.close()
            return 2

    router = Router(ctx)
    client = app.client
    work = ThreadPoolExecutor(max_workers=4, thread_name_prefix="goose-work")
    voice_pool = ThreadPoolExecutor(max_workers=max(1, int(cfg.voice.concurrency)), thread_name_prefix="goose-voice")
    handlers = build_handlers(ctx, router, client,
                              submit=lambda fn, *a: work.submit(fn, *a),
                              voice_submit=lambda fn, *a: voice_pool.submit(fn, *a))

    def on_message(body: Dict[str, Any], **kw: Any) -> None:
        handlers["message"](body)

    def on_app_mention(body: Dict[str, Any], **kw: Any) -> None:
        handlers["app_mention"](body)

    def on_reaction_added(body: Dict[str, Any], **kw: Any) -> None:
        handlers["reaction_added"](body)

    def on_reaction_removed(body: Dict[str, Any], **kw: Any) -> None:
        handlers["reaction_removed"](body)

    def on_member_joined(body: Dict[str, Any], **kw: Any) -> None:
        handlers["member_joined_channel"](body)

    def on_command(ack: Callable[[], Any], body: Dict[str, Any], **kw: Any) -> None:
        handlers["command"](body, ack=ack)

    app.event("message")(on_message)
    app.event("app_mention")(on_app_mention)
    app.event("reaction_added")(on_reaction_added)
    app.event("reaction_removed")(on_reaction_removed)
    app.event("member_joined_channel")(on_member_joined)
    for slash_cmd in (SLASH_COMMAND, "/level", "/rank", "/leaderboard", "/card", "/top", "/help"):
        try:
            app.command(slash_cmd)(on_command)
        except Exception as exc:
            log.warning("could not register slash command %s: %s", slash_cmd, exc)

    stop = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    def heartbeat_loop() -> None:
        while not stop.wait(HEARTBEAT_EVERY_S):
            try:
                beat_heartbeat(ctx.db, clock.now())
            except Exception as exc:
                log.error("heartbeat failed: %s", type(exc).__name__)

    scheduler = Scheduler(ctx, router)
    threads = [
        threading.Thread(target=scheduler.run, args=(stop, make_deliver(ctx, client)), name="goose-scheduler",
                         daemon=True),
        threading.Thread(target=heartbeat_loop, name="goose-heartbeat", daemon=True),
    ]
    socket = SocketModeHandler(app, app_token)
    socket.connect()
    for t in threads:
        t.start()
    log.info("goose-lee %s connected", __version__)
    try:
        stop.wait()
    finally:
        stop.set()
        socket.close()
        work.shutdown(wait=False)
        voice_pool.shutdown(wait=True)
        for t in threads:
            t.join(timeout=5)
        release_heartbeat(ctx.db)
        ctx.db.close()
    log.info("goose-lee stopped")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """CLI. Options: --config PATH (default config/goose.yaml), --check-config (validate config and lines.yaml, print OK or the
    problems, exit 0 or 1, no Slack), --version.
    --check-config prints "OK" when all is set up; when only setup gaps remain it prints "OK, not configured yet:" and the
    gaps, and exits 0. Normal start: load config (fail closed), refuse while setup gaps exist, read SLACK_BOT_TOKEN and
    SLACK_APP_TOKEN from the environment (refuse to start if missing or wrong prefix), build_context, take a database
    heartbeat lock (refuse a second gateway), create VoiceClient (secret from the env var named in config.voice.secret_env),
    auth.test to learn the bot user id, verify every scored channel is the public channel named loadout, register
    handlers: message, app_mention, reaction_added, reaction_removed, member_joined_channel, and the /goose command
    (ack() first, then process). Each handler: normalize -> thread lookups -> Router.handle (in a worker thread so Slack is
    acknowledged first) -> slack_io.execute(actions) -> for each VoiceJob, admit it (at most concurrency + queue_size
    pending) and submit to a ThreadPoolExecutor(config.voice.concurrency) that calls run_voice_job. Start the Scheduler
    thread. Handle SIGINT/SIGTERM cleanly. Logging never includes message text, tokens or secrets."""
    parser = argparse.ArgumentParser(prog="goose-lee", description="The goose-lee gateway.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to the YAML config (default %s)" % DEFAULT_CONFIG)
    parser.add_argument("--check-config", action="store_true",
                        help="validate the config and lines.yaml, print OK or the problems, and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print("goose-lee %s" % __version__)
        return 0
    if args.check_config:
        problems = check_config(args.config)
        if problems:
            for p in problems:
                print(p)
            return 1
        gaps = setup_problems(args.config)
        if gaps:
            print("OK, not configured yet:")
            for gap in gaps:
                print("  - %s" % gap)
            return 0
        print("OK")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return run_gateway(args.config)


if __name__ == "__main__":
    sys.exit(main())
