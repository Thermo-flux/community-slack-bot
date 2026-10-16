"""The context card the voice may see. OWNER: agent A (ledger). Behavior: docs/PLAN.md section 10.5.

The card NEVER holds names, Slack ids, other people's numbers, provisional or live standings, flags, roles or program data.
It is built from the last CONFIRMED snapshot. Places and numbers about the speaker are only present when private=True.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, Optional

SLOTS = ("p1", "p2", "p3")


def activity_band(crumbs_last_week: Optional[float], steady: float, active: float, very_active: float) -> Optional[str]:
    """None when there is no closed week yet. Else "quiet" (< steady), "steady", "active", "very_active"."""
    if crumbs_last_week is None:
        return None
    value = float(crumbs_last_week)
    if value < steady:
        return "quiet"
    if value < active:
        return "steady"
    if value < very_active:
        return "active"
    return "very_active"


def _standing(rows: Iterable[Dict[str, Any]], user_id: str) -> str:
    for index, row in enumerate(rows):
        if row["user_id"] == user_id:
            if index < 3:
                return "top3"
            return "top10" if index < 10 else "other"
    return "other"


def _band_for(ctx: Any, user_id: str, rest: bool, now: float) -> Optional[str]:
    """Activity band from the person's crumbs in the last closed week. Resting people get None."""
    if rest:
        return None
    previous = ctx.clock.previous_week_id(ctx.clock.week_id(now))
    if ctx.weekly.status(previous) is None:
        return None
    crumbs = ctx.ledger.week_breakdown(user_id, previous, include_pending=False).total
    levels_cfg = ctx.config.levels
    return activity_band(crumbs, levels_cfg.band_steady, levels_cfg.band_active, levels_cfg.band_very_active)


def _streak_days(ctx: Any, user_id: str, now: float) -> int:
    """Consecutive local days ending today with a qualified message. 0 if today has none."""
    days = ctx.ledger.message_days(user_id, include_pending=True)
    day = dt.date.fromisoformat(ctx.clock.day_key(now))
    count = 0
    while day.isoformat() in days:
        count += 1
        day -= dt.timedelta(days=1)
    return count


def build_card(ctx: Any, user_id: str, private: bool, now: float) -> Dict[str, Any]:
    """ctx is goose.core.context.AppContext. Returns:
    {"v": 1, "version": <confirmed week version or 0>,
     "you": {"pond_level": int, "pond_title": str, "activity_band": str|None, "streak_days": int,
             "new_member": bool, "resting": bool, "standing": "top3"|"top10"|"other"|None (only if private)},
     "week": {"id": str, "state": "confirmed", "has_top3": bool, "slots": ["p1","p2","p3"]} or {} if none confirmed,
     "rewards_announced": bool}
    activity_band uses the person's crumbs in the last closed week; resting people get activity_band None.
    streak_days = consecutive local days up to today with a qualified message (0 if none)."""
    when = float(now)
    user = ctx.users.get(user_id) or {}
    confirmed = ctx.weekly.last_confirmed()
    lifetime = ctx.ledger.lifetime(user_id, include_pending=False)
    level = max(ctx.levels.level_for(lifetime), int(user.get("level_floor", 1)))
    resting = bool(user.get("rest", False))

    you: Dict[str, Any] = {
        "pond_level": level,
        "pond_title": ctx.levels.title(level),
        "activity_band": _band_for(ctx, user_id, resting, when),
        "streak_days": _streak_days(ctx, user_id, when),
        "new_member": ctx.users.is_newcomer(user_id, when),
        "resting": resting,
    }
    if private:
        rows = confirmed["rows"] if confirmed else []
        you["standing"] = _standing(rows, user_id)

    week: Dict[str, Any] = {}
    if confirmed:
        has_top3 = len(confirmed["top"]) >= 3
        week = {
            "id": confirmed["week_id"],
            "state": "confirmed",
            "has_top3": has_top3,
            "slots": list(SLOTS) if has_top3 else [],
        }

    return {
        "v": 1,
        "version": confirmed["version"] if confirmed else 0,
        "you": you,
        "week": week,
        "rewards_announced": bool(ctx.config.rewards.announced),
    }


def display_names_for_slots(ctx: Any, card_week_rows: Any) -> Dict[str, str]:
    """Map "top1".."top3" to Slack mention tokens like "<@U123>" for the confirmed top 3 who chose show.
    Used by the gateway to expand {{top1}} etc. The voice never sees these. Hidden people map to nothing."""
    if card_week_rows is None:
        confirmed = ctx.weekly.last_confirmed()
        card_week_rows = confirmed["rows"] if confirmed else []
    out: Dict[str, str] = {}
    for index, row in enumerate(list(card_week_rows)[:3], start=1):
        user_id = row["user_id"] if isinstance(row, dict) else str(row)
        user = ctx.users.get(user_id)
        if user is not None and user["show"]:
            out["top%d" % index] = "<@%s>" % user_id
    return out
