"""Tests for goose/core/lines.py and config/lines.yaml: the real file, validation, placeholders and picking."""
from __future__ import annotations

import os
import random
import re

import pytest

from goose.core import wordlists
from goose.core.lines import DE_RE, LineBank

LINES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "lines.yaml")

REQUIRED_MOMENTS = [
    "join", "welcome", "level_up", "star", "bigstar", "level_12", "ack",
    "anger_calm", "anger_annoyed", "anger_angry", "anger_honk", "anger_timeout", "anger_cooling",
    "rank_remark_active", "rank_top10", "rank_below", "rank_duck", "roast", "roast_self",
    "limit_reached", "program_redirect", "not_joined", "unknown_person", "error", "safety",
    "busiest_duck", "pond_report_header", "pond_report_footer", "pond_report_result",
    "chat_fallback", "unknown_phrase", "quack", "pun",
]
# BUILD_SPEC / PLAN_V6 require more than the default three lines for these:
# ack is the eight appendix acknowledgements, the anger stages carry six each,
# and the ranking/roast moments carry the counts in PLAN_V6 section 2.
MOMENT_MINIMUMS = {
    "ack": 8,
    "anger_calm": 6,
    "anger_annoyed": 6,
    "anger_angry": 6,
    "anger_honk": 6,
    "anger_timeout": 6,
    "anger_cooling": 6,
    "rank_remark_active": 5,
    "rank_top10": 4,
    "rank_below": 2,
    "rank_duck": 3,
    "roast": 5,
}
MIN_LINES = 3
REQUIRED_GERMAN = [
    "ente_gut", "entenschuldigung", "quaktuell", "entenentspannt", "quak_mal", "zeitungsente", "gans_falsch",
    "gans_toll", "moin", "mega", "hoppla", "watschel", "quak", "genau", "mensch", "entenzueckend",
    "das_ist_ne_ente", "quacksalber", "entenhausen", "alle_meine_entchen", "federleicht", "wochenende",
    "feierabend", "tschuess",
]
DASHES = ("\u2014", "\u2013")
REWARD_RES = [re.compile(r"(?<!\w)" + r"\s+".join(re.escape(p) for p in w.split()) + r"(?!\w)", re.I)
              for w in list(wordlists.REWARD_STRONG) + list(wordlists.REWARD_WEAK)]


@pytest.fixture
def bank(lines):
    return lines


def _all_lines(bank):
    for moment in bank.moments():
        for line in bank.lines(moment):
            yield moment, line


def test_real_file_loads_and_has_every_required_moment(bank):
    for moment in REQUIRED_MOMENTS:
        assert bank.has_moment(moment), moment
        assert len(bank.lines(moment)) >= 3, moment


def test_every_moment_has_at_least_three_lines(bank):
    for moment in bank.moments():
        assert len(bank.lines(moment)) >= 3, moment


def test_every_required_moment_meets_its_stated_minimum_count(bank):
    for moment in REQUIRED_MOMENTS:
        assert bank.has_moment(moment), moment
        expected = MOMENT_MINIMUMS.get(moment, MIN_LINES)
        assert len(bank.lines(moment)) >= expected, (moment, len(bank.lines(moment)), expected)


def test_rank_top10_lines_each_carry_the_rank_slot(bank):
    for line in bank.lines("rank_top10"):
        assert "{rank}" in line.text, line.id


def test_required_german_ids_exist(bank):
    for de_id in REQUIRED_GERMAN:
        assert de_id in bank.german_ids(), de_id
        entry = bank.german(de_id)
        assert entry["text"] and entry["meaning"] and entry["explain"], de_id


def test_every_placeholder_exists_in_german_table(bank):
    for moment, line in _all_lines(bank):
        for de_id in DE_RE.findall(line.text):
            assert de_id in bank.german_ids(), (moment, line.id, de_id)
        assert sorted(set(line.de_ids)) == sorted(set(DE_RE.findall(line.text))), line.id


def test_line_ids_are_unique(bank):
    ids = [line.id for _, line in _all_lines(bank)]
    assert len(ids) == len(set(ids))


def test_no_dashes_anywhere_in_the_file_or_lines(bank):
    with open(LINES_PATH, "r", encoding="utf-8") as fh:
        raw = fh.read()
    for dash in DASHES:
        assert dash not in raw
    for _, line in _all_lines(bank):
        for dash in DASHES:
            assert dash not in line.text
    for de_id in bank.german_ids():
        for value in bank.german(de_id).values():
            for dash in DASHES:
                assert dash not in value


def test_no_reward_words_outside_reward_moments(bank):
    for moment, line in _all_lines(bank):
        if moment.startswith("reward_"):
            continue
        for rx in REWARD_RES:
            assert not rx.search(line.text), (moment, line.id, rx.pattern)


def test_lines_are_short_enough(bank):
    for moment, line in _all_lines(bank):
        assert len(line.text) < 200, (moment, line.id, len(line.text))


def test_safety_moment_has_no_joke_no_german_no_emoji(bank):
    for line in bank.lines("safety"):
        assert line.de_ids == []
        assert not DE_RE.search(line.text)
        assert not re.search(r":[a-z_]+:", line.text)
        assert "duck" not in line.text.lower()
        assert "{contact}" in line.text


def test_chat_fallback_points_to_help(bank):
    for line in bank.lines("chat_fallback"):
        assert "/goose help" in line.text
        assert not re.search(r":[a-z_]+:", line.text)


def test_anger_lines_never_use_you_or_names(bank):
    for stage in ("calm", "annoyed", "angry", "honk", "timeout", "cooling"):
        for line in bank.lines("anger_" + stage):
            assert not re.search(r"\byou(r|re|'re)?\b", line.text.lower()), line.id


def test_every_moment_has_a_plain_variant(bank):
    for moment in bank.moments():
        assert any(not line.de_ids for line in bank.lines(moment)), moment


def test_german_share_is_about_the_rate_over_400_picks(bank):
    rng = random.Random(7)
    picks = [bank.pick("ack", rng, german_rate=0.25) for _ in range(400)]
    share = sum(1 for p in picks if p.de_ids) / 400.0
    assert 0.15 <= share <= 0.35, share


def test_german_is_never_picked_when_rate_is_zero_or_disallowed(bank):
    rng = random.Random(3)
    for _ in range(100):
        assert not bank.pick("ack", rng, german_rate=0.0).de_ids
        assert not bank.pick("ack", rng, german_rate=1.0, allow_german=False).de_ids


def test_pick_fills_slots_and_keeps_placeholders(bank):
    rng = random.Random(11)
    for _ in range(50):
        line = bank.pick("level_up", rng, slots={"n": 4, "title": "Mallard"})
        assert "{n}" not in line.text and "{title}" not in line.text
        assert "Pond level 4, Mallard" in line.text or "Level 4, Mallard" in line.text
        assert line.moment == "level_up"
        assert sorted(DE_RE.findall(line.text)) == sorted(line.de_ids)


def test_pick_respects_avoid_ids_when_possible(bank):
    rng = random.Random(5)
    lines = bank.lines("quack")
    keep = lines[0].id
    avoid = [line.id for line in lines[1:]]
    for _ in range(20):
        assert bank.pick("quack", rng, avoid_ids=avoid).id == keep
    # when everything is avoided, it still returns a line
    assert bank.pick("quack", rng, avoid_ids=[line.id for line in lines]).id in [line.id for line in lines]


def test_pick_unknown_moment_raises_key_error(bank):
    with pytest.raises(KeyError):
        bank.pick("no_such_moment", random.Random(1))


def test_canned_for_stage_uses_the_anger_moments(bank):
    rng = random.Random(9)
    for stage in ("calm", "annoyed", "angry", "honk", "timeout", "cooling"):
        line = bank.canned_for_stage(stage, rng)
        assert line.moment == "anger_" + stage
    with pytest.raises(KeyError):
        bank.canned_for_stage("furious", rng)


def test_expand_replaces_german_and_accepts_voice_placeholders(bank):
    assert bank.expand("{de:quak}! {de:gans_falsch}") == "Quak! Gans falsch"
    assert bank.expand("{{de:quak}} and {{de:mensch}}") == "Quak and Mensch"
    assert bank.expand("plain text") == "plain text"
    with pytest.raises(KeyError):
        bank.expand("{de:not_a_real_id}")


def test_normalize_placeholders_and_de_ids_in(bank):
    assert bank.normalize_placeholders("{{de:quak}} x {de:mensch}") == "{de:quak} x {de:mensch}"
    assert bank.de_ids_in("{de:a_b} x {{de:quak}} {de:quak}") == ["a_b", "quak"]


def test_render_fills_slots_and_leaves_the_rest(bank):
    out = bank.render("Pond level {n}, {title}. {de:mensch} {other}", {"n": 3})
    assert out == "Pond level 3, {title}. {de:mensch} {other}"
    assert bank.render("no slots") == "no slots"


def test_german_and_explain_accessors(bank):
    entry = bank.german("gans_falsch")
    assert set(entry) == {"text", "meaning", "wordplay", "explain"}
    assert "goose" in bank.explain_text("gans_falsch").lower()
    for de_id in bank.german_ids():
        assert bank.explain_text(de_id).strip()
    with pytest.raises(KeyError):
        bank.german("nope")


def test_gloss_text_uses_a_wrapper_with_the_meaning(bank):
    rng = random.Random(2)
    text = bank.gloss_text("quak_mal", rng)
    assert "look" in text
    assert text.startswith("(") and text.endswith(")")


def test_expand_german_text_has_no_stray_placeholders(bank):
    for moment, line in _all_lines(bank):
        assert "{de:" not in bank.expand(line.text), line.id


# ---- validation of from_dict -------------------------------------------------------------------------------

def _data(lines_text, de=None, extra=None):
    german = {"quak": {"text": "Quak", "meaning": "duck sound", "wordplay": "", "explain": "Quak is a duck sound."}}
    if de:
        german.update(de)
    moments = {"m": [{"id": "x1", "text": lines_text}]}
    if extra:
        moments.update(extra)
    return {"moments": moments, "german": german}


def test_from_dict_accepts_a_good_small_bank():
    good = _data("Hello {de:quak}!")
    good["moments"]["m"][0]["de"] = ["quak"]
    bank = LineBank.from_dict(good)
    assert bank.expand("{de:quak}") == "Quak"
    assert bank.pick("m", random.Random(1)).text == "Hello {de:quak}!"


def test_from_dict_rejects_unknown_german_id():
    data = _data("Hello {de:nope}!")
    data["moments"]["m"][0]["de"] = ["nope"]
    with pytest.raises(ValueError, match="unknown German id nope"):
        LineBank.from_dict(data)


def test_from_dict_rejects_de_list_that_does_not_match():
    data = _data("Hello {de:quak}!")
    with pytest.raises(ValueError, match="does not match"):
        LineBank.from_dict(data)


def test_from_dict_rejects_dashes():
    data = _data("Hello \u2014 there")
    with pytest.raises(ValueError, match="dashes"):
        LineBank.from_dict(data)


def test_from_dict_rejects_reward_words_outside_reward_moments():
    data = _data("You won a prize today")
    with pytest.raises(ValueError, match="reward word"):
        LineBank.from_dict(data)


def test_from_dict_allows_reward_words_in_reward_moments():
    data = _data("unused")
    data["moments"] = {"reward_weekly": [{"id": "r1", "text": "A prize for the top duck."}]}
    bank = LineBank.from_dict(data)
    assert bank.has_moment("reward_weekly")


def test_from_dict_rejects_duplicate_ids_and_long_lines():
    data = _data("one")
    data["moments"]["n"] = [{"id": "x1", "text": "two"}]
    with pytest.raises(ValueError, match="duplicate id x1"):
        LineBank.from_dict(data)
    data = _data("a" * 401)
    with pytest.raises(ValueError, match="over 400"):
        LineBank.from_dict(data)


def test_from_dict_rejects_missing_german_explain():
    data = _data("hi")
    data["german"]["quak"]["explain"] = ""
    with pytest.raises(ValueError, match="explain"):
        LineBank.from_dict(data)


def test_from_dict_reports_all_problems_together():
    data = _data("Hello \u2014 {de:zz}")
    data["moments"]["m"][0]["de"] = []
    with pytest.raises(ValueError) as info:
        LineBank.from_dict(data)
    message = str(info.value)
    assert "dashes" in message and "unknown German id zz" in message


def test_from_file_reports_missing_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        LineBank.from_file(str(tmp_path / "missing.yaml"))
