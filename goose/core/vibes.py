"""Reactions, name/ping handling, roasts, ranking-remark rolls, budgets. OWNER: agent B (vibes).
Behavior: docs/PLAN.md sections 2 and 3. Everything here is deterministic given the rng; no LLM.
"""
from __future__ import annotations

import random
import re
from typing import Optional, Tuple

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.lines import LineBank
from goose.core.rules import (contains_ping, count_exclamations, mentions_name, strip_markup)
from goose.core.safety import contains_word
from goose.types import Event

# word triggers (whole words, ignored inside code/quotes/urls). Draft table from docs/PLAN.md section 3.
# Each entry: (emoji, pattern description). Implement as compiled regexes in the module.
#   duck:            quack, quak, duck, goose-lee
#   tada:            "I" or "we" followed by shipped/launched/released
#   sunrise:         gm / good morning, only when local hour < 12
#   wave:            moin, bye, tschüss/tschuess, cya
#   bread:           bread, only near duck/ducks
#   birthday:        happy birthday, skipped near died/miss/"first without"
#   partying_face:   congrats, congratulations, glückwunsch
#   zzz:             good night, gn
#   feather:         feather, feathers
# There is no trigger for finally / it works / toast.

REACTION_CHANCE = 0.6
BREAD_WINDOW = 6                    # tokens either side of "bread" that may hold "duck" or "ducks"
ORDER = ["tada", "partying_face", "birthday", "sunrise", "wave", "zzz", "feather", "bread", "duck"]
PATTERNS = {
    "tada": re.compile(r"\b(?:i|we)\s+(?:just\s+|finally\s+|also\s+|have\s+)?(?:shipped|launched|released)\b"),
    "partying_face": re.compile(r"\b(?:congrats|congratulations|gl[uü]ckwunsch)\b"),
    "birthday": re.compile(r"\bhappy birthday\b"),
    "sunrise": re.compile(r"\b(?:gm|good morning)\b"),
    "wave": re.compile(r"\b(?:moin|bye|tsch[uü]ss|tschuess|cya)\b"),
    "zzz": re.compile(r"\b(?:good night|gn)\b"),
    "feather": re.compile(r"\bfeathers?\b"),
    "bread": re.compile(r"\bbread\b"),
    "duck": re.compile(r"\b(?:quack|quak|duck|ducks|goose-lee)\b"),
}
BIRTHDAY_BLOCK = re.compile(r"\b(?:died|dies|dead|miss\w*)\b|first without")
LOL_RE = re.compile(r"\blol\b")


def _lower_visible(text: str) -> str:
    return strip_markup(text).lower()


class Vibes:
    def __init__(self, db: Database, config: Config, clock: Clock, lines: LineBank, rng: random.Random):
        self.db = db
        self.config = config
        self.clock = clock
        self.lines = lines
        self.rng = rng

    # ---- word reactions -----------------------------------------------------------------------------------

    def reaction_for(self, text: str, user_id: str, now: float, *, quiet: bool, sad: bool) -> Optional[str]:
        """Emoji name for a word trigger, or None. One reaction per message. Applies: quiet, sad (suppress),
        random chance (0.6), per-person gap (reaction_person_gap_s), hourly budget (reactions_per_hour) via
        reaction_log. Writes reaction_log when it returns an emoji."""
        if quiet or sad:
            return None
        emoji = self._match(text, now)
        if emoji is None:
            return None
        if self.rng.random() >= REACTION_CHANCE:
            return None
        if self._person_recent(user_id, now, ("reaction", "name"), self.config.vibes.reaction_person_gap_s):
            return None
        if self._reactions_in_hour(now) >= self.config.vibes.reactions_per_hour:
            return None
        self._log(user_id, now, "reaction")
        return emoji

    def _match(self, text: str, now: float) -> Optional[str]:
        low = _lower_visible(text)
        if not low:
            return None
        for emoji in ORDER:
            if emoji == "sunrise":
                if self.clock.hour_of_day(now) >= 12:
                    continue
            if emoji == "birthday":
                if not PATTERNS["birthday"].search(low) or BIRTHDAY_BLOCK.search(low):
                    continue
                return emoji
            if emoji == "bread":
                if self._bread_near_duck(low):
                    return emoji
                continue
            if PATTERNS[emoji].search(low):
                return emoji
        return None

    @staticmethod
    def _bread_near_duck(low: str) -> bool:
        tokens = re.findall(r"\w+", low)
        for i, tok in enumerate(tokens):
            if tok != "bread":
                continue
            lo, hi = max(0, i - BREAD_WINDOW), min(len(tokens), i + BREAD_WINDOW + 1)
            if any(t in ("duck", "ducks") for t in tokens[lo:hi]):
                return True
        return False

    def name_reaction(self, text: str, user_id: str, now: float, *, quiet: bool, sad: bool) -> Optional[str]:
        """'duck' when the text mentions the bot's name (no ping), with chance name_reaction_chance, at most once per
        person per 10 minutes, inside the hourly budget."""
        if quiet or sad:
            return None
        if not mentions_name(text):
            return None
        if self.rng.random() >= self.config.vibes.name_reaction_chance:
            return None
        if self._person_recent(user_id, now, ("name",), self.config.vibes.reaction_person_gap_s):
            return None
        if self._reactions_in_hour(now) >= self.config.vibes.reactions_per_hour:
            return None
        self._log(user_id, now, "name")
        return "duck"

    def ack_allowed(self, user_id: str, now: float) -> bool:
        """True at most once per person per ack_person_gap_s for ping/name acknowledgements. Records it when True."""
        if self._person_recent(user_id, now, ("ack",), self.config.vibes.ack_person_gap_s, table="remark_log"):
            return False
        self._log(user_id, now, "ack", table="remark_log")
        return True

    # ---- roasts and ranking remarks ------------------------------------------------------------------------

    def roast_template(self, text: str, user_id: str, now: float, *, quiet: bool, sad: bool, newcomer: bool,
                       others_teasing: int = 0) -> Optional[str]:
        """A fixed-template roast line (about harmless habits only) or None. Conditions: not quiet, not sad, not a
        newcomer, fewer than 3 others teasing, this person not roasted in the last roast_person_gap_s, and a roll under
        roast_rate. Triggers: 4+ exclamation marks, three 'lol' in one message. (The 'Nth duck reaction' trigger in
        the plan needs reaction counts per person, which this module does not have, so it is not implemented.)
        Returns a ready line (from the bank moment 'roast'). Writes remark_log."""
        if quiet or sad or newcomer or others_teasing >= 3:
            return None
        if self._person_recent(user_id, now, ("roast",), self.config.vibes.roast_person_gap_s, table="remark_log"):
            return None
        excl = count_exclamations(text)
        lol = len(LOL_RE.findall(_lower_visible(text)))
        if excl >= 4:
            candidates = [ln for ln in self.lines.lines("roast") if "{excl}" in ln.text]
        elif lol >= 3:
            candidates = [ln for ln in self.lines.lines("roast") if "{excl}" not in ln.text]
        else:
            return None
        if not candidates or self.rng.random() >= self.config.vibes.roast_rate:
            return None
        chosen = self.rng.choice(candidates)
        self._log(user_id, now, "roast", table="remark_log")
        return self.lines.render(chosen.text, {"excl": excl})

    def rank_remark_allowed(self, user_id: str, now: float, *, quiet: bool, shown: bool, resting: bool,
                            band: Optional[str]) -> bool:
        """Unprompted admiring remark: only band active/very_active, shown, not quiet, not resting, about
        rank_remark_chance per message, at most once per rank_remark_gap_s per person. Writes remark_log when True."""
        if quiet or resting or not shown or band not in ("active", "very_active"):
            return False
        if self._person_recent(user_id, now, ("rank",), self.config.vibes.rank_remark_gap_s, table="remark_log"):
            return False
        if self.rng.random() >= self.config.vibes.rank_remark_chance:
            return False
        self._log(user_id, now, "rank", table="remark_log")
        return True

    def event_remark_allowed(self, now: float) -> bool:
        """Channel-wide limits for event remarks: voice.event_remarks_per_hour and per_day via remark_log kind 'event'."""
        hour = self._count("event", now - 3600, table="remark_log")
        day = self._count("event", now - 86400, table="remark_log")
        voice = self.config.voice
        if hour >= voice.event_remarks_per_hour or day >= voice.event_remarks_per_day:
            return False
        self._log("", now, "event", table="remark_log")
        return True

    # ---- addressing ---------------------------------------------------------------------------------------

    def classify_addressing(self, event: Event, bot_user_id: str) -> str:
        """'ping' (bot mentioned), 'name' (name without ping), '' otherwise. A message that also pings or mentions
        Orry (text contains 'orry', case-insensitive, or its user id handled by the caller) returns 'orry' so the duck stays silent."""
        text = event.text or ""
        ping = bool(event.bot_mentioned) or contains_ping(text, bot_user_id)
        name = not ping and mentions_name(text)
        if not (ping or name):
            return ""
        if contains_word(text, ["orry"]):
            return "orry"
        return "ping" if ping else "name"

    def set_not_funny(self, user_id: str) -> bool:
        """Detect 'not funny' style replies elsewhere; this just records remark_log kind 'stop' (caller sets quiet)."""
        self._log(user_id, self.clock.now(), "stop", table="remark_log")
        return True

    # ---- storage helpers ----------------------------------------------------------------------------------

    def _reactions_in_hour(self, now: float) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM reaction_log WHERE ts>=? AND kind IN ('reaction','name')",
                          (now - 3600,))
        return int(row["n"])

    def _person_recent(self, user_id: str, now: float, kinds: Tuple[str, ...], window: float,
                       table: str = "reaction_log") -> bool:
        marks = ",".join("?" for _ in kinds)
        row = self.db.one("SELECT COUNT(*) AS n FROM %s WHERE user_id=? AND ts>? AND kind IN (%s)" % (table, marks),
                          (user_id, now - window) + tuple(kinds))
        return int(row["n"]) > 0

    def _count(self, kind: str, since: float, table: str) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM %s WHERE kind=? AND ts>=?" % table, (kind, since))
        return int(row["n"])

    def _log(self, user_id: str, now: float, kind: str, table: str = "reaction_log") -> None:
        self.db.execute("INSERT INTO %s(user_id,ts,kind) VALUES(?,?,?)" % table, (user_id, float(now), kind))
