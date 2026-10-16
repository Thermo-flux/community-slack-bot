"""Time helpers. Weeks run from Monday 00:00 (local, half-open) to the next Monday 00:00."""
from __future__ import annotations

import datetime as dt
import time
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _tz(name: str):
    if name.upper() in ("UTC", "Z", ""):
        return dt.timezone.utc
    if ZoneInfo is None:
        raise ValueError("zoneinfo is not available, use UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError("unknown timezone %r (is the tzdata package installed?)" % name)


class Clock:
    def __init__(self, tz_name: str = "UTC", now_fn: Optional[Callable[[], float]] = None):
        self.tz_name = tz_name
        self.tz = _tz(tz_name)
        self._now = now_fn or time.time

    def now(self) -> float:
        return float(self._now())

    def local(self, ts: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts, self.tz)

    def _midnight(self, d: dt.date) -> dt.datetime:
        return dt.datetime(d.year, d.month, d.day, tzinfo=self.tz)

    def week_start(self, ts: float) -> float:
        d = self.local(ts).date()
        monday = d - dt.timedelta(days=d.weekday())
        return self._midnight(monday).timestamp()

    def week_end(self, ts: float) -> float:
        d = self.local(ts).date()
        monday = d - dt.timedelta(days=d.weekday())
        return self._midnight(monday + dt.timedelta(days=7)).timestamp()

    def week_id(self, ts: float) -> str:
        d = self.local(ts).date()
        return (d - dt.timedelta(days=d.weekday())).isoformat()

    def week_start_from_id(self, week_id: str) -> float:
        return self._midnight(dt.date.fromisoformat(week_id)).timestamp()

    def week_end_from_id(self, week_id: str) -> float:
        return self._midnight(dt.date.fromisoformat(week_id) + dt.timedelta(days=7)).timestamp()

    def previous_week_id(self, week_id: str) -> str:
        return (dt.date.fromisoformat(week_id) - dt.timedelta(days=7)).isoformat()

    def next_week_id(self, week_id: str) -> str:
        return (dt.date.fromisoformat(week_id) + dt.timedelta(days=7)).isoformat()

    def day_key(self, ts: float) -> str:
        return self.local(ts).date().isoformat()

    def hour_of_day(self, ts: float) -> int:
        return self.local(ts).hour

    def weekday(self, ts: float) -> int:
        """Monday is 0."""
        return self.local(ts).weekday()

    def date_to_ts(self, iso_date: str) -> float:
        return self._midnight(dt.date.fromisoformat(iso_date)).timestamp()


class FakeClock(Clock):
    """A clock tests can set and advance."""

    def __init__(self, start: float = 1_760_000_000.0, tz_name: str = "UTC"):
        self._t = float(start)
        super().__init__(tz_name, now_fn=lambda: self._t)

    def set(self, ts: float) -> None:
        self._t = float(ts)

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)
