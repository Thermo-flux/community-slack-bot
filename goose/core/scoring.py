"""Pure scoring formulas. OWNER: agent A (ledger).

Weekly crumbs from facts (see docs/PLAN.md section 4):
  chat    = 8 * ln(1 + n_msgs / 5) + 1.5 * min(active_days, 7)        (curve "log")
            linear curve: 0.8 * n_msgs + 1.5 * min(active_days, 7)
  thanks  = 6 * ln(1 + n_thanks / 3)
  helpful = 14 * ln(1 + n_helpful / 3)
  stars   = sum of star points
Nothing has a cap. Each extra unit counts a little less.
"""
from __future__ import annotations

import math

from goose.types import Breakdown

MAX_ACTIVE_DAYS = 7


def _count(n: int) -> int:
    """Counts never go negative."""
    return max(0, int(n))


def chat_score(n_msgs: int, active_days: int, curve: str = "log") -> float:
    n = _count(n_msgs)
    days = min(_count(active_days), MAX_ACTIVE_DAYS)
    if curve == "log":
        volume = 8.0 * math.log(1.0 + n / 5.0)
    elif curve == "linear":
        volume = 0.8 * n
    else:
        raise ValueError("unknown message curve %r" % (curve,))
    return volume + 1.5 * days


def thanks_score(n_thanks: int) -> float:
    return 6.0 * math.log(1.0 + _count(n_thanks) / 3.0)


def helpful_score(n_helpful: int) -> float:
    return 14.0 * math.log(1.0 + _count(n_helpful) / 3.0)


def score_week(n_msgs: int, active_days: int, n_thanks: int, n_helpful: int,
               star_points: float, star_count: int = 0, curve: str = "log") -> Breakdown:
    """Combine the pieces into a Breakdown (total = chat + thanks + helpful + stars)."""
    chat = chat_score(n_msgs, active_days, curve)
    thanks = thanks_score(n_thanks)
    helpful = helpful_score(n_helpful)
    stars = float(star_points)
    return Breakdown(
        chat=chat,
        thanks=thanks,
        helpful=helpful,
        stars=stars,
        total=chat + thanks + helpful + stars,
        n_msgs=_count(n_msgs),
        active_days=_count(active_days),
        n_thanks=_count(n_thanks),
        n_helpful=_count(n_helpful),
        star_count=_count(star_count),
    )
