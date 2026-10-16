"""Tests for goose/core/safety.py: whole-word gates, distress and sad lists, program questions, the safety text."""
from __future__ import annotations

from goose.core import safety


def test_find_words_is_whole_word_and_phrase_aware(cfg):
    assert safety.find_words("I feel alone today", ["alone"]) == ["alone"]
    assert safety.find_words("aloneness is a word", ["alone"]) == []
    assert safety.find_words("I MISS HIM so much", ["miss him"]) == ["miss him"]
    assert safety.find_words("self-harm talk", ["self harm"]) == ["self harm"]
    assert safety.find_words("", ["alone"]) == []


def test_contains_word(cfg):
    assert safety.contains_word("so worthless", ["worthless"])
    assert not safety.contains_word("worthlessly", ["worthless"])


def test_distress_uses_builtin_list_and_config_extras(cfg):
    assert safety.is_distress("I want to die, nobody talks to me", cfg)
    assert safety.is_distress("thinking about suicide", cfg)
    assert not safety.is_distress("I shipped it", cfg)
    cfg.safety.distress_words = ["xyzzy plugh"]
    assert safety.is_distress("say xyzzy plugh now", cfg)


def test_sad_includes_distress_and_sad_words(cfg):
    assert safety.is_sad("my grandma died last night", cfg)
    assert safety.is_sad("I miss him", cfg)
    assert safety.is_sad("I want to die", cfg)            # distress counts as sad
    assert safety.is_sad("so anxious about the test", cfg)
    assert not safety.is_distress("my grandma died last night", cfg)
    assert not safety.is_sad("so tired of this pond today", cfg)
    assert not safety.is_sad("the deadline is next week", cfg)   # 'dead' is whole-word only
    cfg.safety.sad_words = ["bleh"]
    assert safety.is_sad("total bleh", cfg)


def test_is_program_question_cases():
    assert safety.is_program_question("how much ore per hour")
    assert safety.is_program_question("when does loadout launch")
    assert safety.is_program_question("what is a hackatime")
    assert safety.is_program_question("when does the program close")
    assert not safety.is_program_question("is the deadline in march?")
    assert not safety.is_program_question("launch a rocket")
    assert not safety.is_program_question("who is bruce lee")
    assert not safety.is_program_question("what a lovely day")


def test_safety_reply_is_plain_and_points_to_contact(cfg):
    text = safety.safety_reply(cfg)
    assert cfg.safety.contact_text in text
    assert "bot" in text
    assert "!" not in text
    assert ":" not in text                     # no emoji shortcodes
    assert "duck" not in text.lower()          # no joke
    assert all(ord(ch) < 128 for ch in text)   # no German umlauts or other joke-like extras
