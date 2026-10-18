"""
==============================================================================
SLACK EVENT & COMMAND HANDLERS (src/handlers.py)
==============================================================================
Handles Slack message events, @mentions, and slash commands (/level, /rank,
/card, /leaderboard).
- Thread context is fetched only when needed; same-channel history only for short or referential calls.
- Ordinary uncalled messages earn XP without sending message contents to AI.
- Common credentials and tokens are redacted before reaching any AI model.
==============================================================================
"""

from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import re
import threading
import time
from typing import Dict, List, Optional
from slack_bolt import App

from config import (
    AI_REPLY_QUEUE_SIZE,
    AI_REPLY_WORKERS,
    AI_TIMEOUT_SECONDS,
    ASSETS,
    CONFIG,
    DISTRESS,
    GOOSE_METER,
    OFFICIAL_LOADOUT_CHANNEL_ID,
    ORGANIZER_CHANNEL_ID,
    ORGANIZER_USER_IDS,
    OWNER_USER_ID,
    REACTIONS,
    REWARDS,
    ROASTS,
    SEASON,
    VOICE_ENABLED,
    VOICE_HOURLY_LIMIT,
    is_channel_watched,
)
from database import (
    _DB_LOCK,
    _managed_connection,
    add_appeal,
    add_audit,
    backup_database,
    check_cooldown,
    claim_state,
    consume_voice_quota,
    create_report,
    get_appeals,
    get_channel_counter,
    get_flag,
    get_leaderboard,
    get_or_create_user,
    get_reports,
    get_state,
    resolve_appeal,
    increment_message_count_only,
    record_message_and_add_xp,
    resolve_user,
    set_flag,
    set_state,
    touch_channel_counter,
)
from leveling import (
    evaluate_message_xp,
    format_leaderboard,
    format_level_up_message,
    format_rank_card,
    get_level_info_for_xp,
)
import ai_service
import crumbs
import slack_assets

logger = logging.getLogger("SlackBotHandlers")

THREAD_PAGE_SIZE = 200
CHANNEL_CONTEXT_LIMIT = 10
LEADERBOARD_DISPLAY_LIMIT = 200
BOT_THREAD_CACHE_SIZE = 512

_IGNORED_SUBTYPES = {
    "bot_message",
    "message_deleted",
    "message_changed",
    "channel_join",
    "channel_leave",
    "pinned_info",
    "channel_topic",
    "channel_purpose",
    "channel_name",
}

_BOT_NAME_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hey|yo|hi|hello)\s+)?@?(?:goose(?:-|\s)?lee|goose|duck)\b"
    r"(?:\s*[,!:]\s*|\s+(?=(?:can|could|would|will|what|who|how|help|please|check|look|tell|show|explain|do|is|are|your)\b)|\s*$)",
    re.IGNORECASE,
)
_GREETING_WORDS = r"(?:hey|yo|hi|hello|sup|oi|ok|okay)"
_GOOSE_NAME = r"(?:goose(?:-|\s)?lee|goose|duck)"
_GOOSE_STRONG_NAME = r"(?:goose(?:-|\s)?lee|goose)"
# A name counts as a call only in vocative positions: at the start of the
# message, after a greeting, as @goose, after a comma, or followed by
# punctuation. A bare mid-sentence mention (e.g. "that goose is ...") is NOT a call.
_GOOSE_CALL_RE = re.compile(
    r"(?:"
    rf"^\s*(?:{_GREETING_WORDS}\s+)?@?{_GOOSE_STRONG_NAME}\b"
    rf"|^\s*(?:{_GREETING_WORDS}\s+)?@?duck\b\s*[?!,:]"
    rf"|(?:^|\s){_GREETING_WORDS}\s+@?{_GOOSE_NAME}\b"
    rf"|(?:^|\s|,)@{_GOOSE_NAME}\b"
    rf"|,\s*@?{_GOOSE_NAME}\b"
    rf"|@?{_GOOSE_NAME}\b\s*[?!]"
    rf"|@?{_GOOSE_NAME}\b\s*,"
    r")",
    re.IGNORECASE,
)
_CARD_KEYWORD_WORD_RE = re.compile(r"\b(?:card|level|rank|stats|xp|points)\b", re.IGNORECASE)
_COMMAND_PREFIX_RE = re.compile(
    r"^[!/.](?:level|rank|card|stats|xp|leaderboard|top|ranks|repo|github|help|bothelp)\b",
    re.IGNORECASE,
)
_CONTEXT_CUE_RE = re.compile(
    r"^\s*(?:and|but|so|also|then|plus|what about|how about|why|"
    r"it|this|that|these|those|they|them|he|she|him|her|there)\b",
    re.IGNORECASE,
)

_AI_REPLY_EXECUTOR = ThreadPoolExecutor(
    max_workers=AI_REPLY_WORKERS, thread_name_prefix="goose-ai"
)
_AI_REPLY_SLOTS = threading.BoundedSemaphore(AI_REPLY_WORKERS + AI_REPLY_QUEUE_SIZE)
_BOT_THREADS: OrderedDict = OrderedDict()
_BOT_THREAD_QUEUE_KEYS: Dict = {}
_BOT_THREADS_LOCK = threading.Lock()
_AI_REPLY_QUEUES: Dict = {}
_AI_REPLY_ACTIVE_KEYS = set()
_AI_REPLY_QUEUE_LOCK = threading.Lock()

_REPLY_TRIGGER_TTL_SECONDS = 120.0
_REPLY_TRIGGER_CACHE_SIZE = 1024
_RECENT_REPLY_TRIGGERS: "OrderedDict[tuple, float]" = OrderedDict()
_RECENT_REPLY_TRIGGERS_LOCK = threading.Lock()


def _claim_reply_trigger(channel_id: Optional[str], message_ts: Optional[str]) -> bool:
    """
    Claim a (channel, message) trigger so the same message yields exactly one AI reply.

    Slack can deliver the same message as both an app_mention and a message event,
    and can redeliver on reconnect. Returns True for the first claim, False for a
    duplicate seen within the TTL window.
    """
    if not message_ts:
        return True
    key = (str(channel_id or ""), str(message_ts))
    now = time.monotonic()
    with _RECENT_REPLY_TRIGGERS_LOCK:
        while _RECENT_REPLY_TRIGGERS:
            oldest_key, claimed_at = next(iter(_RECENT_REPLY_TRIGGERS.items()))
            if now - claimed_at > _REPLY_TRIGGER_TTL_SECONDS:
                _RECENT_REPLY_TRIGGERS.popitem(last=False)
            else:
                break
        if key in _RECENT_REPLY_TRIGGERS:
            return False
        _RECENT_REPLY_TRIGGERS[key] = now
        while len(_RECENT_REPLY_TRIGGERS) > _REPLY_TRIGGER_CACHE_SIZE:
            _RECENT_REPLY_TRIGGERS.popitem(last=False)
    return True


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
    text = re.sub(
        r"\b(?:sk|ghp|gho|github_pat|glpat|AIza|AKIA)[A-Za-z0-9_\-]{12,}\b",
        "[REDACTED_API_KEY]",
        text,
    )
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


def needs_channel_context(text: str) -> bool:
    """True only for very short or referential messages that cannot stand alone."""
    cleaned = sanitize_context_text(text or "").strip()
    if not cleaned:
        return False
    words = re.findall(r"[A-Za-z0-9']+", cleaned)
    if len(words) <= 3:
        return True
    return bool(_CONTEXT_CUE_RE.match(cleaned))


def is_addressed_to_bot(
    text: str, bot_user_id: Optional[str] = None, is_dm: bool = False
) -> bool:
    if is_dm:
        return True
    if not text:
        return False
    if bot_user_id and re.search(
        rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", text, flags=re.IGNORECASE
    ):
        return True
    return bool(_GOOSE_CALL_RE.search(text))


def clean_bot_addressing(text: str, bot_user_id: Optional[str] = None) -> str:
    """
    Removes only the bot's Slack mention tag and leading Goose name prefix,
    preserving mentions of other users in the message.
    """
    if not text:
        return ""
    cleaned = text.strip()
    if bot_user_id:
        cleaned = re.sub(
            rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
    else:
        cleaned = re.sub(
            r"^\s*<@[A-Z0-9_]+(?:\|[^>]+)?>\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
    return _BOT_NAME_PREFIX_RE.sub("", cleaned, count=1).strip()


def _remember_bot_thread(
    channel_id: Optional[str], thread_ts: Optional[str], queue_key=None
) -> None:
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


def _split_target_tokens(raw_targets: str) -> List[str]:
    cleaned = re.sub(r"[?!.]+$", "", raw_targets.strip()).strip()
    return [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", cleaned) if t.strip()]


def parse_card_request_targets(
    text: str, caller_id: str, bot_user_id: Optional[str] = None
) -> Optional[List[str]]:
    """
    Parses a message or command string to determine if it is asking for a rank/level card,
    and returns a list of target user identifiers (mentions, IDs, or usernames).
    """
    if not text:
        return None

    clean = text.strip()
    if bot_user_id:
        clean = re.sub(
            rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", "", clean, flags=re.IGNORECASE
        ).strip()
    clean_lower = clean.lower().strip()

    # 1. Exact and conversational self card commands
    self_triggers = {
        "/level", "!level", ".level", "level",
        "/rank", "!rank", ".rank", "rank",
        "/card", "!card", ".card", "card",
        "/stats", "!stats", ".stats", "stats",
        "/xp", "!xp", ".xp", "xp",
        "my level", "my rank", "my card", "my stats", "my xp", "my points",
        "my rank card", "what is my level", "what's my level",
        "what is my rank", "what's my rank", "what is my card", "what's my card",
        "what is my rank card", "what's my rank card",
        "check level", "check my level", "show my stats", "show me my stats",
        "show my card", "show me my card", "show my level", "show me my level",
        "give me my card", "give me my stats",
    }
    self_patterns = [
        r"^what(?:'s|\s+is)?\s+my\s+(?:current\s+)?(?:rank|card|level|stats|xp|points)\b.*$",
        r"^what\s+(?:level|rank|tier)\s+(?:am\s+i|i\s+am|i'?m)\b.*$",
        r"^what\s+level\s+i\s+am\b.*$",
        r"^which\s+(?:level|rank|tier)\s+am\s+i\b.*$",
        r"^how\s+(?:much|many)\s+(?:xp|points|ore)(?:\s+(?:do\s+i\s+have|have\s+i\s+got|have\s+i))?\b.*$",
        r"^am\s+i\s+(?:a\s+)?(?:level|rank|tier)\b.*$",
        r"^do\s+i\s+have\s+(?:any\s+)?(?:xp|points)\b.*$",
        r"^(?:show|give|gimme|check)\s+(?:me\s+)?my\s+(?:current\s+)?(?:rank|card|level|stats|xp|points)\b.*$",
        r"^my\s+(?:current\s+)?(?:rank|card|level|stats|xp|points)(?:\s+card)?\b.*$",
        r"^(?:level|rank|card)\s+me\b.*$",
    ]
    if clean_lower in self_triggers or any(
        re.match(pattern, clean_lower) for pattern in self_patterns
    ):
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
            tokens = _split_target_tokens(m.group(1))
            return tokens if tokens else [caller_id]

    # 3a. Prefixed command with argument: "!level @user", "/level U12345", ".card netic"
    prefixed_cmd_pattern = r"^(?:!|/|\.)(?:level|rank|card|stats|xp)\s+(.+)$"
    m = re.match(prefixed_cmd_pattern, clean, re.IGNORECASE)
    if m:
        tokens = _split_target_tokens(m.group(1))
        return tokens if tokens else [caller_id]

    # 3b. Unprefixed short command with explicit target identifier: "level netic", "card <@U123>"
    unprefixed_cmd_pattern = (
        r"^(?:level|rank|card|stats|xp)\s+"
        r"((?:<@[UW][A-Z0-9]+(?:\|[^>]+)?>|@[A-Za-z0-9_.\-]+|[A-Za-z0-9_.\-]+)"
        r"(?:\s*(?:,|\band\b|&)\s*(?:<@[UW][A-Z0-9]+(?:\|[^>]+)?>|@[A-Za-z0-9_.\-]+|[A-Za-z0-9_.\-]+))*)$"
    )
    m = re.match(unprefixed_cmd_pattern, clean, re.IGNORECASE)
    if m:
        tokens = _split_target_tokens(m.group(1))
        return tokens if tokens else [caller_id]

    # 4. Mention(s) combined with a standalone card/level/rank/stats/xp/points word
    mentions = re.findall(r"(<@[UW][A-Z0-9]+(?:\|[^>]+)?>)", clean, flags=re.IGNORECASE)
    if bot_user_id:
        mentions = [m for m in mentions if bot_user_id.lower() not in m.lower()]
    if mentions and _CARD_KEYWORD_WORD_RE.search(clean_lower):
        return [re.sub(r"^<@([uw][a-z0-9]+)", lambda m_: f"<@{m_.group(1).upper()}", tok) for tok in mentions]

    return None


def _escape_mrkdwn(text: str) -> str:
    """Neutralize Slack mrkdwn so echoed user input cannot ping or link."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_cards_response_for_targets(targets: List[str], caller_id: str) -> str:
    """Resolves targets (usernames, user IDs, or mentions) to Pond Level cards."""
    cards = []
    for target in targets:
        uid, _user_data = resolve_user(target, create_if_missing=False)
        if not uid:
            clean_name = target.strip().lstrip("@")
            cards.append(f"No activity record found for '{_escape_mrkdwn(clean_name)}' yet.")
            continue
        status = crumbs.get_pond_status(uid)
        if uid != caller_id and not _is_organizer(caller_id) and not status.get("privacy_show"):
            cards.append("That member keeps their pond card private.")
            continue
        cards.append(crumbs.format_pond_card(status))
    return "\n\n".join(cards)


def _is_organizer(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    return user_id.upper() in {u.upper() for u in ORGANIZER_USER_IDS}


POND_HELP_TEXT = (
    "Pond commands: `goose join` to start earning crumbs, `goose me` for your private card, "
    "`goose top` / `goose leaderboard` for the boards, `goose show` / `goose hide` for public privacy, "
    "`goose quiet on/off`, `goose leave` to opt out, `goose report <text>`, `goose appeal <id> [reason]`, "
    "`goose season`. Organizers: `goose star|bigstar @person`, `goose revoke <id>`, `goose roster`, "
    "`goose results`, `goose reports`, `goose resolve <appeal id>`, `goose pause|resume`, `goose freeze|unfreeze`, "
    "`goose voice on|off|status`, `goose set-end <unix ts|off>`, `goose status`."
)


_POND_NO_ARG_COMMANDS = {
    "join", "leave", "top", "all", "help", "pond", "show", "hide", "roster",
    "results", "reports", "status", "pause", "resume", "freeze", "unfreeze",
    "leaderboard", "season",
}


def _valid_pond_args(sub: str, args: List[str]) -> bool:
    """Reject bare words that only look like a command because of extra chatter."""
    if sub in _POND_NO_ARG_COMMANDS:
        return not args
    if sub in ("me", "quiet", "voice"):
        return len(args) <= 1
    if sub == "set-end":
        return len(args) == 1
    if sub in ("report", "appeal", "star", "bigstar", "revoke", "resolve"):
        return bool(args)
    return True


def parse_pond_command(text: str, allow_bare: bool = False) -> Optional[tuple]:
    """Parse a Pond text command from already-cleaned/addressed text.

    A command needs an explicit ``goose`` prefix unless the caller explicitly allows a
    bare subcommand (for a direct @mention or a vocative Gooose call). Arity is checked
    so ordinary sentences like "leave me alone" never trigger a command.
    """
    t = (text or "").strip().lower()
    rest = t
    prefixed = False
    for prefix in ("!goose", ".goose", "goose", "!g", ".g"):
        if t == prefix:
            return ("help", [])
        if t.startswith(prefix + " "):
            rest = t[len(prefix) + 1:].strip()
            prefixed = True
            break
    if not prefixed:
        if not allow_bare:
            return None
        rest = t
    parts = [p for p in rest.split() if p]
    if not parts:
        return None
    sub = parts[0]
    if sub in (
        "join", "leave", "me", "top", "all", "help", "pond", "show", "hide",
        "quiet", "report", "appeal", "star", "bigstar", "revoke", "roster",
        "results", "reports", "resolve", "pause", "resume", "status",
        "freeze", "unfreeze", "voice", "leaderboard", "season", "set-end",
    ) and _valid_pond_args(sub, parts[1:]):
        return (sub, parts[1:])
    return None


def _parse_event_id(value: str) -> Optional[int]:
    """Parse a ledger id into SQLite's signed 64-bit range, or None."""
    try:
        number = int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 2**63 else None


def _dispatch_pond_command(
    sub: str,
    args: List[str],
    client,
    say,
    channel_id: str,
    user_id: str,
    thread_ts: Optional[str],
) -> bool:
    """Run a Pond command without letting bad input crash the Slack listener."""
    try:
        return _handle_pond_command(sub, args, client, say, channel_id, user_id, thread_ts)
    except Exception as exc:
        logger.error("Pond command %s failed (%s)", sub, type(exc).__name__)
        _post_private(
            client, say, channel_id, user_id,
            "Something went sideways with that command. Try again in a moment.",
            thread_ts,
        )
        return True


def _resolve_target_identifier(args: List[str]) -> Optional[str]:
    if not args:
        return None
    uid, _data = resolve_user(args[0], create_if_missing=True)
    return uid


def _post_private(client, say, channel_id: str, user_id: str, text: str, thread_ts: Optional[str]) -> None:
    """Send a private note: ephemeral in-channel if possible, else a DM. Never post publicly."""
    try:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=text, thread_ts=thread_ts)
        return
    except Exception:
        pass
    try:
        if client is not None and user_id:
            client.chat_postMessage(channel=user_id, text=text)
            return
    except Exception:
        pass
    logger.error("Could not send a private pond note; leaving it unsent.")


def _handle_pond_command(
    sub: str,
    args: List[str],
    client,
    say,
    channel_id: str,
    user_id: str,
    thread_ts: Optional[str],
) -> bool:
    if sub in ("help", "pond"):
        _post_private(client, say, channel_id, user_id, POND_HELP_TEXT, thread_ts)
        return True

    if sub == "join":
        already = crumbs.is_opted_in(user_id)
        crumbs.set_opted_in(user_id)
        status = crumbs.get_pond_status(user_id)
        lead = "You're already in the pond. " if already else "Welcome to the pond. "
        lead += "You're on the public board now. "
        _post_private(client, say, channel_id, user_id, lead + "\n" + crumbs.format_pond_card(status), thread_ts)
        return True

    if sub == "leave":
        crumbs.set_opted_out(user_id)
        _post_private(client, say, channel_id, user_id,
                      "You're out of the pond. Your crumbs stay saved; say `goose join` to come back.", thread_ts)
        return True

    if sub == "me":
        target_id = _resolve_target_identifier(args)
        if target_id and target_id != user_id:
            if not _is_organizer(user_id):
                _post_private(client, say, channel_id, user_id,
                              "Only organizers can look up another member. Use `goose me` for your own card.", thread_ts)
                return True
            _post_private(client, say, channel_id, user_id,
                          crumbs.format_pond_card(crumbs.get_pond_status(target_id)), thread_ts)
            return True
        _post_private(client, say, channel_id, user_id,
                      crumbs.format_pond_card(crumbs.get_pond_status(user_id)), thread_ts)
        return True

    if sub in ("top", "all", "leaderboard"):
        entries = crumbs.get_pond_leaderboard(limit=LEADERBOARD_DISPLAY_LIMIT, public=True)
        board = crumbs.format_pond_leaderboard(entries, limit=LEADERBOARD_DISPLAY_LIMIT)
        _safe_say(say, text=board, thread_ts=thread_ts)
        return True

    if sub in ("show", "hide"):
        show = sub == "show"
        crumbs.set_privacy_show(user_id, show)
        note = ("You're on the public boards now." if show
                else "You're hidden from the public boards now.")
        _post_private(client, say, channel_id, user_id, note, thread_ts)
        return True

    if sub == "quiet":
        current = crumbs.is_quiet(user_id)
        wanted = args[0].lower() if args else ("off" if current else "on")
        if wanted not in ("on", "off"):
            _post_private(client, say, channel_id, user_id,
                          "Usage: `goose quiet on` or `goose quiet off`.", thread_ts)
            return True
        crumbs.set_quiet(user_id, wanted == "on")
        _post_private(client, say, channel_id, user_id,
                      f"Quiet mode is now {wanted}. " +
                      ("No more reactions or roast bits aimed at you." if wanted == "on"
                       else "Back to normal."), thread_ts)
        return True

    if sub == "report":
        message = " ".join(args).strip()
        if not message:
            _post_private(client, say, channel_id, user_id, "Usage: `goose report <what happened>`", thread_ts)
            return True
        _alert_organizers(client, f":memo: Report from <@{user_id}>: {message}")
        _post_private(client, say, channel_id, user_id, "Thanks - I passed that to the organizers.", thread_ts)
        return True

    if sub == "appeal":
        if not args:
            _post_private(client, say, channel_id, user_id,
                          "Usage: `goose appeal <ledger id> [reason]`", thread_ts)
            return True
        event_id = _parse_event_id(args[0])
        if event_id is None:
            _post_private(client, say, channel_id, user_id,
                          "That ledger id does not look right.", thread_ts)
            return True
        appeal_id = add_appeal(user_id, event_id, " ".join(args[1:]).strip())
        _alert_organizers(client, f":scales: Appeal #{appeal_id} from <@{user_id}> about ledger entry #{event_id}.")
        _post_private(client, say, channel_id, user_id,
                      f"Appeal #{appeal_id} logged for entry #{event_id}. An organizer will take a look.", thread_ts)
        return True

    if sub in ("star", "bigstar"):
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can give stars.", thread_ts)
            return True
        target_id = _resolve_target_identifier(args)
        if not target_id:
            _post_private(client, say, channel_id, user_id,
                          f"Usage: `goose {sub} @person [reason]`", thread_ts)
            return True
        kind = "star" if sub == "star" else "bigstar"
        amount = crumbs.STAR_CRUMBS if sub == "star" else crumbs.BIGSTAR_CRUMBS
        reason = " ".join(args[1:]).strip()
        before = crumbs.get_pond_status(target_id)
        event_id = crumbs.add_ledger_event(
            target_id, kind, amount=amount, ref=f"by {user_id}: {reason}", source_ts=time.time()
        )
        after = crumbs.get_pond_status(target_id)
        add_audit(user_id, kind, target=target_id, detail=f"ledger #{event_id}: {reason}")
        if after["level"] > before["level"]:
            _post_private(client, say, channel_id, target_id, crumbs.level_up_message(after), thread_ts)
        _post_private(client, say, channel_id, user_id,
                      f"{kind.title()} logged for <@{target_id}> (ledger #{event_id}) - "
                      f"now PL.{after['level']} {after['title']}.", thread_ts)
        return True

    if sub == "revoke":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can revoke entries.", thread_ts)
            return True
        if not args:
            _post_private(client, say, channel_id, user_id,
                          "Usage: `goose revoke <ledger id> [reason]`", thread_ts)
            return True
        event_id = _parse_event_id(args[0])
        if event_id is None:
            _post_private(client, say, channel_id, user_id,
                          "That ledger id does not look right.", thread_ts)
            return True
        reason = " ".join(args[1:]).strip()
        row = crumbs.void_ledger_event(event_id, reason)
        if not row:
            _post_private(client, say, channel_id, user_id,
                          f"No ledger entry #{event_id} found.", thread_ts)
            return True
        affected = row["user_id"]
        after = crumbs.get_pond_status(affected)
        add_audit(user_id, "revoke", target=affected, detail=f"ledger #{event_id}: {reason}")
        _post_private(client, say, channel_id, affected,
                      crumbs.DEMOTE_LINE.format(level=after["level"], title=after["title"] or ""), thread_ts)
        _post_private(client, say, channel_id, user_id,
                      f"Revoked entry #{event_id}. <@{affected}> is now PL.{after['level']} {after['title']}.", thread_ts)
        return True

    if sub == "roster":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can view the roster.", thread_ts)
            return True
        roster = crumbs.get_roster(limit=50)
        if not roster:
            _post_private(client, say, channel_id, user_id, "Nobody has opted in yet.", thread_ts)
            return True
        lines = [f"{i}. <@{e['user_id']}> - PL.{e['level']} {e['title'] or ''} ({e['crumbs']:g})"
                 for i, e in enumerate(roster, 1)]
        _post_private(client, say, channel_id, user_id, "*Pond roster*\n" + "\n".join(lines), thread_ts)
        return True

    if sub == "results":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can view full results.", thread_ts)
            return True
        board = crumbs.format_all_time(crumbs.get_pond_leaderboard(limit=3, public=False))
        _post_private(client, say, channel_id, user_id, board + "\n" + _season_line() + "\n" + _status_line(), thread_ts)
        return True

    if sub in ("season", "set-end"):
        if sub == "set-end":
            if not _is_organizer(user_id):
                _post_private(client, say, channel_id, user_id, "Only organizers can set the season.", thread_ts)
                return True
            if not args:
                _post_private(client, say, channel_id, user_id, "Usage: `goose set-end <unix timestamp|off>`", thread_ts)
                return True
            value = args[0].lower()
            if value in ("off", "none", "clear", "0"):
                set_state("season_end_ts", "0")
            else:
                try:
                    end_ts = int(args[0])
                except (TypeError, ValueError):
                    end_ts = -1
                if end_ts <= 0 or end_ts > 2**31 - 1:
                    _post_private(client, say, channel_id, user_id, "Give me a Unix timestamp or `off`.", thread_ts)
                    return True
                set_state("season_end_ts", str(end_ts))
        _refresh_state_cache()
        _post_private(client, say, channel_id, user_id, _season_line(), thread_ts)
        return True

    if sub == "reports":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can view reports.", thread_ts)
            return True
        appeals = get_appeals("open")
        pending = get_reports("pending")
        lines = ["*Open items*"]
        lines.append(f"- Appeals: {len(appeals)}")
        for a in appeals[:10]:
            lines.append(f"  #{a['id']} <@{a['user_id']}> entry #{a['ledger_event_id']}: {a['reason'] or '(no reason)'}")
        lines.append(f"- Pending reports: {len(pending)}")
        _post_private(client, say, channel_id, user_id, "\n".join(lines), thread_ts)
        return True

    if sub == "resolve":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can resolve items.", thread_ts)
            return True
        if not args:
            _post_private(client, say, channel_id, user_id, "Usage: `goose resolve <appeal id> [note]`", thread_ts)
            return True
        appeal_id = _parse_event_id(args[0])
        if appeal_id is None:
            _post_private(client, say, channel_id, user_id, "That appeal id does not look right.", thread_ts)
            return True
        note = " ".join(args[1:]).strip()
        row = resolve_appeal(appeal_id, "resolved", note)
        if not row:
            _post_private(client, say, channel_id, user_id, f"No appeal #{appeal_id} found.", thread_ts)
            return True
        add_audit(user_id, "resolve", target=row["user_id"], detail=f"appeal #{appeal_id}: {note}")
        _post_private(client, say, channel_id, row["user_id"],
                      f":scales: Your appeal #{appeal_id} was reviewed by an organizer. {note}", thread_ts)
        _post_private(client, say, channel_id, user_id, f"Appeal #{appeal_id} marked resolved.", thread_ts)
        return True

    if sub in ("pause", "resume"):
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can control the bot.", thread_ts)
            return True
        _set_paused(sub == "pause", user_id)
        _post_private(client, say, channel_id, user_id, _status_line(), thread_ts)
        return True

    if sub in ("freeze", "unfreeze"):
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can control scoring.", thread_ts)
            return True
        _set_frozen(sub == "freeze", user_id)
        _post_private(client, say, channel_id, user_id, _status_line(), thread_ts)
        return True

    if sub == "status":
        _post_private(client, say, channel_id, user_id, _status_line(), thread_ts)
        return True

    if sub == "voice":
        if not _is_organizer(user_id):
            _post_private(client, say, channel_id, user_id, "Only organizers can toggle the voice.", thread_ts)
            return True
        arg = args[0].lower() if args else "status"
        if arg in ("on", "off"):
            _set_voice(arg == "on", user_id)
        _post_private(client, say, channel_id, user_id, _status_line(), thread_ts)
        return True

    return False


def _score_loadout_activity(user_id: str, channel_id: str, event, client, text: str, rules) -> None:
    """Score a qualified message in the official #loadout channel for opted-in members."""
    if _season_ended():
        return
    if _is_paused() or _is_frozen():
        return
    if channel_id != OFFICIAL_LOADOUT_CHANNEL_ID:
        return
    if not crumbs.is_opted_in(user_id):
        return
    if not crumbs.is_qualified_message(text):
        return
    thread_ts = event.get("thread_ts") or event.get("ts")
    result = crumbs.record_qualified_message(user_id, channel_id, text, event.get("ts") or "")
    if result:
        _post_private(client, None, channel_id, user_id, crumbs.level_up_message(result["after"]), thread_ts)


def _context_turns(
    raw_messages: List[Dict],
    bot_user_id: Optional[str],
    current_ts: Optional[str],
    excluded_ts: Optional[str] = None,
) -> List[Dict[str, str]]:
    turns: List[Dict[str, str]] = []
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


def fetch_mentioned_thread_context(
    client,
    channel_id: str,
    thread_ts: str,
    current_ts: Optional[str],
    bot_user_id: Optional[str],
) -> List[Dict[str, str]]:
    """Fetch messages in the thread containing the current bot mention up to current_ts."""
    if not client or not channel_id or not thread_ts or not current_ts:
        return []

    messages: List[Dict] = []
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


def fetch_mentioned_channel_context(
    client,
    channel_id: str,
    current_ts: Optional[str],
    bot_user_id: Optional[str],
) -> List[Dict[str, str]]:
    """Fetch recent messages from the same channel, only after an explicit bot call."""
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


def _post_final_ai_reply(
    client, channel_id: str, thread_ts: Optional[str], text: str, queue_key=None
) -> bool:
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


def _deliver_ai_reply(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    placeholder_ts: str,
    text: str,
    queue_key=None,
) -> bool:
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


def _post_ai_placeholder(
    client, channel_id: str, thread_ts: Optional[str], queue_key=None
) -> str:
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


def _run_ai_reply(
    client,
    channel_id: str,
    user_id: str,
    user_message: str,
    thread_ts: Optional[str],
    context_thread_ts: Optional[str],
    current_ts: Optional[str],
    bot_user_id: Optional[str],
    require_decision: bool,
    queue_key,
) -> None:
    try:
        # Voice gate: no chat when muted, and a per-person hourly cap keeps a burst safe.
        if not _voice_enabled():
            return
        hour_key = time.strftime("%Y-%m-%dT%H", time.gmtime())
        if user_id and not consume_voice_quota(user_id, hour_key, VOICE_HOURLY_LIMIT):
            logger.info("Voice hourly cap reached for a member; skipping reply.")
            return

        placeholder_ts = (
            "" if require_decision else _post_ai_placeholder(client, channel_id, thread_ts, queue_key)
        )
        try:
            if context_thread_ts:
                context_messages = fetch_mentioned_thread_context(
                    client, channel_id, context_thread_ts, current_ts, bot_user_id
                )
            elif needs_channel_context(user_message):
                context_messages = fetch_mentioned_channel_context(
                    client, channel_id, current_ts, bot_user_id
                )
            else:
                context_messages = []
            roast_allowed = (not require_decision) and _roast_budget_ok(user_id)
            reply = ai_service.generate_response(
                user_id=user_id,
                user_message=sanitize_context_text(user_message) or "Hello!",
                channel_id=channel_id,
                context_messages=context_messages,
                timeout=AI_TIMEOUT_SECONDS,
                may_decline=require_decision,
                roast_allowed=roast_allowed,
                goose_meter=_goose_meter_value(channel_id),
            )
            if require_decision and ai_service.is_no_reply(reply):
                return
            reply = (reply or "").strip() or ai_service.get_offline_fallback(user_id)
        except Exception as exc:
            logger.error("AI reply failed (%s)", type(exc).__name__)
            if require_decision:
                return
            reply = "I could not complete that reply. Please try again."

        reply = ai_service.post_filter_voice(reply) or (
            "I'm going to keep that one to myself. Ask me about the pond instead."
        )
        if placeholder_ts:
            _deliver_ai_reply(client, channel_id, thread_ts, placeholder_ts, reply, queue_key)
        else:
            _post_final_ai_reply(client, channel_id, thread_ts, reply, queue_key)
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
            _run_ai_reply(*job[:10])
        except Exception as exc:
            logger.error("Queued AI reply worker failed (%s)", type(exc).__name__)


def _queue_ai_reply(
    client,
    say,
    channel_id: str,
    user_id: str,
    message: str,
    thread_ts: Optional[str],
    context_thread_ts: Optional[str],
    current_ts: Optional[str],
    bot_user_id: Optional[str],
    require_decision: bool = False,
) -> None:
    if not _claim_reply_trigger(channel_id, current_ts):
        return
    if not _AI_REPLY_SLOTS.acquire(blocking=False):
        try:
            say(
                text="I am handling several replies right now. Please try again in a moment.",
                channel=channel_id,
                thread_ts=thread_ts,
            )
        except Exception as exc:
            logger.error("Could not send busy notice (%s)", type(exc).__name__)
        return

    thread_root = context_thread_ts or thread_ts
    key = _bot_thread_queue_key(channel_id, thread_root) or (
        (str(channel_id or ""), "thread", str(thread_root))
        if thread_root
        else (str(channel_id or ""), "user", str(user_id or ""))
    )
    job = (
        client,
        channel_id,
        user_id,
        message,
        thread_ts,
        context_thread_ts,
        current_ts,
        bot_user_id,
        require_decision,
        key,
        say,
    )
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
            _, job_channel, job_user, _, job_thread, _, _, _, _, _, job_say = pending_job
            try:
                job_say(
                    text=ai_service.get_offline_fallback(job_user),
                    channel=job_channel,
                    thread_ts=job_thread,
                )
            except Exception as send_exc:
                logger.error("Could not send an AI fallback (%s)", type(send_exc).__name__)


def _safe_say(say, **kwargs) -> None:
    try:
        say(**kwargs)
    except Exception as exc:
        logger.error("Failed to send Slack message (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# Bot state: kill switch (pause) vs freeze (scoring only), cached and persisted
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_STATE_CACHE: Dict[str, bool] = {}


def _load_season_end_ts() -> float:
    try:
        end = float(get_state("season_end_ts", str(SEASON.get("end_ts") or 0)) or 0)
    except (TypeError, ValueError):
        return 0.0
    return end if 0 < end <= 2**31 - 1 else 0.0


def _refresh_state_cache() -> Dict[str, bool]:
    with _STATE_LOCK:
        _STATE_CACHE["paused"] = get_flag("paused", False)
        _STATE_CACHE["frozen"] = get_flag("frozen", False)
        _STATE_CACHE["voice"] = get_flag("voice", VOICE_ENABLED)
        _STATE_CACHE["season_end"] = _load_season_end_ts()  # type: ignore[assignment]
        return dict(_STATE_CACHE)


def _state_value(key: str, default: bool) -> bool:
    with _STATE_LOCK:
        if key in _STATE_CACHE:
            return _STATE_CACHE[key]
    refreshed = _refresh_state_cache()
    return refreshed.get(key, default)


def _is_paused() -> bool:
    return _state_value("paused", False)


def _is_frozen() -> bool:
    return _state_value("frozen", False)


def _voice_enabled() -> bool:
    return _state_value("voice", VOICE_ENABLED)


def _set_state(key: str, value: bool, action: str, actor: str = "") -> None:
    set_flag(key, value)
    with _STATE_LOCK:
        _STATE_CACHE[key] = bool(value)
    add_audit(actor, action, detail=("on" if value else "off"))


def _set_paused(value: bool, actor: str = "") -> None:
    _set_state("paused", value, "pause" if value else "resume", actor)


def _set_frozen(value: bool, actor: str = "") -> None:
    _set_state("frozen", value, "freeze" if value else "unfreeze", actor)


def _set_voice(value: bool, actor: str = "") -> None:
    _set_state("voice", value, "voice-on" if value else "voice-off", actor)


def _status_line() -> str:
    return (
        f"Status - paused: {'on' if _is_paused() else 'off'} | "
        f"frozen (scoring): {'on' if _is_frozen() else 'off'} | "
        f"voice: {'on' if _voice_enabled() else 'off'}"
    )


def _season_end_ts() -> float:
    with _STATE_LOCK:
        if "season_end" in _STATE_CACHE:
            return float(_STATE_CACHE["season_end"])
    _refresh_state_cache()
    with _STATE_LOCK:
        return float(_STATE_CACHE.get("season_end", 0.0))


def _season_ended() -> bool:
    end = _season_end_ts()
    return end > 0 and time.time() >= end


def _season_line() -> str:
    end = _season_end_ts()
    if end <= 0:
        return "Season: open-ended."
    try:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(end))
    except (OverflowError, OSError, ValueError):
        return "Season end date needs to be reset by an organizer."
    if time.time() >= end:
        return f"Season ended {when}."
    return f"Season ends {when}."


def _control_sub(text: str, allow_bare: bool = False) -> Optional[str]:
    """Return the Pond control sub-command in text, if it is a global control command."""
    parsed = parse_pond_command(text, allow_bare=allow_bare)
    if parsed and parsed[0] in ("pause", "resume", "status", "freeze", "unfreeze", "voice", "help"):
        return parsed[0]
    return None


# ---------------------------------------------------------------------------
# Distress, goose meter, roasts, reactions
# ---------------------------------------------------------------------------

_GOOSE_METER_WORD_RE = re.compile(r"\bgoose\b", re.IGNORECASE)
_REACTION_AT_BOT_RE = re.compile(r"<@[A-Z0-9]+>", re.IGNORECASE)


def _distress_reply_and_alert(client, say, channel_id: str, user_id: str, thread_ts) -> None:
    """Plain safety reply plus an organizer alert; never routed through the persona."""
    text = ai_service.format_distress_reply()
    if client is not None:
        _post_private(client, say, channel_id, user_id, text, thread_ts)
    else:
        _safe_say(say, text=text, channel=channel_id, thread_ts=thread_ts)
    alert = f":rotating_light: A member may need a real check-in (in <#{channel_id}>). {_status_line()}"
    _alert_organizers(client, alert)


def _safe_log_text(text: str, limit: int = 200) -> str:
    """Strip control characters and bound length so member input cannot poison logs."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", text or "")
    return cleaned[:limit]


def _alert_organizers(client, text: str) -> None:
    if not client or not ORGANIZER_CHANNEL_ID:
        logger.warning("Organizer alert (no channel configured): %s", _safe_log_text(text))
        return
    try:
        client.chat_postMessage(channel=ORGANIZER_CHANNEL_ID, text=text, parse="none")
    except Exception as exc:
        logger.error("Could not post organizer alert (%s)", type(exc).__name__)


def _bump_goose_meter(channel_id: str, text: str) -> int:
    """Persisted, match-gated counter of how often a channel calls Goose a goose."""
    if not (GOOSE_METER.get("enabled", True)) or not channel_id:
        return 0
    if not _GOOSE_METER_WORD_RE.search(text or ""):
        return 0
    try:
        value = touch_channel_counter(
            channel_id, "goose_meter", 1.0, float(GOOSE_METER.get("reset_hours", 24) or 24)
        )
        return int(value)
    except Exception as exc:
        logger.warning("Goose meter update failed (%s)", type(exc).__name__)
        return 0


def _goose_meter_value(channel_id: str) -> int:
    try:
        return int(get_channel_counter(channel_id, "goose_meter"))
    except Exception:
        return 0


def _roast_budget_ok(user_id: str, event=None) -> bool:
    """Roasts are rare: gated by daily budget and limited to known, non-distressed members."""
    if not ROASTS.get("enabled", True):
        return False
    if not user_id:
        return False
    try:
        status = crumbs.get_pond_status(user_id)
        if not status.get("opted_in"):
            return False
        if status.get("quiet"):
            return False
    except Exception:
        return False
    day = time.strftime("%Y-%m-%d", time.localtime())
    key = f"roasts:{day}"
    try:
        used = int(get_state(key, "0"))
    except Exception:
        used = 0
    if used >= int(ROASTS.get("max_per_day", 3) or 3):
        return False
    set_state(key, str(used + 1))
    return True


def _maybe_send_asset(
    client,
    say,
    channel_id: str,
    text: str,
    thread_ts: Optional[str],
    message_ts: Optional[str],
) -> bool:
    """Send a rare reaction clip or a bad sifu quote. Returns True when it handled the message."""
    if not ASSETS.get("enabled", True):
        return False

    if slack_assets.is_quote_request(text):
        if not _claim_reply_trigger(channel_id, message_ts):
            return True
        if not slack_assets.allow_quote(channel_id):
            _safe_say(say, text="One prophecy at a time. Let the last one settle.", thread_ts=thread_ts)
            return True
        quote = slack_assets.generate_quote()
        line = slack_assets.format_quote(quote)
        image = quote.get("image")
        if not (image and slack_assets.upload_asset(client, channel_id, thread_ts, image, initial_comment=line)):
            _safe_say(say, text=line, channel=channel_id, thread_ts=thread_ts)
        return True

    moment = slack_assets.detect_moment(text)
    if not moment:
        return False
    if not slack_assets.allow_asset(channel_id):
        return False
    if not _claim_reply_trigger(channel_id, message_ts):
        return True
    if not slack_assets.upload_asset(
        client, channel_id, thread_ts, moment["image"], initial_comment=moment["line"]
    ):
        _safe_say(say, text=moment["line"], channel=channel_id, thread_ts=thread_ts)
    return True


def _react_to_message(client, channel_id: str, text: str, author_id: str, message_ts: str) -> None:
    """Add a light reaction to noteworthy messages, within a per-channel hourly budget."""
    if not client or not (REACTIONS.get("enabled", True)):
        return
    if not channel_id or not message_ts or not author_id:
        return
    words = [str(w).lower() for w in (REACTIONS.get("trigger_words") or [])]
    lower = (text or "").lower()
    matched = any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words if w)
    at_bot = bool(re.search(rf"<@{re.escape(_bot_user_id_global or '')}>", text or "", re.IGNORECASE)) if _bot_user_id_global else False
    if not matched and not at_bot:
        return
    try:
        if crumbs.is_quiet(author_id):
            return
    except Exception:
        pass
    budget = int(REACTIONS.get("budget_per_hour", 6) or 6)
    hour_bucket = time.strftime("%Y%m%d%H", time.gmtime())
    try:
        used = touch_channel_counter(channel_id, f"reactions_hour:{hour_bucket}", 1.0, 0.0)
    except Exception:
        return
    if used > budget:
        return
    emoji = ":duck:"
    try:
        client.reactions_add(channel=channel_id, timestamp=message_ts, name=emoji.strip(":"))
    except Exception as exc:
        logger.debug("Could not add reaction (%s)", type(exc).__name__)


_bot_user_id_global: Optional[str] = None


def run_scheduled_maintenance(client, force: bool = False) -> None:
    """Maintenance: back up the DB once a day, and on Monday post the weekly Pond Report.

    The backup runs regardless of pause/freeze so a paused week still gets a snapshot. The
    weekly report is claimed atomically per ISO week, so overlapping ticks or a restart
    cannot post it twice.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if get_state("last_backup_day", "") != today:
        set_state("last_backup_day", today)
        try:
            backup_database()
        except Exception as exc:
            logger.warning("Database backup failed (%s)", type(exc).__name__)

    if not (force or time.gmtime().tm_wday == 0):
        return
    if _is_paused() or _is_frozen():
        return
    week = crumbs.week_key_now()
    if get_state("last_weekly_report", "") == week:
        return
    try:
        entries = crumbs.get_weekly_leaderboard(week, limit=10)
    except Exception as exc:
        logger.error("Weekly report query failed (%s)", type(exc).__name__)
        return
    if not claim_state("last_weekly_report", week):
        return
    if ORGANIZER_CHANNEL_ID and client is not None:
        try:
            client.chat_postMessage(
                channel=ORGANIZER_CHANNEL_ID,
                text=(f"Monday check-in for <#{OFFICIAL_LOADOUT_CHANNEL_ID}>:\n"
                      + crumbs.format_weekly_report(week, entries)),
                parse="none",
            )
        except Exception as exc:
            logger.error("Could not post the weekly report (%s)", type(exc).__name__)
    else:
        logger.info("Weekly report ready for %s but no organizer channel is configured.", week)
    try:
        crumbs.snapshot_week(week)
    except Exception as exc:
        logger.warning("Weekly snapshot failed (%s)", type(exc).__name__)



def register_handlers(app: App, bot_user_id: Optional[str] = None) -> None:
    """Registers all Slack Bolt event listeners and slash commands."""
    global _bot_user_id_global
    _bot_user_id_global = bot_user_id
    _refresh_state_cache()


    # 1. MESSAGE EVENT: Track activity and support in-chat command triggers
    @app.event("message")
    def handle_message_events(body, client, say):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        text = (event.get("text") or "").strip()
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")

        if bot_id or subtype in _IGNORED_SUBTYPES or not user_id:
            return
        if bot_user_id and user_id == bot_user_id:
            return
        if not is_channel_watched(channel_id):
            return

        rules = CONFIG.get("interaction_rules") or {}
        # ALWAYS reply in a thread: continue the existing thread, or start one on the message.
        target_thread_ts = event.get("thread_ts") or event.get("ts")

        is_dm = bool(channel_id and channel_id.startswith("D")) or event.get("channel_type") == "im"
        is_direct_bot_mention = bool(
            bot_user_id
            and re.search(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", text, flags=re.IGNORECASE)
        )
        mentions_other_user_at_start = bool(
            not is_direct_bot_mention
            and re.match(r"^\s*<@[UW][A-Z0-9]+(?:\|[^>]+)?>", text, flags=re.IGNORECASE)
        )
        is_named = is_addressed_to_bot(text, bot_user_id, is_dm=is_dm)
        is_thread_followup = (
            _is_known_bot_thread(channel_id, event.get("thread_ts"))
            and not mentions_other_user_at_start
        )
        is_addressed = is_named or is_thread_followup
        is_prefixed_cmd = bool(_COMMAND_PREFIX_RE.match(text))
        is_called = is_addressed or is_prefixed_cmd
        clean_addressed = clean_bot_addressing(text, bot_user_id)
        clean_lower = clean_addressed.lower().strip()
        raw_lower = text.lower()
        has_explicit_prefix = any(
            raw_lower == p or raw_lower.startswith(p + " ")
            for p in ("!goose", ".goose", "goose", "!g", ".g")
        )
        allow_bare_command = (is_named and not is_dm) or (is_dm and has_explicit_prefix)

        # Distress takes priority over the persona, the kill switch, and every other bit.
        if (
            DISTRESS.get("enabled", True)
            and ai_service.detect_distress(text)
            and (is_dm or is_addressed or is_direct_bot_mention)
        ):
            _distress_reply_and_alert(client, say, channel_id, user_id, target_thread_ts)
            return

        # Global kill switch: when paused, only organizers may run control commands.
        if _is_paused():
            control = _control_sub(clean_lower, allow_bare=allow_bare_command)
            if not (control and _is_organizer(user_id)):
                return

        # Goose meter + light reactions for channel activity (persisted, match-gated, budgeted).
        _bump_goose_meter(channel_id, text)
        _react_to_message(client, channel_id, text, user_id, event.get("ts") or "")

        if not is_called:
            _score_loadout_activity(user_id, channel_id, event, client, text, rules)
            return

        if not is_direct_bot_mention:
            pond_cmd = parse_pond_command(clean_lower, allow_bare=allow_bare_command)
            if pond_cmd is not None:
                if _dispatch_pond_command(pond_cmd[0], pond_cmd[1], client, say, channel_id, user_id, target_thread_ts):
                    return

            card_targets = parse_card_request_targets(
                clean_addressed, caller_id=user_id, bot_user_id=bot_user_id
            )
            if card_targets is not None:
                reply_text = get_cards_response_for_targets(card_targets, caller_id=user_id)
                _safe_say(say, text=reply_text, channel=channel_id, thread_ts=target_thread_ts)
                return

            if clean_lower in (
                "/leaderboard", "!leaderboard", ".leaderboard", "leaderboard",
                "/top", "!top", ".top", "top", "ranks", "pond leaderboard",
            ):
                board = crumbs.format_pond_leaderboard(
                    crumbs.get_pond_leaderboard(limit=LEADERBOARD_DISPLAY_LIMIT, public=True),
                    limit=LEADERBOARD_DISPLAY_LIMIT,
                )
                _safe_say(say, text=board, channel=channel_id, thread_ts=target_thread_ts)
                return

            if clean_lower in ("/repo", "!repo", ".repo", "repo", "github", "gh repo", "!github"):
                repo_msg = (
                    "Repository: <https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>."
                )
                _safe_say(say, text=repo_msg, channel=channel_id, thread_ts=target_thread_ts)
                return

            if clean_lower in ("!help", "/help", ".help", "bot help", "bothelp", "help"):
                templates = CONFIG.get("templates") or {}
                help_msg = templates.get("help_message", "Type `!level` or `!leaderboard`!")
                formatted_help = help_msg.format(
                    bot_id=bot_user_id or "bot",
                    cooldown=rules.get("cooldown_seconds", 30),
                )
                _safe_say(say, text=formatted_help, channel=channel_id, thread_ts=target_thread_ts)
                return

        should_respond = is_addressed and (is_dm or not is_direct_bot_mention)
        if is_direct_bot_mention and not is_dm:
            return
        # A real addressed message (not a command) still counts as pond activity.
        _score_loadout_activity(user_id, channel_id, event, client, clean_addressed, rules)
        if should_respond and _maybe_send_asset(
            client, say, channel_id, clean_addressed, target_thread_ts, event.get("ts")
        ):
            return
        if should_respond:
            require_decision = (
                is_thread_followup and not is_named and not is_prefixed_cmd and not is_dm
            )
            _queue_ai_reply(
                client=client,
                say=say,
                channel_id=channel_id,
                user_id=user_id,
                message=sanitize_context_text(clean_addressed) or "Hello!",
                thread_ts=target_thread_ts,
                context_thread_ts=event.get("thread_ts") or (
                    event.get("ts") if event.get("reply_count") else None
                ),
                current_ts=event.get("ts"),
                bot_user_id=bot_user_id,
                require_decision=require_decision,
            )

    # 2. APP MENTION EVENT: Handle @bot mentions (AI chat or commands)
    @app.event("app_mention")
    def handle_app_mentions(body, say, client):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        raw_text = event.get("text", "")
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")

        if bot_id or subtype in _IGNORED_SUBTYPES or not user_id:
            return
        if bot_user_id and user_id == bot_user_id:
            return
        if not is_channel_watched(channel_id):
            return

        rules = CONFIG.get("interaction_rules") or {}
        # ALWAYS reply in a thread: continue the existing thread, or start one on the mention.
        target_thread_ts = event.get("thread_ts") or event.get("ts")

        cleaned_text = sanitize_context_text(clean_bot_addressing(raw_text, bot_user_id))
        clean_lower = cleaned_text.lower()

        # Distress takes priority over the persona, the kill switch, and every other bit.
        if DISTRESS.get("enabled", True) and ai_service.detect_distress(raw_text or cleaned_text):
            _distress_reply_and_alert(client, say, channel_id, user_id, target_thread_ts)
            return

        # Global kill switch: when paused, only organizers may run control commands.
        if _is_paused():
            control = _control_sub(clean_lower, allow_bare=True)
            if not (control and _is_organizer(user_id)):
                return

        _bump_goose_meter(channel_id, raw_text)
        _react_to_message(client, channel_id, raw_text, user_id, event.get("ts") or "")

        pond_cmd = parse_pond_command(clean_lower, allow_bare=True)
        if pond_cmd is not None:
            if _dispatch_pond_command(pond_cmd[0], pond_cmd[1], client, say, channel_id, user_id, target_thread_ts):
                return

        card_targets = parse_card_request_targets(
            raw_text, caller_id=user_id, bot_user_id=bot_user_id
        )
        if card_targets is not None:
            reply_text = get_cards_response_for_targets(card_targets, caller_id=user_id)
            _safe_say(say, text=reply_text, thread_ts=target_thread_ts)
            return

        if clean_lower in (
            "/leaderboard", "!leaderboard", ".leaderboard", "leaderboard",
            "/top", "!top", ".top", "top", "ranks", "scores", "pond leaderboard",
        ):
            board = crumbs.format_pond_leaderboard(
                crumbs.get_pond_leaderboard(limit=LEADERBOARD_DISPLAY_LIMIT, public=True),
                limit=LEADERBOARD_DISPLAY_LIMIT,
            )
            _safe_say(say, text=board, thread_ts=target_thread_ts)
            return

        if clean_lower in (
            "repo", "github", "gh repo", "gimme the gh repo",
            "give me the gh repo", "give me the repo", "link",
        ):
            repo_msg = (
                "Repository: <https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>."
            )
            _safe_say(say, text=repo_msg, thread_ts=target_thread_ts)
            return

        if clean_lower in ("help", "commands"):
            templates = CONFIG.get("templates") or {}
            help_msg = templates.get("help_message", "Type `!level` or `!leaderboard`!")
            formatted_help = help_msg.format(
                bot_id=bot_user_id or "bot",
                cooldown=rules.get("cooldown_seconds", 30),
            )
            _safe_say(say, text=formatted_help, thread_ts=target_thread_ts)
            return

        if _maybe_send_asset(client, say, channel_id, cleaned_text, target_thread_ts, event.get("ts")):
            return

        _score_loadout_activity(user_id, channel_id, event, client, cleaned_text, rules)
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

    # 3. REACTIONS: :duck: = thanks, :white_check_mark: from the thread asker = helpful
    @app.event("reaction_added")
    def handle_reaction_added(body, client):
        event = body.get("event", {})
        reaction = (event.get("reaction") or "").lower()
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        channel_id = item.get("channel")
        message_ts = item.get("ts")
        reactor = event.get("user")
        author = event.get("item_user")
        if not channel_id or not author or not reactor or not message_ts:
            return
        if channel_id != OFFICIAL_LOADOUT_CHANNEL_ID:
            return
        if _is_paused() or _is_frozen() or _season_ended():
            return
        if reactor == author or (bot_user_id and reactor == bot_user_id):
            return
        if not crumbs.is_opted_in(author):
            return

        kind = None
        if reaction in ("duck", "gooselove"):
            kind = "thanks"
        elif reaction in ("white_check_mark", "heavy_check_mark"):
            try:
                resp = client.conversations_replies(channel=channel_id, ts=message_ts, limit=2)
                parent = (resp.get("messages") or [{}])[0]
                if parent.get("user") != reactor:
                    return
            except Exception:
                return
            kind = "helpful"
        if kind is None:
            return

        before = crumbs.get_pond_status(author)
        crumbs.add_ledger_event(
            author, kind, channel_id=channel_id, message_ts=message_ts,
            ref=reactor, source_ts=time.time(),
        )
        after = crumbs.get_pond_status(author)
        if after["level"] > before["level"]:
            _post_private(client, None, channel_id, author, crumbs.level_up_message(after), message_ts)

    # 4. SLASH COMMANDS
    def _handle_card_slash_command(ack, command, respond, say):
        ack()
        caller_id = command.get("user_id")
        raw_text = (command.get("text") or "").strip()
        targets = _split_target_tokens(raw_text) if raw_text else [caller_id]
        if not targets:
            targets = [caller_id]
        reply = get_cards_response_for_targets(targets, caller_id)
        try:
            respond(text=reply, response_type="in_channel")
        except Exception:
            _safe_say(say, text=reply)

    @app.command("/level")
    def handle_level_command(ack, command, respond, say):
        _handle_card_slash_command(ack, command, respond, say)

    @app.command("/rank")
    def handle_rank_command(ack, command, respond, say):
        _handle_card_slash_command(ack, command, respond, say)

    @app.command("/card")
    def handle_card_command(ack, command, respond, say):
        _handle_card_slash_command(ack, command, respond, say)

    @app.command("/leaderboard")
    def handle_leaderboard_command(ack, respond, say):
        ack()
        board = crumbs.format_pond_leaderboard(
                crumbs.get_pond_leaderboard(limit=LEADERBOARD_DISPLAY_LIMIT, public=True),
                limit=LEADERBOARD_DISPLAY_LIMIT,
            )
        try:
            respond(text=board, response_type="in_channel")
        except Exception:
            _safe_say(say, text=board)
