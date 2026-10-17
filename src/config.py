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
import math
import re
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
        "bot_name": "goose-lee",
        "official_loadout_channel_id": "C0C5XUDMRH9",
        "loadout_development_channel_id": "C0C68G3K0AJ",
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
            "frequency": "low",
            "allowed_emojis": [":duck:", ":eyes:", ":sparkles:", ":rocket:"]
        },
        "profile": {
            "custom_pfp_emoji": ":duck:"
        },
        "level_tiers": [
            {"level": 1, "min_xp": 0, "title": "Hatchling"},
            {"level": 2, "min_xp": 10, "title": "Duckling"},
            {"level": 3, "min_xp": 25, "title": "Paddler"},
            {"level": 4, "min_xp": 50, "title": "Dabbler"},
            {"level": 5, "min_xp": 100, "title": "Mallard"},
            {"level": 6, "min_xp": 155, "title": "Diver"},
            {"level": 7, "min_xp": 235, "title": "Pond Scout"},
            {"level": 8, "min_xp": 330, "title": "Flock Leader"},
            {"level": 9, "min_xp": 445, "title": "Wing Commander"},
            {"level": 10, "min_xp": 580, "title": "Golden Beak"},
            {"level": 11, "min_xp": 735, "title": "Pond Elder"},
            {"level": 12, "min_xp": 890, "title": "Head Duck"}
        ],
        "templates": {
            "level_up": ":sparkles: Level up, <@{user_id}>. You reached Level {level}: {title}. Nice work.",
            "rank_card": "*Level Card for <@{user_id}>*\n• Level {level}: {title}\n• XP: {xp} XP\n• Progress: {progress_bar} {percent}%\n• XP Needed: {xp_needed} XP",
            "leaderboard_header": "Top contributors",
            "leaderboard_row": "*{rank}.* <@{user_id}> — Level {level} (*{title}*) • `{xp} XP`",
            "leaderboard_empty": "No contributions recorded yet. Start chatting to earn XP.",
            "help_message": "I'm Goose Lee. Use /level for a card or /leaderboard for the standings. Mention me to chat in #loadout or #loadout-development.",
            "ai_offline_fallback": "Hi <@{user_id}>. I can't reach my answer service right now. Try again shortly."
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


def _positive_float_setting(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid %s; using the default", name)
        return default
    if not math.isfinite(value) or value <= 0:
        logger.warning("Invalid %s; using the default", name)
        return default
    return value


def _positive_int_setting(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid %s; using the default", name)
        return default
    if value < minimum or value > maximum:
        logger.warning("Invalid %s; using the default", name)
        return default
    return value


AI_TIMEOUT_SECONDS = _positive_float_setting("AI_TIMEOUT_SECONDS", 15.0)
AI_MAX_OUTPUT_TOKENS = _positive_int_setting("AI_MAX_OUTPUT_TOKENS", 512, maximum=16384)
AI_MAX_REPLY_CHARS = _positive_int_setting("AI_MAX_REPLY_CHARS", 400, maximum=4000)
AI_REPLY_WORKERS = _positive_int_setting("AI_REPLY_WORKERS", 4, maximum=16)
AI_REPLY_QUEUE_SIZE = _positive_int_setting("AI_REPLY_QUEUE_SIZE", 12, minimum=0, maximum=128)

def _channel_id_setting(env_name: str, config_name: str) -> str:
    channel_id = os.getenv(env_name, "").strip()
    if not channel_id:
        channel_id = str(CONFIG.get(config_name, "")).strip()
    if channel_id and not re.fullmatch(r"C[A-Z0-9]+", channel_id):
        logger.warning("Invalid %s; that channel is disabled", env_name)
        return ""
    return channel_id


# Each public channel is configured separately and verified against its exact
# Slack name at startup. Wildcards, lists, and duplicate IDs never get watched.
OFFICIAL_LOADOUT_CHANNEL_ID = _channel_id_setting(
    "OFFICIAL_LOADOUT_CHANNEL_ID", "official_loadout_channel_id"
)
LOADOUT_DEVELOPMENT_CHANNEL_ID = _channel_id_setting(
    "LOADOUT_DEVELOPMENT_CHANNEL_ID", "loadout_development_channel_id"
)
VERIFIED_CHANNEL_IDS = set()
WATCHED_CHANNELS = []


def get_configured_channels():
    """Return unique configured channel IDs paired with their required Slack names."""
    candidates = [
        (OFFICIAL_LOADOUT_CHANNEL_ID, "loadout"),
        (LOADOUT_DEVELOPMENT_CHANNEL_ID, "loadout-development"),
    ]
    configured_ids = [channel_id for channel_id, _name in candidates if channel_id]
    duplicates = {channel_id for channel_id in configured_ids if configured_ids.count(channel_id) > 1}
    if duplicates:
        logger.error("Duplicate channel IDs configured; disabling the ambiguous channel entries")
    return [
        (channel_id, name)
        for channel_id, name in candidates
        if channel_id and channel_id not in duplicates
    ]


def set_watched_channel_verified(channel_id: str, name: str, is_private: bool) -> bool:
    """Enable one configured channel only after Slack confirms its exact public name."""
    expected_name = dict(get_configured_channels()).get(channel_id)
    verified = bool(
        expected_name
        and str(name or "") == expected_name
        and is_private is False
    )
    if verified:
        VERIFIED_CHANNEL_IDS.add(channel_id)
    else:
        VERIFIED_CHANNEL_IDS.discard(channel_id)
    WATCHED_CHANNELS[:] = [
        configured_id
        for configured_id, _expected_name in get_configured_channels()
        if configured_id in VERIFIED_CHANNEL_IDS
    ]
    return verified


def set_official_loadout_channel_verified(channel_id: str, name: str, is_private: bool) -> bool:
    """Compatibility wrapper for callers that verify the official channel."""
    if channel_id != OFFICIAL_LOADOUT_CHANNEL_ID:
        return False
    return set_watched_channel_verified(channel_id, name, is_private)

# Database path (always resolve relative to PROJECT_DIR if relative)
_env_db = os.getenv("DB_PATH", "").strip()
if _env_db:
    _p = Path(_env_db)
    DB_PATH = str(_p if _p.is_absolute() else (PROJECT_DIR / _p))
else:
    DB_PATH = str(PROJECT_DIR / "data" / "community_karma.db")



def is_channel_watched(channel_id: str) -> bool:
    if not channel_id:
        return False
    # Always allow Direct Messages (Slack DM channel IDs start with 'D')
    if channel_id.startswith("D"):
        return True
    return bool(
        channel_id in VERIFIED_CHANNEL_IDS
        and channel_id in {configured_id for configured_id, _name in get_configured_channels()}
    )
