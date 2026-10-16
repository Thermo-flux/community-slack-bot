"""Time-driven work. OWNER: agent C2b (ops-wiring). Behavior: docs/PLAN.md sections 6 and 10.10.

tick(now) is idempotent and safe to call every 30 seconds, and after a restart it catches up on whatever it missed.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Callable, Set

from goose.core.commands_organizer import format_rows
from goose.core.context import AppContext
from goose.core.router import Router
from goose.types import Action, Result

log = logging.getLogger("goose.scheduler")

ALLTIME_KEY = "alltime_posted:%s"
FINAL_KEY = "final_posted"
MAINTENANCE_KEY = "maintenance_at"
MAINTENANCE_EVERY_S = 3600
PROCESSED_TTL_S = 2 * 86400


class Scheduler:
    def __init__(self, ctx: AppContext, router: Router):
        self.ctx = ctx
        self.router = router
        self._lock = threading.Lock()
        self._pending_notes: Set[str] = set()

    # ------------------------------------------------------------------------------------------------ helpers

    def _paused(self) -> bool:
        return self.ctx.db.get_setting("paused", "0") == "1"

    def _organizer_alert(self, text: str) -> Action:
        return Action(kind="organizer_post", channel=self.ctx.organizer_channel, text=text)

    def _post_closed_weeks(self, now: float) -> Result:
        """Close every week that is due (catch-up after downtime), then post the organizer message for every
        provisional week that has not been posted yet. Idempotent through week_status."""
        out = Result()
        for week_id in sorted(self.ctx.weekly.closable_weeks(now)):
            self.ctx.weekly.close_week(week_id, now)
        rows = self.ctx.db.query(
            "SELECT week_id FROM week_status WHERE state='provisional' AND organizer_posted=0 ORDER BY week_id")
        for row in rows:
            week_id = row["week_id"]
            ranking = self.ctx.weekly.snapshot(week_id)[:10]
            body = format_rows(ranking, 10)
            out.actions.append(self._organizer_alert(
                "Week %s is closed. Provisional top 10 (organizers only):\n%s\n"
                "Next: /goose confirm-week %s, /goose disqualify @person %s <reason>, or /goose skip-week <reason>."
                % (week_id, body, week_id, week_id)))
            self.ctx.weekly.mark_organizer_posted(week_id)
        return out

    def _deadline(self, week_id: str) -> float:
        """The Tuesday after week_id ends (its Monday plus 8 days), at schedule.confirm_deadline_hour, local time."""
        tuesday = dt.date.fromisoformat(week_id) + dt.timedelta(days=8)
        return self.ctx.clock.date_to_ts(tuesday.isoformat()) + self.ctx.config.schedule.confirm_deadline_hour * 3600

    def _reminders(self, now: float) -> Result:
        out = Result()
        rows = self.ctx.db.query("SELECT week_id FROM week_status WHERE state='provisional' AND reminded=0 "
                                 "ORDER BY week_id")
        for row in rows:
            week_id = row["week_id"]
            if now < self._deadline(week_id):
                continue
            out.actions.append(self._organizer_alert(
                "Reminder: week %s is not confirmed yet. Confirm with /goose confirm-week %s, or /goose skip-week."
                % (week_id, week_id)))
            self.ctx.weekly.mark_reminded(week_id)
        return out

    def _alltime(self, now: float) -> Result:
        """On the first Monday of each month (after the snapshot time), the all-time top 10, once per month."""
        local_day = self.ctx.clock.local(now).date()
        first = dt.date(local_day.year, local_day.month, 1)
        first = first + dt.timedelta(days=(7 - first.weekday()) % 7)
        trigger = self.ctx.clock.date_to_ts(first.isoformat()) + self.ctx.config.schedule.snapshot_delay_s
        if now < trigger:
            return Result()
        key = ALLTIME_KEY % local_day.strftime("%Y-%m")
        if self.ctx.db.get_setting(key, ""):
            return Result()
        rows = self.ctx.weekly.alltime_ranking(limit=10, now=now)
        self.ctx.db.set_setting(key, "1")
        if not rows:
            return Result()
        return Result.of(self._organizer_alert(
            "All-time top 10 for %s (organizers only):\n%s" % (local_day.strftime("%Y-%m"), format_rows(rows, 10))))

    def _final(self, now: float) -> Result:
        end = self.ctx.weekly.end_ts()
        if end is None or now < end or self.ctx.db.get_setting(FINAL_KEY, ""):
            return Result()
        self.ctx.db.set_setting(FINAL_KEY, "1")
        rows = self.ctx.weekly.final_ranking(now)[:3]
        return Result.of(self._organizer_alert(
            "The program has reached its end date. Provisional final top 3 (organizers only):\n%s\n"
            "The owner confirms with /goose confirm-final." % format_rows(rows, 3)))

    def _maintenance(self, now: float) -> None:
        last = self.ctx.db.get_setting(MAINTENANCE_KEY, "")
        try:
            last_ts = float(last) if last else 0.0
        except ValueError:
            last_ts = 0.0
        if now - last_ts < MAINTENANCE_EVERY_S:
            return
        self.ctx.db.set_setting(MAINTENANCE_KEY, "%.3f" % now)
        self.ctx.explain.purge(now)
        self.ctx.db.execute("DELETE FROM processed WHERE ts < ?", (now - PROCESSED_TTL_S,))

    # ---------------------------------------------------------------------------------------------- public

    def tick(self, now: float) -> Result:
        """1. Ledger.finalize_due(now) and Router.level_up_notes for the people affected.
        2. For each Weekly.closable_weeks(now): close_week, and post the organizer message (top 10 with breakdowns, next
           steps: confirm-week / disqualify / skip-week) to ctx.organizer_channel; mark organizer_posted.
        3. Tuesday reminder: provisional weeks not confirmed after Tuesday `schedule.confirm_deadline_hour` get one
           reminder in the organizer channel; mark_reminded. Never announces on its own.
        4. First Monday of a month: the all-time top 10 to the organizer channel (once, settings key 'alltime_posted:<YYYY-MM>').
        5. At the end date (Weekly.end_ts) once: the provisional final top 3 to the organizer channel (settings 'final_posted').
        6. ExplainService.purge and processed-table pruning (older than 2 days) once an hour.
        Does nothing at all while settings 'paused' == '1' except finalize_due."""
        with self._lock:
            return self._tick(now)

    def _tick(self, now: float) -> Result:
        result = Result()
        changed = self.ctx.ledger.finalize_due(now)
        self._pending_notes.update(changed)
        if self._paused():
            return result
        if self._pending_notes:
            ids = sorted(self._pending_notes)
            self._pending_notes.clear()
            result.extend(self.router.level_up_notes(ids))
        result.extend(self._post_closed_weeks(now))
        result.extend(self._reminders(now))
        result.extend(self._alltime(now))
        result.extend(self._final(now))
        self._maintenance(now)
        return result

    def run(self, stop: threading.Event, deliver: Callable[[Result], None]) -> None:
        """Loop: every schedule.tick_seconds call tick(clock.now()) and hand the Result to `deliver`; exceptions are
        logged (no secrets) and the loop continues; exits when `stop` is set."""
        interval = max(1, int(self.ctx.config.schedule.tick_seconds))
        log.info("scheduler started, tick every %ss", interval)
        while not stop.is_set():
            try:
                result = self.tick(self.ctx.clock.now())
                if result.actions or result.jobs:
                    deliver(result)
            except Exception as exc:  # the loop must survive any single failure
                log.error("scheduler tick failed: %s", type(exc).__name__)
            stop.wait(interval)
        log.info("scheduler stopped")
