"""Scoring formulas: the plan's numbers and the persona totals from tools/level_sim.py."""
from __future__ import annotations

import math

import pytest

from goose.core import scoring
from goose.core.scoring import chat_score, helpful_score, score_week, thanks_score


def test_log_chat_formula_matches_plan():
    # 8 * ln(1 + 200 / 5) + 1.5 * 7
    assert chat_score(200, 7) == pytest.approx(8 * math.log(41) + 10.5)


def test_active_days_are_capped_at_seven():
    assert chat_score(10, 9) == chat_score(10, 7)
    assert chat_score(10, 7) - chat_score(10, 6) == pytest.approx(1.5)


def test_linear_curve_is_pure_volume():
    assert chat_score(10, 0, "linear") == pytest.approx(8.0)
    assert chat_score(10, 2, "linear") == pytest.approx(8.0 + 3.0)


def test_unknown_curve_is_refused():
    with pytest.raises(ValueError):
        chat_score(10, 1, "square")


def test_thanks_and_helpful_formulas():
    assert thanks_score(3) == pytest.approx(6 * math.log(2))
    assert helpful_score(3) == pytest.approx(14 * math.log(2))
    assert thanks_score(0) == 0.0
    assert helpful_score(0) == 0.0


def test_negative_counts_count_as_zero():
    assert chat_score(-5, -2) == 0.0
    assert thanks_score(-1) == 0.0
    assert helpful_score(-1) == 0.0


def test_no_cap_and_gentle_curve():
    assert chat_score(1001, 7) > chat_score(1000, 7)
    marginal = chat_score(1000, 7) - chat_score(999, 7)
    assert marginal > 0.005
    ratio = chat_score(1000, 0) / chat_score(50, 0)
    assert 1.8 < ratio < 3.0


def test_heavy_contributor_matches_simulation():
    # tools/level_sim.py: heavy contributor weekly 136.6 (200 msgs, 7 days, 15 thanks, 8 helpful, 1.5 small + 0.5 big stars)
    bd = score_week(200, 7, 15, 8, star_points=1.5 * 25 + 0.5 * 60, star_count=2)
    assert bd.total == pytest.approx(136.6, abs=0.2)


def test_solid_helper_matches_simulation():
    bd = score_week(120, 6, 10, 6, star_points=0.5 * 25)
    assert bd.total == pytest.approx(71.4, abs=0.2)


def test_very_active_writer_with_one_star_beats_solid_helper():
    bd = score_week(1000, 7, 10, 2, star_points=0.0)
    assert bd.total == pytest.approx(68.9, abs=0.2)
    assert bd.total + 25 > scoring.score_week(120, 6, 10, 6, 12.5).total


def test_score_week_total_is_sum_of_parts():
    bd = score_week(20, 3, 4, 2, star_points=25, star_count=1)
    assert bd.total == pytest.approx(bd.chat + bd.thanks + bd.helpful + bd.stars)
    assert bd.stars == 25
    assert bd.n_msgs == 20
    assert bd.active_days == 3
    assert bd.n_thanks == 4
    assert bd.n_helpful == 2
    assert bd.star_count == 1


def test_empty_week_is_zero():
    bd = score_week(0, 0, 0, 0, star_points=0.0)
    assert bd.total == 0.0
