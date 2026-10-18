"""Rare visual bits for Goose: bad sifu quotes and reaction GIFs.

Assets live under ``assets/goose`` and are posted with ``files_upload_v2``. When that
scope is unavailable, the same asset is posted from its public repository URL so the
clips still appear. Callers fall back to text only if both paths fail.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

from config import ASSETS
from database import get_state, set_state

logger = logging.getLogger("SlackBotAssets")

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "goose"
ASSET_BASE_URL = (
    os.getenv("ASSETS_BASE_URL", "").strip()
    or str(ASSETS.get("base_url") or "").strip()
    or "https://raw.githubusercontent.com/Thermo-flux/community-slack-bot/feat/goose-lee-v6/assets/goose"
).rstrip("/")

QUOTES = [
    {
        "text": "Don't let anyone ruin your day. It's your day, ruin it yourself.",
        "who": "Paw Tzu",
        "image": None,
    },
    {
        "text": "Defeat yourself so your enemy can't defeat you, as you're already defeated.",
        "who": "Master Cat Tzu",
        "image": "quotes/already_defeated.png",
    },
    {
        "text": "If you want to avoid a bad day, wake up at night.",
        "who": "Ancient Sifu Wisdom",
        "image": None,
    },
    {
        "text": "If your enemy can predict your next move, don't move.",
        "who": "Master Sifu Cat",
        "image": "quotes/dont_move.png",
    },
    {
        "text": "If at first you don't succeed, skydiving is not for you.",
        "who": "Grandmaster Dog Tzu",
        "image": "quotes/skydiving.png",
    },
    {
        "text": "Hard work pays off in the future, but laziness pays off right now.",
        "who": "Sifu Cat",
        "image": "quotes/laziness.png",
    },
    {
        "text": "Before you marry a person, first make them use a computer with slow internet to see who they really are.",
        "who": "Goose Lee",
        "image": None,
    },
    {
        "text": "Never argue with an idiot. They will drag you down to their level and beat you with experience.",
        "who": "Sifu Shifu",
        "image": None,
    },
]

_QUOTE_RE = re.compile(
    r"\b(?:tell|give|got|share|drop|send)\b[^.?!]{0,24}\b(?:quote|quotes|wisdom|wise words)\b"
    r"|\b(?:words? of wisdom|ancient wisdom|sifu wisdom|paw tzu|cat tzu|sifu quote|quote me)\b"
    r"|^\s*(?:quote|quotes|wisdom)\b"
    r"|\b(?:quote|quotes|wisdom)\b\s*(?:please)?\s*[?!.]*$",
    re.IGNORECASE,
)

_SLANDER = r"(?:bad|mid|overrated|trash|awful|gross|terrible|sucks|worst|disgusting|fake|boring|overhyped)"
_FOOD = r"(?:boba|dim sum|noodles?|fried rice|dumplings?)"

_RAGE_RULES = [
    (
        re.compile(
            rf"\bmsg\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\bmsg\b", re.IGNORECASE
        ),
        ("rage/msg_sacred.gif", "MSG is sacred. This is a personal attack."),
    ),
    (
        re.compile(
            rf"\b{_FOOD}\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\b{_FOOD}\b",
            re.IGNORECASE,
        ),
        ("rage/food_slander.gif", "Aiya. We do not speak about the food that way."),
    ),
    (
        re.compile(
            rf"\bbruce lee\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\bbruce lee\b",
            re.IGNORECASE,
        ),
        ("rage/bruce_lee.gif", "That is my namesake. Choose your next words very carefully."),
    ),
    (
        re.compile(r"\b(?:nunchaku|nunchuck)\b", re.IGNORECASE),
        ("rage/nunchaku_fail.gif", "I have trained my whole life for this. Watch."),
    ),
    (
        re.compile(
            r"\b(?:show|demonstrate|teach|do|use)\b[^.?!]{0,24}\b(?:kung fu|wing chun|martial arts|karate)\b",
            re.IGNORECASE,
        ),
        ("rage/nunchaku_fail.gif", "I have trained my whole life for this. Watch."),
    ),
    (
        re.compile(
            r"\b(?:build|ci|deploy|pipeline|tests?)\b.{0,30}\b(?:fail|failed|failing|red|broke|broken)\b",
            re.IGNORECASE,
        ),
        ("rage/failure.gif", "The build has dishonored the pond."),
    ),
    (
        re.compile(
            r"\b(?:emotional damage|my code (?:broke|is broken)|everything is broken)\b",
            re.IGNORECASE,
        ),
        ("rage/emotional_damage.gif", "Emotional damage."),
    ),
]

_VICTORY_RE = re.compile(
    r"\b(?:build|ci|deploy|pipeline|tests?)\b.{0,30}\b(?:pass|passed|passing|green|succe(?:ed|eded|ss)|works?)\b"
    r"|\bshipped\b",
    re.IGNORECASE,
)


def is_quote_request(text: str) -> bool:
    return bool(_QUOTE_RE.search(text or ""))


SIFU_IMAGES = [
    "quotes/paw_tzu_wise.png",
    "quotes/bearded_shifu_peace.png",
    "quotes/shifu_deep_sigh.png",
    "quotes/shifu_frustrated.png",
    "quotes/pai_mei_smirk.png",
    "quotes/sifu_cat_stance.png",
    "quotes/dog_master_staff.png",
]

GOOSE_IMAGES = [
    "quotes/goose_typing.gif",
    "quotes/goose_kick.gif",
]


def pick_quote() -> Dict[str, Optional[str]]:
    """Pick a quote and attach an unpaired sifu still, or a duck clip for Goose's own line."""
    import random

    quote = dict(random.choice(QUOTES))
    if not quote.get("image"):
        pool = GOOSE_IMAGES if quote.get("who") == "Goose Lee" else SIFU_IMAGES
        quote["image"] = random.choice(pool)
    return quote


def format_quote(quote: Dict[str, Optional[str]]) -> str:
    return f'"{quote["text"]}" - {quote["who"]}'


def detect_moment(text: str) -> Optional[Dict[str, str]]:
    body = text or ""
    for pattern, (image, line) in _RAGE_RULES:
        if pattern.search(body):
            return {"image": image, "line": line, "kind": "rage"}
    if _VICTORY_RE.search(body):
        return {"image": "victory/waddle.gif", "line": "victory waddle", "kind": "victory"}
    return None


def allow_asset(channel_id: str) -> bool:
    """Per-channel cooldown plus a chance gate so reaction clips stay rare."""
    if not ASSETS.get("enabled", True) or not channel_id:
        return False
    cooldown = float(ASSETS.get("cooldown_seconds", 900) or 0)
    chance = float(ASSETS.get("chance", 0.5) or 0)
    now = time.time()
    try:
        last = float(get_state(f"asset_last:{channel_id}", "0") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if cooldown > 0 and now - last < cooldown:
        return False
    if chance < 1.0:
        import random

        if random.random() > chance:
            return False
    try:
        set_state(f"asset_last:{channel_id}", str(now))
    except Exception:
        pass
    return True


def asset_url(relative_path: str) -> str:
    return f"{ASSET_BASE_URL}/{relative_path.replace(os.sep, '/')}"


def post_asset_url(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    relative_path: str,
    caption: str = "",
) -> bool:
    """Post an asset from its public URL when a direct upload is not allowed."""
    blocks = [
        {
            "type": "image",
            "image_url": asset_url(relative_path),
            "alt_text": caption or "Goose Lee",
        }
    ]
    if caption:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": caption}]}
        )
    try:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=caption or "Goose Lee",
            blocks=blocks,
        )
        return True
    except Exception as exc:
        logger.warning("Could not post %s from its URL (%s)", relative_path, type(exc).__name__)
        return False


def upload_asset(
    client,
    channel_id: str,
    thread_ts: Optional[str],
    relative_path: str,
    initial_comment: str = "",
) -> bool:
    path = ASSETS_DIR / relative_path
    if not path.is_file():
        logger.warning("Asset is missing locally: %s", relative_path)
        return post_asset_url(client, channel_id, thread_ts, relative_path, initial_comment)
    try:
        client.files_upload_v2(
            channel=channel_id,
            thread_ts=thread_ts or None,
            file=str(path),
            filename=path.name,
            initial_comment=initial_comment or None,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Could not upload %s (%s); trying the public asset URL",
            relative_path,
            type(exc).__name__,
        )
        return post_asset_url(client, channel_id, thread_ts, relative_path, initial_comment)
