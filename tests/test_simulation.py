"""The scoring simulation (tools/level_sim.py) and its agreement with goose.core.scoring."""
from __future__ import annotations

import importlib.util
import os
import runpy

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_PATH = os.path.join(ROOT, "tools", "level_sim.py")
CONFIG_PATH = os.path.join(ROOT, "config", "goose.yaml")

# (days active, messages, thanks received, helpful marks) copied from PEOPLE in tools/level_sim.py
CASES = {
    "casual": (3, 5, 1, 0),
    "regular chatter": (5, 40, 4, 1),
    "very active writer": (7, 1000, 10, 2),
}


def load_sim():
    spec = importlib.util.spec_from_file_location("level_sim_under_test", SIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_level_sim_runs_and_all_assertions_pass(capsys):
    runpy.run_path(SIM_PATH, run_name="__main__")
    assert "ALL ASSERTIONS PASSED" in capsys.readouterr().out


def test_simulation_personas_match_the_cases_used_here():
    sim = load_sim()
    for name, (days, msgs, _thanks, _helpful) in CASES.items():
        assert sim.BY[name].msgs == msgs, "tools/level_sim.py changed: update CASES here"
        assert sim.chat(msgs, days) == pytest.approx(sim.BY[name].chat)


def test_simulation_thresholds_match_the_shipped_config():
    sim = load_sim()
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        shipped = yaml.safe_load(fh)
    thresholds = list((shipped.get("levels") or {}).get("thresholds") or [])
    derived = [sim.threshold(n) for n in range(1, 13)]
    assert derived == thresholds, (
        "tools/level_sim.py and config/goose.yaml have drifted: %s vs %s" % (derived, thresholds)
    )


def test_scoring_module_matches_the_simulation_formulas():
    sim = load_sim()
    from goose.core import scoring

    try:
        scoring.score_week(1, 1, 1, 1, 25.0)
    except NotImplementedError:
        pytest.skip("goose/core/scoring.py is still a stub (agent A); the cross-check runs once it is implemented")

    for name, (days, msgs, thanks_n, helpful_n) in CASES.items():
        # star_points is the total star crumbs for the week; these personas have no stars
        b = scoring.score_week(msgs, days, thanks_n, helpful_n, 0.0, 0, "log")
        assert b.chat == pytest.approx(sim.chat(msgs, days), abs=1e-6), name
        assert b.thanks == pytest.approx(sim.thanks(thanks_n), abs=1e-6), name
        assert b.helpful == pytest.approx(sim.helpful(helpful_n), abs=1e-6), name
        assert b.stars == pytest.approx(0.0), name
        assert b.total == pytest.approx(sim.BY[name].weekly, abs=1e-6), name
