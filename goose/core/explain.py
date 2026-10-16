"""Explain German on request, from the approved table, with no LLM. OWNER: agent B (vibes).

Stored per duck message that used German: (channel, message_ts, de_ids) for 7 days, never text.
"""
from __future__ import annotations

import random
import uuid
from typing import List, Optional

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.lines import DE_RE, LineBank
from goose.core.safety import is_plain_context

NOTE_TTL_S = 7 * 86400
LOG_TTL_S = 86400
CHANNEL_WINDOW_S = 600


class ExplainService:
    def __init__(self, db: Database, config: Config, clock: Clock, lines: LineBank, rng: random.Random):
        self.db = db
        self.config = config
        self.clock = clock
        self.lines = lines
        self.rng = rng

    def record(self, channel: str, message_ts: str, de_ids: List[str], now: float) -> None:
        ids = list(dict.fromkeys(de_ids))
        if not ids:
            return
        with self.db.tx():
            self.db.execute(
                "INSERT OR REPLACE INTO german_notes(channel,message_ts,de_ids,ts) VALUES(?,?,?,?)",
                (channel, message_ts, ",".join(ids), float(now)))

    def lookup(self, channel: str, message_ts: str) -> List[str]:
        row = self.db.one("SELECT de_ids, ts FROM german_notes WHERE channel=? AND message_ts=?",
                          (channel, message_ts))
        if row is None or float(row["ts"]) < self.clock.now() - NOTE_TTL_S:
            return []
        return [x for x in str(row["de_ids"]).split(",") if x]

    def explanation(self, channel: str, message_ts: str, now: float) -> Optional[str]:
        """The playful explanation text for the German in that duck message (one E-style reply per id, joined),
        or None when: nothing stored, an explanation for that message was given less than
        config.vibes.explain_message_gap_s ago, or the channel already had config.vibes.explain_channel_per_10min
        in the last 10 minutes. Writes explain_log. Never quotes the thread."""
        ids = self.lookup(channel, message_ts)
        if not ids:
            return None
        cfg = self.config.vibes
        msg_key = "msg:%s:%s" % (channel, message_ts)
        last = self.db.one("SELECT ts FROM explain_log WHERE key=?", (msg_key,))
        if last is not None and now - float(last["ts"]) < cfg.explain_message_gap_s:
            return None
        prefix = "chan:%s:" % channel
        recent = self.db.one(
            "SELECT COUNT(*) AS n FROM explain_log WHERE substr(key,1,?)=? AND ts>=?",
            (len(prefix), prefix, now - CHANNEL_WINDOW_S))
        if int(recent["n"]) >= cfg.explain_channel_per_10min:
            return None
        text = " ".join(self.lines.explain_text(de_id) for de_id in ids)
        with self.db.tx():
            self.db.execute("INSERT OR REPLACE INTO explain_log(key,ts) VALUES(?,?)", (msg_key, float(now)))
            self.db.execute("INSERT INTO explain_log(key,ts) VALUES(?,?)",
                            ("chan:%s:%s" % (channel, uuid.uuid4().hex), float(now)))
        return text

    def unknown_reply(self) -> str:
        """Fixed line for a phrase that is not in the table."""
        return "That phrase is not in my table, so the duck won't guess what it means."

    def compose(self, text: str, user_id: str, rng: random.Random, *,
                allow_german: bool = True, context: str = "") -> str:
        """The one deterministic way to put an approved German line in front of a person.

        Every {de:id} (or {{de:id}} in a voice reply) is expanded from the approved table, and the approved
        English gloss for each id is attached right after, per the german_gloss dial. The gloss is read only
        from the table (LineBank.gloss_text); nothing here invents a gloss. Under 'always' every id is glossed.

        Plain contexts and allow_german=False carry no German at all: the {de:id} slots are dropped and no
        gloss is added, so an error, privacy line, receipt or the distress path never shows German (PLAN
        sections 2 and 11)."""
        normalized = self.lines.normalize_placeholders(text or "")
        ids = self.lines.de_ids_in(normalized)
        if not ids:
            return normalized
        if not allow_german or is_plain_context(context):
            return " ".join(DE_RE.sub(" ", normalized).split())
        expanded = self.lines.expand(normalized)
        gloss = self.maybe_gloss(user_id, ids, rng)
        return expanded + " " + gloss if gloss else expanded

    def maybe_gloss(self, user_id: str, de_ids: List[str], rng: random.Random) -> str:
        """Gloss text to append for the german_gloss dial: 'on_request' -> '', 'always' -> glosses for all ids,
        'first_time' -> glosses only for ids this person has not seen (records gloss_seen)."""
        mode = self.config.vibes.german_gloss
        ids = list(dict.fromkeys(de_ids))
        if not ids or mode == "on_request":
            return ""
        if mode == "always":
            targets = ids
        else:
            targets = []
            with self.db.tx():
                for de_id in ids:
                    seen = self.db.one("SELECT 1 FROM gloss_seen WHERE user_id=? AND de_id=?", (user_id, de_id))
                    if seen is None:
                        self.db.execute("INSERT OR IGNORE INTO gloss_seen(user_id,de_id) VALUES(?,?)",
                                        (user_id, de_id))
                        targets.append(de_id)
        return " ".join(self.lines.gloss_text(de_id, rng) for de_id in targets)

    def purge(self, now: float) -> int:
        """Delete german_notes older than 7 days and explain_log older than a day. Returns rows deleted."""
        with self.db.tx():
            notes = self.db.execute("DELETE FROM german_notes WHERE ts < ?", (now - NOTE_TTL_S,)).rowcount
            logs = self.db.execute("DELETE FROM explain_log WHERE ts < ?", (now - LOG_TTL_S,)).rowcount
        return int(notes or 0) + int(logs or 0)
