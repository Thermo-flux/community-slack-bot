"""
==============================================================================
MAIN SLACK BOT ENTRY POINT (src/bot.py)
==============================================================================
"""

import sys
import signal
import logging

# Ensure standard output and error support UTF-8 on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import (
    SLACK_BOT_TOKEN,
    SLACK_APP_TOKEN,
    CONFIG,
    WATCHED_CHANNELS,
    get_configured_channels,
    set_watched_channel_verified,
    AI_PROVIDER,
    AI_MODEL,
    DB_PATH
)
from database import init_db
from handlers import register_handlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("CommunityBot")


def print_banner(bot_name: str, bot_user_id: str):
    """Prints a friendly startup banner in the terminal."""
    banner = f"""
================================================================
🚀 {bot_name.upper()} IS ONLINE!
================================================================
• Bot User ID       : {bot_user_id or "Connected via Socket Mode"}
• Monitored Channels: {', '.join(WATCHED_CHANNELS)}
• AI Provider       : {AI_PROVIDER} (Model: {AI_MODEL or "default"})
• Database Path     : {DB_PATH}
• Base XP/Message   : {CONFIG.get('interaction_rules', {}).get('base_xp_per_message', 10)} XP
• Anti-Spam Wait    : {CONFIG.get('interaction_rules', {}).get('cooldown_seconds', 30)}s cooldown
================================================================
Ready and listening for Slack events! Press Ctrl+C to stop.
    """
    print(banner)


def check_tokens() -> bool:
    """Checks that the required Slack tokens are present."""
    missing = []
    if not SLACK_BOT_TOKEN or not SLACK_BOT_TOKEN.startswith("xoxb-"):
        missing.append("SLACK_BOT_TOKEN (must start with 'xoxb-')")
    if not SLACK_APP_TOKEN or not SLACK_APP_TOKEN.startswith("xapp-"):
        missing.append("SLACK_APP_TOKEN (must start with 'xapp-')")

    if missing:
        print("\n" + "=" * 70)
        print("❌ MISSING CONFIGURATION CREDENTIALS")
        print("=" * 70)
        print("The bot cannot start because the following tokens are missing or invalid:")
        for item in missing:
            print(f"  • {item}")
        print("\n👉 QUICK FIX:")
        print("  1. Copy '.env.example' to '.env'")
        print("  2. Open '.env' and paste your Slack tokens.")
        print("  3. For setup guidance, check 'README.md'.")
        print("=" * 70 + "\n")
        return False

    return True


def main():
    """Initializes and runs the Slack bot."""
    print("Initializing Community Slack Bot...")

    if not check_tokens():
        sys.exit(1)

    logger.info("Initializing SQLite database...")
    init_db(DB_PATH)

    app = App(token=SLACK_BOT_TOKEN)

    bot_user_id = ""
    try:
        auth_test_res = app.client.auth_test()
        bot_user_id = auth_test_res.get("user_id", "")
        bot_name = auth_test_res.get("user", CONFIG.get("bot_name", "Community Bot"))
        logger.info(f"Authenticated with Slack as @{bot_name} ({bot_user_id})")
    except Exception as e:
        logger.error(f"Failed to authenticate with Slack: {e}")
        print("\n⚠️ Authentication with Slack failed. Please verify your SLACK_BOT_TOKEN.\n")
        sys.exit(1)

    configured_channels = get_configured_channels()
    if not configured_channels:
        logger.warning("No valid public channels are configured; channel handling is disabled (DMs remain enabled).")
    for channel_id, expected_name in configured_channels:
        try:
            channel_info = app.client.conversations_info(channel=channel_id).get("channel", {})
            if set_watched_channel_verified(
                channel_id=channel_info.get("id", ""),
                name=channel_info.get("name", ""),
                is_private=channel_info.get("is_private"),
            ):
                logger.info("Verified public #%s channel.", expected_name)
            else:
                logger.error(
                    "Configured channel did not resolve to public #%s; that channel is disabled.",
                    expected_name,
                )
        except Exception as exc:
            set_watched_channel_verified(channel_id, "", True)
            logger.error("Could not verify public #%s (%s); that channel is disabled.", expected_name, type(exc).__name__)

    register_handlers(app, bot_user_id=bot_user_id)

    bot_display_name = CONFIG.get("bot_name", "Community Champion Bot")
    print_banner(bot_display_name, bot_user_id)

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    def handle_exit(sig, frame):
        logger.info("Shutdown signal received. Closing Slack connection...")
        try:
            handler.close()
        except Exception:
            pass
        logger.info("Bot stopped cleanly. Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        handler.start()
    except Exception as e:
        logger.error(f"Socket Mode encountered an error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
