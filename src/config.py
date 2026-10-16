"""
==============================================================================
CONFIG LOADER MODULE (src/config.py)
==============================================================================
Loads settings from config/config.json and credentials from .env.
Searches project paths automatically for seamless execution from any folder.
==============================================================================
"""

import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SlackBotConfig")

# Base paths
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent

def _resolve_file(filename: str, subfolder: str = "") -> Path:
    """Finds a file in project root, config folder, or src folder."""
    candidates = [
        PROJECT_DIR / subfolder / filename,
        PROJECT_DIR / filename,
        SRC_DIR / subfolder / filename,
        SRC_DIR / filename
    ]
    for path in candidates:
        if path.exists():
            return path
    return PROJECT_DIR / subfolder / filename

ENV_PATH = _resolve_file(".env")
CONFIG_JSON_PATH = _resolve_file("config.json", "config")
SOUL_PATH = _resolve_file("soul.md", "config")

# Load environment variables
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    logger.info(f"Loaded environment variables from {ENV_PATH.name}")
else:
    load_dotenv()


def _clean_json_comments(data):
    if isinstance(data, dict):
        return {
            k: _clean_json_comments(v)
            for k, v in data.items()
            if not k.startswith("_comment")
        }
    elif isinstance(data, list):
        return [_clean_json_comments(item) for item in data]
    return data


def load_json_config():
    if not CONFIG_JSON_PATH.exists():
        logger.warning(f"Config file not found at {CONFIG_JSON_PATH}! Using safe defaults.")
        return _get_default_config()

    try:
        with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            cleaned_data = _clean_json_comments(raw_data)
            logger.info("Successfully loaded configuration.")
            return cleaned_data
    except Exception as e:
        logger.error(f"Failed to parse config file ({e}). Using defaults.")
        return _get_default_config()


def _get_default_config():
    return {
        "bot_name": "Community Champion Bot",
        "watched_channels": ["*"],
        "interaction_rules": {
            "cooldown_seconds": 30,
            "base_xp_per_message": 10,
            "bonus_xp_long_message": 5,
            "long_message_threshold": 80,
            "bonus_xp_suggestion": 15,
            "suggestion_keywords": ["suggest", "idea", "propose", "what if", "rfc"],
            "announce_level_up_in_channel": True,
            "reply_in_thread": False
        },
        "emoji_settings": {
            "frequency": "moderate",
            "allowed_emojis": ["🎉", "⭐", "🚀", "💡", "🔥", "✨"]
        },
        "profile": {
            "custom_pfp_emoji": ":robot_face:"
        },
        "level_tiers": [
            {"level": 1, "min_xp": 0, "title": "🌱 Novice Explorer"},
            {"level": 2, "min_xp": 50, "title": "🌿 Active Contributor"},
            {"level": 3, "min_xp": 150, "title": "⭐ Thoughtful Voice"},
            {"level": 4, "min_xp": 300, "title": "💡 Idea Champion"},
            {"level": 5, "min_xp": 550, "title": "🚀 Pillar of Community"}
        ],
        "templates": {
            "level_up": "🎉 *LEVEL UP!* Congratulations <@{user_id}>! You just reached *Level {level}: {title}*! 🚀",
            "rank_card": "*Level Card for <@{user_id}>*\n• Level: {level} ({title})\n• XP: {xp} XP\n• Progress: {progress_bar} {percent}%\n• XP Needed: {xp_needed} XP",
            "leaderboard_header": "🏆 *Top Contributors Leaderboard* 🏆",
            "leaderboard_row": "*{rank}.* <@{user_id}> — Level {level} (*{title}*) • `{xp} XP`",
            "leaderboard_empty": "No contributions recorded yet. Be the first to start chatting! 🚀",
            "help_message": "Type `/level` to see your rank, or `/leaderboard` to view top contributors!",
            "ai_offline_fallback": "Hey <@{user_id}>! I received your message. Tracking your XP! 🚀"
        }
    }


CONFIG = load_json_config()

# Credentials
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()

# AI settings
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "").strip()

# Channel whitelist
_env_channels = os.getenv("WATCHED_CHANNELS", "").strip()
if _env_channels:
    if _env_channels == "*":
        WATCHED_CHANNELS = ["*"]
    else:
        WATCHED_CHANNELS = [c.strip() for c in _env_channels.split(",") if c.strip()]
else:
    WATCHED_CHANNELS = CONFIG.get("watched_channels", ["*"])

# Database path
default_db = PROJECT_DIR / "data" / "community_karma.db"
DB_PATH = os.getenv("DB_PATH", str(default_db))


def is_channel_watched(channel_id: str) -> bool:
    if not channel_id:
        return False
    if "*" in WATCHED_CHANNELS:
        return True
    return channel_id in WATCHED_CHANNELS
