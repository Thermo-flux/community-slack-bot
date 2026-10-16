"""Tests for goose/core/rules.py: qualification, normalization, simhash, mentions and explain requests."""
from __future__ import annotations

from goose.core import rules


def test_ok_sounds_good_to_me_qualifies():
    assert rules.qualify_message("ok sounds good to me")


def test_emoji_link_mention_and_short_text_do_not_qualify():
    assert not rules.qualify_message(":tada:")
    assert not rules.qualify_message("<https://example.com/some/page>")
    assert not rules.qualify_message("https://example.com/some/page/here")
    assert not rules.qualify_message("<@U123>")
    assert not rules.qualify_message("<@U123|sam>")
    assert not rules.qualify_message("ok")
    assert not rules.qualify_message(":tada: :sparkles: :duck:")


def test_code_spans_and_blocks_are_ignored():
    assert not rules.qualify_message("`print the whole thing now`")
    assert not rules.qualify_message("```print the whole thing now please```")
    assert rules.qualify_message("see `x` and then the rest of the thing")


def test_strip_markup_keeps_plain_words_only():
    assert rules.strip_markup("hi <@U1|bob> :tada: `x` there") == "hi there"
    assert rules.strip_markup("a &amp; b &lt;c&gt;") == "a & b <c>"
    assert rules.strip_markup("<!here> hello") == "hello"


def test_word_count_counts_letters_and_digits_only():
    assert rules.word_count("one two 3") == 3
    assert rules.word_count(":tada: --- !!!") == 0
    assert rules.word_count("<@U1> hi there") == 2


def test_normalize_text_lowercases_and_collapses_repeats():
    assert rules.normalize_text("Sooooo GOOD!!!") == "soo good"
    assert rules.normalize_text("What's   that?") == "whats that"


def test_simhash_ignores_case_and_punctuation():
    a = rules.simhash("Hello there, friend!")
    b = rules.simhash("hello there friend")
    assert a == b
    assert rules.is_near_duplicate(a, [b], max_distance=0)


def test_simhash_is_deterministic_and_zero_for_empty():
    assert rules.simhash("some words here") == rules.simhash("some words here")
    assert rules.simhash("") == 0
    assert rules.simhash(":tada:") == 0


def test_near_duplicate_is_closer_than_an_unrelated_sentence():
    original = "we finally got the ore smelting loop working tonight with the new furnace layout"
    one_word_changed = "we finally got the ore smelting loop working tonight with the old furnace layout"
    unrelated = "my cat sat on the keyboard and typed a very long essay about fish"
    h = rules.simhash(original)
    near = rules.hamming(h, rules.simhash(one_word_changed))
    far = rules.hamming(h, rules.simhash(unrelated))
    assert near < far
    assert far > 20
    assert rules.is_near_duplicate(h, [rules.simhash(one_word_changed)], max_distance=20)
    assert not rules.is_near_duplicate(h, [rules.simhash(unrelated)], max_distance=3)


def test_hamming_and_is_near_duplicate_basics():
    assert rules.hamming(0b1011, 0b0010) == 2
    assert rules.is_near_duplicate(5, [], 3) is False
    assert rules.is_near_duplicate(5, [4], 3) is True


def test_extract_user_ids_order_and_no_duplicates():
    text = "<@U123> hi <@U123|bob> and <@W9|x> also <@U5>"
    assert rules.extract_user_ids(text) == ["U123", "W9", "U5"]
    assert rules.extract_user_ids("no mentions here") == []


def test_contains_ping_matches_id_with_or_without_label():
    assert rules.contains_ping("hey <@UBOT>", "UBOT")
    assert rules.contains_ping("hey <@UBOT|goose-lee>", "UBOT")
    assert not rules.contains_ping("hey <@UOTHER>", "UBOT")
    assert not rules.contains_ping("hey UBOT", "UBOT")


def test_mentions_name_cases():
    assert rules.mentions_name("hey goose-lee")
    assert rules.mentions_name("Goose-Lee is great")
    assert rules.mentions_name("hi goose lee")
    assert rules.mentions_name("gooselee?")
    assert not rules.mentions_name("you are a goose")
    assert not rules.mentions_name("see https://goose-lee.example.com/page now")
    assert not rules.mentions_name("> goose-lee said hi")
    assert not rules.mentions_name("&gt; goose-lee said hi")
    assert not rules.mentions_name("use `goose-lee` for it")
    assert not rules.mentions_name("<@U1|goose-lee> hi")
    assert not rules.mentions_name("goose-leer is a different word")


def test_mentions_goose_is_a_bare_word_only():
    assert rules.mentions_goose("you are a goose")
    assert rules.mentions_goose("a goose stole my bread")
    assert not rules.mentions_goose("goose-lee")
    assert not rules.mentions_goose("Mother Goose")
    assert not rules.mentions_goose("goosebumps")
    assert not rules.mentions_goose("gooseberry")
    assert not rules.mentions_goose("goose bumps")
    assert not rules.mentions_goose("> a goose")
    assert not rules.mentions_goose("`goose`")
    assert not rules.mentions_goose("https://example.com/goose")


def test_is_explain_request_positive_cases():
    assert rules.is_explain_request("what does that mean?")
    assert rules.is_explain_request("what does it mean")
    assert rules.is_explain_request("what's that mean")
    assert rules.is_explain_request("what does that say?")
    assert rules.is_explain_request("translate")
    assert rules.is_explain_request("can you explain?")
    assert rules.is_explain_request("meaning?")
    assert rules.is_explain_request("was heißt das?")
    assert rules.is_explain_request("was heisst das")
    assert rules.is_explain_request("was bedeutet das")
    assert rules.is_explain_request("bedeutet das?")
    assert rules.is_explain_request("übersetz bitte")


def test_is_explain_request_false_positives_are_rejected():
    assert not rules.is_explain_request("translate this python function")
    assert not rules.is_explain_request("translate the build error")
    assert not rules.is_explain_request("what does that mean `code`")
    assert not rules.is_explain_request("translate see https://example.com")
    assert not rules.is_explain_request("> translate that line")
    assert not rules.is_explain_request("the meaning of life is nice")
    assert not rules.is_explain_request(
        "please translate this for me because I was reading a long story about the pond and the weather today")
    assert not rules.is_explain_request("this is a long chat message about nothing in particular at all")


def test_is_elaborate_request():
    assert rules.is_elaborate_request("more")
    assert rules.is_elaborate_request("why?")
    assert rules.is_elaborate_request("tell me more")
    assert rules.is_elaborate_request("go on")
    assert rules.is_elaborate_request("details please")
    assert not rules.is_elaborate_request("")
    assert not rules.is_elaborate_request("I went to the shop and bought a lot of bread for the pond today")


def test_count_exclamations_ignores_slack_specials():
    assert rules.count_exclamations("!!!! hi") == 4
    assert rules.count_exclamations("lol 4!!!!") == 4
    assert rules.count_exclamations("<!here> hi!") == 1
    assert rules.count_exclamations(":tada: wow!") == 1
    assert rules.count_exclamations("calm down") == 0
