"""Gates: distress, sad, program-question, text helpers. OWNER: agent B (vibes). Lists are in core/wordlists.py.

All matching is whole-word, case-insensitive; multi-word entries match as phrases; punctuation is ignored.
"""
from __future__ import annotations

import re
from typing import Iterable, List

from goose.config import Config
from goose.core import wordlists

_NON_WORD_RE = re.compile(r"[^\w]+")


def _norm(text: str) -> str:
    """Lowercase, every run of non-word characters (punctuation, hyphens, apostrophes) becomes one space."""
    return " " + " ".join(_NON_WORD_RE.sub(" ", (text or "").lower()).split()) + " "


def find_words(text: str, words: Iterable[str]) -> List[str]:
    """Entries of `words` that occur in `text` as whole words or phrases (case-insensitive). Returns the matches."""
    padded = _norm(text)
    found: List[str] = []
    for entry in words:
        needle = _norm(entry)
        if needle.strip() and needle in padded:
            found.append(entry)
    return found


def contains_word(text: str, words: Iterable[str]) -> bool:
    return bool(find_words(text, words))


def is_distress(text: str, config: Config) -> bool:
    """Built-in DISTRESS_WORDS plus config.safety.distress_words."""
    words = list(wordlists.DISTRESS_WORDS) + list(config.safety.distress_words)
    return contains_word(text, words)


def is_sad(text: str, config: Config) -> bool:
    """Distress, or any SAD_WORDS / config.safety.sad_words. Used to suppress reactions, roasts and jokes."""
    if is_distress(text, config):
        return True
    words = list(wordlists.SAD_WORDS) + list(config.safety.sad_words)
    return contains_word(text, words)


def is_program_question(text: str) -> bool:
    """True for PROGRAM_STRONG_PATTERNS, or a PROGRAM_WEAK_PATTERNS match together with a PROGRAM_CONTEXT_PATTERNS
    match. 'launch a rocket' is False; 'when does loadout launch' is True; 'how much ore per hour' is True."""
    low = (text or "").lower()
    for pattern in wordlists.PROGRAM_STRONG_PATTERNS:
        if re.search(pattern, low):
            return True
    weak = any(re.search(p, low) for p in wordlists.PROGRAM_WEAK_PATTERNS)
    if not weak:
        return False
    return any(re.search(p, low) for p in wordlists.PROGRAM_CONTEXT_PATTERNS)


def safety_reply(config: Config) -> str:
    """The plain safety text: says it is a bot, points to a trusted adult and the organizers (config.safety.contact_text).
    No joke, no German, no emoji."""
    return "I'm a bot, not a person, so I can't help with this one. " + config.safety.contact_text
