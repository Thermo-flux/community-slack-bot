"""Weekly and all-time results. OWNER: agent A (ledger). Behavior: docs/PLAN.md section 6.

A week closes at Monday 00:00; the snapshot is taken at Monday 00:00 + schedule.snapshot_delay_s from final facts only.
Eligibility (simple version): joined, show on, not an organizer/owner, not disqualified for that week (or 'all'),
not frozen. Tie-breaks: more stars, then more recognizers (thanks + helpful), then earlier first fact.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.levels import Levels
from goose.core.ledger import Ledger
from goose.core.users import Users
from goose.types import Breakdown, RankRow

TOP_N = 10                 # snapshot size (the provisional top 10)
PODIUM = 3                 # winners per week and final
FINAL_MIN_STARS = 3        # lifetime stars needed for the final ranking
FINAL_MIN_DAYS = 14        # joined this many days before the end
DAY_S = 86400.0
STAR_FIELDS = ("id", "kind", "amount", "actor", "reason", "link")


def _status_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data["snapshot"] = json.loads(data["snapshot_json"] or "[]")
    data["confirmers"] = json.loads(data["confirmers_json"] or "[]")
    for key in ("organizer_posted", "reminded", "announced"):
        data[key] = bool(data[key])
    return data


def _row_dict(row: RankRow) -> Dict[str, Any]:
    return dataclasses.asdict(row)


class Weekly:
    def __init__(self, db: Database, config: Config, clock: Clock, ledger: Ledger, users: Users, levels: Levels):
        self.db = db
        self.config = config
        self.clock = clock
        self.ledger = ledger
        self.users = users
        self.levels = levels

    def _now(self, now: Optional[float]) -> float:
        return self.clock.now() if now is None else float(now)

    # ------------------------------------------------------------------ eligibility and ranking

    def _is_eligible(self, user_id: str, now: float, week_id: Optional[str]) -> bool:
        """week_id None means all-time: only 'all' disqualifications count there."""
        user = self.users.get(user_id)
        if user is None or not user["show"]:
            return False
        if self.users.is_organizer(user_id):
            return False
        if self.users.is_frozen(user_id, now):
            return False
        scopes = ("all",) if week_id is None else (week_id, "all")
        for flag in self.users.open_flags(user_id):
            if flag["kind"] == "disqualify" and flag["scope"] in scopes:
                return False
        return True

    def eligible(self, user_id: str, week_id: str, now: float) -> bool:
        return self._is_eligible(user_id, float(now), week_id)

    def _row(self, user_id: str, breakdown: Breakdown, week_id: Optional[str], include_pending: bool) -> RankRow:
        user = self.users.get(user_id) or {}
        stars = [{k: s[k] for k in STAR_FIELDS}
                 for s in self.ledger.stars_for(user_id, week_id, include_pending=include_pending)]
        return RankRow(
            user_id=user_id,
            total=breakdown.total,
            breakdown=breakdown,
            stars=stars,
            recognizers=self.ledger.recognizers(user_id, week_id, include_pending=include_pending),
            joined_ts=float(user.get("joined_ts", 0.0)),
            flags=[f["kind"] for f in self.users.open_flags(user_id)],
        )

    def _rank(self, breakdowns: Dict[str, Breakdown], week_id: Optional[str], include_pending: bool,
              now: float, limit: Optional[int]) -> List[RankRow]:
        keyed: List[Tuple[Tuple[Any, ...], RankRow]] = []
        for user_id, breakdown in breakdowns.items():
            if breakdown.total <= 0 or not self._is_eligible(user_id, now, week_id):
                continue
            row = self._row(user_id, breakdown, week_id, include_pending)
            first = self.ledger.first_fact_ts(user_id, week_id, include_pending=include_pending)
            key = (-round(row.total, 6), -breakdown.star_count, -row.recognizers, first, user_id)
            keyed.append((key, row))
        keyed.sort(key=lambda item: item[0])
        rows = [row for _, row in keyed]
        return rows if limit is None else rows[:limit]

    def ranking(self, week_id: str, limit: int = 10, include_pending: bool = False,
                now: Optional[float] = None) -> List[RankRow]:
        """Eligible people for that week, sorted. Only people with total > 0."""
        when = self._now(now)
        breakdowns = self.ledger.week_breakdowns(week_id, include_pending=include_pending)
        return self._rank(breakdowns, week_id, include_pending, when, limit)

    # ------------------------------------------------------------------ closing and snapshots

    def closable_weeks(self, now: float) -> List[str]:
        """Week ids that ended at least snapshot_delay_s ago, have facts or members, and have no week_status row."""
        when = float(now)
        delay = float(self.config.schedule.snapshot_delay_s)
        candidates = set(self.ledger.weeks_with_facts())
        for member in self.users.roster():
            candidates.add(self.clock.week_id(float(member["joined_ts"])))
        done = {r["week_id"] for r in self.db.query("SELECT week_id FROM week_status")}
        out = []
        for week_id in sorted(candidates):
            if week_id in done:
                continue
            if self.clock.week_end_from_id(week_id) + delay <= when:
                out.append(week_id)
        return out

    def close_week(self, week_id: str, now: float) -> Dict[str, Any]:
        """Finalize pending facts of that week, store the top-10 snapshot (JSON of RankRow dicts) and set state
        'provisional'. Idempotent: returns the existing status if already closed. Returns the status row as a dict."""
        when = float(now)
        existing = self.status(week_id)
        if existing is not None:
            return existing
        with self.db.tx():
            self.ledger.finalize_week(week_id)
            rows = self.ranking(week_id, limit=TOP_N, include_pending=False, now=when)
            snapshot = [_row_dict(r) for r in rows]
            self.db.execute(
                "INSERT INTO week_status (week_id, state, snapshot_json, closed_ts, version) VALUES (?,?,?,?,1)",
                (week_id, "provisional", json.dumps(snapshot), when))
        return self.status(week_id)  # type: ignore[return-value]

    def status(self, week_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM week_status WHERE week_id = ?", (week_id,))
        return _status_dict(row) if row is not None else None

    def snapshot(self, week_id: str) -> List[Dict[str, Any]]:
        """The stored (possibly re-ranked) snapshot rows as dicts, best first."""
        status = self.status(week_id)
        return status["snapshot"] if status is not None else []

    def _write_week_results(self, week_id: str, rows: Sequence[Dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM results WHERE scope = 'week' AND week_id = ?", (week_id,))
        for rank, row in enumerate(rows[:PODIUM], start=1):
            self.db.execute(
                "INSERT INTO results (scope, week_id, rank, user_id, total, label) VALUES ('week', ?, ?, ?, ?, '')",
                (week_id, rank, row["user_id"], float(row["total"])))

    @staticmethod
    def _top_ids(rows: Sequence[Dict[str, Any]]) -> List[str]:
        return [r["user_id"] for r in rows[:PODIUM]]

    # ------------------------------------------------------------------ confirmations

    def confirm_week(self, week_id: str, confirmer: str, now: float) -> Tuple[str, Dict[str, Any]]:
        """Records the confirmer. Returns ("confirmed", info) once config.people.confirmations_required distinct
        confirmers are in (writes results rows for the top 3, sets confirmed_ts, version+1), ("need_more", info) if
        more are needed, ("already", info) if already confirmed, ("error", {"reason": ...}) otherwise (unknown week,
        skipped week, confirmer already counted)."""
        status = self.status(week_id)
        if status is None:
            return "error", {"reason": "unknown_week"}
        if status["state"] == "skipped":
            return "error", {"reason": "skipped"}
        if status["state"] == "confirmed":
            return "already", self._confirmed_info(status)
        if not self.users.is_organizer(confirmer):
            return "error", {"reason": "not_organizer"}
        confirmers = list(status["confirmers"])
        if confirmer in confirmers:
            return "error", {"reason": "already_confirmed"}
        confirmers.append(confirmer)
        need = int(self.config.people.confirmations_required)
        if len(confirmers) < need:
            self.db.execute("UPDATE week_status SET confirmers_json = ? WHERE week_id = ?",
                            (json.dumps(confirmers), week_id))
            return "need_more", {"week_id": week_id, "have": len(confirmers), "need": need}
        with self.db.tx():
            self._write_week_results(week_id, status["snapshot"])
            self.db.execute(
                "UPDATE week_status SET state = 'confirmed', confirmers_json = ?, confirmed_ts = ?, "
                "version = version + 1 WHERE week_id = ?",
                (json.dumps(confirmers), float(now), week_id))
        return "confirmed", self._confirmed_info(self.status(week_id))  # type: ignore[arg-type]

    @staticmethod
    def _confirmed_info(status: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "week_id": status["week_id"],
            "version": status["version"],
            "top": Weekly._top_ids(status["snapshot"]),
            "confirmers": list(status["confirmers"]),
        }

    def disqualify(self, scope: str, user_id: str, reason: str, actor: str, now: float) -> bool:
        """scope is a week_id or 'all'. Adds a flag, re-ranks the stored snapshot of that week (or every
        provisional/confirmed snapshot for 'all' is NOT rewritten; 'all' only affects all-time and future weeks).
        For a confirmed week the results rows are rewritten and the version increases."""
        when = float(now)
        if not str(reason or "").strip():
            return False
        if scope != "all":
            dt.date.fromisoformat(scope)  # raises ValueError for a bad week id
        if not self.users.is_joined(user_id):
            return False
        with self.db.tx():
            self.users.add_flag(user_id, "disqualify", scope, reason, actor, when)
            if scope != "all":
                status = self.status(scope)
                if status is not None and status["state"] in ("provisional", "confirmed"):
                    self._rerank_without(status, user_id, when)
        return True

    def _rerank_without(self, status: Dict[str, Any], user_id: str, now: float) -> None:
        rows = status["snapshot"]
        if not any(r["user_id"] == user_id for r in rows):
            return  # not on the stored board, so nothing to re-rank
        if len(rows) >= TOP_N:
            # The snapshot may be truncated: rebuild it from facts so the next person is pulled in.
            new_rows = [_row_dict(r) for r in self.ranking(status["week_id"], limit=TOP_N,
                                                             include_pending=False, now=now)]
        else:
            new_rows = [r for r in rows if r["user_id"] != user_id]
        self.db.execute("UPDATE week_status SET snapshot_json = ? WHERE week_id = ?",
                        (json.dumps(new_rows), status["week_id"]))
        if status["state"] == "confirmed":
            self._write_week_results(status["week_id"], new_rows)
            self.db.execute("UPDATE week_status SET version = version + 1 WHERE week_id = ?", (status["week_id"],))

    @staticmethod
    def _snapshot_differs(stored: Sequence[Dict[str, Any]], fresh: Sequence[Dict[str, Any]]) -> bool:
        """True when the stored board no longer matches a fresh ranking of the same week."""
        if len(stored) != len(fresh):
            return True
        for old, new in zip(stored, fresh):
            if old["user_id"] != new["user_id"]:
                return True
            if round(float(old["total"]), 6) != round(float(new["total"]), 6):
                return True
        return False

    def _corrections_since(self, week_id: str, closed_ts: Optional[float]) -> int:
        """Final fact rows of this week voided after it was closed (a late edit or delete correction)."""
        if closed_ts is None:
            return 0
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM facts WHERE week_id = ? AND voided_ts IS NOT NULL AND voided_ts > ?",
            (week_id, float(closed_ts)))
        return int(row["n"])

    def reconcile(self, now: float) -> Dict[str, Any]:
        """Rebuild provisional snapshots from final facts after late corrections (PLAN_V6 4.5, 10.13).

        A late edit or delete appends a correction (a void); this recomputes any not-yet-confirmed board so
        organizers review the corrected ranking, and clears its unannounced confirmers, which invalidates a
        partial confirmation (organizers must confirm the corrected snapshot). Confirmed and skipped weeks are
        never silently rewritten. Idempotent: a snapshot that already matches is left alone. Returns
        {"weeks": [changed week ids], "checked": <provisional weeks inspected>}."""
        when = float(now)
        changed: List[str] = []
        rows = self.db.query(
            "SELECT week_id FROM week_status WHERE state = 'provisional' ORDER BY week_id")
        for row in rows:
            week_id = row["week_id"]
            status = self.status(week_id)
            if status is None:
                continue
            fresh = [_row_dict(r) for r in self.ranking(week_id, limit=TOP_N, include_pending=False, now=when)]
            corrected = self._corrections_since(week_id, status["closed_ts"])
            stale = self._snapshot_differs(status["snapshot"], fresh)
            if stale or (corrected and status["confirmers"]):
                with self.db.tx():
                    self.db.execute(
                        "UPDATE week_status SET snapshot_json = ?, confirmers_json = '[]' "
                        "WHERE week_id = ? AND state = 'provisional'",
                        (json.dumps(fresh), week_id))
                changed.append(week_id)
        return {"weeks": changed, "checked": len(rows)}

    def skip_week(self, week_id: str, reason: str, actor: str, now: float) -> bool:
        """No winners for this week. Refused for a confirmed week, or without a reason. `actor` is not stored here."""
        if not str(reason or "").strip():
            return False
        status = self.status(week_id)
        if status is not None and status["state"] in ("confirmed", "skipped"):
            return False
        with self.db.tx():
            if status is None:
                self.db.execute(
                    "INSERT INTO week_status (week_id, state, snapshot_json, closed_ts, skipped_reason, version) "
                    "VALUES (?, 'skipped', '[]', ?, ?, 1)", (week_id, float(now), reason))
            else:
                self.db.execute("UPDATE week_status SET state = 'skipped', skipped_reason = ? WHERE week_id = ?",
                                (reason, week_id))
        return True

    def mark_organizer_posted(self, week_id: str) -> None:
        self.db.execute("UPDATE week_status SET organizer_posted = 1 WHERE week_id = ?", (week_id,))

    def mark_reminded(self, week_id: str) -> None:
        self.db.execute("UPDATE week_status SET reminded = 1 WHERE week_id = ?", (week_id,))

    def mark_announced(self, week_id: str) -> None:
        self.db.execute("UPDATE week_status SET announced = 1 WHERE week_id = ?", (week_id,))

    # ------------------------------------------------------------------ results

    def _results(self, scope: str, week_id: str) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT rank, user_id, total FROM results WHERE scope = ? AND week_id = ? ORDER BY rank",
            (scope, week_id))
        return [dict(r) for r in rows]

    def winners(self) -> Dict[str, Any]:
        """{"weeks": [{"week_id", "top3": [{"rank","user_id","total"}]}], "final": [...]} for confirmed results."""
        weeks = []
        for row in self.db.query("SELECT week_id FROM week_status WHERE state = 'confirmed' ORDER BY week_id"):
            weeks.append({"week_id": row["week_id"], "top3": self._results("week", row["week_id"])})
        return {"weeks": weeks, "final": self._results("final", "")}

    def alltime_ranking(self, limit: int = 10, now: Optional[float] = None) -> List[RankRow]:
        """By lifetime crumbs. Same eligibility, plus 'all' disqualifications."""
        when = self._now(now)
        breakdowns = self.ledger.lifetime_breakdowns(include_pending=False)
        return self._rank(breakdowns, None, False, when, limit)

    def final_ranking(self, now: float) -> List[RankRow]:
        """All-time ranking with the final rules: joined at least 14 days before the end, at least 3 lifetime stars."""
        when = float(now)
        end = self.end_ts()
        cutoff = (end if end is not None else when) - FINAL_MIN_DAYS * DAY_S
        eligible: Dict[str, Breakdown] = {}
        for user_id, breakdown in self.ledger.lifetime_breakdowns(include_pending=False).items():
            user = self.users.get(user_id)
            if user is None or float(user["joined_ts"]) > cutoff:
                continue
            if self.ledger.star_count(user_id) < FINAL_MIN_STARS:
                continue
            eligible[user_id] = breakdown
        return self._rank(eligible, None, False, when, None)

    def confirm_final(self, owner: str, now: float) -> Tuple[str, Dict[str, Any]]:
        """Owner only. Writes results rows scope='final' (top 3)."""
        if not self.users.is_owner(owner):
            return "error", {"reason": "not_owner"}
        existing = self._results("final", "")
        if existing:
            return "already", {"top": existing}
        ranked = self.final_ranking(now)[:PODIUM]
        if not ranked:
            return "error", {"reason": "no_candidates"}
        with self.db.tx():
            for rank, row in enumerate(ranked, start=1):
                self.db.execute(
                    "INSERT INTO results (scope, week_id, rank, user_id, total, label) VALUES ('final', '', ?, ?, ?, '')",
                    (rank, row.user_id, float(row.total)))
        return "confirmed", {"top": self._results("final", "")}

    # ------------------------------------------------------------------ program dates

    def end_ts(self) -> Optional[float]:
        """End of the program: settings 'end_date' if set, else schedule.end_date, else launch_date + program_weeks."""
        stored = self.db.get_setting("end_date", "")
        if stored:
            return self.clock.date_to_ts(stored)
        if self.config.schedule.end_date:
            return self.clock.date_to_ts(self.config.schedule.end_date)
        if self.config.schedule.launch_date:
            launch = self.clock.date_to_ts(self.config.schedule.launch_date)
            return launch + self.config.schedule.program_weeks * 7 * DAY_S
        return None

    def set_end(self, iso_date: str, actor: str) -> None:
        dt.date.fromisoformat(iso_date)  # raises ValueError for a bad date
        self.db.set_setting("end_date", iso_date)
        self.db.set_setting("end_date_actor", actor)

    def last_confirmed(self) -> Optional[Dict[str, Any]]:
        """{"week_id", "version", "top": [user_id, ...3], "rows": snapshot} of the latest confirmed week, else None."""
        row = self.db.one(
            "SELECT * FROM week_status WHERE state = 'confirmed' ORDER BY week_id DESC LIMIT 1")
        if row is None:
            return None
        status = _status_dict(row)
        return {
            "week_id": status["week_id"],
            "version": status["version"],
            "top": self._top_ids(status["snapshot"]),
            "rows": status["snapshot"],
        }
