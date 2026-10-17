"""Slack payloads <-> goose types. OWNER: agent C2b (ops-wiring). No network here except `execute`, which takes a client.

`client` is anything with the slack_sdk WebClient methods chat_postMessage, chat_postEphemeral, reactions_add,
files_upload_v2 (tests pass a fake).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from goose.types import Action, Event

log = logging.getLogger("goose.slack")

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_IGNORED_SUBTYPES = ("", "thread_broadcast", "file_share", "bot_message")
_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,60}$")


def _mentions(text: str) -> List[str]:
    found: List[str] = []
    for uid in _MENTION_RE.findall(text or ""):
        if uid not in found:
            found.append(uid)
    return found


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _payload(body: Any) -> Dict[str, Any]:
    """Accept either the envelope (with 'event') or the event payload itself."""
    if not isinstance(body, dict):
        return {}
    if isinstance(body.get("event"), dict):
        return body["event"]
    return body


def parse_command_text(text: str) -> Dict[str, Any]:
    """'star <@U123|sam> help thanks for the fix https://...' -> {"command": "star", "args": "<@U123|sam> help ...",
    "mentions": ["U123"]}. The first word is the subcommand (lowercase, '' -> 'help'). Mentions come from <@U..|name> tokens
    (the label is ignored and never stored). Plain '@name' text is left alone: the handlers refuse it with a hint."""
    raw = (text or "").strip()
    if not raw:
        return {"command": "help", "args": "", "mentions": []}
    parts = raw.split(None, 1)
    command = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return {"command": command, "args": args, "mentions": _mentions(raw)}


def _is_bot_mention(text: str, bot_user_id: str) -> bool:
    if not bot_user_id or not text:
        return False
    return re.search(r"<@%s(?:\|[^>]*)?>" % re.escape(bot_user_id), text) is not None


def _message_fields(inner: Dict[str, Any]) -> Dict[str, str]:
    message_ts = str(inner.get("ts") or "")
    thread_raw = str(inner.get("thread_ts") or "")
    return {
        "message_ts": message_ts,
        "thread_ts": thread_raw if thread_raw and thread_raw != message_ts else "",
    }


def normalize_message(body: Dict[str, Any], bot_user_id: str) -> Optional[Event]:
    """Slack `event` payload (body['event']) for type 'message', including subtypes: message_changed (use event['message']
    and event['previous_message']['text']), message_deleted (event['deleted_ts'], event['previous_message']),
    thread_broadcast/file_share are treated as plain messages. Bots and subtypes bot_message set is_bot. Returns None for
    channel_join/leave and other subtypes we ignore. Fills thread_ts only for replies (thread_ts != ts). bot_mentioned when
    <@bot_user_id> is in the text. mentions from the text."""
    ev = _payload(body)
    if ev.get("type") != "message":
        return None
    sub = str(ev.get("subtype") or "")
    channel = str(ev.get("channel") or "")
    is_bot_flag = bool(ev.get("bot_id")) or sub == "bot_message"

    if sub == "message_changed":
        inner = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        prev = ev.get("previous_message") if isinstance(ev.get("previous_message"), dict) else {}
        text = str(inner.get("text") or "")
        fields = _message_fields(inner)
        return Event(
            kind="message_changed", ts=_to_float(ev.get("event_ts") or fields["message_ts"]), channel=channel,
            user=str(inner.get("user") or ""), message_ts=fields["message_ts"], thread_ts=fields["thread_ts"],
            text=text, is_bot=is_bot_flag or bool(inner.get("bot_id")),
            bot_mentioned=_is_bot_mention(text, bot_user_id), mentions=_mentions(text),
            previous_text=str(prev.get("text") or ""),
        )

    if sub == "message_deleted":
        prev = ev.get("previous_message") if isinstance(ev.get("previous_message"), dict) else {}
        deleted_ts = str(ev.get("deleted_ts") or "")
        fields = _message_fields(prev)
        return Event(
            kind="message_deleted", ts=_to_float(ev.get("event_ts") or deleted_ts), channel=channel,
            user=str(prev.get("user") or ""), message_ts=deleted_ts, thread_ts=fields["thread_ts"],
            is_bot=is_bot_flag or bool(prev.get("bot_id")), deleted_ts=deleted_ts,
        )

    if sub not in _IGNORED_SUBTYPES:
        return None
    text = str(ev.get("text") or "")
    fields = _message_fields(ev)
    return Event(
        kind="message", ts=_to_float(ev.get("ts")), channel=channel, user=str(ev.get("user") or ""),
        message_ts=fields["message_ts"], thread_ts=fields["thread_ts"], text=text, is_bot=is_bot_flag,
        bot_mentioned=_is_bot_mention(text, bot_user_id), mentions=_mentions(text),
    )


def normalize_app_mention(body: Dict[str, Any], bot_user_id: str) -> Optional[Event]:
    """app_mention arrives as a second event for the same message: produce the same Event kind 'message' (same
    message_ts) so the router's dedupe by (channel, class, message_ts) collapses them."""
    ev = _payload(body)
    if ev.get("type") != "app_mention":
        return None
    text = str(ev.get("text") or "")
    fields = _message_fields(ev)
    return Event(
        kind="message", ts=_to_float(ev.get("ts")), channel=str(ev.get("channel") or ""),
        user=str(ev.get("user") or ""), message_ts=fields["message_ts"], thread_ts=fields["thread_ts"],
        text=text, is_bot=bool(ev.get("bot_id")), bot_mentioned=True, mentions=_mentions(text),
    )


def normalize_reaction(body: Dict[str, Any], added: bool) -> Optional[Event]:
    """reaction_added / reaction_removed: user, reaction, item.channel, item.ts (-> message_ts), item_user."""
    ev = _payload(body)
    if ev.get("type") not in ("reaction_added", "reaction_removed"):
        return None
    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    if item.get("type") != "message":
        return None
    kind = "reaction_added" if added else "reaction_removed"
    return Event(
        kind=kind, ts=_to_float(ev.get("event_ts") or item.get("ts")), channel=str(item.get("channel") or ""),
        user=str(ev.get("user") or ""), message_ts=str(item.get("ts") or ""),
        reaction=str(ev.get("reaction") or "").strip(":"), item_user=str(ev.get("item_user") or ""),
    )


def normalize_command(body: Dict[str, Any], now: Optional[float] = None) -> Event:
    """Slash command payload (command '/goose', text, user_id, channel_id). With should_escape true mentions arrive as
    <@U123|name>. `now` is the receive time; the gateway always passes ctx.clock.now(). Without it the event time is 0.0."""
    raw = str(body.get("text") or "").strip()
    slash = str(body.get("command") or "").lower().strip()
    parsed = parse_command_text(raw)
    trigger = str(body.get("trigger_id") or "")

    cmd = parsed["command"]
    args = parsed["args"]
    mentions = parsed["mentions"]

    if slash in ("/level", "/rank", "/card"):
        cmd = "me"
        args = raw
    elif slash in ("/leaderboard", "/top"):
        cmd = "top"
        args = raw
    elif slash == "/help":
        cmd = "help"
        args = raw

    return Event(
        kind="command", ts=float(now) if now is not None else 0.0,
        channel=str(body.get("channel_id") or ""), user=str(body.get("user_id") or ""),
        event_id="cmd:%s" % trigger if trigger else "", text=raw, command=cmd,
        args=args, mentions=mentions,
    )


def normalize_member_joined(body: Dict[str, Any]) -> Optional[Event]:
    ev = _payload(body)
    if ev.get("type") != "member_joined_channel":
        return None
    return Event(
        kind="member_joined", ts=_to_float(ev.get("event_ts")), channel=str(ev.get("channel") or ""),
        user=str(ev.get("user") or ""),
    )


def _error_code(exc: BaseException) -> str:
    """A short Slack error code when there is one, else the exception class name. Never the message text."""
    code = ""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            code = str(response["error"])
    except Exception:
        code = ""
    if not _ERROR_CODE_RE.match(code or ""):
        code = type(exc).__name__
    return code


def _run_action(client: Any, action: Action, default_channel: str) -> None:
    kind = action.kind
    channel = action.channel or default_channel
    if kind in ("post", "organizer_post"):
        if not action.text:
            return
        client.chat_postMessage(
            channel=channel, text=action.text,
            thread_ts=(action.thread_ts or None) if kind == "post" else None,
            parse="none", link_names=False, unfurl_links=False, unfurl_media=False,
        )
    elif kind == "ephemeral":
        if not action.text or not action.user:
            return
        client.chat_postEphemeral(
            channel=channel, user=action.user, text=action.text, thread_ts=action.thread_ts or None,
            parse="none", link_names=False,
        )
    elif kind == "reaction":
        client.reactions_add(channel=channel, name=action.emoji, timestamp=action.message_ts)
    elif kind == "file":
        kwargs: Dict[str, Any] = {"channel": channel, "filename": action.filename, "content": action.content,
                                  "title": action.filename}
        if action.text:
            kwargs["initial_comment"] = action.text
        client.files_upload_v2(**kwargs)
    elif kind == "update":
        if not action.text or not action.message_ts:
            return
        try:
            client.chat_update(
                channel=channel, ts=action.message_ts, text=action.text,
                parse="none", link_names=False,
            )
        except Exception as exc:
            log.warning("chat_update failed: %s, falling back to chat_postMessage", type(exc).__name__)
            client.chat_postMessage(
                channel=channel, text=action.text,
                thread_ts=action.thread_ts or None,
                parse="none", link_names=False, unfurl_links=False, unfurl_media=False,
            )
    else:
        raise ValueError("unknown_kind")


def execute(client: Any, actions: List[Action], default_channel: str = "") -> List[str]:
    """Run actions. post -> chat_postMessage(channel, text, thread_ts, parse='none', link_names=False, unfurl_links=False,
    unfurl_media=False); ephemeral -> chat_postEphemeral(channel, user, text, thread_ts); reaction -> reactions_add
    (ignore already_reacted); organizer_post -> chat_postMessage to the action's channel; file -> files_upload_v2 with
    filename and content. Each action is wrapped: one failure is logged (error code only, no text) and does not stop the
    others. Returns a list of error codes for failed actions."""
    errors: List[str] = []
    for action in actions:
        try:
            _run_action(client, action, default_channel)
        except Exception as exc:  # one bad action must not stop the rest
            code = _error_code(exc)
            if action.kind == "reaction" and code == "already_reacted":
                continue
            log.warning("slack action %s failed: %s", action.kind, code)
            errors.append(code)
    return errors
