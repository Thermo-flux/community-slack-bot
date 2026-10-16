"""Levels: thresholds, the uncapped tail, titles, progress, and the simulator's level table."""
from __future__ import annotations

import pytest

from goose.config import default_config
from goose.core.levels import Levels
from goose.core.scoring import score_week


def test_table_thresholds_come_from_config(levels, cfg):
    assert [levels.threshold(n) for n in range(1, 13)] == cfg.levels.thresholds


def test_tail_matches_plan_table(levels):
    # PLAN section 5: 13 = 1,070, 14 = 1,280, 15 = 1,540, 16 = 1,845
    assert [levels.threshold(n) for n in (13, 14, 15, 16)] == [1070, 1280, 1540, 1845]


def test_threshold_below_one_is_refused(levels):
    with pytest.raises(ValueError):
        levels.threshold(0)


def test_level_boundaries(levels):
    assert levels.level_for(0) == 1
    assert levels.level_for(9.99) == 1
    assert levels.level_for(10) == 2
    assert levels.level_for(889.9) == 11
    assert levels.level_for(890) == 12
    assert levels.level_for(1069.9) == 12
    assert levels.level_for(1070) == 13


def test_negative_crumbs_are_level_one(levels):
    assert levels.level_for(-5) == 1


def test_non_finite_crumbs_are_refused(levels):
    with pytest.raises(ValueError):
        levels.level_for(float("inf"))


def test_very_large_crumbs_terminate(levels):
    assert levels.level_for(1e9) > 20


def test_titles_and_tail_titles(levels):
    assert levels.title(1) == "Hatchling"
    assert levels.title(12) == "Head Duck"
    assert levels.title(13) == "Head Duck +1"
    assert levels.title(14) == "Head Duck +2"
    assert levels.title(30) == "Head Duck +18"


def test_growth_factor_comes_from_config():
    cfg = default_config()
    cfg.levels.growth_above_top = 1.5
    lv = Levels(cfg)
    assert lv.threshold(13) == round(890 * 1.5 / 5) * 5


def test_progress_at_zero(levels):
    p = levels.progress(0)
    assert p["level"] == 1
    assert p["title"] == "Hatchling"
    assert p["next_level"] == 2
    assert p["next_threshold"] == 10
    assert p["to_next"] == 10
    assert p["percent"] == 0.0


def test_progress_inside_a_level(levels):
    p = levels.progress(15)
    assert p["level"] == 2
    assert p["percent"] == pytest.approx(33.3, abs=0.05)
    assert p["to_next"] == 10


def test_progress_above_the_table(levels):
    p = levels.progress(1200)
    assert p["level"] == 13
    assert p["title"] == "Head Duck +1"
    assert p["next_threshold"] == 1280
    assert p["to_next"] == 80.0
    assert p["percent"] == pytest.approx(61.9, abs=0.05)


def test_progress_keys(levels):
    assert set(levels.progress(50)) == {
        "level", "title", "next_level", "next_title", "next_threshold", "to_next", "percent"}


def test_simulator_level_table_is_reproduced(levels):
    """tools/level_sim.py: heavy contributor and solid helper levels at weeks 4, 6.5, 9 and 13."""
    heavy = score_week(200, 7, 15, 8, star_points=1.5 * 25 + 0.5 * 60, star_count=2).total
    helper = score_week(120, 6, 10, 6, star_points=0.5 * 25).total
    assert [levels.level_for(heavy * w) for w in (4, 6.5, 9, 13)] == [9, 11, 13, 15]
    assert [levels.level_for(helper * w) for w in (4, 6.5, 9, 13)] == [7, 9, 10, 12]
