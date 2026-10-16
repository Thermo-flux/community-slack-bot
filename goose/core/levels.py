"""Levels: crumbs only, uncapped. OWNER: agent A (ledger).

Levels 1..len(thresholds) follow config.levels.thresholds. Above the last one each level costs growth_above_top times the
previous step (round to the nearest 5). Titles come from config.levels.titles;
above the last title use "<last title> +<n>".
"""
from __future__ import annotations

import math
from typing import Any, Dict

from goose.config import Config


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


class Levels:
    def __init__(self, config: Config):
        self._thresholds = [int(t) for t in config.levels.thresholds]
        self._titles = list(config.levels.titles)
        self._growth = float(config.levels.growth_above_top)

    def threshold(self, level: int) -> int:
        """Crumbs needed to reach `level` (level 1 -> 0)."""
        level = int(level)
        if level < 1:
            raise ValueError("levels start at 1")
        known = len(self._thresholds)
        if level <= known:
            return self._thresholds[level - 1]
        # Above the table: the top threshold grows by growth_above_top per level (matches tools/level_sim.py).
        return _round5(self._thresholds[-1] * self._growth ** (level - known))

    def level_for(self, crumbs: float) -> int:
        """Highest level whose threshold is <= crumbs."""
        value = float(crumbs)
        if not math.isfinite(value):
            raise ValueError("crumbs must be a finite number")
        level = 1
        while self.threshold(level + 1) <= value:
            level += 1
        return level

    def title(self, level: int) -> str:
        lv = max(1, int(level))
        if lv <= len(self._titles):
            return self._titles[lv - 1]
        return "%s +%d" % (self._titles[-1], lv - len(self._titles))

    def progress(self, crumbs: float) -> Dict[str, Any]:
        """{"level", "title", "next_level", "next_title", "next_threshold", "to_next", "percent"}"""
        value = float(crumbs)
        level = self.level_for(value)
        current = self.threshold(level)
        next_level = level + 1
        next_threshold = self.threshold(next_level)
        span = next_threshold - current
        percent = (value - current) / span * 100.0
        percent = min(100.0, max(0.0, percent))
        return {
            "level": level,
            "title": self.title(level),
            "next_level": next_level,
            "next_title": self.title(next_level),
            "next_threshold": next_threshold,
            "to_next": round(max(0.0, next_threshold - value), 1),
            "percent": round(percent, 1),
        }
