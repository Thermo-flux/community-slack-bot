"""
==============================================================================
SLACK EVENT & COMMAND HANDLERS (src/handlers.py)
==============================================================================
This module contains the event listeners and slash command handlers for Slack.
Supports querying anyone's level card, public contributor stats, and AI chat.
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
    return re.sub(r"<@[A-Z0-9]+(?:\|[^>]+)?>", "", text).strip()


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

    # 1. Exact self card commands
    self_triggers = (
        "/level", "!level", ".level", "level",
        "/rank", "!rank", ".rank", "rank",
        "/card", "!card", ".card", "card",
        "/stats", "!stats", ".stats", "stats",
        "/xp", "!xp", ".xp", "xp",
        "my level", "my rank", "my card", "my stats", "my xp", "my points",
        "check level", "check my level"
    )
    if clean_lower in self_triggers:
        return [caller_id]

    # 2. Conversational card queries like "give me the card for X", "card for X and Y"
    conversational_patterns = [
        r"^(?:please\s+)?(?:give\s+me|show\s+me|show|fetch|get|check|display|view|send)\s+(?:the\s+)?(?:card|level|rank|stats|xp|points)\s+(?:for|of)\s+(.+)$",
        r"^(?:what\s+is|what\'?s|how\s+much\s+is|check)\s+(?:the\s+)?(?:level|rank|stats|xp|points)\s+(?:of|for)\s+(.+)$",
        r"^(?:card|stats|level|rank|xp|points)\s+(?:for|of)\s+(.+)$",
        r"^(?:who\s+is)\s+(.+)$",
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
                f"They haven't earned any XP in this channel yet! As soon as they start chatting or sharing ideas, their level card will be generated. 🌱"
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
    Fetches recent conversation turns from Slack to provide multi-turn memory to the AI.
    If in a thread, fetches thread replies.
    If in a channel, filters to relevant interactions with the bot or current user.
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
            raw_msgs.reverse()  # history returns newest first; reverse for chronological order

        for msg in raw_msgs:
            msg_ts = msg.get("ts")
            # Exclude the current incoming message being handled
            if exclude_ts and msg_ts == exclude_ts:
                continue

            sender = msg.get("user")
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            # Skip command triggers like /level or !leaderboard from conversation history
            if text.lower().startswith(("/", "!", ".")):
                continue

            # If not in a thread, only include messages involving the bot or current user
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
                if clean_text:
                    history.append({"role": "user", "content": clean_text})

        # Keep at most max_messages turns
        if len(history) > max_messages:
            history = history[-max_messages:]

    except Exception as e:
        logger.warning(f"Could not retrieve conversation history from Slack: {e}")

    return history


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
        target_thread_ts = event.get("thread_ts") or (event.get("ts") if reply_in_thread else None)

        # Never process messages from bots to prevent loops
        if bot_id or subtype == "bot_message" or not user_id:
            return
        if subtype in ["message_deleted", "message_changed", "channel_join", "channel_leave", "pinned_info"]:
            return
        if not is_channel_watched(channel_id):
            return

        clean_lower = text.lower().strip()

        # Check if the user is asking for their card or anyone else's card
        card_targets = parse_card_request_targets(text, caller_id=user_id, bot_user_id=bot_user_id)
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
            repo_msg = "🚀 *Community Slack Bot GitHub Repository:*\n<https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>\nFeel free to explore the code, open issues, or contribute! ✨"
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

        # Anti-spam cooldown check
        cooldown_sec = rules.get("cooldown_seconds", 30)
        can_earn, remaining = check_cooldown(user_id, cooldown_sec)

        if not can_earn:
            increment_message_count_only(user_id)
            return

        xp_earned, is_suggestion, is_long = evaluate_message_xp(text)
        current_user = get_or_create_user(user_id)
        old_level, old_title = get_level_info_for_xp(current_user["xp"])

        new_total_xp = current_user["xp"] + xp_earned
        new_level, new_title = get_level_info_for_xp(new_total_xp)

        record_message_and_add_xp(
            user_id=user_id,
            channel_id=channel_id,
            xp_to_add=xp_earned,
            new_level=new_level,
            message_length=len(text),
            is_suggestion=is_suggestion,
            update_cooldown=True
        )

        logger.info(f"Awarded {xp_earned} XP to {user_id} in {channel_id}.")

        if is_suggestion and event.get("ts"):
            try:
                client.reactions_add(channel=channel_id, timestamp=event["ts"], name="bulb")
            except Exception:
                pass

        if new_level > old_level:
            logger.info(f"User {user_id} leveled up to Level {new_level}!")
            if rules.get("announce_level_up_in_channel", True):
                announcement = format_level_up_message(user_id, new_level, new_title)
                try:
                    say(text=announcement, channel=channel_id)
                    if event.get("ts"):
                        client.reactions_add(channel=channel_id, timestamp=event["ts"], name="tada")
                except Exception as e:
                    logger.error(f"Failed to post level up celebration: {e}")

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

        cleaned_text = clean_mention_text(raw_text).strip()
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
            repo_msg = "🚀 *Here is our GitHub repository:*\n<https://github.com/Thermo-flux/community-slack-bot|Thermo-flux/community-slack-bot>\nCheck out the code, star it, or propose features! ✨"
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

        # AI Chat with conversation memory & injected real-time community context
        history = fetch_conversation_history(
            client=client,
            channel_id=channel_id,
            thread_ts=event.get("thread_ts"),
            bot_user_id=bot_user_id,
            current_user_id=user_id,
            exclude_ts=event.get("ts"),
            max_messages=10
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

    @app.command("/leaderboard")
    def handle_leaderboard_command(ack, respond, say):
        ack()
        leaders = get_leaderboard(limit=10)
        board = format_leaderboard(leaders)
        try:
            respond(text=board, response_type="in_channel")
        except Exception:
            say(text=board)
