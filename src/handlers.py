"""
==============================================================================
SLACK EVENT & COMMAND HANDLERS (src/handlers.py)
==============================================================================
This module contains the event listeners and slash command handlers for Slack.
Supports querying anyone's level card, public contributor stats, and AI chat.
Recent context is fetched only after a direct call, an explicit named opinion question,
or a follow-up in a thread Goose opened. Uncalled message text is not sent to the AI
or scanned for XP bonuses, and context is sanitized before use.
Recent channel context or the full current thread is fetched only after Goose is called.
Uncalled message text is not sent to the AI or scanned for XP bonuses.
Context remains in memory and common credentials are redacted before use.
==============================================================================
"""

import re
import logging
import math
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict
from slack_bolt import App

from config import (
    AI_REPLY_QUEUE_SIZE,
    AI_REPLY_WORKERS,
    AI_TIMEOUT_SECONDS,
    CONFIG,
    is_channel_watched,
)
from database import (
    get_or_create_user,
    resolve_user,
    check_cooldown,
    record_message_and_add_xp,
    increment_message_count_only,
    get_leaderboard
)
from leveling import (
    get_level_info_for_xp,
    format_rank_card,
    format_level_up_message,
    format_leaderboard
)
import ai_service

logger = logging.getLogger("SlackBotHandlers")
THREAD_PAGE_SIZE = 200
CHANNEL_CONTEXT_LIMIT = 10
BOT_THREAD_CACHE_SIZE = 512
_ORRY_NAME_RE = re.compile(r"\borry\b", re.IGNORECASE)
_NAMED_GOOSE_RE = re.compile(r"\bGoose(?:\s+Lee)?\b")
_BOT_NAME_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hey|yo|hi|hello)\s+)?@?(?:goose(?:-|\s)?lee|goose|duck)\b"
    r"(?:\s*[,!:]\s*|\s+(?=(?:can|could|would|will|what|who|how|help|please|check|look|tell|show|explain|do|is|are|your)\b))",
    re.IGNORECASE,
)
_ADDRESSED_GOOSE_RE = re.compile(r"[,!:]\s*@?goose\b\s*[,!?]", re.IGNORECASE)
_GOOSE_COMPARISON_RE = re.compile(r"\b(?:you\s+or|between)\s+goose\b|\bgoose\s+or\s+orry\b", re.IGNORECASE)
_EXPLICIT_OPINION_RE = re.compile(
    r"\b(?:what\s+do\s+you\s+think(?:\s+about)?|do\s+you\s+think|what(?:['’]s| is)\s+your\s+(?:opinion|take)|"
    r"what\s+are\s+your\s+thoughts(?:\s+on)?|your\s+(?:opinion|thoughts)(?:\s+(?:on|about))?|"
    r"who\s+do\s+you\s+think|do\s+you\s+prefer|which\s+do\s+you\s+prefer|who(?:['’]s| is)\s+better|"
    r"is\s+.{1,80}\s+better\s+than)\b",
    re.IGNORECASE,
)


_AI_REPLY_EXECUTOR = ThreadPoolExecutor(max_workers=AI_REPLY_WORKERS, thread_name_prefix="goose-ai")
_AI_REPLY_SLOTS = threading.BoundedSemaphore(AI_REPLY_WORKERS + AI_REPLY_QUEUE_SIZE)
_BOT_THREADS = OrderedDict()
_BOT_THREAD_QUEUE_KEYS = {}
_BOT_THREADS_LOCK = threading.Lock()
_AI_REPLY_QUEUES = {}
_AI_REPLY_ACTIVE_KEYS = set()
_AI_REPLY_QUEUE_LOCK = threading.Lock()


def clean_mention_text(text: str) -> str:
    """Removes Slack user mention tags like <@U12345678> from text."""
    if not text:
        return ""
    return re.sub(r"<@[A-Z0-9_]+(?:\|[^>]+)?>", "", text, flags=re.IGNORECASE).strip()


def sanitize_context_text(text: str) -> str:
    """Redact common credentials before message text reaches the model."""
    if not text:
        return ""
    text = re.sub(r"xox[baprst]-[A-Za-z0-9-]+", "[REDACTED_SLACK_TOKEN]", text)
    text = re.sub(r"\b(?:sk|ghp|gho|glpat|AIza)[A-Za-z0-9_\-]{16,}\b", "[REDACTED_API_KEY]", text)
    text = re.sub(
        r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
        "Bearer [REDACTED_TOKEN]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.DOTALL,
    )


def is_addressed_to_bot(text: str, bot_user_id: Optional[str] = None, is_dm: bool = False) -> bool:
    if is_dm:
        return True
    if not text:
        return False
    if bot_user_id and re.search(
        rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", text, flags=re.IGNORECASE
    ):
        return True
    return bool(_BOT_NAME_PREFIX_RE.search(text))


def clean_bot_addressing(text: str, bot_user_id: Optional[str] = None) -> str:
    """Remove Slack mentions or a clear Goose name prefix from the addressed message."""
    cleaned = clean_mention_text(text or "")
    return _BOT_NAME_PREFIX_RE.sub("", cleaned, count=1).strip()


def _remember_bot_thread(channel_id: Optional[str], thread_ts: Optional[str], queue_key=None) -> None:
    if not channel_id or not thread_ts:
        return
    key = (str(channel_id), str(thread_ts))
    with _BOT_THREADS_LOCK:
        _BOT_THREADS[key] = None
        _BOT_THREADS.move_to_end(key)
        if queue_key is not None:
            _BOT_THREAD_QUEUE_KEYS[key] = queue_key
        while len(_BOT_THREADS) > BOT_THREAD_CACHE_SIZE:
            old_key, _ = _BOT_THREADS.popitem(last=False)
            _BOT_THREAD_QUEUE_KEYS.pop(old_key, None)


def _is_known_bot_thread(channel_id: Optional[str], thread_ts: Optional[str]) -> bool:
    if not channel_id or not thread_ts:
        return False
    with _BOT_THREADS_LOCK:
        return (str(channel_id), str(thread_ts)) in _BOT_THREADS


def _bot_thread_queue_key(channel_id: Optional[str], thread_ts: Optional[str]):
    if not channel_id or not thread_ts:
        return None
    with _BOT_THREADS_LOCK:
        return _BOT_THREAD_QUEUE_KEYS.get((str(channel_id), str(thread_ts)))


def is_explicit_opinion_request(text: str) -> bool:
    """Only recognize explicit opinion requests that name Goose or Orry."""
    message = text or ""
    names_goose_or_orry = any(
        matcher.search(message)
        for matcher in (
            _ORRY_NAME_RE,
            _NAMED_GOOSE_RE,
            _BOT_NAME_PREFIX_RE,
            _ADDRESSED_GOOSE_RE,
            _GOOSE_COMPARISON_RE,
        )
    )
    return bool(_EXPLICIT_OPINION_RE.search(message) and names_goose_or_orry)


def parse_card_request_targets(text: str, caller_id: str, bot_user_id: Optional[str] = None) -> Optional[List[str]]:
    """
    Parses a message or command string to determine if it is asking for a rank/level card,
    and returns a list of target user identifiers (mentions, IDs, or usernames).

    Returns:
      - None if text is not asking for a card.
      - [caller_id] if asking for caller's own card (e.g. '!level', 'card').
      - List of target strings if asking for others (e.g. ['jerry', 'netic']).
    """
    if not text:
        return None

    clean = text.strip()
    if bot_user_id:
        clean = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", "", clean).strip()
    clean_lower = clean.lower().strip()

    # 1. Exact and conversational self card commands
    self_triggers = (
        "/level", "!level", ".level", "level",
        "/rank", "!rank", ".rank", "rank",
        "/card", "!card", ".card", "card",
        "/stats", "!stats", ".stats", "stats",
        "/xp", "!xp", ".xp", "xp",
        "my level", "my rank", "my card", "my stats", "my xp", "my points",
        "my rank card", "what is my level", "what's my level", "what is my rank", "what's my rank",
        "what is my card", "what's my card", "what is my rank card", "what's my rank card",
        "check level", "check my level", "show my stats", "show me my stats",
        "show my card", "show me my card", "show my level", "show me my level",
        "give me my card", "give me my stats"
    )
    self_regex = r"^(?:what(?:'s|\s+is)\s+my|show\s+(?:me\s+)?my|give\s+me\s+my|check\s+my)\s+(?:rank|card|level|stats|xp|points)(?:\s+card)?.*$"
    if clean_lower in self_triggers or re.match(self_regex, clean_lower):
        return [caller_id]

    # 2. Conversational card queries like "give me the card for X", "card for X and Y"
    conversational_patterns = [
        r"^(?:please\s+)?(?:give\s+me|show\s+me|show|fetch|get|check|display|view|send)\s+(?:the\s+)?(?:card|level|rank|stats|xp|points)\s+(?:for|of)\s+(.+)$",
        r"^(?:what\s+is|what\'?s|how\s+much\s+is|check)\s+(?:the\s+)?(?:level|rank|stats|xp|points)\s+(?:of|for)\s+(.+)$",
        r"^(?:card|stats|level|rank|xp|points)\s+(?:for|of)\s+(.+)$",
    ]
    for pat in conversational_patterns:
        m = re.match(pat, clean, re.IGNORECASE)
        if m:
            raw_targets = m.group(1).strip()
            raw_targets = re.sub(r"[?!.]+$", "", raw_targets).strip()
            tokens = [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_targets) if t.strip()]
            return tokens if tokens else [caller_id]

    # 3. Direct command with argument: "!level @user", "level netic", "/level U12345", "!card @user"
    cmd_prefix_pattern = r"^(!|/|\.)?(level|rank|card|stats|xp)\s+(.+)$"
    m = re.match(cmd_prefix_pattern, clean, re.IGNORECASE)
    if m:
        raw_targets = m.group(3).strip()
        raw_targets = re.sub(r"[?!.]+$", "", raw_targets).strip()
        tokens = [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_targets) if t.strip()]
        return tokens if tokens else [caller_id]

    # 4. If text contains mention(s) and keyword card/level/rank/stats
    mentions = re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>", clean)
    if mentions:
        keywords = ("card", "level", "rank", "stats", "xp", "points")
        if any(kw in clean_lower for kw in keywords):
            return mentions

    return None


def get_cards_response_for_targets(targets: List[str], caller_id: str) -> str:
    """
    Resolves targets (usernames, user IDs, or mentions) to level cards.
    Formats cards with progress bars and celebration titles.
    """
    cards = []
    for target in targets:
        uid, user_data = resolve_user(target)
        if user_data:
            cards.append(format_rank_card(user_data))
        else:
            clean_name = target.strip().lstrip("@")
            cards.append(f"No activity record found for '{clean_name}' yet.")
    return "\n\n".join(cards)


def _context_turns(raw_messages: List[Dict], bot_user_id: Optional[str], current_ts: Optional[str],
                   excluded_ts: Optional[str] = None) -> List[Dict[str, str]]:
    turns = []
    if not current_ts:
        return turns
    try:
        current_time = float(current_ts)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid Slack timestamp; dropping conversation context")
        return turns
    if not math.isfinite(current_time):
        logger.warning("Non-finite Slack timestamp; dropping conversation context")
        return turns

    for message in raw_messages:
        message_ts = str(message.get("ts") or "")
        if excluded_ts and message_ts == str(excluded_ts):
            continue
        if not message_ts:
            continue
        try:
            message_time = float(message_ts)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(message_time) or message_time > current_time:
            continue

        text = sanitize_context_text(str(message.get("text") or "").strip())
        if not text:
            continue
        sender = message.get("user")
        if not sender:
            continue
        if sender == bot_user_id:
            clean_text = ai_service.clean_ai_output(text)
            if clean_text:
                turns.append({"role": "assistant", "content": clean_text})
        else:
            clean_text = clean_mention_text(text)
            clean_text = sanitize_context_text(clean_text)
            if clean_text:
                turns.append({"role": "user", "content": clean_text})
    return turns


def fetch_mentioned_thread_context(client, channel_id: str, thread_ts: str,
                                   current_ts: Optional[str], bot_user_id: Optional[str]) -> List[Dict[str, str]]:
    """Fetch every message in the one thread containing the current bot mention."""
    if not client or not channel_id or not thread_ts or not current_ts:
        return []

    messages = []
    cursor = None
    seen_cursors = set()
    try:
        while True:
            arguments = {"channel": channel_id, "ts": thread_ts, "limit": THREAD_PAGE_SIZE}
            if cursor:
                arguments["cursor"] = cursor
            response = client.conversations_replies(**arguments)
            if response.get("ok") is False:
                raise RuntimeError("Slack rejected thread context request")
            messages.extend(response.get("messages", []))
            next_cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        messages.sort(key=lambda message: float(message.get("ts") or 0))
        return _context_turns(messages, bot_user_id, current_ts, excluded_ts=current_ts)
    except Exception as exc:
        logger.warning("Could not retrieve the mentioned Slack thread (%s)", type(exc).__name__)
        return []


def fetch_mentioned_channel_context(client, channel_id: str, current_ts: Optional[str],
                                    bot_user_id: Optional[str]) -> List[Dict[str, str]]:
    """Fetch recent messages from the same channel, only after an explicit bot mention."""
    if not client or not channel_id or not current_ts:
        return []
    try:
        response = client.conversations_history(
            channel=channel_id,
            latest=current_ts,
            inclusive=True,
            limit=CHANNEL_CONTEXT_LIMIT,
        )
        if response.get("ok") is False:
            raise RuntimeError("Slack rejected channel context request")
        messages = list(reversed(response.get("messages", [])))
        return _context_turns(messages, bot_user_id, current_ts, excluded_ts=current_ts)
    except Exception as exc:
        logger.warning("Could not retrieve recent channel context (%s)", type(exc).__name__)
        return []


def _post_final_ai_reply(client, channel_id: str, thread_ts: Optional[str], text: str,
                         queue_key=None) -> bool:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=text,
            thread_ts=thread_ts or None,
            parse="none",
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
        )
        getter = getattr(response, "get", None)
        final_ts = getter("ts") if callable(getter) else None
        if not isinstance(final_ts, str):
            final_ts = ""
        _remember_bot_thread(channel_id, thread_ts or final_ts, queue_key)
        return True
    except Exception as exc:
        logger.error("Could not deliver the AI reply (%s)", type(exc).__name__)
        return False


def _is_definitive_placeholder_update_failure(error) -> bool:
    data = error if callable(getattr(error, "get", None)) else None
    if data is None:
        response = getattr(error, "response", None)
        data = getattr(response, "data", None)
        if data is None and callable(getattr(response, "get", None)):
            data = response
    code = data.get("error") if callable(getattr(data, "get", None)) else None
    return code in {"message_not_found", "cant_update_message"}


def _deliver_ai_reply(client, channel_id: str, thread_ts: Optional[str], placeholder_ts: str, text: str,
                      queue_key=None) -> bool:
    if placeholder_ts:
        try:
            response = client.chat_update(
                channel=channel_id,
                ts=placeholder_ts,
                text=text,
                parse="none",
                link_names=False,
            )
            getter = getattr(response, "get", None)
            if callable(getter) and getter("ok") is False:
                if _is_definitive_placeholder_update_failure(response):
                    return _post_final_ai_reply(client, channel_id, thread_ts, text, queue_key)
                logger.warning("Slack rejected an AI placeholder update")
                return False
            return True
        except Exception as exc:
            logger.warning("Could not update the AI reply placeholder (%s)", type(exc).__name__)
            if _is_definitive_placeholder_update_failure(exc):
                return _post_final_ai_reply(client, channel_id, thread_ts, text, queue_key)
            return False
    return _post_final_ai_reply(client, channel_id, thread_ts, text, queue_key)


def _post_ai_placeholder(client, channel_id: str, thread_ts: Optional[str], queue_key=None) -> str:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text="_Goose Lee is writing a short reply..._",
            thread_ts=thread_ts or None,
            parse="none",
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
        )
        getter = getattr(response, "get", None)
        if callable(getter):
            placeholder_ts = str(getter("ts") or "")
        else:
            placeholder_ts = str(response["ts"] or "") if response is not None else ""
        _remember_bot_thread(channel_id, thread_ts or placeholder_ts, queue_key)
        return placeholder_ts
    except Exception as exc:
        logger.warning("Could not post an AI reply placeholder (%s)", type(exc).__name__)
        return ""


def _run_ai_reply(client, channel_id: str, user_id: str, user_message: str,
                  thread_ts: Optional[str], context_thread_ts: Optional[str],
                  current_ts: Optional[str], bot_user_id: Optional[str], queue_key) -> None:
    try:
        placeholder_ts = _post_ai_placeholder(client, channel_id, thread_ts, queue_key)
        try:
            if context_thread_ts:
                context_messages = fetch_mentioned_thread_context(
                    client, channel_id, context_thread_ts, current_ts, bot_user_id
                )
            else:
                context_messages = fetch_mentioned_channel_context(
                    client, channel_id, current_ts, bot_user_id
                )
            reply = ai_service.generate_response(
                user_id=user_id,
                user_message=user_message or "Hello!",
                channel_id=channel_id,
                context_messages=context_messages,
                timeout=AI_TIMEOUT_SECONDS,
            )
            reply = (reply or "").strip() or ai_service.get_offline_fallback(user_id)
        except Exception as exc:
            logger.error("AI reply failed (%s)", type(exc).__name__)
            reply = "I could not complete that reply. Please try again."

        _deliver_ai_reply(client, channel_id, thread_ts, placeholder_ts, reply, queue_key)
    finally:
        _AI_REPLY_SLOTS.release()


def _drain_ai_reply_queue(key) -> None:
    """Run each user's/thread's replies sequentially while allowing other keys to run in parallel."""
    while True:
        with _AI_REPLY_QUEUE_LOCK:
            jobs = _AI_REPLY_QUEUES.get(key)
            if not jobs:
                _AI_REPLY_QUEUES.pop(key, None)
                _AI_REPLY_ACTIVE_KEYS.discard(key)
                return
            job = jobs.popleft()
        try:
            _run_ai_reply(*job[:9])
        except Exception as exc:
            logger.error("Queued AI reply worker failed (%s)", type(exc).__name__)


def _queue_ai_reply(client, say, channel_id: str, user_id: str, message: str,
                    thread_ts: Optional[str], context_thread_ts: Optional[str],
                    current_ts: Optional[str], bot_user_id: Optional[str]) -> None:
    if not _AI_REPLY_SLOTS.acquire(blocking=False):
        say(
            text="I am handling several replies right now. Please try again in a moment.",
            channel=channel_id,
            thread_ts=thread_ts,
        )
        return

    thread_root = context_thread_ts or thread_ts
    key = _bot_thread_queue_key(channel_id, thread_root) or ((
        str(channel_id or ""),
        "thread",
        str(thread_root),
    ) if thread_root else (
        str(channel_id or ""),
        "user",
        str(user_id or ""),
    ))
    job = (client, channel_id, user_id, message, thread_ts, context_thread_ts, current_ts, bot_user_id, key, say)
    try:
        with _AI_REPLY_QUEUE_LOCK:
            queue = _AI_REPLY_QUEUES.setdefault(key, deque())
            queue.append(job)
            should_submit = key not in _AI_REPLY_ACTIVE_KEYS
            if should_submit:
                _AI_REPLY_ACTIVE_KEYS.add(key)
        if should_submit:
            _AI_REPLY_EXECUTOR.submit(_drain_ai_reply_queue, key)
    except Exception as exc:
        logger.error("Could not queue an AI reply (%s)", type(exc).__name__)
        with _AI_REPLY_QUEUE_LOCK:
            pending = list(_AI_REPLY_QUEUES.pop(key, ()))
            _AI_REPLY_ACTIVE_KEYS.discard(key)
        if not pending:
            pending = [job]
        for pending_job in pending:
            _AI_REPLY_SLOTS.release()
            _, job_channel, job_user, _, job_thread, _, _, _, _, job_say = pending_job
            try:
                job_say(
                    text=ai_service.get_offline_fallback(job_user),
                    channel=job_channel,
                    thread_ts=job_thread,
                )
            except Exception as send_exc:
                logger.error("Could not send an AI fallback (%s)", type(send_exc).__name__)


def _record_private_activity(user_id: str, channel_id: str, event, client, say, rules) -> None:
    """Award base XP without reading or storing uncalled message text."""
    cooldown_sec = rules.get("cooldown_seconds", 30)
    can_earn, _remaining = check_cooldown(user_id, cooldown_sec)
    if not can_earn:
        increment_message_count_only(user_id)
        return

    base_xp = rules.get("base_xp_per_message", 10)
    current_user = get_or_create_user(user_id)
    old_level, _old_title = get_level_info_for_xp(current_user["xp"])
    new_total_xp = current_user["xp"] + base_xp
    new_level, new_title = get_level_info_for_xp(new_total_xp)
    record_message_and_add_xp(
        user_id=user_id,
        channel_id=channel_id,
        xp_to_add=base_xp,
        new_level=new_level,
        message_length=0,
        is_suggestion=False,
        update_cooldown=True,
    )
    if new_level > old_level and event.get("ts"):
        try:
            client.reactions_add(channel=channel_id, timestamp=event["ts"], name="tada")
        except Exception:
            pass
    if new_level > old_level and new_level >= 4 and rules.get("announce_level_up_in_channel", True):
        try:
            say(text=format_level_up_message(user_id, new_level, new_title), channel=channel_id)
        except Exception as exc:
            logger.warning("Could not announce level up (%s)", type(exc).__name__)


def register_handlers(app: App, bot_user_id: Optional[str] = None) -> None:
    """Registers all Slack Bolt event listeners and slash commands."""

    # 1. MESSAGE EVENT: Track activity and support in-chat command triggers
    @app.event("message")
    def handle_message_events(body, client, say):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        text = (event.get("text") or "").strip()
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")

        # Determine thread target based on config
        rules = CONFIG.get("interaction_rules", {})
        reply_in_thread = rules.get("reply_in_thread", False)
        target_thread_ts = event.get("thread_ts") or (
            event.get("ts") if reply_in_thread or event.get("reply_count") else None
        )

        # Never process messages from bots to prevent loops
        if bot_id or subtype == "bot_message" or not user_id:
            return
        if subtype in ["message_deleted", "message_changed", "channel_join", "channel_leave", "pinned_info"]:
            return
        if not is_channel_watched(channel_id):
            return

        is_dm = bool(channel_id and channel_id.startswith("D")) or event.get("channel_type") == "im"
        is_direct_bot_mention = bool(
            bot_user_id
            and re.search(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", text, flags=re.IGNORECASE)
        )
        is_addressed = is_addressed_to_bot(text, bot_user_id, is_dm=is_dm) or _is_known_bot_thread(
            channel_id, event.get("thread_ts")
        )
        is_opinion_question = not is_direct_bot_mention and is_explicit_opinion_request(text)
        is_prefixed_cmd = text.startswith(("!", "/", "."))
        is_called = is_addressed or is_opinion_question or is_prefixed_cmd

        # Ordinary messages get metadata-only XP. Do not scan their contents for scoring.
        if not is_called:
            _record_private_activity(user_id, channel_id, event, client, say, rules)
            return

        clean_lower = clean_bot_addressing(text, bot_user_id).lower().strip()

        # Only process in-chat commands if the message is NOT directly mentioning the bot.
        # When mentioning the bot, handle_app_mentions will handle the command/reply, avoiding double-replies!
        if not is_direct_bot_mention:
            # Check if the user is asking for their card or anyone else's card
            card_targets = parse_card_request_targets(clean_lower, caller_id=user_id, bot_user_id=bot_user_id)
            if card_targets is not None:
                reply_text = get_cards_response_for_targets(card_targets, caller_id=user_id)
                try:
                    say(text=reply_text, channel=channel_id, thread_ts=target_thread_ts)
                except Exception as e:
                    logger.error(f"Failed to send rank card via chat command: {e}")
                return

            # Leaderboard in-chat trigger
            if clean_lower in ("/leaderboard", "!leaderboard", ".leaderboard", "leaderboard", "/top", "!top", "top", "ranks"):
                leaders = get_leaderboard(limit=10)
                board = format_leaderboard(leaders)
                try:
                    say(text=board, channel=channel_id, thread_ts=target_thread_ts)
                except Exception as e:
                    logger.error(f"Failed to send leaderboard via chat command: {e}")
                return

            # Repo in-chat trigger
            if clean_lower in ("/repo", "!repo", ".repo", "repo", "github", "gh repo", "!github"):
                repo_msg = "Repository: <https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>."
                try:
                    say(text=repo_msg, channel=channel_id, thread_ts=target_thread_ts)
                except Exception as e:
                    logger.error(f"Failed to send repo: {e}")
                return

            # Help in-chat trigger
            if clean_lower in ("!help", "/help", "bot help", "bothelp", "help"):
                templates = CONFIG.get("templates", {})
                help_msg = templates.get("help_message", "Type `/level` or `/leaderboard`!")
                formatted_help = help_msg.format(
                    bot_id=bot_user_id or "bot",
                    cooldown=rules.get("cooldown_seconds", 30)
                )
                try:
                    say(text=formatted_help, channel=channel_id, thread_ts=target_thread_ts)
                except Exception as e:
                    logger.error(f"Failed to send help: {e}")
                return

        should_respond = is_addressed and (is_dm or not is_direct_bot_mention)
        if is_direct_bot_mention and not is_dm:
            # The app_mention listener owns public Slack mention events.
            return
        if should_respond or is_opinion_question:
            _queue_ai_reply(
                client=client,
                say=say,
                channel_id=channel_id,
                user_id=user_id,
                message=sanitize_context_text(clean_bot_addressing(text, bot_user_id)) or "Hello!",
                thread_ts=target_thread_ts,
                context_thread_ts=event.get("thread_ts") or (
                    event.get("ts") if event.get("reply_count") else None
                ),
                current_ts=event.get("ts"),
                bot_user_id=bot_user_id,
            )
            return

    # 2. APP MENTION EVENT: Handle @bot mentions (AI chat or commands)
    @app.event("app_mention")
    def handle_app_mentions(body, say, client):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        raw_text = event.get("text", "")

        rules = CONFIG.get("interaction_rules", {})
        reply_in_thread = rules.get("reply_in_thread", False)
        target_thread_ts = event.get("thread_ts") or (
            event.get("ts") if reply_in_thread or event.get("reply_count") else None
        )

        if not user_id:
            return
        if not is_channel_watched(channel_id):
            return

        cleaned_text = sanitize_context_text(clean_bot_addressing(raw_text, bot_user_id))
        clean_lower = cleaned_text.lower()

        # Check if the user is asking for their card or someone else's card
        card_targets = parse_card_request_targets(raw_text, caller_id=user_id, bot_user_id=bot_user_id)
        if card_targets is not None:
            reply_text = get_cards_response_for_targets(card_targets, caller_id=user_id)
            say(text=reply_text, thread_ts=target_thread_ts)
            return

        # Leaderboard command
        if clean_lower in ("leaderboard", "top", "ranks", "scores"):
            leaders = get_leaderboard(limit=10)
            board = format_leaderboard(leaders)
            say(text=board, thread_ts=target_thread_ts)
            return

        # Repo command
        if clean_lower in ("repo", "github", "gh repo", "gimme the gh repo", "give me the gh repo", "give me the repo", "link"):
            repo_msg = "Repository: <https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>."
            say(text=repo_msg, thread_ts=target_thread_ts)
            return

        # Help command
        if clean_lower in ("help", "commands"):
            templates = CONFIG.get("templates", {})
            help_msg = templates.get("help_message", "Type `/level` or `/leaderboard`!")
            formatted_help = help_msg.format(
                bot_id=bot_user_id or "bot",
                cooldown=rules.get("cooldown_seconds", 30)
            )
            say(text=formatted_help, thread_ts=target_thread_ts)
            return

        # Keep Slack event handlers free while the bounded worker pool gets the model answer.
        _queue_ai_reply(
            client=client,
            say=say,
            channel_id=channel_id,
            user_id=user_id,
            message=cleaned_text or "Hello!",
            thread_ts=target_thread_ts,
            context_thread_ts=event.get("thread_ts") or (
                event.get("ts") if event.get("reply_count") else None
            ),
            current_ts=event.get("ts"),
            bot_user_id=bot_user_id,
        )


    # 3. SLASH COMMANDS
    @app.command("/level")
    def handle_level_command(ack, command, respond, say):
        ack()
        caller_id = command.get("user_id")
        raw_text = (command.get("text") or "").strip()

        if raw_text:
            tokens = [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_text) if t.strip()]
            targets = tokens if tokens else [caller_id]
        else:
            targets = [caller_id]

        reply = get_cards_response_for_targets(targets, caller_id)
        try:
            respond(text=reply, response_type="in_channel")
        except Exception:
            say(text=reply)

    @app.command("/rank")
    def handle_rank_command(ack, command, respond, say):
        ack()
        caller_id = command.get("user_id")
        raw_text = (command.get("text") or "").strip()

        if raw_text:
            tokens = [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_text) if t.strip()]
            targets = tokens if tokens else [caller_id]
        else:
            targets = [caller_id]

        reply = get_cards_response_for_targets(targets, caller_id)
        try:
            respond(text=reply, response_type="in_channel")
        except Exception:
            say(text=reply)

    @app.command("/card")
    def handle_card_command(ack, command, respond, say):
        ack()
        caller_id = command.get("user_id")
        raw_text = (command.get("text") or "").strip()
        if raw_text:
            targets = [
                target.strip()
                for target in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_text)
                if target.strip()
            ] or [caller_id]
        else:
            targets = [caller_id]
        reply = get_cards_response_for_targets(targets, caller_id)
        try:
            respond(text=reply, response_type="in_channel")
        except Exception:
            say(text=reply)

    @app.command("/leaderboard")
    def handle_leaderboard_command(ack, respond, say):
        ack()
        leaders = get_leaderboard(limit=10)
        board = format_leaderboard(leaders)
        try:
            respond(text=board, response_type="in_channel")
        except Exception:
            say(text=board)
