"""Member state: join, leave, show, hide, rest, quiet, freeze. OWNER: agent A (ledger).

Nothing is scored before `join`. `show` defaults to off (hidden from boards). `leave` deletes the person's rows
and is refused while an open flag exists.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.ledger import tombstone

FLAG_KINDS = ("disqualify", "freeze", "note")
# Per-person rows that leave() deletes outright. Ledger.anonymize_user handles facts.
PERSONAL_TABLES = ("gloss_seen", "voice_person", "remark_log", "reaction_log", "consents", "seen_hash", "mood_events")
DAY_S = 86400.0


class Users:
    def __init__(self, db: Database, config: Config, clock: Clock, ledger: Optional[object] = None):
        self.db = db
        self.config = config
        self.clock = clock
        self.ledger = ledger

    # ------------------------------------------------------------------ reads

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        data = dict(row)
        for key in ("show", "rest", "quiet"):
            data[key] = bool(data[key])
        return data

    def is_joined(self, user_id: str) -> bool:
        return self.db.one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is not None

    def roster(self) -> List[Dict[str, Any]]:
        """Everyone joined: user_id, joined_ts, show, rest, quiet, frozen_until, open flags count."""
        rows = self.db.query(
            "SELECT u.user_id, u.joined_ts, u.show, u.rest, u.quiet, u.frozen_until, "
            "(SELECT COUNT(*) FROM flags f WHERE f.user_id = u.user_id AND f.closed_ts IS NULL) AS open_flags "
            "FROM users u ORDER BY u.joined_ts, u.user_id")
        out = []
        for row in rows:
            item = dict(row)
            for key in ("show", "rest", "quiet"):
                item[key] = bool(item[key])
            out.append(item)
        return out

    def is_organizer(self, user_id: str) -> bool:
        return self.config.is_organizer(user_id)

    def is_owner(self, user_id: str) -> bool:
        return self.config.is_owner(user_id)

    def has_organizer_star(self, user_id: str) -> bool:
        """True when the person holds at least one non-voided organizer star (the v6 level gate, PLAN_V6 section 5).
        Pending stars count, exactly like Ledger.star_count; a voided star does not, so revoking the last star
        closes the gate again and Levels.effective_level falls back to at most gate_level."""
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND kind IN ('star', 'bigstar') "
            "AND state != 'voided'", (user_id,))
        return int(row["n"]) > 0

    def is_frozen(self, user_id: str, now: float) -> bool:
        row = self.db.one("SELECT frozen_until FROM users WHERE user_id = ?", (user_id,))
        return row is not None and float(row["frozen_until"]) > float(now)

    def open_flags(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM flags WHERE user_id = ? AND closed_ts IS NULL ORDER BY id", (user_id,))
        return [dict(r) for r in rows]

    def is_newcomer(self, user_id: str, now: float) -> bool:
        """Joined less than config.vibes.newcomer_days ago."""
        row = self.db.one("SELECT joined_ts FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            return False
        return float(now) - float(row["joined_ts"]) < self.config.vibes.newcomer_days * DAY_S

    # ------------------------------------------------------------------ membership

    def join(self, user_id: str, now: float, channel: str = "", consent_version: str = "1") -> bool:
        """False if already joined. Writes a consents row."""
        if not user_id:
            raise ValueError("user_id is required")
        with self.db.tx():
            if self.is_joined(user_id):
                return False
            self.db.execute(
                "INSERT INTO users (user_id, joined_ts, consent_version, last_channel) VALUES (?,?,?,?)",
                (user_id, float(now), consent_version, channel))
            self.db.execute("INSERT INTO consents (user_id, version, ts) VALUES (?,?,?)",
                            (user_id, consent_version, float(now)))
        return True

    def leave(self, user_id: str, now: float) -> bool:
        """Delete the person (users row, own facts, gloss_seen, voice_person, remark_log, reaction_log rows) and
        anonymize references (Ledger.anonymize_user). False if not joined or an open flag exists.
        `now` is accepted for symmetry with join; nothing is recorded with it."""
        if not self.is_joined(user_id):
            return False
        if self.open_flags(user_id):
            return False
        with self.db.tx():
            self.db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,))
            if self.ledger is not None:
                self.ledger.anonymize_user(user_id)  # type: ignore[attr-defined]
            for table in PERSONAL_TABLES:
                self.db.execute("DELETE FROM %s WHERE user_id = ?" % table, (user_id,))
            # Closed flags stay for the record, but without the person's id.
            self.db.execute("UPDATE flags SET user_id = ? WHERE user_id = ?", (tombstone(user_id), user_id))
            self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return True

    # ------------------------------------------------------------------ settings

    def _set(self, user_id: str, column: str, value: Any) -> None:
        self.db.execute("UPDATE users SET %s = ? WHERE user_id = ?" % column, (value, user_id))

    def set_show(self, user_id: str, value: bool) -> None:
        self._set(user_id, "show", 1 if value else 0)

    def set_rest(self, user_id: str, value: bool) -> None:
        self._set(user_id, "rest", 1 if value else 0)

    def set_quiet(self, user_id: str, value: bool) -> None:
        self._set(user_id, "quiet", 1 if value else 0)

    def set_level_floor(self, user_id: str, level: int) -> None:
        self._set(user_id, "level_floor", max(1, int(level)))

    def set_last_channel(self, user_id: str, channel: str) -> None:
        self._set(user_id, "last_channel", channel)

    def set_announced_level(self, user_id: str, level: int) -> None:
        self._set(user_id, "announced_level", max(1, int(level)))

    # ------------------------------------------------------------------ freezes and flags

    def freeze(self, user_id: str, until_ts: float, reason: str, actor: str, now: float) -> None:
        """Pause scoring until `until_ts`. Opens a 'freeze' flag that unfreeze closes."""
        if not self.is_joined(user_id):
            raise ValueError("cannot freeze a person who has not joined")
        with self.db.tx():
            for flag in self.open_flags(user_id):
                if flag["kind"] == "freeze":
                    self.close_flag(flag["id"], now)
            self._set(user_id, "frozen_until", float(until_ts))
            self.add_flag(user_id, "freeze", "", reason, actor, now)

    def unfreeze(self, user_id: str, actor: str, now: float) -> bool:
        """True if anything was unfrozen (the freeze time or an open freeze flag). `actor` is not stored here."""
        changed = False
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE users SET frozen_until = 0 WHERE user_id = ? AND frozen_until > 0", (user_id,))
            changed = cur.rowcount > 0
            for flag in self.open_flags(user_id):
                if flag["kind"] == "freeze":
                    self.close_flag(flag["id"], now)
                    changed = True
        return changed

    def add_flag(self, user_id: str, kind: str, scope: str, reason: str, actor: str, now: float) -> int:
        if kind not in FLAG_KINDS:
            raise ValueError("unknown flag kind %r" % (kind,))
        cur = self.db.execute(
            "INSERT INTO flags (user_id, kind, scope, reason, actor, opened_ts) VALUES (?,?,?,?,?,?)",
            (user_id, kind, scope, reason, actor, float(now)))
        return int(cur.lastrowid)

    def close_flag(self, flag_id: int, now: float) -> bool:
        cur = self.db.execute(
            "UPDATE flags SET closed_ts = ? WHERE id = ? AND closed_ts IS NULL", (float(now), int(flag_id)))
        return cur.rowcount > 0
