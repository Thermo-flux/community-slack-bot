"""
==============================================================================
SLACK EVENT & COMMAND HANDLERS (src/handlers.py)
==============================================================================
This module contains event listeners and slash command handlers for Slack.
Supports querying anyone's level card, public contributor stats, conversational AI chat,
and responsive bot name addressing while strictly enforcing message privacy:
- Uncalled messages are NEVER read, inspected, or processed for text content.
- 5-10 message conversation context is ONLY fetched on-demand when called.
- Context is held ephemerally in-memory and sanitized against secret leakage.
==============================================================================
"""

import re
import logging
from typing import Optional, List, Dict
from slack_bolt import App

from config import CONFIG, is_channel_watched
from database import (
    get_or_create_user,
    resolve_user,
    register_user_alias,
    check_cooldown,
    record_message_and_add_xp,
    increment_message_count_only,
    get_leaderboard
)
from leveling import (
    evaluate_message_xp,
    get_level_info_for_xp,
    format_rank_card,
    format_level_up_message,
    format_leaderboard
)
import ai_service

logger = logging.getLogger("SlackBotHandlers")


def clean_mention_text(text: str) -> str:
    """Removes Slack user mention tags like <@U12345678> from text."""
    if not text:
        return ""
    return re.sub(r"<@[A-Z0-9_]+(?:\|[^>]+)?>", "", text, flags=re.IGNORECASE).strip()


def sanitize_context_text(text: str) -> str:
    """
    Sanitizes conversation history text to prevent accidental leakage of secrets,
    Slack tokens, or sensitive credentials to the AI model or downstream processors.
    """
    if not text:
        return ""
    # Redact Slack tokens (xoxb, xapp, xoxp, xoxt, etc.)
    text = re.sub(r"xox[baprst]-[A-Za-z0-9-]+", "[REDACTED_SLACK_TOKEN]", text)
    # Redact generic API keys (sk-..., ghp_..., gho_..., AIza..., etc.)
    text = re.sub(r"\b(?:sk|ghp|gho|glpat|AIza)[A-Za-z0-9_\-]{16,}\b", "[REDACTED_API_KEY]", text)
    # Redact Bearer authorization headers
    text = re.sub(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    # Redact private SSH keys or RSA blocks
    text = re.sub(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", text, flags=re.DOTALL)
    return text


def is_addressed_to_bot(text: str, bot_user_id: Optional[str] = None, is_dm: bool = False) -> bool:
    """
    Checks if a message is intended for goose-lee:
    - 100% of messages in Direct Messages (DMs).
    - Rich Slack user mentions (<@bot_user_id>).
    - Conversational name triggers (e.g., 'goose-lee:', 'hey goose', '@gooselee', 'goose,').
    """
    if is_dm:
        return True
    if not text:
        return False
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return True

    # Check for text names at start of sentence, or calling out goose/duck
    pattern = r"(?:^|\b)(?:@?goose-?lee|@?goose|hey\s+goose|yo\s+goose|duck:|@?duck)\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def clean_bot_addressing(text: str, bot_user_id: Optional[str] = None) -> str:
    """Strips both Slack mention tags and conversational name prefixes/suffixes from text."""
    if not text:
        return ""
    cleaned = clean_mention_text(text)
    # Strip leading @gooselee, goose-lee:, hey goose, etc.
    cleaned = re.sub(
        r"^(?:(?:hey|yo|hi|hello)\s+)?@?(?:goose-?lee|goose|duck)[:,\s]*",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()
    return cleaned


def is_bot_thread(client, channel_id: str, thread_ts: Optional[str], bot_user_id: Optional[str]) -> bool:
    """Checks if the bot has previously sent a message in this thread."""
    if not client or not bot_user_id or not thread_ts:
        return False
    try:
        res = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=10)
        messages = res.get("messages", [])
        return any(m.get("user") == bot_user_id for m in messages)
    except Exception:
        return False


def parse_card_request_targets(text: str, caller_id: str, bot_user_id: Optional[str] = None) -> Optional[List[str]]:
    """
    Parses a message or command string to determine if it is asking for a rank/level card,
    and returns a list of target user identifiers (mentions, IDs, or usernames).
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
            cards.append(
                f"🔍 *No activity record found for '{clean_name}'.*\n"
                f"They haven't earned any XP in this channel yet! As soon as they start chatting or sharing ideas, their level card will be generated. 🐣"
            )
    return "\n\n".join(cards)


def fetch_conversation_history(
    client,
    channel_id: str,
    thread_ts: Optional[str] = None,
    bot_user_id: Optional[str] = None,
    current_user_id: Optional[str] = None,
    exclude_ts: Optional[str] = None,
    max_messages: int = 10
) -> List[Dict[str, str]]:
    """
    Pulls recent 5-10 conversation turns from Slack on-demand when the bot is called.
    Sanitizes all text to redact secrets, tokens, and sensitive credentials.
    Held strictly in-memory and NEVER persisted to disk.
    """
    history: List[Dict[str, str]] = []
    if not client or not channel_id:
        return history

    try:
        raw_msgs = []
        if thread_ts:
            res = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=max_messages + 3)
            raw_msgs = res.get("messages", [])
        else:
            res = client.conversations_history(channel=channel_id, limit=max_messages + 5)
            raw_msgs = res.get("messages", [])
            raw_msgs.reverse()

        for msg in raw_msgs:
            msg_ts = msg.get("ts")
            if exclude_ts and msg_ts == exclude_ts:
                continue

            sender = msg.get("user")
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            if text.lower().startswith(("/", "!", ".")):
                continue

            if not thread_ts:
                is_bot = sender == bot_user_id
                is_bot_mention = bot_user_id and f"<@{bot_user_id}>" in text
                is_curr_user = current_user_id and sender == current_user_id
                if not (is_bot or is_bot_mention or is_curr_user):
                    continue

            if sender == bot_user_id:
                clean_text = ai_service.clean_ai_output(text)
                if clean_text:
                    history.append({"role": "assistant", "content": clean_text})
            elif sender:
                clean_text = clean_mention_text(text)
                # Privacy: sanitize any accidental tokens or secrets from user messages
                clean_text = sanitize_context_text(clean_text)
                if clean_text:
                    history.append({"role": "user", "content": clean_text})

        if len(history) > max_messages:
            history = history[-max_messages:]

    except Exception as e:
        logger.warning(f"Could not retrieve conversation history from Slack: {e}")

    return history


def register_handlers(app: App, bot_user_id: Optional[str] = None) -> None:
    """Registers all Slack Bolt event listeners and slash commands."""

    # 1. MESSAGE EVENT: Track activity, support commands, and respond when addressed
    @app.event("message")
    def handle_message_events(body, client, say):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        text = (event.get("text") or "").strip()
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")
        thread_ts = event.get("thread_ts")

        # Never process messages from bots to prevent loops
        if bot_id or subtype == "bot_message" or not user_id:
            return
        if subtype in ["message_deleted", "message_changed", "channel_join", "channel_leave", "pinned_info"]:
            return
        if not is_channel_watched(channel_id):
            return

        is_dm = bool(channel_id and channel_id.startswith("D")) or (event.get("channel_type") == "im")
        is_direct_bot_mention = bool(bot_user_id and f"<@{bot_user_id}>" in text)
        is_in_bot_thread = bool(thread_ts and is_bot_thread(client, channel_id, thread_ts, bot_user_id))
        is_addressed = is_addressed_to_bot(text, bot_user_id, is_dm=is_dm) or is_in_bot_thread
        is_prefixed_cmd = text.strip().startswith(("!", "/", "."))
        is_called = is_addressed or is_prefixed_cmd

        # =====================================================================
        # PRIVACY GATE: If the bot is NOT called, DO NOT inspect or read message contents!
        # Only record background activity metadata for XP tracking.
        # =====================================================================
        if not is_called:
            rules = CONFIG.get("interaction_rules", {})
            cooldown_sec = rules.get("cooldown_seconds", 30)
            can_earn, remaining = check_cooldown(user_id, cooldown_sec)

            if not can_earn:
                increment_message_count_only(user_id)
                return

            base_xp = rules.get("base_xp_per_message", 10)
            current_user = get_or_create_user(user_id)
            old_level, old_title = get_level_info_for_xp(current_user["xp"])

            new_total_xp = current_user["xp"] + base_xp
            new_level, new_title = get_level_info_for_xp(new_total_xp)

            record_message_and_add_xp(
                user_id=user_id,
                channel_id=channel_id,
                xp_to_add=base_xp,
                new_level=new_level,
                message_length=0,   # Privacy: never inspect or store message length
                is_suggestion=0,    # Privacy: never inspect or scan message text
                update_cooldown=True
            )

            if new_level > old_level and event.get("ts"):
                try:
                    client.reactions_add(channel=channel_id, timestamp=event["ts"], name="tada")
                except Exception:
                    pass
            return

        # =====================================================================
        # THE BOT WAS CALLED: Process command or pull in 5-10 message context for AI
        # =====================================================================
        rules = CONFIG.get("interaction_rules", {})
        reply_in_thread = rules.get("reply_in_thread", False)
        target_thread_ts = thread_ts or (event.get("ts") if reply_in_thread else None)

        cleaned_text = clean_bot_addressing(text, bot_user_id)
        clean_lower = cleaned_text.lower().strip()
        should_respond = is_addressed and (is_dm or not is_direct_bot_mention)

        # A. Card Command Check
        card_targets = parse_card_request_targets(cleaned_text, caller_id=user_id, bot_user_id=bot_user_id)
        if card_targets is not None and (should_respond or is_prefixed_cmd):
            reply_text = get_cards_response_for_targets(card_targets, caller_id=user_id)
            try:
                say(text=reply_text, channel=channel_id, thread_ts=target_thread_ts)
            except Exception as e:
                logger.error(f"Failed to send rank card via chat command: {e}")
            return

        # B. Leaderboard Command Check (prefixed or addressed)
        if (is_prefixed_cmd and clean_lower in ("/leaderboard", "!leaderboard", ".leaderboard", "!top", "/top")) or \
           (should_respond and clean_lower in ("leaderboard", "top", "ranks", "scores", "!leaderboard", "/leaderboard")):
            leaders = get_leaderboard(limit=10)
            board = format_leaderboard(leaders)
            try:
                say(text=board, channel=channel_id, thread_ts=target_thread_ts)
            except Exception as e:
                logger.error(f"Failed to send leaderboard: {e}")
            return

        # C. Repo Command Check (prefixed or addressed)
        if (is_prefixed_cmd and clean_lower in ("/repo", "!repo", ".repo", "!github", "/github")) or \
           (should_respond and clean_lower in ("repo", "github", "gh repo", "bot repo")):
            repo_msg = "🚀 *Here is our GitHub repository:*\n<https://github.com/jeremy341/community-slack-bot|jeremy341/community-slack-bot>\nCheck out the code, star it, or propose features! ✨"
            try:
                say(text=repo_msg, channel=channel_id, thread_ts=target_thread_ts)
            except Exception as e:
                logger.error(f"Failed to send repo: {e}")
            return

        # D. Help Command Check (prefixed or addressed)
        if (is_prefixed_cmd and clean_lower in ("!help", "/help", ".help")) or \
           (should_respond and clean_lower in ("help", "commands")):
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

        # E. Conversational AI Response: PULLS IN RECENT 5-10 MESSAGE CONTEXT ON-DEMAND
        if should_respond and cleaned_text:
            history = fetch_conversation_history(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                bot_user_id=bot_user_id,
                current_user_id=user_id,
                exclude_ts=event.get("ts"),
                max_messages=10  # On-demand 5-10 message context window
            )

            ai_reply = ai_service.generate_response(
                user_id=user_id,
                user_message=cleaned_text,
                channel_id=channel_id,
                conversation_history=history
            )
            try:
                say(text=ai_reply, channel=channel_id, thread_ts=target_thread_ts)
            except Exception as e:
                logger.error(f"Failed to send AI response: {e}")
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
        target_thread_ts = event.get("thread_ts") or (event.get("ts") if reply_in_thread else None)

        if not user_id:
            return

        cleaned_text = clean_bot_addressing(raw_text, bot_user_id)
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
            repo_msg = "🚀 *Here is our GitHub repository:*\n<https://github.com/jeremy341/community-slack-bot|jeremy341/community-slack-bot>\nCheck out the code, star it, or propose features! ✨"
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

        # AI Chat with on-demand 5-10 message context
        history = fetch_conversation_history(
            client=client,
            channel_id=channel_id,
            thread_ts=event.get("thread_ts"),
            bot_user_id=bot_user_id,
            current_user_id=user_id,
            exclude_ts=event.get("ts"),
            max_messages=10  # On-demand 5-10 message context window
        )

        ai_reply = ai_service.generate_response(
            user_id=user_id,
            user_message=cleaned_text or "Hello!",
            channel_id=channel_id,
            conversation_history=history
        )
        say(text=ai_reply, thread_ts=target_thread_ts)


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
            tokens = [t.strip() for t in re.split(r",\s*|\s+and\s+|\s+&\s+", raw_text) if t.strip()]
            targets = tokens if tokens else [caller_id]
        else:
            targets = [caller_id]

        reply = get_cards_response_for_targets(targets, caller_id)
        try:
            respond(text=reply, response_type="in_channel")
        except Exception:
            say(text=reply)

    @app.command("/leaderboard")
    def handle_leaderboard_command(ack, command, respond, say):
        ack()
        leaders = get_leaderboard(limit=10)
        board = format_leaderboard(leaders)
        try:
            respond(text=board, response_type="in_channel")
        except Exception:
            say(text=board)
