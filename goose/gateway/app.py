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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from goose import __version__
from goose.core.db import Database
from goose.gateway import slack_io
from goose.gateway.poster import PostFilter, finalize
from goose.types import Event, Result, VoiceJob, VoiceResult

log = logging.getLogger("goose.app")

HEARTBEAT_KEY = "gateway_heartbeat"
HEARTBEAT_STALE_S = 90
HEARTBEAT_EVERY_S = 30
DEFAULT_CONFIG = "config/goose.yaml"
BOT_PREFIX = "xoxb-"
APP_PREFIX = "xapp-"
SLASH_COMMAND = "/goose"


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


def run_voice_job(ctx: Any, client: Any, job: VoiceJob) -> None:
    """Call the voice, post-filter, then post (or the canned fallback). Never raises."""
    voice = ctx.voice_client
    try:
        if voice is None:
            vr = VoiceResult(ok=False, reason="disabled")
        else:
            vr = voice.call(job.request, user_id=job.user)
        names = _names_for(ctx, job)
        post_filter = PostFilter(ctx.config, ctx.lines)
        message = (job.request.get("input") or {}).get("message") or {}
        user_text = str(message.get("text") or "")
        if vr.ok and voice is not None:
            checked = post_filter.check(vr.response or {}, job.request, user_text=user_text, names=names)
            if not checked.ok:
                voice.mark_junk(str(job.request.get("request_id") or ""))
        actions = finalize(job, vr, post_filter, ctx.lines, names=names)
        slack_io.execute(client, actions, job.channel)
    except Exception as exc:
        log.error("voice job failed: %s", type(exc).__name__)


def build_handlers(ctx: Any, router: Any, client: Any, *, submit: Optional[Callable[..., Any]] = None,
                   voice_submit: Optional[Callable[..., Any]] = None) -> Dict[str, Callable[..., None]]:
    """Plain callables for each Slack listener. Each one normalizes its payload, then hands the work to `submit`
    (a worker pool in production; a direct call in tests). Slack is acknowledged before submit runs. Returns a dict with
    keys: message, app_mention, reaction_added, reaction_removed, member_joined_channel, command (takes body, ack)."""
    submit_fn = submit or _inline
    voice_fn = voice_submit or _inline
    bot_id = str(ctx.bot_user_id or "")

    def process(event: Event) -> None:
        try:
            result: Result = router.handle(event)
            slack_io.execute(client, result.actions, event.channel)
            for job in result.jobs:
                voice_fn(run_voice_job, ctx, client, job)
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
    """Problems with the config file and lines.yaml (empty list when all is well). Needs no Slack tokens."""
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


def run_gateway(config_path: str) -> int:
    """The real start path. Returns a process exit code."""
    from goose.config import ConfigError, load_config
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
    app.command(SLASH_COMMAND)(on_command)

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
    Normal start: load config (fail closed), read SLACK_BOT_TOKEN and SLACK_APP_TOKEN from the environment (refuse to start if
    missing or wrong prefix), build_context, take a database heartbeat lock (refuse a second gateway), create VoiceClient
    (secret from the env var named in config.voice.secret_env), auth.test to learn the bot user id, register handlers:
    message, app_mention, reaction_added, reaction_removed, member_joined_channel, and the /goose command (ack() first,
    then process). Each handler: normalize -> Router.handle (in a worker thread so Slack is acknowledged first) ->
    slack_io.execute(actions) -> for each VoiceJob submit to a ThreadPoolExecutor(config.voice.concurrency) that calls
    VoiceClient.call, poster.finalize, then execute. Start the Scheduler thread. Handle SIGINT/SIGTERM cleanly.
    Logging never includes message text, tokens or secrets."""
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
        print("OK")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return run_gateway(args.config)


if __name__ == "__main__":
    sys.exit(main())
