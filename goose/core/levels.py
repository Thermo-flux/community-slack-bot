"""Levels: crumbs only, uncapped after the v6 organizer-star gate. OWNER: agent A (ledger).

Levels 1..len(thresholds) follow config.levels.thresholds. Above the last one each level costs growth_above_top times
the previous step (round to the nearest 5). Titles come from config.levels.titles;
above the last title use "<last title> +<n>".

v6 organizer-star gate (PLAN_V6 section 5): reaching a level above `gate_level` needs at least one non-voided
organizer star. Without one a person is capped at `gate_level`, however many crumbs they have; levels 1..gate_level
are unchanged. After a star, levels are uncapped. The gate reads config.levels.gate_enabled and
config.levels.gate_level and fails closed: a bad value raises, so a typo can never quietly unlock the gate.
"""
from __future__ import annotations

import math
from typing import Any, Dict

from goose.config import Config

DEFAULT_GATE_LEVEL = 5


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def _read_gate_enabled(config: Config) -> bool:
    """config.levels.gate_enabled, defaulting to on. Anything that is not a real bool fails closed."""
    value = getattr(config.levels, "gate_enabled", True)
    if not isinstance(value, bool):
        raise ValueError("levels.gate_enabled must be true or false")
    return value


def _read_gate_level(config: Config) -> int:
    """config.levels.gate_level, defaulting to 5. Anything that is not a whole number of at least 1 fails closed."""
    value = getattr(config.levels, "gate_level", DEFAULT_GATE_LEVEL)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("levels.gate_level must be a whole number of at least 1")
    return value


class Levels:
    def __init__(self, config: Config):
        self._thresholds = [int(t) for t in config.levels.thresholds]
        self._titles = list(config.levels.titles)
        self._growth = float(config.levels.growth_above_top)
        self.gate_enabled = _read_gate_enabled(config)
        self.gate_level = _read_gate_level(config)

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
        """Highest level whose threshold is <= crumbs (crumbs only, the gate is not applied here)."""
        value = float(crumbs)
        if not math.isfinite(value):
            raise ValueError("crumbs must be a finite number")
        level = 1
        while self.threshold(level + 1) <= value:
            level += 1
        return level

    def cap_level(self, level: int, has_star: bool) -> int:
        """v6 gate: without an organizer star, a level above gate_level is capped at gate_level."""
        value = int(level)
        if not self.gate_enabled or has_star:
            return value
        return min(value, self.gate_level)

    def effective_level(self, crumbs: float, has_star: bool) -> int:
        """The level a member sees: their crumbs level, capped at gate_level until they have an organizer star.
        Pass has_star from Users.has_organizer_star so a revoked last star falls the level back to at most gate_level."""
        return self.cap_level(self.level_for(crumbs), has_star)

    def needs_star(self, level: int, has_star: bool) -> bool:
        """True when this level is above the gate and the person has no star yet (for a private hint)."""
        return self.gate_enabled and not has_star and int(level) > self.gate_level

    def title(self, level: int) -> str:
        lv = max(1, int(level))
        if lv <= len(self._titles):
            return self._titles[lv - 1]
        return "%s +%d" % (self._titles[-1], lv - len(self._titles))

    def progress(self, crumbs: float, has_star: bool = True) -> Dict[str, Any]:
        """{"level", "title", "next_level", "next_title", "next_threshold", "to_next", "percent"}.
        With has_star False the level is gated (see effective_level); the default keeps the ungated level."""
        value = float(crumbs)
        level = self.cap_level(self.level_for(value), has_star)
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
