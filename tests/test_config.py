"""Config tests: the real file loads, and every bad input fails closed with a clear message."""
from __future__ import annotations

import dataclasses
import inspect
import os

import pytest

import goose.config as config_module
from goose.config import Config, ConfigError, config_from_dict, load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "config", "goose.yaml")


def write(tmp_path, text, bom=False):
    path = tmp_path / "goose.yaml"
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return str(path)


def test_real_config_loads_and_keeps_the_safe_defaults():
    cfg = load_config(REAL)
    assert isinstance(cfg, Config)
    assert cfg.levels.thresholds[0] == 0
    assert len(cfg.levels.titles) == len(cfg.levels.thresholds)
    assert cfg.voice.enabled is False, "the voice must ship switched off"
    assert cfg.rewards.announced is False, "no reward wording until the owner approves"


def test_defaults_validate():
    assert config_from_dict(None) == Config()


def test_unknown_key_fails_and_the_key_is_named(tmp_path):
    path = write(tmp_path, "scoring:\n  min_wrds: 3\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "min_wrds" in str(exc.value)


def test_wrong_type_fails_and_the_path_is_named(tmp_path):
    path = write(tmp_path, "scoring:\n  min_words: three\n")
    with pytest.raises(ConfigError, match="scoring.min_words"):
        load_config(path)


def test_top_level_typo_fails_and_names_the_key():
    with pytest.raises(ConfigError, match="channles"):
        config_from_dict({"channles": {"scored": ["C1"]}})


def test_nested_bad_bool_fails_closed():
    with pytest.raises(ConfigError, match="voice.enabled"):
        config_from_dict({"voice": {"enabled": "yes"}})


def test_nested_typo_in_a_known_section_fails_closed():
    with pytest.raises(ConfigError, match="voice.messages_per_hour"):
        config_from_dict({"voice": {"messages_per_hour": 5}})


def test_utf8_bom_file_loads(tmp_path):
    with open(REAL, "r", encoding="utf-8") as fh:
        text = fh.read()
    path = write(tmp_path, text, bom=True)
    assert load_config(path) == load_config(REAL)


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "does-not-exist.yaml"))


def test_thresholds_must_start_at_zero():
    with pytest.raises(ConfigError, match="levels.thresholds"):
        config_from_dict({"levels": {"thresholds": [5, 10, 25]}})


def test_titles_count_must_match_thresholds():
    with pytest.raises(ConfigError, match="levels.titles"):
        config_from_dict({"levels": {"titles": ["Only", "Two"]}})


def test_german_rate_out_of_range_fails():
    with pytest.raises(ConfigError, match="vibes.german_rate"):
        config_from_dict({"vibes": {"german_rate": 1.5}})


def test_invalid_timezone_fails():
    with pytest.raises(ConfigError, match="schedule.timezone"):
        config_from_dict({"schedule": {"timezone": "Mars/Olympus_Base"}})


def test_valid_named_timezone_loads():
    pytest.importorskip("tzdata", reason="tzdata is needed for named timezones on this machine")
    cfg = config_from_dict({"schedule": {"timezone": "Europe/Berlin"}})
    assert cfg.schedule.timezone == "Europe/Berlin"


def test_message_limit_of_zero_fails():
    with pytest.raises(ConfigError, match="voice.messages_per_person_per_hour"):
        config_from_dict({"voice": {"messages_per_person_per_hour": 0}})


def _config_classes():
    return [
        obj for _, obj in inspect.getmembers(config_module, inspect.isclass)
        if dataclasses.is_dataclass(obj) and obj.__module__ == config_module.__name__
    ]


def test_there_is_no_spend_cap_setting_anywhere():
    # The owner removed spend caps. The only budget-like field allowed is the optional heads-up alert.
    allowed = {"alert_tokens_per_day"}
    words = ("budget", "monthly", "daily_tokens", "spend")
    names = {f.name for cls in _config_classes() for f in dataclasses.fields(cls)}
    offending = sorted(n for n in names if n not in allowed and any(w in n for w in words))
    assert offending == []
    assert "alert_tokens_per_day" in names
    assert Config().voice.alert_tokens_per_day is None, "the heads-up alert is off by default"
