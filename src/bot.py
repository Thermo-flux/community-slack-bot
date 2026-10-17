"""
==============================================================================
MAIN SLACK BOT ENTRY POINT (src/bot.py)
==============================================================================
"""

import logging
import signal
import sys
import threading
import time

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import (
    AI_MODEL,
    AI_PROVIDER,
    CONFIG,
    ConfigValidationError,
    DB_PATH,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    WATCHED_CHANNELS,
    assert_valid_config,
    disable_watched_channel,
    get_configured_channels,
    set_watched_channel_verified,
)
from database import init_db
from handlers import register_handlers, run_scheduled_maintenance

logger = logging.getLogger("CommunityBot")

_PLACEHOLDER_TOKENS = {
    "xoxb-your-slack-bot-token-here",
    "xoxb-your-bot-token-here",
    "xapp-your-slack-app-token-here",
    "xapp-1-your-app-level-token-here",
}


def print_banner(bot_name: str, bot_user_id: str) -> None:
    """Prints a friendly startup banner in the terminal."""
    safe_name = str(bot_name or "goose-lee")
    rules = CONFIG.get("interaction_rules") or {}
    channels_str = ", ".join(str(c) for c in (WATCHED_CHANNELS or [])) or "DMs only"
    banner = f"""
================================================================
🚀 {safe_name.upper()} IS ONLINE!
================================================================
• Bot User ID       : {bot_user_id or "Connected via Socket Mode"}
• Monitored Channels: {channels_str}
• AI Provider       : {AI_PROVIDER} (Model: {AI_MODEL or "default"})
• Database Path     : {DB_PATH}
• Base XP/Message   : {rules.get('base_xp_per_message', 10)} XP
• Anti-Spam Wait    : {rules.get('cooldown_seconds', 30)}s cooldown
================================================================
Ready and listening for Slack events! Press Ctrl+C to stop.
    """
    print(banner, flush=True)


def check_tokens() -> bool:
    """Checks that the required Slack tokens are present and not example placeholders."""
    missing = []
    if (
        not SLACK_BOT_TOKEN
        or not SLACK_BOT_TOKEN.startswith("xoxb-")
        or SLACK_BOT_TOKEN in _PLACEHOLDER_TOKENS
    ):
        missing.append("SLACK_BOT_TOKEN (must start with 'xoxb-')")
    if (
        not SLACK_APP_TOKEN
        or not SLACK_APP_TOKEN.startswith("xapp-")
        or SLACK_APP_TOKEN in _PLACEHOLDER_TOKENS
    ):
        missing.append("SLACK_APP_TOKEN (must start with 'xapp-')")

    if missing:
        print("\n" + "=" * 70, flush=True)
        print("❌ MISSING CONFIGURATION CREDENTIALS", flush=True)
        print("=" * 70, flush=True)
        print("The bot cannot start because the following tokens are missing or invalid:", flush=True)
        for item in missing:
            print(f"  • {item}", flush=True)
        print("\n👉 QUICK FIX:", flush=True)
        print("  1. Copy '.env.example' to '.env'", flush=True)
        print("  2. Open '.env' and paste your Slack tokens.", flush=True)
        print("  3. For setup guidance, check 'README.md'.", flush=True)
        print("=" * 70 + "\n", flush=True)
        return False

    return True


def main() -> None:
    """Initializes and runs the Slack bot."""
    print("Initializing Community Slack Bot...", flush=True)

    if not check_tokens():
        sys.exit(1)

    try:
        assert_valid_config()
    except ConfigValidationError as e:
        print("\n" + "=" * 70, flush=True)
        print("❌ INVALID CONFIGURATION - refusing to start", flush=True)
        print(f"  • {e}", flush=True)
        print("Fix config/config.json, then restart.", flush=True)
        print("=" * 70 + "\n", flush=True)
        sys.exit(1)

    logger.info("Initializing SQLite database...")
    try:
        init_db(DB_PATH)
    except Exception as e:
        logger.error("Failed to initialize SQLite database: %s", e)
        sys.exit(1)

    bot_user_id = ""
    try:
        app = App(token=SLACK_BOT_TOKEN)
        auth_test_res = app.client.auth_test()
        bot_user_id = auth_test_res.get("user_id", "")
        bot_name = auth_test_res.get("user", CONFIG.get("bot_name") or "goose-lee")
        logger.info("Authenticated with Slack as @%s (%s)", bot_name, bot_user_id)
    except Exception as e:
        logger.error("Failed to authenticate with Slack: %s", e)
        print("\n⚠️ Authentication with Slack failed. Please verify your SLACK_BOT_TOKEN.\n", flush=True)
        sys.exit(1)

    configured_channels = get_configured_channels()
    if not configured_channels:
        logger.warning(
            "No valid public channels are configured; channel handling is disabled (DMs remain enabled)."
        )
    for channel_id, expected_name in configured_channels:
        try:
            channel_info = app.client.conversations_info(channel=channel_id).get("channel", {})
            resolved_name = str(channel_info.get("name") or "") or channel_id
            if set_watched_channel_verified(
                channel_id=channel_info.get("id", ""),
                name=channel_info.get("name", ""),
                is_private=channel_info.get("is_private"),
            ):
                privacy = "private" if channel_info.get("is_private") else "public"
                logger.info(
                    "Verified #%s channel (%s).",
                    expected_name or resolved_name,
                    privacy,
                )
            else:
                logger.error(
                    "Configured channel did not resolve to public #%s; that channel is disabled.",
                    expected_name or resolved_name,
                )
        except Exception as exc:
            disable_watched_channel(channel_id)
            logger.error(
                "Could not verify #%s (%s); that channel is disabled.",
                expected_name or channel_id,
                type(exc).__name__,
            )

    register_handlers(app, bot_user_id=bot_user_id)

    def _scheduler_loop() -> None:
        while True:
            try:
                run_scheduled_maintenance(app.client)
            except Exception as exc:
                logger.warning("Scheduled maintenance error (%s)", type(exc).__name__)
            time.sleep(3600)

    threading.Thread(target=_scheduler_loop, name="goose-scheduler", daemon=True).start()

    bot_display_name = CONFIG.get("bot_name") or "goose-lee"
    print_banner(bot_display_name, bot_user_id)

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    def handle_exit(sig, frame):
        try:
            handler.close()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        handler.start()
    except Exception as e:
        logger.error("Socket Mode encountered an error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
