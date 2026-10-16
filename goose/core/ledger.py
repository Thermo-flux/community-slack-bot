"""The facts ledger. OWNER: agent A (ledger).

Facts are append-only rows (table `facts`). Scoring is a pure function over a week's facts (scoring.score_week).
A new fact is "pending" until `hold_seconds` have passed, then "final". Voids are state changes with actor and reason;
voided rows never count, and never take a place on the curve. A repeated idem_key is ignored (Slack redelivery).

Weeks use Clock.week_id(ts) (the Monday date, local time, half-open).
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from goose.config import Config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.scoring import score_week
from goose.types import FACT_KINDS, Breakdown

STAR_KINDS = ("star", "bigstar")
COUNTED_STATES = ("final",)
PENDING_STATES = ("final", "pending")          # every state that is not voided


def tombstone(user_id: str) -> str:
    """A stable stand-in for a deleted person: 'deleted-' + first 6 hex of sha256(user_id)."""
    return "deleted-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:6]


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _states(include_pending: bool) -> Tuple[str, ...]:
    return PENDING_STATES if include_pending else COUNTED_STATES


def _sum_breakdowns(items: Iterable[Breakdown]) -> Breakdown:
    total = Breakdown()
    for b in items:
        total.chat += b.chat
        total.thanks += b.thanks
        total.helpful += b.helpful
        total.stars += b.stars
        total.total += b.total
        total.n_msgs += b.n_msgs
        total.active_days += b.active_days
        total.n_thanks += b.n_thanks
        total.n_helpful += b.n_helpful
        total.star_count += b.star_count
    return total


class Ledger:
    def __init__(self, db: Database, config: Config, clock: Clock):
        self.db = db
        self.config = config
        self.clock = clock

    # ------------------------------------------------------------------ helpers

    def _now(self, now: Optional[float]) -> float:
        return self.clock.now() if now is None else float(now)

    def _select(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.query(sql, params)]

    def _counted(self, include_pending: bool, week_id: Optional[str] = None,
                 user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        states = _states(include_pending)
        sql = "SELECT * FROM facts WHERE state IN (%s)" % _marks(states)
        params: List[Any] = list(states)
        if week_id is not None:
            sql += " AND week_id = ?"
            params.append(week_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY id"
        return self._select(sql, params)

    def _breakdown_from_facts(self, facts: Sequence[Dict[str, Any]]) -> Breakdown:
        n_msgs = 0
        days: Set[str] = set()
        n_thanks = 0
        n_helpful = 0
        stars = 0.0
        star_count = 0
        for fact in facts:
            kind = fact["kind"]
            if kind == "msg":
                n_msgs += 1
                days.add(self.clock.day_key(fact["ts"]))
            elif kind == "thanks":
                n_thanks += 1
            elif kind == "helpful":
                n_helpful += 1
            elif kind in STAR_KINDS:
                stars += float(fact["amount"] or 0.0)
                star_count += 1
        return score_week(n_msgs, len(days), n_thanks, n_helpful, stars, star_count,
                          curve=self.config.scoring.message_curve)

    @staticmethod
    def _group_by_user(facts: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for fact in facts:
            groups.setdefault(fact["user_id"], []).append(fact)
        return groups

    def _breakdowns_by_user_week(self, facts: Sequence[Dict[str, Any]]) -> Dict[str, Breakdown]:
        """Sum of the per-week breakdowns for every user (lifetime)."""
        per_user: Dict[str, List[Breakdown]] = {}
        for user_id, user_facts in self._group_by_user(facts).items():
            weeks: Dict[str, List[Dict[str, Any]]] = {}
            for fact in user_facts:
                weeks.setdefault(fact["week_id"], []).append(fact)
            per_user[user_id] = [self._breakdown_from_facts(w) for w in weeks.values()]
        return {uid: _sum_breakdowns(items) for uid, items in per_user.items()}

    def _default_amount(self, kind: str) -> Optional[float]:
        if kind == "star":
            return float(self.config.scoring.star_points)
        if kind == "bigstar":
            return float(self.config.scoring.bigstar_points)
        return None

    # ------------------------------------------------------------------ writes

    def add_fact(self, *, idem_key: str, user_id: str, kind: str, ts: float, counterparty: str = "",
                 amount: Optional[float] = None, actor: str = "", reason: str = "", link: str = "",
                 channel: str = "", message_ts: str = "", state: str = "pending") -> Optional[int]:
        """Insert a fact. Returns the new id, or None when idem_key already exists. kind in types.FACT_KINDS.
        week_id is derived from ts. Stars/bigstars default amount to config.scoring.star_points / bigstar_points."""
        if kind not in FACT_KINDS:
            raise ValueError("unknown fact kind %r" % (kind,))
        if state not in ("pending", "final"):
            raise ValueError("a new fact must be pending or final, not %r" % (state,))
        if not idem_key:
            raise ValueError("idem_key is required")
        if amount is None:
            amount = self._default_amount(kind)
        week_id = self.clock.week_id(float(ts))
        with self.db.tx():
            existing = self.db.one("SELECT id FROM facts WHERE idem_key = ?", (idem_key,))
            if existing is not None:
                return None
            cur = self.db.execute(
                "INSERT INTO facts (idem_key, week_id, user_id, kind, counterparty, ts, amount, state, actor, reason, "
                "link, channel, message_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (idem_key, week_id, user_id, kind, counterparty, float(ts), amount, state, actor, reason, link,
                 channel, message_ts))
            return int(cur.lastrowid)

    def void_fact(self, *, fact_id: Optional[int] = None, idem_key: Optional[str] = None,
                  actor: str = "", reason: str = "", now: Optional[float] = None) -> bool:
        """Void one fact (not already voided). Returns True if a row changed."""
        if fact_id is None and not idem_key:
            raise ValueError("give fact_id or idem_key")
        when = self._now(now)
        if fact_id is not None:
            where, arg = "id = ?", fact_id
        else:
            where, arg = "idem_key = ?", idem_key
        with self.db.tx():
            cur = self.db.execute(
                "UPDATE facts SET state = 'voided', void_actor = ?, void_reason = ?, voided_ts = ? "
                "WHERE %s AND state != 'voided'" % where,
                (actor, reason, when, arg))
            return cur.rowcount > 0

    def finalize_due(self, now: float) -> List[str]:
        """pending -> final when ts + hold_seconds <= now. Returns the user ids whose facts changed."""
        hold = float(self.config.scoring.hold_seconds)
        when = float(now)
        with self.db.tx():
            rows = self._select(
                "SELECT DISTINCT user_id FROM facts WHERE state = 'pending' AND ts + ? <= ? ORDER BY user_id",
                (hold, when))
            if rows:
                self.db.execute(
                    "UPDATE facts SET state = 'final' WHERE state = 'pending' AND ts + ? <= ?", (hold, when))
        return [r["user_id"] for r in rows]

    def finalize_week(self, week_id: str) -> List[str]:
        """pending -> final for every pending fact of one week, regardless of hold (the week is over).
        Returns the user ids whose facts changed."""
        with self.db.tx():
            rows = self._select(
                "SELECT DISTINCT user_id FROM facts WHERE state = 'pending' AND week_id = ? ORDER BY user_id",
                (week_id,))
            if rows:
                self.db.execute("UPDATE facts SET state = 'final' WHERE state = 'pending' AND week_id = ?",
                                (week_id,))
        return [r["user_id"] for r in rows]

    def anonymize_user(self, user_id: str) -> int:
        """leave(): delete the person's own facts and replace their id in other people's facts (counterparty/actor)
        with a stable tombstone like 'deleted-ab12cd'. Returns the number of rows touched."""
        stone = tombstone(user_id)
        touched = 0
        with self.db.tx():
            touched += self.db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,)).rowcount
            for column in ("counterparty", "actor", "void_actor"):
                touched += self.db.execute(
                    "UPDATE facts SET %s = ? WHERE %s = ?" % (column, column), (stone, user_id)).rowcount
        return touched

    # ------------------------------------------------------------------ reads

    def get_fact(self, fact_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM facts WHERE id = ?", (fact_id,))
        return dict(row) if row is not None else None

    def find_by_message(self, channel: str, message_ts: str, kinds: Sequence[str] = ("msg",)) -> List[Dict[str, Any]]:
        """Non-voided facts that came from this message (used for edits and deletes)."""
        if not kinds:
            return []
        sql = ("SELECT * FROM facts WHERE channel = ? AND message_ts = ? AND kind IN (%s) "
               "AND state != 'voided' ORDER BY id" % _marks(kinds))
        return self._select(sql, [channel, message_ts, *kinds])

    def week_breakdown(self, user_id: str, week_id: str, include_pending: bool = False) -> Breakdown:
        """Crumbs for one person and week from non-voided facts (final, plus pending if asked).
        n_msgs counts msg facts, active_days counts distinct local days of msg facts (Clock.day_key),
        n_thanks counts thanks facts, n_helpful counts helpful facts, stars sum star/bigstar amounts."""
        return self._breakdown_from_facts(self._counted(include_pending, week_id=week_id, user_id=user_id))

    def week_breakdowns(self, week_id: str, include_pending: bool = False) -> Dict[str, Breakdown]:
        """Every user with counted facts in the week (final, plus pending if asked)."""
        facts = self._counted(include_pending, week_id=week_id)
        return {uid: self._breakdown_from_facts(user_facts)
                for uid, user_facts in self._group_by_user(facts).items()}

    def lifetime_breakdown(self, user_id: str, include_pending: bool = False) -> Breakdown:
        """Every week's breakdown for this person, summed."""
        facts = self._counted(include_pending, user_id=user_id)
        return self._breakdowns_by_user_week(facts).get(user_id, Breakdown())

    def lifetime_breakdowns(self, include_pending: bool = False) -> Dict[str, Breakdown]:
        return self._breakdowns_by_user_week(self._counted(include_pending))

    def lifetime(self, user_id: str, include_pending: bool = False) -> float:
        """Sum of week_breakdown totals over every week the person has facts in."""
        return self.lifetime_breakdown(user_id, include_pending).total

    def lifetime_totals(self, include_pending: bool = False) -> Dict[str, float]:
        return {uid: bd.total for uid, bd in self.lifetime_breakdowns(include_pending).items()}

    def weeks_with_facts(self, user_id: Optional[str] = None) -> List[str]:
        """Week ids that hold non-voided facts (for one person, or for everyone)."""
        sql = "SELECT DISTINCT week_id FROM facts WHERE state != 'voided'"
        params: List[Any] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY week_id"
        return [r["week_id"] for r in self.db.query(sql, params)]

    def stars_for(self, user_id: str, week_id: Optional[str] = None,
                  include_pending: bool = True) -> List[Dict[str, Any]]:
        """Non-voided star/bigstar facts: {id, kind, amount, actor, reason, link, ts, week_id}.
        include_pending=False keeps only final stars (the same rule as the weekly breakdown)."""
        states = _states(include_pending)
        sql = ("SELECT id, kind, amount, actor, reason, link, ts, week_id FROM facts WHERE user_id = ? "
               "AND kind IN (%s) AND state IN (%s)" % (_marks(STAR_KINDS), _marks(states)))
        params: List[Any] = [user_id, *STAR_KINDS, *states]
        if week_id is not None:
            sql += " AND week_id = ?"
            params.append(week_id)
        sql += " ORDER BY ts, id"
        rows = self._select(sql, params)
        for row in rows:
            row["amount"] = float(row["amount"] or 0.0)
        return rows

    def star_count(self, user_id: str) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND kind IN (%s) AND state != 'voided'"
            % _marks(STAR_KINDS), [user_id, *STAR_KINDS])
        return int(row["n"])

    def recognizers(self, user_id: str, week_id: Optional[str] = None, include_pending: bool = True) -> int:
        """Distinct counterparties of non-voided thanks/helpful facts (one week, or all weeks when week_id is None)."""
        states = _states(include_pending)
        sql = ("SELECT COUNT(DISTINCT counterparty) AS n FROM facts WHERE user_id = ? AND kind IN ('thanks', 'helpful') "
               "AND counterparty != '' AND state IN (%s)" % _marks(states))
        params: List[Any] = [user_id, *states]
        if week_id is not None:
            sql += " AND week_id = ?"
            params.append(week_id)
        return int(self.db.one(sql, params)["n"])

    def first_fact_ts(self, user_id: str, week_id: Optional[str] = None, include_pending: bool = True) -> float:
        """Earliest counted fact of the person (one week, or all weeks). 0.0 when there is none."""
        states = _states(include_pending)
        sql = "SELECT MIN(ts) AS t FROM facts WHERE user_id = ? AND state IN (%s)" % _marks(states)
        params: List[Any] = [user_id, *states]
        if week_id is not None:
            sql += " AND week_id = ?"
            params.append(week_id)
        value = self.db.one(sql, params)["t"]
        return float(value) if value is not None else 0.0

    def message_days(self, user_id: str, include_pending: bool = True) -> Set[str]:
        """Local day keys (YYYY-MM-DD) on which the person has a non-voided qualified message."""
        facts = self._counted(include_pending, user_id=user_id)
        return {self.clock.day_key(f["ts"]) for f in facts if f["kind"] == "msg"}

    def all_facts(self, week_id: Optional[str] = None, kinds: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """For export. Includes voided rows with their state."""
        sql = "SELECT * FROM facts WHERE 1 = 1"
        params: List[Any] = []
        if week_id is not None:
            sql += " AND week_id = ?"
            params.append(week_id)
        if kinds:
            sql += " AND kind IN (%s)" % _marks(kinds)
            params.extend(kinds)
        sql += " ORDER BY ts, id"
        return self._select(sql, params)

    def last_channel(self, user_id: str) -> str:
        row = self.db.one(
            "SELECT channel FROM facts WHERE user_id = ? AND state != 'voided' AND channel != '' "
            "ORDER BY ts DESC, id DESC LIMIT 1", (user_id,))
        return row["channel"] if row is not None else ""
