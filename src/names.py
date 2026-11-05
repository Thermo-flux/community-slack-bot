"""
Display-name resolution for public boards.

Public leaderboards used to write members as ``<@U123>`` mentions, which pings
every person on the board each time it is posted. This module turns stored
Slack user IDs into plain-text labels instead, so boards never notify anyone.

Identity is never decided by name: scoring and ranking stay keyed on user IDs.
Names are only decoration, so this module also makes sure two board entries can
never *look* identical, and that nobody on a board can look like the bot.

Needs the ``users:read`` bot scope (already in config/slack_manifest.json).
"""

import logging
import threading
import time
import unicodedata
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Profiles change rarely; refresh every few hours so renames show up the same day.
CACHE_TTL_SECONDS = 6 * 60 * 60
# On a failed lookup, retry soon rather than caching the failure for hours.
FAILURE_TTL_SECONDS = 5 * 60

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()

# Characters that render as nothing and are commonly used to fake a duplicate name.
_INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "­"}


def fallback_label(user_id: str) -> str:
    """Neutral, non-pinging label for when a profile can't be fetched."""
    tail = (user_id or "")[-4:] or "????"
    return f"member ·{tail}"


def _escape(text: str) -> str:
    """Escape Slack control characters so a name can never become a mention or link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = "".join(ch for ch in text if ch not in _INVISIBLE and unicodedata.category(ch) != "Cc")
    return " ".join(text.split())


def _key(label: str) -> str:
    """Comparison key: case, width, spacing, punctuation and invisible chars ignored."""
    norm = unicodedata.normalize("NFKC", _clean(label)).casefold()
    return "".join(ch for ch in norm if ch.isalnum())


def _lookup(client, user_id: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(user_id)
        if hit and hit[0] > now:
            return hit[1]
    profile: Optional[Dict[str, Any]] = None
    ttl = FAILURE_TTL_SECONDS
    try:
        resp = client.users_info(user=user_id)
        profile = _profile_from_user(resp.get("user") or {})
        ttl = CACHE_TTL_SECONDS
    except Exception as exc:
        logger.warning("users.info failed for a board entry (%s)", type(exc).__name__)
    with _cache_lock:
        _cache[user_id] = (now + ttl, profile)
    return profile


def _profile_from_user(user: Dict[str, Any]) -> Dict[str, Any]:
    prof = user.get("profile") or {}
    return {
        "display": _clean(prof.get("display_name_normalized") or prof.get("display_name")),
        "real": _clean(prof.get("real_name_normalized") or prof.get("real_name") or user.get("real_name")),
        "handle": _clean(user.get("name")),
        "deleted": bool(user.get("deleted")),
        "is_bot": bool(user.get("is_bot")),
    }


# Above this many uncached IDs, one paged users.list is cheaper than many users.info calls.
BULK_THRESHOLD = 15


def _prefetch_all(client, max_pages: int = 20) -> None:
    """Fill the cache from users.list (200 per page) so a big cold board isn't 200 API calls."""
    now = time.time()
    cursor = None
    try:
        for _ in range(max_pages):
            resp = client.users_list(limit=200, cursor=cursor) if cursor else client.users_list(limit=200)
            with _cache_lock:
                for user in resp.get("members") or []:
                    if user.get("id"):
                        _cache[user["id"]] = (now + CACHE_TTL_SECONDS, _profile_from_user(user))
            cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
    except Exception as exc:
        logger.warning("users.list prefetch failed (%s); falling back to per-user lookups", type(exc).__name__)


def _uncached(user_ids) -> list:
    now = time.time()
    with _cache_lock:
        return [u for u in user_ids if not (u in _cache and _cache[u][0] > now)]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def board_labels(
    client,
    user_ids: Iterable[str],
    reserved_ids: Iterable[str] = (),
) -> Dict[str, str]:
    """
    Map each user ID to a plain-text, non-pinging label that is unique on this board.

    ``reserved_ids`` (e.g. the bot itself) are looked up only so that nobody on the
    board can display a name that looks like theirs.
    """
    ids = list(dict.fromkeys(uid for uid in user_ids if uid))
    if client is None:
        return {uid: fallback_label(uid) for uid in ids}

    if len(_uncached(ids)) > BULK_THRESHOLD:
        _prefetch_all(client)
    profiles = {uid: _lookup(client, uid) for uid in ids}

    # Names held by reserved accounts (the bot) that members must not appear as.
    reserved_keys = set()
    for rid in dict.fromkeys(r for r in reserved_ids if r and r not in profiles):
        prof = _lookup(client, rid)
        if prof:
            for value in (prof["display"], prof["real"], prof["handle"]):
                if _key(value):
                    reserved_keys.add(_key(value))

    base: Dict[str, str] = {}
    for uid in ids:
        prof = profiles[uid]
        if not prof:
            base[uid] = fallback_label(uid)
        elif prof["deleted"]:
            base[uid] = "former member"
        else:
            base[uid] = prof["display"] or prof["real"] or prof["handle"] or fallback_label(uid)

    # Step 1: names that clash with each other, or with a reserved account, get the real name added.
    counts: Dict[str, int] = {}
    for uid in ids:
        counts[_key(base[uid])] = counts.get(_key(base[uid]), 0) + 1

    labels: Dict[str, str] = {}
    for uid in ids:
        label = base[uid]
        k = _key(label)
        prof = profiles[uid]
        if (counts[k] > 1 or k in reserved_keys or not k) and prof and prof["real"] and _key(prof["real"]) != k:
            label = f"{label} ({prof['real']})"
        labels[uid] = label

    # Step 2: anything still ambiguous gets the tail of its permanent Slack ID.
    counts = {}
    for uid in ids:
        counts[_key(labels[uid])] = counts.get(_key(labels[uid]), 0) + 1
    for uid in ids:
        k = _key(labels[uid])
        if counts[k] > 1 or k in reserved_keys or not k:
            if not labels[uid].startswith("member ·"):
                labels[uid] = f"{labels[uid]} ·{uid[-4:]}"

    return {uid: _escape(label) for uid, label in labels.items()}
