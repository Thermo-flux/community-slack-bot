"""
==============================================================================
CRUMBS & POND LEVELS (src/crumbs.py)
==============================================================================
Deterministic, crumbs-only progression for goose-lee v6.
The hard-coded core owns all level math; the LLM voice never calculates levels.

- Crumbs come from qualified chatter, active days, thanks, helpful marks, and
  organizer stars in the official #loadout channel (after opt-in).
- Pond Levels PL.1..PL.12 use fixed thresholds; PL.6+ needs an organizer star.
- Above PL.12 each level costs 20% more crumbs, uncapped.
- Levels are derived from the ledger, never stored (only level_floor is stored).
==============================================================================
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from database import _DB_LOCK, _managed_connection, get_or_create_user

BOT_USER_ID = "U0C9JMMGDDH"

STAR_GATE_LEVEL = 6
STAR_GATE_MAX_LEVEL = 5
STAR_CRUMBS = 25
BIGSTAR_CRUMBS = 60
FLOOD_SECONDS = 30
REPEAT_WINDOW_SECONDS = 3600

_BASE_THRESHOLDS = [0, 10, 25, 50, 100, 155, 235, 330, 445, 580, 735, 890]
_TITLES: Dict[int, str] = {
    1: "Hatchling", 2: "Duckling", 3: "Paddler", 4: "Dabbler", 5: "Mallard",
    6: "Diver", 7: "Pond Scout", 8: "Flock Leader", 9: "Wing Commander",
    10: "Golden Beak", 11: "Pond Elder", 12: "Head Duck",
}

LEVEL_UP_LINE = "Pond level {level}, {title}. The duck wrote it down with a very serious face."
LEVEL_12_LINE = "Pond level 12, Head Duck. Mega! The duck salutes with one wing."
DEMOTE_LINE = "Heads up: your pond level is now {level} ({title}). No drama, just the ledger talking."


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

def crumbs_to_reach(level: int) -> int:
    """Crumbs needed to reach a pond level. Fixed to PL.12, then +20% per level (nearest 5)."""
    if level <= 1:
        return 0
    if level <= 12:
        return _BASE_THRESHOLDS[level - 1]
    return int(round((890.0 * (1.2 ** (level - 12))) / 5.0) * 5)


def title_for_level(level: int) -> Optional[str]:
    return _TITLES.get(level)


def level_for_crumbs(crumbs: float, has_star: bool) -> int:
    """Highest pond level unlocked by crumbs, capped at PL.5 without an active organizer star."""
    candidate = 1
    while crumbs_to_reach(candidate + 1) <= crumbs:
        candidate += 1
        if candidate > 1_000_000:
            break
    if not has_star and candidate > STAR_GATE_MAX_LEVEL:
        candidate = STAR_GATE_MAX_LEVEL
    return candidate


def weekly_crumbs(
    messages: int = 0,
    active_days: int = 0,
    thanks: int = 0,
    helpful: int = 0,
    stars: int = 0,
    bigstars: int = 0,
) -> float:
    crumbs = 8.0 * math.log(1 + messages / 5.0)
    crumbs += min(1.5 * active_days, 10.5)
    crumbs += 6.0 * math.log(1 + thanks / 3.0)
    crumbs += 14.0 * math.log(1 + helpful / 3.0)
    crumbs += STAR_CRUMBS * stars + BIGSTAR_CRUMBS * bigstars
    return crumbs


# ---------------------------------------------------------------------------
# Qualified-message rules
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+(?:\|[^>]+)?>", re.IGNORECASE)
_SLACK_LINK_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[a-z0-9_+\-]+:\d+>", re.IGNORECASE)
_SHORTCODE_RE = re.compile(r":[a-z0-9_+\-]+:", re.IGNORECASE)
_UNICODE_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def strip_for_scoring(text: str) -> str:
    clean = _CUSTOM_EMOJI_RE.sub(" ", text or "")
    clean = _MENTION_RE.sub(" ", clean)
    clean = _URL_RE.sub(" ", clean)
    clean = _SLACK_LINK_RE.sub(" ", clean)
    clean = _SHORTCODE_RE.sub(" ", clean)
    clean = _UNICODE_EMOJI_RE.sub(" ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def is_qualified_message(text: str) -> bool:
    clean = strip_for_scoring(text)
    if len(clean) < 12:
        return False
    words = [w for w in re.split(r"\s+", clean) if w]
    return len(words) >= 3


def week_key(ts: float) -> str:
    iso = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def day_key(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Ledger access
# ---------------------------------------------------------------------------

def is_opted_in(user_id: str, db_path=None) -> bool:
    if not user_id:
        return False
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute(
                "SELECT opted_in FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return bool(row and row["opted_in"])


def set_opted_in(user_id: str, db_path=None) -> Dict[str, Any]:
    now = time.time()
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            with conn:
                conn.execute(
                    """
                    UPDATE users
                    SET opted_in = 1,
                        joined_at = CASE WHEN joined_at > 0 THEN joined_at ELSE ? END,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, now, user_id),
                )
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else {}


def add_ledger_event(
    user_id: str,
    kind: str,
    channel_id: str = "",
    message_ts: str = "",
    ref: str = "",
    amount: float = 0.0,
    source_ts: Optional[float] = None,
    db_path=None,
) -> int:
    now = source_ts if source_ts is not None else time.time()
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO crumbs_ledger (
                        user_id, kind, amount, week_key, day_key, source_ts,
                        channel_id, message_ts, ref, voided, void_reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?)
                    """,
                    (user_id, kind, amount, week_key(now), day_key(now), now,
                     channel_id, message_ts, ref, time.time()),
                )
                return int(cur.lastrowid)


def void_ledger_event(event_id: int, reason: str = "", db_path=None) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM crumbs_ledger WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return None
            with conn:
                conn.execute(
                    "UPDATE crumbs_ledger SET voided = 1, void_reason = ? WHERE id = ?",
                    (reason or "", event_id),
                )
            return dict(row)


def _active_events(user_id: str, conn) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM crumbs_ledger WHERE user_id = ? AND voided = 0", (user_id,)
    )
    return [dict(r) for r in cur.fetchall()]


def compute_breakdown(user_id: str, db_path=None) -> Dict[str, Any]:
    """Derive total crumbs, weekly buckets, and star status from active ledger rows."""
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            events = _active_events(user_id, conn)

    weeks: Dict[str, Dict[str, Any]] = {}
    stars = bigstars = 0
    for ev in events:
        kind = ev["kind"]
        if kind == "star":
            stars += 1
        elif kind == "bigstar":
            bigstars += 1
        else:
            bucket = weeks.setdefault(ev["week_key"], {"messages": 0, "days": set(), "thanks": 0, "helpful": 0})
            if kind == "message":
                bucket["messages"] += 1
                bucket["days"].add(ev["day_key"])
            elif kind == "thanks":
                bucket["thanks"] += 1
            elif kind == "helpful":
                bucket["helpful"] += 1

    total = 0.0
    weekly: List[Dict[str, Any]] = []
    for wk in sorted(weeks):
        b = weeks[wk]
        cr = weekly_crumbs(b["messages"], len(b["days"]), b["thanks"], b["helpful"])
        total += cr
        weekly.append({
            "week": wk, "messages": b["messages"], "active_days": len(b["days"]),
            "thanks": b["thanks"], "helpful": b["helpful"], "crumbs": round(cr, 1),
        })
    total += STAR_CRUMBS * stars + BIGSTAR_CRUMBS * bigstars
    has_star = (stars + bigstars) > 0
    return {
        "crumbs": round(total, 1),
        "stars": stars,
        "bigstars": bigstars,
        "has_star": has_star,
        "weekly": weekly,
    }


def get_pond_status(user_id: str, db_path=None) -> Dict[str, Any]:
    breakdown = compute_breakdown(user_id, db_path)
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            row = conn.execute(
                "SELECT opted_in, level_floor FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
    floor = int(row["level_floor"]) if row and row["level_floor"] else 1
    derived = level_for_crumbs(breakdown["crumbs"], breakdown["has_star"])
    level = max(derived, floor)
    next_level = level + 1
    next_reach = crumbs_to_reach(next_level)
    return {
        "user_id": user_id,
        "opted_in": bool(row and row["opted_in"]),
        "crumbs": breakdown["crumbs"],
        "level": level,
        "title": title_for_level(level) or "",
        "next_level": next_level,
        "next_title": title_for_level(next_level) or "",
        "crumbs_to_next": max(0, int(round(next_reach - breakdown["crumbs"]))),
        "next_threshold": next_reach,
        "has_star": breakdown["has_star"],
        "stars": breakdown["stars"],
        "bigstars": breakdown["bigstars"],
        "level_floor": floor,
        "weekly": breakdown["weekly"],
        "gated": (not breakdown["has_star"]) and level >= STAR_GATE_MAX_LEVEL,
    }


def set_level_floor(user_id: str, level: int, db_path=None) -> None:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            with conn:
                conn.execute(
                    "UPDATE users SET level_floor = MAX(level_floor, ?) WHERE user_id = ?",
                    (int(level), user_id),
                )


def get_pond_leaderboard(limit: int = 10, db_path=None) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            cur = conn.execute(
                "SELECT user_id FROM users WHERE opted_in = 1 AND user_id != ?",
                (BOT_USER_ID,),
            )
            user_ids = [r["user_id"] for r in cur.fetchall()]
    entries = []
    for uid in user_ids:
        status = get_pond_status(uid, db_path)
        if status["crumbs"] > 0:
            entries.append(status)
    entries.sort(key=lambda e: (e["crumbs"], e["level"]), reverse=True)
    return entries[:limit]


def format_pond_card(status: Dict[str, Any], mention: Optional[str] = None) -> str:
    who = mention or f"<@{status['user_id']}>"
    lines = [f"*Level card for {who}*",
             f"- Pond Level PL.{status['level']}: {status['title'] or 'unranked'}",
             f"- Crumbs: {status['crumbs']:g}"]
    if status["next_threshold"]:
        lines.append(
            f"- Next: PL.{status['next_level']} {status['next_title'] or ''} "
            f"({status['crumbs_to_next']} crumbs away)"
        )
    if status["level"] >= STAR_GATE_MAX_LEVEL and not status["has_star"]:
        lines.append("- Star gate: PL.6 and up needs at least one organizer star.")
    if status["weekly"]:
        w = status["weekly"][-1]
        lines.append(
            f"_This week ({w['week']}): {w['messages']} msgs, {w['active_days']} active days, "
            f"{w['thanks']} thanks, {w['helpful']} helpful -> {w['crumbs']:g} crumbs_"
        )
    if not status["opted_in"]:
        lines.insert(0, "_Not joined yet - say `goose join` to start earning crumbs._\n")
    return "\n".join(lines)


def format_pond_leaderboard(entries: List[Dict[str, Any]], limit: int = 10) -> str:
    if not entries:
        return "No crumbs on the board yet. Say `goose join`, then chat in #loadout to start."
    out = ["*Pond standings*"]
    for rank, entry in enumerate(entries[:limit], start=1):
        out.append(
            f"{rank}. <@{entry['user_id']}> - PL.{entry['level']} "
            f"{entry['title'] or ''} - {entry['crumbs']:g} crumbs"
        )
    return "\n".join(out)


def level_up_message(status: Dict[str, Any]) -> str:
    if status["level"] >= 12:
        return LEVEL_12_LINE
    return LEVEL_UP_LINE.format(level=status["level"], title=status["title"] or "")


def record_qualified_message(
    user_id: str, channel_id: str, text: str, message_ts: str,
    flood_seconds: int = FLOOD_SECONDS, db_path=None,
) -> Optional[Dict[str, Any]]:
    """Score one qualified message if not flooding or repeating. Returns level-up info or None."""
    now = time.time()
    clean = strip_for_scoring(text)
    with _DB_LOCK:
        with _managed_connection(db_path) as conn:
            get_or_create_user(user_id, db_path)
            row = conn.execute(
                "SELECT opted_in, last_scored_ts, last_scored_text FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row or not row["opted_in"]:
                return None
            last_ts = float(row["last_scored_ts"] or 0.0)
            last_text = (row["last_scored_text"] or "")
            if last_ts and (now - last_ts) < flood_seconds:
                return None
            if last_text and clean and clean == last_text and (now - last_ts) < REPEAT_WINDOW_SECONDS:
                return None

            before = get_pond_status(user_id, db_path)
            with conn:
                conn.execute(
                    """
                    INSERT INTO crumbs_ledger (
                        user_id, kind, amount, week_key, day_key, source_ts,
                        channel_id, message_ts, ref, voided, void_reason, created_at
                    )
                    VALUES (?, 'message', 0, ?, ?, ?, ?, ?, '', 0, '', ?)
                    """,
                    (user_id, week_key(now), day_key(now), now, channel_id, message_ts, now),
                )
                conn.execute(
                    "UPDATE users SET last_scored_ts = ?, last_scored_text = ?, updated_at = ? WHERE user_id = ?",
                    (now, clean, now, user_id),
                )
    after = get_pond_status(user_id, db_path)
    if after["level"] > before["level"]:
        return {"before": before, "after": after, "delta": after["crumbs"] - before["crumbs"]}
    return None
