"""Tests for goose/core/vibes.py: word reactions, name and ping handling, budgets, roasts, ranking remarks."""
from __future__ import annotations

import pytest

from goose.core.vibes import Vibes
from goose.types import Event

HOUR = 3600
DAY = 86400


class Sure:
    """Every chance roll passes (random() is 0.0) and choice() takes the first item."""

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


class Never:
    """Every chance roll fails (random() is 0.999)."""

    def random(self):
        return 0.999

    def choice(self, seq):
        return seq[0]


@pytest.fixture
def vibes(db, cfg, clock, lines):
    return Vibes(db, cfg, clock, lines, Sure())


def react(v, text, user="U1", now=None, quiet=False, sad=False):
    return v.reaction_for(text, user, now if now is not None else v.clock.now(), quiet=quiet, sad=sad)


# ---- word reactions -----------------------------------------------------------------------------------------

def test_gm_inside_gmail_does_not_fire(vibes):
    assert react(vibes, "check my gmail inbox") is None
    assert react(vibes, "gm everyone") == "sunrise"


def test_good_morning_fires_only_before_noon_local(vibes, clock):
    assert clock.hour_of_day(clock.now()) < 12                 # the fixture starts at 08:53 UTC
    assert react(vibes, "good morning pond") == "sunrise"
    clock.advance(4 * HOUR)                                    # 12:53
    assert react(vibes, "good morning pond", user="U2") is None
    assert react(vibes, "gm", user="U3") is None


def test_shipped_fires_tada_and_plain_words_do_not(vibes, clock):
    assert react(vibes, "I shipped it") == "tada"
    assert react(vibes, "we launched the new page", user="U5") == "tada"
    assert react(vibes, "it finally works", user="U2") is None
    assert react(vibes, "toast", user="U3") is None
    assert react(vibes, "that was released yesterday", user="U4") is None   # needs I or we


def test_other_word_triggers(vibes, clock):
    assert react(vibes, "congrats on the release", user="U1") == "partying_face"
    assert react(vibes, "lots of feathers today", user="U2") == "feather"
    assert react(vibes, "good night all", user="U3") == "zzz"
    assert react(vibes, "moin moin", user="U4") == "wave"
    assert react(vibes, "tschüss for now", user="U5") == "wave"
    assert react(vibes, "tschuess for now", user="U6") == "wave"


def test_birthday_is_skipped_near_death_or_missing(vibes, clock):
    assert react(vibes, "happy birthday pond") == "birthday"
    assert react(vibes, "happy birthday, I miss this pond", user="U2") is None
    assert react(vibes, "happy birthday, we miss you", user="U3") is None
    assert react(vibes, "happy birthday, my friend died last year", user="U4") is None


def test_bread_only_near_duck(vibes, clock):
    assert react(vibes, "bread") is None
    assert react(vibes, "bread for the duck", user="U2") == "bread"
    # the duck word is far from bread, so only the plain duck trigger fires
    assert react(vibes, "bread one two three four five six seven eight duck", user="U3") == "duck"


def test_duck_word_fires_but_name_word_is_not_a_word_trigger(vibes, clock):
    assert react(vibes, "quack quack", user="U1") == "duck"
    assert react(vibes, "look at this duck", user="U2") == "duck"
    assert react(vibes, "goose-lee said hi", user="U3") is None


def test_one_reaction_per_message_uses_the_first_match(vibes, clock):
    assert react(vibes, "I shipped it, congrats everyone") == "tada"


def test_plain_text_gets_no_reaction(vibes, clock):
    assert react(vibes, "the weather is fine and the code compiles") is None


def test_sad_or_quiet_suppresses_word_reactions(vibes, clock):
    assert react(vibes, "I shipped it", sad=True) is None
    assert react(vibes, "I shipped it", quiet=True, user="U2") is None
    assert react(vibes, "I shipped it", user="U3") == "tada"


def test_random_chance_can_skip_a_match(db, cfg, clock, lines):
    v = Vibes(db, cfg, clock, lines, Never())
    assert react(v, "I shipped it") is None


def test_hourly_budget_of_six_reactions(vibes, clock):
    got = [react(vibes, "quack", user="U%d" % i) for i in range(1, 8)]
    assert got[:6] == ["duck"] * 6
    assert got[6] is None
    clock.advance(HOUR + 1)
    assert react(vibes, "quack", user="U9") == "duck"


def test_per_person_gap_for_reactions(vibes, clock):
    assert react(vibes, "quack", user="U1") == "duck"
    clock.advance(60)
    assert react(vibes, "quack", user="U1") is None
    clock.advance(600)
    assert react(vibes, "quack", user="U1") == "duck"


# ---- names and pings ----------------------------------------------------------------------------------------

def test_name_reaction_only_for_the_name(vibes, clock):
    assert vibes.name_reaction("hey goose-lee", "U1", clock.now(), quiet=False, sad=False) == "duck"
    assert vibes.name_reaction("you are a goose", "U2", clock.now(), quiet=False, sad=False) is None
    assert vibes.name_reaction("hey goose-lee", "U3", clock.now(), quiet=True, sad=False) is None
    assert vibes.name_reaction("hey goose-lee", "U4", clock.now(), quiet=False, sad=True) is None


def test_name_reaction_once_per_person_per_ten_minutes(vibes, clock):
    now = clock.now()
    assert vibes.name_reaction("hey goose-lee", "U1", now, quiet=False, sad=False) == "duck"
    assert vibes.name_reaction("goose-lee again", "U1", now + 120, quiet=False, sad=False) is None
    assert vibes.name_reaction("goose-lee again", "U1", now + 601, quiet=False, sad=False) == "duck"


def test_name_reaction_respects_chance(db, cfg, clock, lines):
    v = Vibes(db, cfg, clock, lines, Never())
    assert v.name_reaction("hey goose-lee", "U1", clock.now(), quiet=False, sad=False) is None


def test_ack_is_allowed_once_per_person_per_gap(vibes, clock):
    assert vibes.ack_allowed("U1", clock.now()) is True
    assert vibes.ack_allowed("U1", clock.now() + 60) is False
    assert vibes.ack_allowed("U2", clock.now() + 60) is True
    assert vibes.ack_allowed("U1", clock.now() + 601) is True


def test_classify_addressing(vibes, clock):
    now = clock.now()
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="<@UBOT> hi"), "UBOT") == "ping"
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="hi", bot_mentioned=True), "UBOT") == "ping"
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="hey goose-lee"), "UBOT") == "name"
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="<@UBOT> ask orry"), "UBOT") == "orry"
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="goose-lee, ask Orry"), "UBOT") == "orry"
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="a goose stole my bread"), "UBOT") == ""
    assert vibes.classify_addressing(Event(kind="message", ts=now, text="ask orry about it"), "UBOT") == ""


def test_set_not_funny_records_a_stop(vibes, db):
    assert vibes.set_not_funny("U1") is True
    rows = db.query("SELECT kind FROM remark_log WHERE user_id='U1'")
    assert [r["kind"] for r in rows] == ["stop"]


# ---- roasts -------------------------------------------------------------------------------------------------

def test_roast_for_four_exclamations_uses_the_count(vibes, clock):
    line = vibes.roast_template("that was great!!!!", "U1", clock.now(), quiet=False, sad=False, newcomer=False)
    assert line == "That message had 4 exclamation marks. The pond felt it."


def test_roast_for_three_lol(vibes, clock):
    line = vibes.roast_template("lol lol lol that is funny", "U1", clock.now(), quiet=False, sad=False,
                                newcomer=False)
    assert line and "lol" in line and "{" not in line


def test_no_roast_without_a_trigger(vibes, clock):
    assert vibes.roast_template("nice day for bread!!", "U1", clock.now(), quiet=False, sad=False,
                                newcomer=False) is None
    assert vibes.roast_template("lol lol nice", "U1", clock.now(), quiet=False, sad=False, newcomer=False) is None


def test_roast_never_for_newcomers_sad_quiet_or_pile_ons(vibes, clock):
    text = "wow!!!!"
    now = clock.now()
    assert vibes.roast_template(text, "U1", now, quiet=True, sad=False, newcomer=False) is None
    assert vibes.roast_template(text, "U2", now, quiet=False, sad=True, newcomer=False) is None
    assert vibes.roast_template(text, "U3", now, quiet=False, sad=False, newcomer=True) is None
    assert vibes.roast_template(text, "U4", now, quiet=False, sad=False, newcomer=False,
                                others_teasing=3) is None
    assert vibes.roast_template("I feel alone!!!!", "U5", now, quiet=False, sad=True, newcomer=False) is None


def test_roast_at_most_once_per_day_per_person(vibes, clock):
    assert vibes.roast_template("wow!!!!", "U1", clock.now(), quiet=False, sad=False, newcomer=False)
    clock.advance(3600)
    assert vibes.roast_template("wow!!!!", "U1", clock.now(), quiet=False, sad=False, newcomer=False) is None
    clock.advance(DAY)
    assert vibes.roast_template("wow!!!!", "U1", clock.now(), quiet=False, sad=False, newcomer=False)


def test_roast_roll_can_refuse(db, cfg, clock, lines):
    v = Vibes(db, cfg, clock, lines, Never())
    assert v.roast_template("wow!!!!", "U1", clock.now(), quiet=False, sad=False, newcomer=False) is None


# ---- ranking remarks ----------------------------------------------------------------------------------------

@pytest.fixture
def sure_rank(db, cfg, clock, lines):
    cfg.vibes.rank_remark_chance = 1.0
    return Vibes(db, cfg, clock, lines, Sure())


def test_rank_remark_only_for_active_or_very_active_shown_people(sure_rank, clock):
    now = clock.now()
    ok = dict(quiet=False, shown=True, resting=False)
    assert sure_rank.rank_remark_allowed("U1", now, band="active", **ok) is True
    assert sure_rank.rank_remark_allowed("U2", now, band="very_active", **ok) is True
    assert sure_rank.rank_remark_allowed("U3", now, band="steady", **ok) is False
    assert sure_rank.rank_remark_allowed("U4", now, band="quiet", **ok) is False
    assert sure_rank.rank_remark_allowed("U5", now, band=None, **ok) is False
    assert sure_rank.rank_remark_allowed("U6", now, band="active", quiet=True, shown=True, resting=False) is False
    assert sure_rank.rank_remark_allowed("U7", now, band="active", quiet=False, shown=False, resting=False) is False
    assert sure_rank.rank_remark_allowed("U8", now, band="active", quiet=False, shown=True, resting=True) is False


def test_rank_remark_at_most_once_a_day_per_person(sure_rank, clock):
    now = clock.now()
    assert sure_rank.rank_remark_allowed("U1", now, band="active", quiet=False, shown=True, resting=False)
    assert not sure_rank.rank_remark_allowed("U1", now + 3600, band="active", quiet=False, shown=True,
                                             resting=False)
    assert sure_rank.rank_remark_allowed("U1", now + DAY + 1, band="active", quiet=False, shown=True,
                                         resting=False)


def test_rank_remark_roll_can_refuse(db, cfg, clock, lines):
    cfg.vibes.rank_remark_chance = 0.5
    v = Vibes(db, cfg, clock, lines, Never())
    assert v.rank_remark_allowed("U1", clock.now(), band="active", quiet=False, shown=True, resting=False) is False


def test_event_remarks_are_limited_per_hour_and_day(vibes, clock):
    now = clock.now()
    got = [vibes.event_remark_allowed(now + i) for i in range(6)]
    assert got == [True, True, True, True, False, False]        # voice.event_remarks_per_hour is 4
    clock.advance(HOUR + 10)
    assert vibes.event_remark_allowed(clock.now()) is True
