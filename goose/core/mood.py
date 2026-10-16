"""The goose meter. OWNER: agent B (vibes). Behavior: docs/PLAN.md section 3 and docs/APPENDIX_VOICE.md section B.

Deterministic. Heat is per channel set (one meter), the sum of each person's contribution in a 30 minute window,
halving every 10 minutes (config.mood). It never touches scores, flags, quiet, rest, freeze or any command, and never
stores message text.

Caller duties (not checked here): call register() only for a valid goose call (the duck is addressed, the word
'goose' stands alone, no other person is named, the sender is human). Bot messages, including the duck's own lines,
must never be registered. The caller also picks the text: MoodResult says whether to post a line, which stage it
is, and whether it is the honk slip. The line itself comes from LineBank.canned_for_stage(stage).

Storage: one row per call in mood_events (amount 0 for calls that add nothing, which the gag counts), and the stored
heat, cooldowns and gag end times in mood_state. Only these two tables are touched.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database

_HISTORY_S = 3600          # call rows older than this are dropped (longer than any window used here)


@dataclass
class MoodResult:
    stage: str = "calm"             # calm | annoyed | angry | timeout | cooling | silent
    heat: float = 0.0
    reply: bool = False             # post a text line (else maybe only the :duck: reaction)
    react: bool = True              # add the :duck: reaction
    honk: bool = False              # the angry-stage slip
    counted: bool = False           # this call added heat
    gag: bool = False               # this person is in the quiet gag


class Mood:
    def __init__(self, db: Database, config: Config, clock: Clock, rng: random.Random):
        self.db = db
        self.config = config
        self.cfg = config.mood
        self.clock = clock
        self.rng = rng

    def register(self, user_id: str, now: float) -> MoodResult:
        """Register one valid goose call (the caller already checked: duck addressed, bare 'goose', no other person
        named, human sender). Applies per-person add (first in 10 minutes +1, repeats +0.5, cap 3.0 per 30 minutes),
        the quiet gag (3 calls in 5 minutes -> 30 minutes of no heat and no reply for that person), decay, stage
        thresholds (annoyed 3, angry 6, timeout 10), anger line gap (120 s), honk chance and gap, timeout
        (once per 30 minutes, at most once a day, then silent on goose calls for 10 minutes, no new heat),
        and cooling (one line per 5 minutes; ends when heat < 3 or 30 minutes after the timeout started, heat -> 0)."""
        with self.db.tx():
            return self._register(user_id, float(now))

    def heat(self, now: float) -> float:
        value = self._opt_f("heat_value") or 0.0
        since = self._opt_f("heat_ts")
        if since is None or now <= since:
            return value
        return value * 0.5 ** ((now - since) / float(self.cfg.half_life_s))

    def stage(self, now: float) -> str:
        h = self.heat(now)
        if h >= self.cfg.timeout_at:
            return "timeout"
        if h >= self.cfg.angry_at:
            return "angry"
        if h >= self.cfg.annoyed_at:
            return "annoyed"
        return "calm"

    def reset(self) -> None:
        """Forget the meter (heat, cooldowns, gag, call history). Touches only the mood tables."""
        with self.db.tx():
            self.db.execute("DELETE FROM mood_events")
            self.db.execute("DELETE FROM mood_state")

    # ---- the register flow -------------------------------------------------------------------------------------

    def _register(self, user: str, now: float) -> MoodResult:
        cfg = self.cfg
        self.db.execute("DELETE FROM mood_events WHERE ts < ?", (now - _HISTORY_S,))

        if now < (self._opt_f("gag:" + user) or 0.0):
            self._log_call(user, now, 0.0)
            return MoodResult(stage="silent", heat=self.heat(now), reply=False, react=False, gag=True)

        silent_until = self._opt_f("timeout_silent_until")
        if silent_until is not None and now < silent_until:
            self._log_call(user, now, 0.0)
            return MoodResult(stage="silent", heat=self.heat(now), reply=False, react=False)

        cooling_from = self._opt_f("cooling_from")
        if cooling_from is not None:
            if now - cooling_from >= cfg.cooling_max_s:
                self._store_heat(0.0, now)
                self._set("cooling_from", None)
            elif self.heat(now) < cfg.annoyed_at:
                self._set("cooling_from", None)
                cooling_from = None

        # The call that completes gag_calls within gag_window_s starts the quiet gag and adds nothing (no heat, no reply).
        if self._calls_since(user, now - cfg.gag_window_s) >= cfg.gag_calls - 1:
            self._set("gag:" + user, now + cfg.gag_duration_s)
            self._log_call(user, now, 0.0)
            return MoodResult(stage="silent", heat=self.heat(now), reply=False, react=False, gag=True)

        add = self._person_add(user, now)
        heat = self.heat(now) + add
        self._store_heat(heat, now)
        self._log_call(user, now, add)

        counted = add > 0
        if heat >= cfg.timeout_at:
            if self._timeout_allowed(now):
                self._set("timeout_silent_until", now + cfg.timeout_silent_s)
                self._set("last_timeout", now)
                self._bump_timeout_day(now)
                self._set("cooling_from", now)
                self._set("cooling_last_line", None)
                return MoodResult(stage="timeout", heat=heat, reply=True, react=True, counted=counted)
            return self._anger("angry", heat, counted, now)

        if cooling_from is not None and heat >= cfg.annoyed_at:
            last = self._opt_f("cooling_last_line")
            reply = last is None or now - last >= cfg.cooling_gap_s
            if reply:
                self._set("cooling_last_line", now)
            return MoodResult(stage="cooling", heat=heat, reply=reply, react=True, counted=counted)

        if heat >= cfg.angry_at:
            return self._anger("angry", heat, counted, now)
        if heat >= cfg.annoyed_at:
            return self._anger("annoyed", heat, counted, now)
        reply = self.rng.random() < cfg.calm_line_chance
        return MoodResult(stage="calm", heat=heat, reply=reply, react=True, counted=counted)

    def _anger(self, stage: str, heat: float, counted: bool, now: float) -> MoodResult:
        cfg = self.cfg
        last_line = self._opt_f("last_anger_line")
        reply = last_line is None or now - last_line >= cfg.anger_line_gap_s
        if not reply:
            return MoodResult(stage=stage, heat=heat, reply=False, react=True, counted=counted)
        self._set("last_anger_line", now)
        honk = False
        if stage == "angry":
            last_honk = self._opt_f("last_honk")
            if (last_honk is None or now - last_honk >= cfg.honk_gap_s) and self.rng.random() < cfg.honk_chance:
                honk = True
                self._set("last_honk", now)
        return MoodResult(stage=stage, heat=heat, reply=True, react=True, honk=honk, counted=counted)

    def _person_add(self, user: str, now: float) -> float:
        cfg = self.cfg
        last_add = self._last_positive_add(user)
        base = cfg.first_add if (last_add is None or now - last_add >= cfg.person_gap_s) else cfg.repeat_add
        window_sum = self._window_sum(user, now - cfg.window_s)
        room = max(0.0, cfg.person_cap - window_sum)
        return float(min(base, room))

    def _timeout_allowed(self, now: float) -> bool:
        last = self._opt_f("last_timeout")
        if last is not None and now - last < self.cfg.timeout_gap_s:
            return False
        day = self.clock.day_key(now)
        stored_day = self._get("timeout_day") or ""
        count = int(self._opt_f("timeout_day_count") or 0) if stored_day == day else 0
        return count < self.cfg.timeout_per_day

    def _bump_timeout_day(self, now: float) -> None:
        day = self.clock.day_key(now)
        stored_day = self._get("timeout_day") or ""
        count = int(self._opt_f("timeout_day_count") or 0) if stored_day == day else 0
        self._set("timeout_day", day)
        self._set("timeout_day_count", count + 1)

    # ---- storage helpers -----------------------------------------------------------------------------------

    def _get(self, key: str) -> Optional[str]:
        row = self.db.one("SELECT value FROM mood_state WHERE key=?", (key,))
        return row["value"] if row else None

    def _opt_f(self, key: str) -> Optional[float]:
        value = self._get(key)
        if value is None or value == "":
            return None
        return float(value)

    def _set(self, key: str, value) -> None:
        if value is None:
            self.db.execute("DELETE FROM mood_state WHERE key=?", (key,))
            return
        self.db.execute(
            "INSERT INTO mood_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, repr(float(value)) if isinstance(value, float) else str(value)))

    def _store_heat(self, value: float, now: float) -> None:
        self._set("heat_value", float(value))
        self._set("heat_ts", float(now))

    def _log_call(self, user: str, now: float, amount: float) -> None:
        self.db.execute("INSERT INTO mood_events(ts,user_id,amount) VALUES(?,?,?)", (now, user, float(amount)))

    def _last_positive_add(self, user: str) -> Optional[float]:
        row = self.db.one("SELECT MAX(ts) AS t FROM mood_events WHERE user_id=? AND amount>0", (user,))
        return float(row["t"]) if row and row["t"] is not None else None

    def _window_sum(self, user: str, since: float) -> float:
        row = self.db.one("SELECT COALESCE(SUM(amount),0) AS s FROM mood_events WHERE user_id=? AND ts>=?",
                          (user, since))
        return float(row["s"])

    def _calls_since(self, user: str, since: float) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM mood_events WHERE user_id=? AND ts>=?", (user, since))
        return int(row["n"])
