"""goose-lee v6 simulation: community-only, official channel only, no limits, heavy writers valued.

Levels are crumbs only and UNCAPPED, but v6 restores the organizer-star gate: reaching a level above GATE_LEVEL needs
at least one non-voided organizer star, so chat, thanks and helpful crumbs alone stop at PL.5. Everything is derived
from the scoring RULES below. Run: python tools/level_sim.py. The assertions at the bottom state both v6 properties:
the gate is required above PL.5, and once the gate is passed levels keep climbing with no cap.
"""
from __future__ import annotations

import math

WEEKS = 13                 # a program of at most 3 months
TARGET_TOP_WEEKS = 6.5     # DESIGN INPUT: the most dedicated contributor should reach level 12 at about 1.5 months.
                           # The shadow weeks measure whether real behavior matches; this file does not prove it.

GATE_LEVEL = 5             # v6: reaching a level above this needs at least one non-voided organizer star


def curve(x, a, b):
    """a * ln(1 + x / b): always increasing, never capped, each extra unit counts a little less."""
    return a * math.log(1 + x / b)


def chat(msgs, days):
    return curve(msgs, 8, 5) + 1.5 * min(days, 7)    # qualified messages + 1.5 per active day


def thanks(n):
    return curve(n, 6, 3)                           # thanks reactions from other people


def helpful(n):
    return curve(n, 14, 3)                          # helpful answers marked by the asker


STAR, BIG_STAR = 25, 60                              # organizer stars, no budgets and no limits


class Person:
    def __init__(self, name, days, msgs, thanks_n, helpful_n, stars=0.0, bigs=0.0):
        self.name = name
        self.msgs = msgs
        self.chat = chat(msgs, days)
        self.thanks = thanks(thanks_n)
        self.helpful = helpful(helpful_n)
        self.stars = stars * STAR + bigs * BIG_STAR
        self.weekly = self.chat + self.thanks + self.helpful + self.stars
        self.has_star = stars > 0 or bigs > 0        # v6 gate: one organizer star unlocks levels above GATE_LEVEL

    @property
    def star_share(self):
        return self.stars / self.weekly


PEOPLE = [
    Person("casual",                 days=3, msgs=5,    thanks_n=1,  helpful_n=0),
    Person("regular chatter",        days=5, msgs=40,   thanks_n=4,  helpful_n=1),
    Person("solid helper",           days=6, msgs=120,  thanks_n=10, helpful_n=6, stars=0.5),
    Person("builder",                days=4, msgs=40,   thanks_n=6,  helpful_n=2, stars=0.5, bigs=0.4),
    Person("heavy contributor",      days=7, msgs=200,  thanks_n=15, helpful_n=8, stars=1.5, bigs=0.5),
    Person("very active writer",     days=7, msgs=1000, thanks_n=10, helpful_n=2),
]
BY = {p.name: p for p in PEOPLE}
HEAVY = BY["heavy contributor"]
ACTIVE = BY["very active writer"]

# level table: levels are crumbs only. Above 12 each level costs 20 percent more. The v6 star gate is applied on top.
SHAPE = [0, 0.0119, 0.0278, 0.0556, 0.111, 0.175, 0.262, 0.373, 0.5, 0.651, 0.825, 1.0]   # levels 1..12
GROWTH_ABOVE_TOP = 1.2


def r5(x):
    return int(round(x / 5.0) * 5)


TOP_SCORE = r5(HEAVY.weekly * TARGET_TOP_WEEKS)


def threshold(level):
    if level <= 12:
        return r5(TOP_SCORE * SHAPE[level - 1])
    return r5(TOP_SCORE * GROWTH_ABOVE_TOP ** (level - 12))


def level_at(weeks, p):
    score = p.weekly * weeks
    return max(n for n in range(1, 41) if score >= threshold(n))


def weeks_to(level, p):
    return threshold(level) / p.weekly


def gated_level(weeks, p):
    """v6 gate: a person without an organizer star is capped at GATE_LEVEL; a starred person keeps climbing."""
    raw = level_at(weeks, p)
    return raw if p.has_star else min(raw, GATE_LEVEL)


if __name__ == "__main__":
    print("level thresholds (crumbs):")
    print("  " + "  ".join("L%d:%d" % (n, threshold(n)) for n in range(1, 17)))
    print()
    print("per-week crumbs from the rules")
    for p in PEOPLE:
        print("  %-22s weekly %6.1f   chat %5.1f  thanks %5.1f  helpful %5.1f  stars %5.1f  (stars are %3.0f%%)%s"
              % (p.name, p.weekly, p.chat, p.thanks, p.helpful, p.stars, p.star_share * 100,
                 "" if p.has_star else "  no star"))
    print()
    print("level held at week 4 / 6.5 / 9 / 13   (v6 gate: no star caps at PL.%d, a star makes levels uncapped)"
          % GATE_LEVEL)
    for p in PEOPLE:
        print("  %-22s" % p.name, "  ".join("%2d" % gated_level(w, p) for w in (4, 6.5, 9, 13)))
    print()
    print("weeks to level 12 from crumbs alone:", {p.name: round(weeks_to(12, p), 1) for p in PEOPLE})
    ranked = sorted(PEOPLE, key=lambda p: p.weekly, reverse=True)
    print("weekly top 3 by crumbs:", [p.name for p in ranked[:3]])
    print("lifetime crumbs after 13 weeks:", {p.name: round(p.weekly * WEEKS) for p in ranked})
    print("a 1,000 message week earns %.2f times what 50 messages earn (messages only)"
          % (curve(1000, 8, 5) / curve(50, 8, 5)))
    print("marginal crumbs of the 1,000th message in a week: %.4f" % (8 / (5 + 1000)))
    one_star = ACTIVE.weekly + STAR
    print("very active writer with one star a week: %.1f (solid helper: %.1f)" % (one_star, BY['solid helper'].weekly))

    # ---- claims, enforced ----
    assert chat(1001, 7) > chat(1000, 7), "there is no cap: every message still counts a little"
    assert 8 / (5 + 1000) > 0.005, "the 1,000th message must still be worth something"
    assert 1.8 < curve(1000, 8, 5) / curve(50, 8, 5) < 3.0, "volume must clearly count without dominating"
    assert ACTIVE.weekly > BY["regular chatter"].weekly * 1.5, "a very active writer must clearly beat a regular chatter"
    assert ACTIVE.weekly < HEAVY.weekly, "heavy contributors with stars still lead"
    assert one_star > BY["solid helper"].weekly, "one star a week must be able to lift a very active writer above a solid helper"
    assert 11.5 <= weeks_to(12, BY["solid helper"]) <= 13.5
    assert 11.0 <= weeks_to(12, BY["builder"]) <= 13.0
    assert 11.5 <= weeks_to(12, ACTIVE) <= 14.0
    assert level_at(WEEKS, BY["regular chatter"]) < 12, "a regular chatter must not reach level 12 inside 13 weeks"
    assert level_at(WEEKS, BY["casual"]) <= 6
    assert level_at(1, BY["casual"]) >= 2, "a new member sees level 2 within a week"

    # v6 property (a): above PL.5 an organizer star is required
    assert GATE_LEVEL == 5, "the gate sits above Pond Level 5"
    assert level_at(WEEKS, ACTIVE) > GATE_LEVEL, "without the gate the raw crumbs would climb past PL.5"
    assert gated_level(WEEKS, ACTIVE) == GATE_LEVEL, "without a star the very active writer stops at PL.5"
    assert gated_level(WEEKS, BY["regular chatter"]) == GATE_LEVEL, "without a star a regular chatter stops at PL.5"
    assert gated_level(13, ACTIVE) == GATE_LEVEL
    assert all(gated_level(w, BY["casual"]) == level_at(w, BY["casual"]) for w in (4, 6.5, 9, 13)), \
        "the gate never changes levels 1 to 5"

    # v6 property (b): once the gate is passed, levels are uncapped
    assert gated_level(WEEKS, BY["solid helper"]) > GATE_LEVEL, "one star opens levels above PL.5"
    assert gated_level(WEEKS, HEAVY) == level_at(WEEKS, HEAVY) > GATE_LEVEL, "a starred leader is never capped"
    assert level_at(20, HEAVY) > gated_level(WEEKS, HEAVY), "after the gate the leader keeps climbing with no cap"
    assert gated_level(20, BY["builder"]) > gated_level(WEEKS, BY["builder"]), \
        "a starred member keeps climbing with no cap"

    print()
    print("ALL ASSERTIONS PASSED")
