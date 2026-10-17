"""Tests for the post-filter, escaping and finalize (goose/gateway/poster.py). Pure functions, no network, no real time.

FakeLines is a small stand-in for goose.core.lines.LineBank with the methods the poster calls. It is used so these tests
do not depend on the line bank loader.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest

from goose.config import default_config
from goose.gateway.poster import (APPROVED_TIMEOUT_JOKE, PostFilter, escape_text, finalize, has_forbidden_markup,
                                  render_canned)
from goose.types import Action, FilterResult, VoiceJob, VoiceResult

REQ_ID = "req-1"
CARD = {
    "level": 4, "title": "Mallard", "band": "active", "streak_days": 3, "crumbs": 58, "rank": 2,
    "top_slots": 3, "week_id": "2025-10-06", "rewards_announced": False,
}
NAMES = {"you": "<@U123>", "top1": "<@U201>", "top2": "<@U202>", "top3": "<@U203>"}


class FakeLines:
    """Stand-in for LineBank: the German table and the placeholder helpers the poster uses."""
    DE = {
        "quak": "Quak", "gans_falsch": "Gans falsch", "zeitungsente": "Zeitungsente", "mensch": "Mensch",
        "genau": "Genau", "hoppla": "Hoppla", "mega": "Mega", "ente_gut": "Ente gut",
    }

    def normalize_placeholders(self, text: str) -> str:
        return re.sub(r"\{\{\s*(de:[^{}]*?)\s*\}\}", r"{\1}", text)

    def expand(self, text: str) -> str:
        def repl(m):
            return self.DE[m.group(1)]          # KeyError for an unknown id, like the real bank
        return re.sub(r"\{de:([A-Za-z0-9_]+)\}", repl, text)

    def de_ids_in(self, text: str) -> List[str]:
        return re.findall(r"\{de:([A-Za-z0-9_]+)\}", text)

    def german_ids(self) -> List[str]:
        return list(self.DE)

    def german(self, de_id: str) -> Dict[str, str]:
        return {"text": self.DE[de_id], "meaning": "", "wordplay": "", "explain": ""}


def request(**over: Any) -> Dict[str, Any]:
    req: Dict[str, Any] = {
        "v": 1, "request_id": REQ_ID, "kind": "addressed", "mode": "chat",
        "persona": {"humor_level": 2, "german_allowed": True, "roast_allowed": False, "max_chars": 300},
        "card": dict(CARD),
        "input": {"message": {"handle": "you", "text": "hi"}, "thread": []},
    }
    req.update(over)
    return req


def reply(text: str, action: str = "speak", reaction: Any = None, flags: Dict[str, Any] = None,
          request_id: str = REQ_ID, v: Any = 1, **extra: Any) -> Dict[str, Any]:
    f: Dict[str, Any] = {"roasted": False, "mentions_rank": False, "uses_german": "{{de:" in text, "numbers_used": []}
    if flags:
        f.update(flags)
    out: Dict[str, Any] = {"v": v, "request_id": request_id, "action": action, "text": text,
                           "reaction": reaction, "flags": f}
    out.update(extra)
    return out


@pytest.fixture
def pf():
    return PostFilter(default_config(), FakeLines())


def reason_of(pf, resp, req=None, **kw):
    """'ok' when the filter accepts, otherwise the reject reason."""
    result = pf.check(resp, req if req is not None else request(), **kw)
    return "ok" if result.ok else result.reason


# ----- escaping -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("a & b", "a &amp; b"),
    ("<!here>", "&lt;!here&gt;"),
    ("&lt;", "&amp;lt;"),                 # & is escaped first, so an existing entity is not decoded twice
    ("<@U123>", "&lt;@U123&gt;"),
    ("plain text", "plain text"),
])
def test_escape_text_escapes_ampersand_first(raw, expected):
    assert escape_text(raw) == expected


@pytest.mark.parametrize("text", ["<!here>", "<@U1>", "<#C1>", "<http://x.test>", "@channel hi", "@here",
                                  "@everyone", "@sam is here", "@x is the busiest"])
def test_forbidden_markup_is_detected(text):
    assert has_forbidden_markup(text) is True


@pytest.mark.parametrize("text", ["Quak!", "Top three, quak.", "Rank #3 this week", "Mega {{de:mega}}", "",
                                  "Yes & no, depends on the fight", "Two plus two is 4, and 3 > 2", "a < b",
                                  "#loadout", "a#b", "email me at a @ b"])
def test_plain_text_and_rank_numbers_are_not_markup(text):
    assert has_forbidden_markup(text) is False


def test_plain_ampersand_and_angle_brackets_are_escaped_not_refused(pf):
    result = pf.check(reply("Yes & no, depends on the fight"), request())
    assert result.ok is True and result.text == "Yes &amp; no, depends on the fight"
    result = pf.check(reply("Two plus two is 4, and 3 > 2"), request())
    assert result.ok is True and result.text == "Two plus two is 4, and 3 &gt; 2"
    assert reason_of(pf, reply("Is 2 < 3? Quak.")) == "ok"


def test_render_canned_expands_german_then_escapes():
    assert render_canned("Rock & roll {de:quak}", FakeLines()) == "Rock &amp; roll Quak"
    assert render_canned("Quak {{de:quak}} <now>", FakeLines()) == "Quak Quak &lt;now&gt;"


# ----- filter step 1: strict JSON shape --------------------------------------------------------------------------------

def test_step1_valid_shape_is_accepted(pf):
    assert reason_of(pf, reply("Quak, the duck is here.")) == "ok"


def test_step1_extra_top_level_key_rejects(pf):
    assert reason_of(pf, reply("Quak.", confirm=True)) == "shape"


def test_step1_missing_required_key_rejects(pf):
    resp = reply("Quak.")
    del resp["flags"]
    assert reason_of(pf, resp) == "shape"


def test_step1_request_id_must_match(pf):
    assert reason_of(pf, reply("Quak.", request_id="other")) == "request_id"


def test_step1_version_must_be_one_integer(pf):
    assert reason_of(pf, reply("Quak.", v=2)) == "version"
    assert reason_of(pf, reply("Quak.", v=True)) == "version"


def test_step1_non_object_is_junk(pf):
    assert reason_of(pf, None) == "json"
    assert reason_of(pf, "Quak") == "json"


def test_step1_unknown_flag_key_rejects(pf):
    resp = reply("Quak.")
    resp["flags"]["mood"] = "happy"
    assert reason_of(pf, resp) == "shape"


def test_step1_flags_must_be_booleans_and_a_list(pf):
    resp = reply("Quak.")
    resp["flags"]["roasted"] = "no"
    assert reason_of(pf, resp) == "shape"
    resp = reply("Quak.")
    resp["flags"]["numbers_used"] = 4
    assert reason_of(pf, resp) == "shape"


def test_step1_reaction_must_be_text_or_null(pf):
    assert reason_of(pf, reply("Quak.", reaction=5)) == "shape"


def test_step1_unknown_action_rejects(pf):
    assert reason_of(pf, reply("Quak.", action="shout")) == "shape"


# ----- filter step 2: action handling and placeholders ----------------------------------------------------------------

def test_step2_silent_is_ok_with_empty_text(pf):
    result = pf.check(reply("", action="silent"), request())
    assert result.ok is True and result.text == "" and result.reaction == ""


def test_step2_redirect_and_decline_are_not_posted(pf):
    assert reason_of(pf, reply("", action="redirect")) == "redirect"
    assert reason_of(pf, reply("", action="decline")) == "decline"


def test_step2_empty_speech_rejects(pf):
    assert reason_of(pf, reply("   ")) == "empty"


def test_step2_known_placeholders_are_accepted(pf):
    result = pf.check(reply("{{top1}} and {{top3}} are doing well."), request(), names=NAMES)
    assert result.ok is True
    assert "<@U201>" in result.text and "<@U203>" in result.text


def test_step2_unknown_placeholder_kind_rejects(pf):
    assert reason_of(pf, reply("Hi {{admin}}."), names=NAMES) == "placeholder"


def test_step2_unapproved_german_id_rejects(pf):
    assert reason_of(pf, reply("Verbrenner {{de:verbrenner}} means a petrol car.")) == "placeholder"


def test_step2_single_brace_slot_rejects(pf):
    assert reason_of(pf, reply("Pond level {rank}.")) == "placeholder"


def test_step2_mention_placeholder_without_name_rejects(pf):
    assert reason_of(pf, reply("Hi {{you}}.")) == "name"


# ----- filter step 3: forbidden markup and control characters ----------------------------------------------------------

@pytest.mark.parametrize("text", ["<!here> quak", "@channel quak", "see <http://x.test>", "<@U1> hi", "<#C1> quak",
                                  "@here quak"])
def test_step3_markup_rejects(pf, text):
    assert reason_of(pf, reply(text)) == "markup"


def test_step3_zero_width_space_rejects(pf):
    assert reason_of(pf, reply("Quak​, quak.")) == "control"


def test_step3_plain_text_passes(pf):
    assert reason_of(pf, reply("Quak, quak.")) == "ok"


# ----- filter step 4: emoji allowlist ----------------------------------------------------------------------------------

def test_step4_allowlisted_emoji_passes(pf):
    assert reason_of(pf, reply("Quak :duck: :tada:")) == "ok"


def test_step4_allowlisted_star_passes(pf):
    assert reason_of(pf, reply("Great work :star:")) == "ok"


def test_step4_six_allowlisted_emoji_pass(pf):
    assert reason_of(pf, reply("Quak :duck: :tada: :wave: :eyes: :bread: :feather:")) == "ok"


def test_step4_unicode_emoji_normalizes_to_allowlisted_shortcode(pf):
    result = pf.check(reply("Quak \U0001F986"), request())
    assert result.ok and result.text == "Quak :duck:"


def test_step4_surrogate_pair_emoji_normalizes_without_rejecting(pf):
    result = pf.check(reply("Quak \ud83e\udd86"), request())
    assert result.ok and result.text == "Quak :duck:"


def test_step4_seven_allowlisted_emoji_reject(pf):
    assert reason_of(pf, reply("Quak :duck: :tada: :wave: :eyes: :bread: :feather: :rocket:")) == "emoji"


def test_step4_reaction_counts_toward_the_limit(pf):
    assert reason_of(pf, reply("Quak :duck: :tada: :wave: :eyes: :bread: :rocket:", reaction="duck")) == "emoji"


def test_step4_reaction_outside_allowlist_rejects(pf):
    assert reason_of(pf, reply("Quak.", reaction="not_allowlisted")) == "emoji"


def test_step4_allowed_reaction_is_returned(pf):
    result = pf.check(reply("Quak.", reaction="duck"), request())
    assert result.ok is True and result.reaction == "duck"


# ----- filter step 5: code, length, sentences --------------------------------------------------------------------------

def test_step5_exactly_300_characters_passes(pf):
    text = "Quak " * 59 + "quak."
    assert len(text) == 300
    assert reason_of(pf, reply(text)) == "ok"


def test_step5_301_characters_rejects(pf):
    text = "Quak " * 59 + "quakk."
    assert len(text) == 301
    assert reason_of(pf, reply(text)) == "length"


def test_step5_persona_max_chars_is_used(pf):
    req = request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": False, "max_chars": 50})
    assert reason_of(pf, reply("x" * 60), req) == "length"
    assert reason_of(pf, reply("Quak " * 9 + "quak."), req) == "ok"


def test_step5_length_is_counted_after_expansion(pf):
    # The raw text is 19 characters; "{{you}}" expands to a 9-character name, so the expanded text is 21 and must fail
    req = request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": False, "max_chars": 20})
    text = "{{you}} " + "x" * 11
    assert len(text) == 19
    assert reason_of(pf, reply(text), req, names={"you": "Alexander"}) == "length"


def test_step5_three_sentences_pass(pf):
    assert reason_of(pf, reply("Quak. Quak. Quak.")) == "ok"


def test_step5_two_sentences_pass(pf):
    assert reason_of(pf, reply("Quak. Quak quak.")) == "ok"


def test_step5_nine_sentences_reject(pf):
    assert reason_of(pf, reply("Quak. Quak. Quak. Quak. Quak. Quak. Quak. Quak. Quak.")) == "sentences"


def test_step5_inline_code_and_backticks_pass(pf):
    assert reason_of(pf, reply("Try `print(1)`.")) == "ok"


# ----- filter step 6: number grounding (only with a stat word) --------------------------------------------------------

def test_step6_grounded_number_with_stat_word_passes(pf):
    assert reason_of(pf, reply("You are at level 4, quak.")) == "ok"


def test_step6_ungrounded_number_with_stat_word_rejects(pf):
    assert reason_of(pf, reply("You are at pond level 7, quak.")) == "number"


def test_step6_ungrounded_count_with_week_rejects(pf):
    assert reason_of(pf, reply("You sent 812 messages this week.")) == "number"


def test_step6_ungrounded_number_word_with_stat_word_rejects(pf):
    assert reason_of(pf, reply("One wing this week, quak.")) == "number"


def test_step6_number_word_grounded_by_card_passes(pf):
    resp = reply("Top three this week, quak.", flags={"mentions_rank": True})
    assert reason_of(pf, resp, private=True) == "ok"          # "three" is 3, and the card has 3 (top_slots)


def test_step6_ordinary_numbers_and_dates_pass_without_stat_word(pf):
    assert reason_of(pf, reply("Bruce Lee was born in 1940, quak.")) == "ok"


def test_step6_placeholders_are_not_read_as_numbers(pf):
    assert reason_of(pf, reply("{{top1}} is the most active, quak."), names=NAMES) == "ok"


# ----- filter step 7: reward and program lint, claims, URLs ----------------------------------------------------------

def test_step7_strong_reward_word_rejects_while_unannounced(pf):
    assert reason_of(pf, reply("Winners get a sticker, quak.")) == "reward"
    assert reason_of(pf, reply("Prizes are coming, quak.")) == "reward"


def test_step7_strong_reward_word_allowed_once_announced(pf):
    card = dict(CARD, rewards_announced=True)
    assert reason_of(pf, reply("Winners get a sticker, quak."), request(card=card),
                     user_text="what do winners get?") == "ok"


def test_step7_weak_win_word_allowed_in_chat_when_user_used_it(pf):
    assert reason_of(pf, reply("Bruce Lee wins, quak."), user_text="who wins?") == "ok"


def test_step7_weak_win_word_rejected_in_event_mode(pf):
    req = request(mode="event")
    assert reason_of(pf, reply("Bruce Lee wins, quak."), req, user_text="who wins?") == "reward"


def test_step7_weak_win_word_allowed_in_general_chat(pf):
    assert reason_of(pf, reply("Bruce Lee won, quak."), user_text="who is Bruce Lee?") == "ok"


def test_step7_weak_win_word_allowed_when_user_has_any_win_word(pf):
    assert reason_of(pf, reply("Bruce Lee won, quak."), user_text="win?") == "ok"
    assert reason_of(pf, reply("The winner is Bruce Lee, quak."), user_text="who is the winner?") == "ok"


def test_step7_strong_reward_word_still_rejected_in_chat_with_win_in_user_text(pf):
    assert reason_of(pf, reply("Stickers for the winner, quak."), user_text="who wins?") == "reward"


def test_step7_claims_are_always_rejected(pf):
    assert reason_of(pf, reply("You've earned a lot of respect, quak.")) == "claim"
    assert reason_of(pf, reply("Congrats on winning, quak."), user_text="winning?") == "claim"


def test_step7_you_won_the_prize_rejects(pf):
    assert reason_of(pf, reply("You won the prize, quak."), user_text="did I win?") == "reward"


@pytest.mark.parametrize("text", ["Ore per hour, quak.", "Orry says hi.", "LOADOUT is great.",
                                  "Requisitions are open.", "Track XP counts double."])
def test_step7_program_words_reject(pf, text):
    assert reason_of(pf, reply(text)) == "program"


def test_step7_generic_program_words_reject_outside_chat(pf):
    # "launch", "season" and "era" are ordinary words, so they are only blocked in modes that are not general chat
    assert reason_of(pf, reply("The launch is near, quak."), request(mode="event")) == "program"


def test_step7_generic_program_words_pass_in_general_chat(pf):
    resp = reply("Rocket launches are loud, quak.")
    assert reason_of(pf, resp, user_text="how loud is a rocket launch?") == "ok"


def test_step7_ordinary_loadout_development_channel_mention_passes(pf):
    assert reason_of(pf, reply("The loadout-development channel came up, quak.")) == "ok"


def test_step7_program_question_never_gets_a_spoken_answer(pf):
    # defense in depth: the input gate should have redirected, but if it slipped through the filter still refuses
    resp = reply("It launches on 2026-10-19, quak.")
    assert reason_of(pf, resp, user_text="When does LOADOUT launch?") == "program"


def test_step7_actual_loadout_launch_answer_is_still_rejected(pf):
    assert reason_of(pf, reply("When does LOADOUT launch?"), user_text="tell me a joke") == "program"


def test_general_chat_about_a_famous_person_passes(pf):
    resp = reply("Top fighter, born in 1940, and his kick was famous.")
    assert reason_of(pf, resp, user_text="who is bruce lee?") == "ok"


def test_step7_plain_chat_about_ducks_passes(pf):
    assert reason_of(pf, reply("Quak, the duck is here.")) == "ok"


@pytest.mark.parametrize("text", ["See example.com for more.", "Go to https://x.test now.", "www.example.org",
                                  "Look at http:x quak.", "watch youtu.be/abc quak", "try bit.ly/xyz quak",
                                  "t.co/abc quak", "goo.gl/maps quak", "see tinyurl.com quak", "join discord.gg/abc",
                                  "read mysite.org/blog quak", "docs.example.dev/path quak"])
def test_step7_urls_reject(pf, text):
    assert reason_of(pf, reply(text)) == "url"


@pytest.mark.parametrize("text", ["2.5 is a decimal, quak.", "Mr. Smith is here, quak.", "Quak, 3.14 is pi."])
def test_step7_plain_decimals_and_titles_pass(pf, text):
    assert reason_of(pf, reply(text)) == "ok"


# ----- filter step 8: rank lint ----------------------------------------------------------------------------------------

def test_step8_rank_word_without_flag_rejects(pf):
    assert reason_of(pf, reply("Rank is fine, quak.")) == "rank"


def test_step8_rank_with_flag_and_private_reply_passes(pf):
    resp = reply("Top three this week, quak.", flags={"mentions_rank": True})
    assert reason_of(pf, resp, private=True) == "ok"


def test_step8_rank_with_flag_but_public_reply_rejects(pf):
    resp = reply("Top three this week, quak.", flags={"mentions_rank": True})
    assert reason_of(pf, resp, private=False) == "rank_public"


def test_step8_hash_rank_public_rejects(pf):
    resp = reply("You are #3 this week.", flags={"mentions_rank": True})
    assert reason_of(pf, resp) == "rank_public"


def test_step8_hash_rank_without_flag_rejects(pf):
    assert reason_of(pf, reply("You are #3 this week.")) == "rank"


def test_step8_bare_standing_is_not_a_rank_word(pf):
    assert reason_of(pf, reply("Standing ovation for the duck, quak.")) == "ok"
    assert reason_of(pf, reply("Your standings are fine, quak.")) == "ok"


def test_step8_top_three_films_in_general_chat_passes(pf):
    assert reason_of(pf, reply("Top 3 Bruce Lee films, quak"), user_text="who is bruce lee?") == "ok"


def test_step8_top_three_next_to_a_community_stat_word_is_a_rank(pf):
    assert reason_of(pf, reply("Top 3 this week, quak.")) == "rank"


def test_step6_top_three_removed_from_stat_words_but_week_still_grounds_numbers(pf):
    card = dict(CARD, streak_days=1, top_slots=5)          # no 3 anywhere on the card
    assert reason_of(pf, reply("You are 3rd this week."), request(card=card)) == "number"
    assert reason_of(pf, reply("You are 1st this week."), request(card=card)) == "ok"


# ----- filter step 9: roast lint ---------------------------------------------------------------------------------------

def test_step9_roast_flag_rejects_when_not_allowed(pf):
    resp = reply("Your jokes are slow, quak.", flags={"roasted": True})
    assert reason_of(pf, resp) == "ok"
    assert resp["flags"]["roasted"] is False


def test_step9_roast_allowed_on_a_harmless_topic_passes(pf):
    req = request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": True, "max_chars": 300})
    assert reason_of(pf, reply("Your jokes are slow, quak.", flags={"roasted": True}), req) == "ok"


def test_step9_roast_on_appearance_rejects_even_when_allowed(pf):
    req = request(persona={"humor_level": 2, "german_allowed": True, "roast_allowed": True, "max_chars": 300})
    assert reason_of(pf, reply("Your hair is awful, quak.", flags={"roasted": True}), req) == "roast_topic"


def test_step9_no_roast_flag_means_no_roast_check(pf):
    assert reason_of(pf, reply("Your hair is nice, quak.")) == "ok"


# ----- filter step 10: threat lint ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["I will ban you, quak.", "Timeout for you.", "Ban the goose, quak."])
def test_step10_threat_words_reject(pf, text):
    assert reason_of(pf, reply(text)) == "threat"


def test_step10_exact_goose_joke_passes(pf):
    result = pf.check(reply(APPROVED_TIMEOUT_JOKE), request())
    assert result.ok is True and result.text == APPROVED_TIMEOUT_JOKE


def test_step10_goose_joke_with_any_change_rejects(pf):
    changed = APPROVED_TIMEOUT_JOKE[:-1] + "!"
    assert reason_of(pf, reply(changed)) == "threat"


def test_step10_goose_joke_aimed_at_a_name_rejects(pf):
    assert reason_of(pf, reply("{{you}}, one more goose and this duck puts itself in timeout."),
                     names=NAMES) == "threat"


# ----- filter step 11: idioms, profanity, insults ----------------------------------------------------------------------

def test_step11_banned_idiom_rejects(pf):
    assert reason_of(pf, reply("Lame duck energy, quak.")) == "idiom"


def test_step11_profanity_rejects(pf):
    assert reason_of(pf, reply("Damn, quak.")) == "profanity"


def test_step11_animal_insult_aimed_at_a_person_rejects(pf):
    assert reason_of(pf, reply("You are a silly goose.")) == "insult"


def test_step11_clean_praise_passes(pf):
    assert reason_of(pf, reply("Hella active, quak. Keep it up.")) == "ok"


def test_hell_is_an_ordinary_word(pf):
    assert reason_of(pf, reply("Hell of a fighter, quak.")) == "ok"


@pytest.mark.parametrize("text", ["That is stupid, quak.", "You are a moron, quak.", "Idiots, quak.",
                                  "Pathetic, quak.", "Quak, that is useless."])
def test_insult_words_are_always_banned(pf, text):
    assert reason_of(pf, reply(text), user_text="hi") == "insult"


def test_third_party_roast_with_a_user_name_is_rejected(pf):
    assert reason_of(pf, reply("Sam is dumb, quak."), user_text="tell Sam he is dumb") == "third_party"


def test_third_party_praise_with_a_user_name_passes(pf):
    assert reason_of(pf, reply("Sam is great, quak."), user_text="tell Sam he is dumb") == "ok"


def test_name_from_user_with_no_roast_word_passes(pf):
    assert reason_of(pf, reply("Bruce Lee is the best fighter, quak."), user_text="who is Bruce Lee?") == "ok"


def test_roast_word_in_a_different_sentence_from_the_name_passes(pf):
    assert reason_of(pf, reply("Bruce Lee was a legend. Lame jokes are not my thing, quak."),
                     user_text="who is Bruce Lee?") == "ok"


# ----- filter step 12: German only through placeholders ----------------------------------------------------------------

def test_step12_german_words_outside_placeholders_reject(pf):
    assert reason_of(pf, reply("Ich bin eine Ente.")) == "ok"


def test_step12_three_german_words_outside_placeholders_reject(pf):
    assert reason_of(pf, reply("Das ist eine Ente.")) == "german"


def test_step12_common_english_overlap_does_not_reject(pf):
    assert reason_of(pf, reply("The duck is here, quak.")) == "ok"


def test_step12_german_inside_placeholder_passes(pf):
    result = pf.check(reply("Quak {{de:quak}}."), request())
    assert result.ok is True
    assert result.text == "Quak Quak."
    assert result.de_ids == ["quak"]


def test_step12_german_placeholder_rejected_when_not_allowed(pf):
    req = request(persona={"humor_level": 2, "german_allowed": False, "roast_allowed": False, "max_chars": 300})
    assert reason_of(pf, reply("Quak {{de:quak}}."), req) == "german"


# ----- filter step 13: flag truth --------------------------------------------------------------------------------------

def test_step13_uses_german_true_without_placeholder_rejects(pf):
    resp = reply("Quak.", flags={"uses_german": True})
    assert reason_of(pf, resp) == "ok"
    assert resp["flags"]["uses_german"] is False


def test_step13_placeholder_without_uses_german_rejects(pf):
    resp = reply("Quak {{de:quak}}.", flags={"uses_german": False})
    assert reason_of(pf, resp) == "ok"
    assert resp["flags"]["uses_german"] is True


def test_step13_matching_flag_passes(pf):
    assert reason_of(pf, reply("Quak {{de:quak}}.", flags={"uses_german": True})) == "ok"


# ----- filter step 14: no repeat within recent texts -------------------------------------------------------------------

def test_step14_repeat_within_recent_texts_rejects(pf):
    assert reason_of(pf, reply("Quak, quak."), recent_texts=["quak, QUAK."]) == "repeat"


def test_step14_new_text_passes_with_recent_texts(pf):
    assert reason_of(pf, reply("Quak, quak."), recent_texts=["Something else."]) == "ok"


# ----- filter step 15: distress check on the final text ----------------------------------------------------------------

def test_step15_distress_phrase_rejects(pf):
    assert reason_of(pf, reply("Please talk to a trusted adult if you want to die.")) == "distress"


def test_step15_configured_distress_word_rejects():
    cfg = default_config()
    cfg.safety.distress_words = ["quack alarm"]
    assert reason_of(PostFilter(cfg, FakeLines()), reply("A quack alarm, quak.")) == "distress"


# ----- check never raises ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, [], 3, {"v": 1}, {"request_id": REQ_ID, "text": {"x": 1}}])
def test_check_never_raises_on_garbage(pf, bad):
    result = pf.check(bad, request())
    assert result.ok is False and result.reason


# ----- finalize ---------------------------------------------------------------------------------------------------------

def job(**over: Any) -> VoiceJob:
    fields: Dict[str, Any] = dict(request=request(), channel="C1", user="U1", thread_ts="1.0", message_ts="2.0",
                                  ephemeral=False, fallback="Quak {de:quak} fallback & more", mode="chat",
                                  kind="addressed")
    fields.update(over)
    return VoiceJob(**fields)


def ok(response):
    return VoiceResult(ok=True, response=response)


def test_finalize_success_posts_in_thread_with_reaction(pf):
    actions = finalize(job(), ok(reply("Quak {{de:quak}}.", reaction="duck")), pf, FakeLines())
    assert [a.kind for a in actions] == ["post", "reaction"]
    assert actions[0].channel == "C1" and actions[0].thread_ts == "1.0" and actions[0].text == "Quak Quak."
    assert actions[1].emoji == "duck" and actions[1].message_ts == "2.0" and actions[1].channel == "C1"


def test_finalize_no_reaction_when_filter_drops_it(pf):
    # a reply whose reaction is not allowlisted is rejected: the canned line goes out and no reaction is added
    actions = finalize(job(), ok(reply("Quak.", reaction="not_allowlisted")), pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]
    assert actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_no_reaction_without_message_ts(pf):
    actions = finalize(job(message_ts=""), ok(reply("Quak.", reaction="duck")), pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]


def test_finalize_ephemeral_delivery_is_private(pf):
    actions = finalize(job(ephemeral=True), ok(reply("Quak.")), pf, FakeLines())
    assert len(actions) == 1
    assert actions[0].kind == "ephemeral"
    assert actions[0].user == "U1" and actions[0].channel == "C1" and actions[0].thread_ts == "1.0"


def test_finalize_ephemeral_without_user_is_never_public(pf):
    assert finalize(job(ephemeral=True, user=""), ok(reply("Quak.")), pf, FakeLines()) == []
    assert finalize(job(ephemeral=True, user=""), VoiceResult(ok=False, reason="timeout"), pf, FakeLines()) == []


def test_finalize_voice_failure_posts_the_fallback(pf):
    actions = finalize(job(), VoiceResult(ok=False, reason="timeout"), pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]
    assert actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_junk_posts_the_fallback(pf):
    actions = finalize(job(), VoiceResult(ok=False, reason="junk"), pf, FakeLines())
    assert actions[0].kind == "post" and actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_filter_rejection_posts_the_fallback(pf):
    actions = finalize(job(), ok(reply("<!here> quak.")), pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]
    assert actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_filter_rejection_on_ephemeral_stays_private(pf):
    actions = finalize(job(ephemeral=True), ok(reply("<!here> quak.")), pf, FakeLines())
    assert [a.kind for a in actions] == ["ephemeral"]


def test_finalize_silent_posts_nothing(pf):
    actions = finalize(job(thinking_ts="3.0"), ok(reply("", action="silent")), pf, FakeLines())
    assert [a.kind for a in actions] == ["update"]
    assert actions[0].message_ts == "3.0" and actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_event_silent_remains_silent(pf):
    assert finalize(job(kind="event", mode="event"), ok(reply("", action="silent")), pf, FakeLines()) == []


def test_finalize_redirect_posts_the_canned_line(pf):
    actions = finalize(job(), ok(reply("", action="redirect")), pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]
    assert actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_recent_duplicate_posts_the_fallback(pf):
    actions = finalize(job(), ok(reply("Quak.")), pf, FakeLines(), recent_texts=["Quak."])
    assert actions[0].text == "Quak Quak fallback &amp; more"


def test_finalize_no_fallback_text_posts_nothing(pf):
    assert finalize(job(fallback=""), VoiceResult(ok=False, reason="timeout"), pf, FakeLines()) == []


def test_finalize_empty_fallback_resolves_addressed_thinking_message(pf):
    actions = finalize(job(fallback="", thinking_ts="3.0"), VoiceResult(ok=False, reason="timeout"), pf, FakeLines())
    assert [a.kind for a in actions] == ["update"]
    assert actions[0].message_ts == "3.0" and actions[0].text


def test_finalize_filter_is_not_repeated_when_result_is_supplied():
    class NoCheck:
        def check(self, *args, **kwargs):
            raise AssertionError("filter should not run again")

    actions = finalize(job(), ok(reply("Quak.")), NoCheck(), FakeLines(),
                       checked_result=FilterResult(ok=True, text="Quak."))
    assert [a.kind for a in actions] == ["post"]


def test_finalize_invalid_result_resolves_addressed_thinking_message(pf):
    actions = finalize(job(fallback="", thinking_ts="3.0"), "broken", pf, FakeLines())
    assert [a.kind for a in actions] == ["update"]
    assert actions[0].message_ts == "3.0" and actions[0].text


def test_finalize_unrenderable_fallback_resolves_addressed_thinking_message(pf):
    actions = finalize(job(fallback="Quak {de:missing}", thinking_ts="3.0"),
                       VoiceResult(ok=False, reason="timeout"), pf, FakeLines())
    assert [a.kind for a in actions] == ["update"]
    assert actions[0].message_ts == "3.0" and actions[0].text


def test_finalize_mention_token_survives_escaping(pf):
    actions = finalize(job(), ok(reply("Hi {{you}}, quak.")), pf, FakeLines(), names=NAMES)
    assert actions[0].text == "Hi <@U123>, quak."


def test_finalize_never_raises_on_broken_input(pf):
    # no request dict and no result: falls back to the canned line, never raises
    actions = finalize(job(request=None), None, pf, FakeLines())
    assert [a.kind for a in actions] == ["post"]
    # a result object of the wrong type: nothing is posted, and nothing raises
    assert finalize(job(), "not a result", pf, FakeLines()) == []  # type: ignore[arg-type]


def test_finalize_actions_are_action_objects(pf):
    actions = finalize(job(), ok(reply("Quak.")), pf, FakeLines())
    assert all(isinstance(a, Action) for a in actions)
