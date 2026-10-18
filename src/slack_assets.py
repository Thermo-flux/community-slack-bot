"""Rare visual bits for Goose: bad sifu quotes and reaction GIFs.

Assets live under ``assets/goose`` and are posted with ``files_upload_v2``. When that
scope is unavailable, the same asset is posted from its public repository URL so the
clips still appear. Callers fall back to text only if both paths fail.
"""

import json
import logging
import os
import random
import re
import threading
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

_QUOTE_SUBJECTS = [
    "the last dumpling",
    "a lukewarm boba",
    "cold fried rice",
    "your unmerged branch",
    "a silent semicolon",
    "the family group chat",
    "one stubborn noodle",
    "a calm merge conflict",
    "the bug you ignored",
    "a spoon in the sink",
    "the auntie at the door",
    "a half-finished congee",
    "your green build",
    "the duck who waits",
    "a suspicious bao",
    "the oldest hot pot",
    "a single spring roll",
    "your commit history",
    "the lint warning you dismissed",
    "a very confident pigeon",
    "the rice cooker at 3am",
    "an empty boba cup",
    "the mat you slipped on",
    "a nunchuck with opinions",
    "the shifu's left sandal",
    "your fifth browser tab",
    "a tiny victory waddle",
    "the pond at midnight",
    "a fork in the noodle bowl",
    "the last piece of ginger",
    "your backup plan",
    "the dumpling steamer",
    "a very serious goose",
    "the chopstick that rolled away",
    "an overcooked noodle",
    "the moon over the mines",
    "a spoonful of chili oil",
    "your first clean deploy",
    "the snack drawer",
    "a bowl of rice with big dreams",
]

_QUOTE_VERBS = [
    "forgives",
    "outranks",
    "teaches",
    "haunts",
    "fears",
    "respects",
    "improves",
    "misunderstands",
    "betrays",
    "comforts",
    "ignores",
    "sharpens",
    "questions",
    "inherits",
    "defeats",
    "befriends",
    "warns",
    "outlasts",
    "judges",
    "nourishes",
    "embarrasses",
    "protects",
    "interrupts",
    "remembers",
]

_QUOTE_OBJECTS = [
    "a calm mind",
    "a clean diff",
    "the family group chat",
    "a warm bowl of rice",
    "your next deploy",
    "a patient shifu",
    "the last slice of ginger",
    "a quiet pond",
    "your future self",
    "a well-timed snack",
    "the ancestor group chat",
    "a green build",
    "one honest commit message",
    "a very small duck",
    "the spirit of Bruce Lee",
    "a fresh pot of tea",
    "your loudest bug",
    "a plate of dumplings",
    "the 3am rice cooker",
    "a serious boba order",
    "the family bill argument",
    "a lint-free morning",
    "the slowest noodle",
    "a patient rice farmer",
    "your oldest branch",
    "the last dumpling",
    "a quiet semicolon",
    "the pond elders",
    "a well-fed duckling",
    "your next idea",
    "the auntie's approval",
    "a stubborn wok",
    "the moon over the pond",
    "a single chopstick",
    "the snack you hid",
    "a calm nunchuck",
    "the smell of garlic",
    "a suspicious spoon",
    "your best excuse",
    "the sound of a steamer",
]

_QUOTE_TEMPLATES = [
    ("{s} {v} {o}.", "wise"),
    ("{s} {v} {o}.", "wise"),
    ("{s} {v} {o}, and the pond nods.", "sassy"),
    ("{s} {v} {o}, which explains everything.", "sassy"),
    ("Beware: {s} {v} {o}.", "sassy"),
    ("The way of the pond: {s} {v} {o}.", "wise"),
    ("{s} {v} {o}, as the ancestors predicted.", "wise"),
    ("{s} {v} {o}, and that is why we eat first.", "sassy"),
]

_QUOTE_AUTHORS = [
    "Paw Tzu",
    "Master Cat Tzu",
    "Ancient Sifu Wisdom",
    "Master Sifu Cat",
    "Grandmaster Dog Tzu",
    "Sifu Cat",
    "Sifu Shifu",
    "Goose Lee",
]

_FOOD_IMAGE_WORDS = (
    "dumpling",
    "boba",
    "rice",
    "noodle",
    "bao",
    "congee",
    "hot pot",
    "spring roll",
    "wok",
    "tea",
    "ginger",
    "chili",
    "snack",
    "steamer",
    "chopstick",
    "garlic",
    "soup",
)

_QUOTE_HISTORY_KEY = "quote_history"
_QUOTE_HISTORY_LIMIT = 50000
_QUOTE_HISTORY_LOCK = threading.RLock()

_QUOTE_COOLDOWN_SECONDS = float(os.getenv("QUOTE_COOLDOWN_SECONDS", "20") or 20)


def _normalize_quote(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _quote_history() -> list:
    try:
        raw = get_state(_QUOTE_HISTORY_KEY, "[]")
        history = json.loads(raw) if raw else []
    except Exception:
        history = []
    if not isinstance(history, list):
        return []
    return history[-_QUOTE_HISTORY_LIMIT:]


def _remember_quote(text: str) -> None:
    with _QUOTE_HISTORY_LOCK:
        history = _quote_history()
        history.append(_normalize_quote(text))
        if len(history) > _QUOTE_HISTORY_LIMIT:
            history = history[-_QUOTE_HISTORY_LIMIT:]
        try:
            set_state(_QUOTE_HISTORY_KEY, json.dumps(history))
        except Exception:
            logger.warning("Could not persist quote history", exc_info=True)


def _compose_quote():
    template, tone = random.choice(_QUOTE_TEMPLATES)
    text = template.format(
        s=random.choice(_QUOTE_SUBJECTS),
        v=random.choice(_QUOTE_VERBS),
        o=random.choice(_QUOTE_OBJECTS),
    )
    return text[0].upper() + text[1:], tone


def _pick_quote_author() -> str:
    if random.random() < 0.2:
        return "Goose Lee"
    return random.choice(_QUOTE_AUTHORS)


def pick_quote_image(text: str, tone: str, who: str) -> str:
    """Choose an image that fits the quote's tone and subject."""
    if who == "Goose Lee":
        return random.choice(GOOSE_IMAGES)
    if re.search(rf"\b(?:{'|'.join(map(re.escape, _FOOD_IMAGE_WORDS))})\b", text.lower()):
        return random.choice(SASSY_IMAGES)
    return random.choice(WISE_IMAGES if tone == "wise" else SASSY_IMAGES)


def generate_quote() -> Dict[str, Optional[str]]:
    """Compose a brand-new faux-sifu quote and attach a matching image.

    Every generated line is checked against the persisted history, so the bot does not
    repeat a quote until the full candidate pool is exhausted.
    """
    with _QUOTE_HISTORY_LOCK:
        history = set(_quote_history())
        text = None
        tone = "wise"
        for _ in range(60):
            candidate, candidate_tone = _compose_quote()
            if _normalize_quote(candidate) not in history:
                text, tone = candidate, candidate_tone
                break
        if text is None:
            for template, candidate_tone in _QUOTE_TEMPLATES:
                for subject in _QUOTE_SUBJECTS:
                    for verb in _QUOTE_VERBS:
                        for obj in _QUOTE_OBJECTS:
                            candidate = template.format(s=subject, v=verb, o=obj)
                            candidate = candidate[0].upper() + candidate[1:]
                            if _normalize_quote(candidate) not in history:
                                text, tone = candidate, candidate_tone
                                break
                        if text:
                            break
                    if text:
                        break
                if text:
                    break
        if text is None:
            base, tone = _compose_quote()
            text = f"{base} (lesson {len(history) + 1})"
        who = _pick_quote_author()
        quote = {"text": text, "who": who, "image": pick_quote_image(text, tone, who)}
        _remember_quote(text)
        return quote


def allow_quote(channel_id: str) -> bool:
    """One prophecy at a time per channel so quote spam cannot rewrite history repeatedly."""
    if not channel_id:
        return True
    if _QUOTE_COOLDOWN_SECONDS <= 0:
        return True
    now = time.time()
    key = f"quote_last:{channel_id}"
    try:
        last = float(get_state(key, "0") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < _QUOTE_COOLDOWN_SECONDS:
        return False
    try:
        set_state(key, str(now))
    except Exception:
        pass
    return True

_QUOTE_RE = re.compile(
    r"\b(?:tell|give|got|share|drop|send|get|fetch|hit)\b[^.?!]{0,24}\b(?:quote|quotes|wisdom|wise words)\b"
    r"|\b(?:words? of wisdom|ancient wisdom|sifu wisdom|sifu quote|quote me|something wise)\b"
    r"|^\s*(?:quote|quotes|wisdom)\b"
    r"|\b(?:quote|quotes|wisdom)\b\s*(?:please)?\s*[?!.]*$",
    re.IGNORECASE,
)

_SLANDER = r"(?:bad|mid|overrated|trash|awful|gross|terrible|sucks|worst|disgusting|fake|boring|overhyped|weird|pointless|useless)"
_NEGATION_RE = re.compile(
    rf"(?i)\b(?:not|never|no|n't|isn't|aren't|wasn't|weren't|don't|doesn't|didn't)\b"
    rf"[^.?!]{{0,30}}\b{_SLANDER}\b"
    rf"|\b{_SLANDER}\b[^.?!]{{0,20}}\b(?:not|never|no|n't)\b"
)
_FOOD = (
    r"(?:msg|boba|dim sum|noodles?|fried rice|dumplings?|hot pot|congee|bao|spring rolls?"
    r"|mapo tofu|chow mein|lo mein|wontons?|xiaolongbao|pho|ramen|sushi|kimchi|matcha)"
)

_RAGE_RULES = [
    (
        re.compile(
            rf"\bmsg\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\bmsg\b", re.IGNORECASE
        ),
        (
            "rage/msg_sacred.gif",
            "AIYA! MSG is sacred! You just insulted every grandmother in the pond. I am shaking. Explain yourself.",
        ),
    ),
    (
        re.compile(
            rf"\b{_FOOD}\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\b{_FOOD}\b",
            re.IGNORECASE,
        ),
        (
            "rage/food_slander.gif",
            "AIYA! You did not just say that about the food. The ancestors heard you. The whole pond heard you.",
        ),
    ),
    (
        re.compile(
            rf"\bbruce lee\b.{{0,40}}\b{_SLANDER}\b|\b{_SLANDER}\b.{{0,20}}\bbruce lee\b",
            re.IGNORECASE,
        ),
        (
            "rage/bruce_lee.gif",
            "That is my namesake. My ancestors are standing up right now. Choose your next words like your life depends on it.",
        ),
    ),
    (
        re.compile(
            rf"\b(?:kung fu|wing chun|martial arts?)\b.{{0,40}}\b{_SLANDER}\b"
            rf"|\b{_SLANDER}\b.{{0,20}}\b(?:kung fu|wing chun|martial arts?)\b",
            re.IGNORECASE,
        ),
        (
            "rage/bruce_lee.gif",
            "Fake? FAKE? My whole bloodline just gasped. The pond is in crisis. I need a moment.",
        ),
    ),
    (
        re.compile(r"\b(?:nunchaku|nunchuck)\b", re.IGNORECASE),
        (
            "rage/nunchaku_fail.gif",
            "I have trained my whole life for this. Watch closely. Do not blink. Do not tell Orry.",
        ),
    ),
    (
        re.compile(
            r"\b(?:show|demonstrate|teach|do|use)\b[^.?!]{0,24}\b(?:kung fu|wing chun|martial arts|karate)\b",
            re.IGNORECASE,
        ),
        (
            "rage/nunchaku_fail.gif",
            "I have trained my whole life for this. Watch closely. Do not blink. Do not tell Orry.",
        ),
    ),
    (
        re.compile(
            r"\b(?:build|ci|deploy|pipeline|tests?)\b.{0,30}\b(?:fail|failed|failing|red|broke|broken)\b",
            re.IGNORECASE,
        ),
        (
            "quotes/shifu_deep_sigh.png",
            "The build failed. The ancestors are disappointed, the pond is in mourning, and I am not okay. Fix it.",
        ),
    ),
    (
        re.compile(
            r"\b(?:emotional damage|my code (?:broke|is broken)|everything is broken)\b",
            re.IGNORECASE,
        ),
        (
            "quotes/shifu_frustrated.png",
            "Emotional damage! I felt that in my tail feathers. My ancestors felt it too. AIYA.",
        ),
    ),
]

_VICTORY_RE = re.compile(
    r"\b(?:build|ci|deploy|pipeline|tests?)\b.{0,30}\b(?:pass|passed|passing|green|succe(?:ed|eded|ss)|works?)\b"
    r"|\bshipped\b",
    re.IGNORECASE,
)


def is_quote_request(text: str) -> bool:
    return bool(_QUOTE_RE.search(text or ""))


def _negated(body: str, start: int, end: int) -> bool:
    window = body[max(0, start - 45):min(len(body), end + 25)]
    if re.search(r"(?i)\bnot\s+(?:gonna|going\s+to)\s+lie\b", window):
        return False
    return bool(_NEGATION_RE.search(window))


WISE_IMAGES = [
    "quotes/paw_tzu_wise.png",
    "quotes/bearded_shifu_peace.png",
    "quotes/dog_master_staff.png",
]

SASSY_IMAGES = [
    "quotes/pai_mei_smirk.png",
    "quotes/sifu_cat_stance.png",
    "quotes/shifu_frustrated.png",
    "quotes/shifu_deep_sigh.png",
]

GOOSE_IMAGES = [
    "quotes/goose_typing.gif",
    "quotes/goose_kick.gif",
]


def format_quote(quote: Dict[str, Optional[str]]) -> str:
    return f'"{quote["text"]}" - {quote["who"]}'


def detect_moment(text: str) -> Optional[Dict[str, str]]:
    body = text or ""
    for pattern, (image, line) in _RAGE_RULES:
        for match in pattern.finditer(body):
            if not _negated(body, match.start(), match.end()):
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
