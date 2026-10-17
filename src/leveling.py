"""
==============================================================================
LEVELING & PROGRESSION MODULE (src/leveling.py)
==============================================================================
Calculates XP, level tiers, progress bars, rank cards, and leaderboards.
==============================================================================
"""

import logging
import math
import re
from typing import Any, Dict, List, Tuple
from config import CONFIG

logger = logging.getLogger("SlackBotLeveling")

_DEFAULT_TIERS: List[Dict[str, Any]] = [
    {"level": 1, "min_xp": 0, "title": "Hatchling"},
    {"level": 2, "min_xp": 12, "title": "Duckling"},
    {"level": 3, "min_xp": 36, "title": "Paddler"},
    {"level": 4, "min_xp": 72, "title": "Dabbler"},
    {"level": 5, "min_xp": 120, "title": "Mallard"},
    {"level": 6, "min_xp": 180, "title": "Diver"},
    {"level": 7, "min_xp": 252, "title": "Pond Scout"},
    {"level": 8, "min_xp": 336, "title": "Flock Leader"},
    {"level": 9, "min_xp": 432, "title": "Wing Commander"},
    {"level": 10, "min_xp": 540, "title": "Golden Beak"},
    {"level": 11, "min_xp": 660, "title": "Pond Elder"},
    {"level": 12, "min_xp": 792, "title": "Head Duck"},
]


def get_level_tiers() -> List[Dict[str, Any]]:
    tiers = CONFIG.get("level_tiers") or _DEFAULT_TIERS
    return sorted(tiers, key=lambda t: int(t.get("min_xp", 0)))


def get_level_info_for_xp(xp: int) -> Tuple[int, str]:
    tiers = get_level_tiers()
    current_level = int(tiers[0]["level"])
    current_title = str(tiers[0]["title"])

    for tier in tiers:
        if xp >= int(tier["min_xp"]):
            current_level = int(tier["level"])
            current_title = str(tier["title"])
        else:
            break
    return current_level, current_title


def get_progress_to_next_level(xp: int) -> Dict[str, Any]:
    tiers = get_level_tiers()
    curr_tier_idx = 0

    for idx, tier in enumerate(tiers):
        if xp >= int(tier["min_xp"]):
            curr_tier_idx = idx
        else:
            break

    curr_tier = tiers[curr_tier_idx]
    is_max_level = curr_tier_idx == len(tiers) - 1

    if is_max_level:
        return {
            "level": int(curr_tier["level"]),
            "title": str(curr_tier["title"]),
            "next_level": None,
            "next_title": "Max Level Reached!",
            "xp_needed": 0,
            "percent": 100,
            "is_max_level": True,
        }

    next_tier = tiers[curr_tier_idx + 1]
    xp_in_curr_tier = xp - int(curr_tier["min_xp"])
    tier_range = int(next_tier["min_xp"]) - int(curr_tier["min_xp"])
    xp_needed = max(0, int(next_tier["min_xp"]) - xp)

    percent = int(math.floor((xp_in_curr_tier / tier_range) * 100)) if tier_range > 0 else 100
    percent = max(0, min(100, percent))

    return {
        "level": int(curr_tier["level"]),
        "title": str(curr_tier["title"]),
        "next_level": int(next_tier["level"]),
        "next_title": str(next_tier["title"]),
        "xp_needed": xp_needed,
        "percent": percent,
        "is_max_level": False,
    }


def render_progress_bar(percent: int, bar_length: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    if percent <= 0:
        filled_length = 0
    elif percent >= 100:
        filled_length = bar_length
    else:
        filled_length = min(bar_length - 1, int((percent * bar_length + 50) // 100))
    empty_length = bar_length - filled_length
    bar = "█" * filled_length + "░" * empty_length
    return f"[{bar}]"


def evaluate_message_xp(text: str) -> Tuple[int, bool, bool]:
    rules = CONFIG.get("interaction_rules") or {}
    base_xp = int(rules.get("base_xp_per_message", 10))
    bonus_long = int(rules.get("bonus_xp_long_message", 5))
    long_threshold = int(rules.get("long_message_threshold", 80))
    bonus_suggestion = int(rules.get("bonus_xp_suggestion", 15))
    keywords = rules.get("suggestion_keywords") or [
        "suggest",
        "suggestion",
        "idea",
        "propose",
        "proposal",
        "what if",
        "how about",
        "rfc",
    ]

    clean_text = (text or "").strip().lower()
    total_xp = base_xp

    is_long = len(clean_text) >= long_threshold
    if is_long:
        total_xp += bonus_long

    is_suggestion = any(
        bool(re.search(rf"\b{re.escape(str(kw).lower())}\b", clean_text))
        for kw in keywords
        if kw
    )
    if is_suggestion:
        total_xp += bonus_suggestion

    return total_xp, is_suggestion, is_long


def format_rank_card(user_data: Dict[str, Any]) -> str:
    templates = CONFIG.get("templates") or {}
    template = templates.get(
        "rank_card",
        "*Level Card for <@{user_id}>*\n• Level {level}: {title}\n• XP: {xp} XP",
    )
    xp = int(user_data.get("xp", 0))
    prog = get_progress_to_next_level(xp)
    progress_bar = render_progress_bar(prog["percent"])

    return template.format(
        user_id=user_data.get("user_id", "Unknown"),
        level=prog["level"],
        title=prog["title"],
        xp=xp,
        progress_bar=progress_bar,
        percent=prog["percent"],
        xp_needed=prog["xp_needed"],
        message_count=int(user_data.get("message_count", 0)),
        next_level=prog["next_level"] or prog["level"],
        next_title=prog["next_title"],
    )


def format_level_up_message(user_id: str, new_level: int, new_title: str) -> str:
    templates = CONFIG.get("templates") or {}
    template = templates.get(
        "level_up",
        ":sparkles: <@{user_id}> hit Level {level}: {title}. The duck does a tiny victory waddle.",
    )
    return template.format(user_id=user_id, level=new_level, title=new_title)


def format_leaderboard(leaderboard_entries: List[Dict[str, Any]]) -> str:
    templates = CONFIG.get("templates") or {}
    header = templates.get("leaderboard_header", "Top contributors")
    row_template = templates.get(
        "leaderboard_row",
        "*{rank}.* <@{user_id}> — Level {level} (*{title}*) • `{xp} XP`",
    )
    empty_template = templates.get(
        "leaderboard_empty",
        "No contributions recorded yet. Start chatting to earn XP.",
    )

    if not leaderboard_entries:
        return f"{header}\n\n{empty_template}"

    rows = []
    for rank, entry in enumerate(leaderboard_entries, start=1):
        xp = int(entry.get("xp", 0))
        level, title = get_level_info_for_xp(xp)
        row = row_template.format(
            rank=rank,
            user_id=entry.get("user_id", "Unknown"),
            level=level,
            title=title,
            xp=xp,
            message_count=int(entry.get("message_count", 0)),
        )
        rows.append(row)

    return f"{header}\n\n" + "\n".join(rows)
