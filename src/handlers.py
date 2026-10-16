"""
==============================================================================
SLACK EVENT & COMMAND HANDLERS (src/handlers.py)
==============================================================================
This module contains the event listeners and slash command handlers for Slack.
==============================================================================
"""

import re
import logging
from typing import Optional
from slack_bolt import App

from config import CONFIG, is_channel_watched
from database import (
    get_or_create_user,
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
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def register_handlers(app: App, bot_user_id: Optional[str] = None) -> None:
    """Registers all Slack Bolt event listeners and slash commands."""

    # 1. MESSAGE EVENT
    @app.event("message")
    def handle_message_events(body, logger_bolt, client, say):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        text = event.get("text", "")
        subtype = event.get("subtype")
        bot_id = event.get("bot_id")

        if bot_id or subtype == "bot_message" or not user_id:
            return
        if subtype in ["message_deleted", "message_changed", "channel_join", "channel_leave", "pinned_info"]:
            return
        if not is_channel_watched(channel_id):
            return

        rules = CONFIG.get("interaction_rules", {})
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

    # 2. APP MENTION EVENT
    @app.event("app_mention")
    def handle_app_mentions(body, say, client):
        event = body.get("event", {})
        channel_id = event.get("channel")
        user_id = event.get("user")
        raw_text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        if not user_id:
            return

        cleaned_text = clean_mention_text(raw_text).strip().lower()

        if cleaned_text in ("level", "rank", "xp", "stats", "my level", "points"):
            user_data = get_or_create_user(user_id)
            card = format_rank_card(user_data)
            say(text=card, thread_ts=thread_ts)
            return

        if cleaned_text in ("leaderboard", "top", "ranks", "scores"):
            leaders = get_leaderboard(limit=10)
            board = format_leaderboard(leaders)
            say(text=board, thread_ts=thread_ts)
            return

        if cleaned_text in ("help", "commands"):
            templates = CONFIG.get("templates", {})
            rules = CONFIG.get("interaction_rules", {})
            help_msg = templates.get("help_message", "Type `/level` or `/leaderboard`!")
            formatted_help = help_msg.format(
                bot_id=bot_user_id or "bot",
                cooldown=rules.get("cooldown_seconds", 30)
            )
            say(text=formatted_help, thread_ts=thread_ts)
            return

        # AI Chat
        ai_reply = ai_service.generate_response(
            user_id=user_id,
            user_message=cleaned_text or "Hello!",
            channel_id=channel_id
        )
        say(text=ai_reply, thread_ts=thread_ts)

    # 3. SLASH COMMANDS
    @app.command("/level")
    def handle_level_command(ack, command, respond):
        ack()
        user_id = command.get("user_id")
        user_data = get_or_create_user(user_id)
        respond(text=format_rank_card(user_data), response_type="ephemeral")

    @app.command("/rank")
    def handle_rank_command(ack, command, respond):
        ack()
        user_id = command.get("user_id")
        user_data = get_or_create_user(user_id)
        respond(text=format_rank_card(user_data), response_type="ephemeral")

    @app.command("/leaderboard")
    def handle_leaderboard_command(ack, respond):
        ack()
        leaders = get_leaderboard(limit=10)
        respond(text=format_leaderboard(leaders), response_type="in_channel")
