"""Escaping and the post-filter for voice replies. OWNER: agent C1 (safe-output). Behavior: docs/PLAN.md section 10.7.

The voice's reply is hostile input. Nothing it writes reaches Slack except through PostFilter.check, and every failure
falls back to a canned line. Canned lines are escaped too.
"""
from __future__ import annotations

import functools
import json
import logging
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from goose.config import Config
from goose.core import wordlists as W
from goose.core.lines import LineBank
from goose.core.safety import is_program_question
from goose.types import Action, FilterResult, VoiceJob, VoiceResult

log = logging.getLogger("goose.poster")

VOICE_VERSION = 1
DEFAULT_MAX_CHARS = 300
RESPONSE_KEYS = frozenset(["v", "request_id", "action", "text", "reaction", "flags"])
REQUIRED_KEYS = frozenset(["v", "request_id", "action", "text", "flags"])
FLAG_KEYS = frozenset(["roasted", "mentions_rank", "uses_german", "numbers_used"])
ACTIONS = frozenset(["speak", "silent", "redirect", "decline"])
PLACEHOLDER_KINDS = frozenset(["you", "top1", "top2", "top3"])

PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")
MENTION_RE = re.compile(r"^<@[UW][A-Z0-9]+>$")
SHORTCODE_RE = re.compile(r":([A-Za-z0-9_+\-]{1,40}):")
RANK_HASH_RE = re.compile(r"#\d+")
NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
FORBIDDEN_MARKUP_RE = re.compile(r"<!|<@|<#|<http|@(?=\w)", re.I)   # any @word is a mention attempt (eval case 26)
URL_RE = re.compile(
    r"https?:|www\.|\b(?:youtu\.be|bit\.ly|t\.co|goo\.gl|tinyurl\.com|discord\.gg)\b"
    r"|\b[a-z0-9-]+\.(?:com|org|net|io|dev|gg|app|me|co|xyz|gov|edu)\b"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*", re.I)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
CAPITAL_WORD_RE = re.compile(r"\b[A-Z][a-z]*\b|\b[A-Z]{2,}\b")

# Rank words need mentions_rank true and a private reply (plan 10.7 step 8).
RANK_WORDS = ["rank", "ranks", "ranked", "ranking", "rankings", "leaderboard",
              "top 3", "top three", "top 10", "top ten", "first place", "second place", "third place"]
# "top 3" and friends only count as a rank when the reply also talks about the community (a stat word), so
# "Top 3 Bruce Lee films" in general chat is fine.
RANK_TOP_PHRASES = ["top 3", "top three", "top 10", "top ten"]
# Words that, in one sentence with a name taken from the user's message, make the reply a roast or a threat.
THIRD_PARTY_TRIGGER_WORDS = ["roast", "roasts", "roasted", "burn", "burns", "burned", "trash", "ratio", "owned",
                             "lame", "ugly", "dumb", "stupid"]
# Capitalised words in a user message that are never treated as a name.
NAME_STOP_WORDS = frozenset([
    "the", "i", "i'm", "you", "a", "an", "is", "it", "who", "what", "why", "how", "when", "where", "tell", "hi", "hey",
    "yes", "no", "and", "but", "do", "does", "can", "he", "she", "they", "we", "this", "that", "quak", "duck", "goose",
    "loadout", "ok", "lol", "so", "are", "am", "my", "me", "your", "if", "or", "not", "of", "to", "in", "on", "at",
    "system", "roast", "roasts", "please", "ya", "ur",
])
# Program words, on top of wordlists.PROGRAM_WORDS_OUT (plan 10.7 step 7 names these too).
EXTRA_PROGRAM_WORDS = ["season", "seasons", "deadline", "deadlines", "eligible", "eligibility", "accepted", "era",
                       "launch", "launches", "launched", "launching"]
# Roast topics the voice must never use (plan 10.6 and 10.7 step 9).
ROAST_TOPICS = ["hair", "face", "looks", "look", "ugly", "body", "fat", "skinny", "tall", "short", "weight", "teeth",
                "skin", "outfit", "clothes", "school", "class", "teacher", "grade", "country", "accent", "language",
                "english", "german", "skill", "level", "score", "age", "old", "young", "baby", "kid", "kids",
                "messages", "message", "length", "spelling"]
ANIMAL_WORDS = ["goose", "gander", "pig", "cow", "donkey", "chicken", "sheep", "rat", "monkey", "ape", "snake",
                "worm", "dog", "cat", "bird"]
INSULT_RE = re.compile(r"\byou\b[^.!?]{0,20}?\b(?:" + "|".join(ANIMAL_WORDS) + r")\b|\bsilly\s+(?:goose|duck)\b",
                       re.IGNORECASE)

# The one approved goose-gag timeout joke (docs/APPENDIX_VOICE.md, T1). Only this exact text may carry a threat word.
# The line bank has no way to list lines by moment, so the approved text is held here.
APPROVED_TIMEOUT_JOKE = "One more goose and this duck puts itself in timeout. Nobody else is in trouble."

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
_GERMAN_SET = frozenset(w.lower() for w in W.GERMAN_STOPWORDS)
_EMOJI_ALLOWED = frozenset(W.VOICE_EMOJI_ALLOWLIST)


class _Fail(Exception):
    """Internal: a filter step rejected the reply. `reason` is a short code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def escape_text(text: str) -> str:
    """Escape & < > for Slack (& first)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def has_forbidden_markup(text: str) -> bool:
    """True for Slack control markup: <!, <@, <#, <http, and the words @channel, @here, @everyone. Plain & < > are not
    refused here: escape_text turns them into entities before anything is posted. Raw URLs are caught by the URL lint
    in PostFilter.check."""
    if not isinstance(text, str):
        return True
    return FORBIDDEN_MARKUP_RE.search(text) is not None


@functools.lru_cache(maxsize=None)
def _phrase_re(phrase: str) -> Any:
    parts = [re.escape(p) for p in phrase.lower().split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)")


def _first_word(text_lower: str, words: Iterable[str]) -> Optional[str]:
    for w in words:
        if _phrase_re(w).search(text_lower):
            return w
    return None


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF


def _bad_control(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co") and c != "\n" for c in text)


def _card_numbers(card: Any) -> set:
    text = json.dumps(card, sort_keys=True, default=str, ensure_ascii=False)
    nums = {int(x) for x in re.findall(r"\d+", text)}
    nums.update(NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in NUMBER_WORDS)
    return nums


def _user_text(request: Dict[str, Any]) -> str:
    try:
        return str(request["input"]["message"]["text"])
    except (KeyError, TypeError):
        return ""


def _user_names(user_text: str) -> List[str]:
    """Capitalised words of the user's own message, lowercased, minus duck words and common words. The reply must not
    use one of these names together with a roast or insult word (step 11)."""
    names: List[str] = []
    for word in CAPITAL_WORD_RE.findall(user_text or ""):
        low = word.lower()
        if len(low) >= 2 and low not in NAME_STOP_WORDS and low not in names:
            names.append(low)
    return names


def _name_token(names: Optional[Dict[str, str]], kind: str) -> str:
    value = None
    if names:
        value = names.get(kind) or names.get("{{%s}}" % kind)
    if not value:
        raise _Fail("name")
    value = str(value)
    if MENTION_RE.match(value):
        return value                       # a Slack mention token the gateway built; never escaped again
    return escape_text(value)


def _escape_literal(text: str) -> str:
    if "{" in text or "}" in text:
        raise _Fail("placeholder")
    return escape_text(text)


def _unique(items: List[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def render_canned(text: str, lines: LineBank) -> str:
    """Expand {de:id} placeholders and escape. Used for every canned line before posting."""
    normalized = lines.normalize_placeholders(text or "")
    return escape_text(lines.expand(normalized))


class PostFilter:
    def __init__(self, config: Config, lines: LineBank):
        self.config = config
        self.lines = lines
        extra: List[str] = []
        safety = getattr(config, "safety", None)
        if safety is not None:
            extra = [str(w).lower() for w in (getattr(safety, "distress_words", None) or [])]
        self._distress = list(W.DISTRESS_WORDS) + extra
        self._program = list(W.PROGRAM_WORDS_OUT)

    def check(self, response: Dict[str, Any], request: Dict[str, Any], *, user_text: str = "",
              names: Optional[Dict[str, str]] = None, private: bool = False,
              recent_texts: Optional[List[str]] = None) -> FilterResult:
        """Validate a voice response against the request. `names` maps placeholders {{you}} {{top1..3}} to Slack mention
        tokens (built by the gateway; may be empty). All 15 steps of PLAN section 10.7, in order:
        strict JSON shape (v, request_id match, allowed keys: v request_id action text reaction flags);
        action handling (silent -> ok with empty text; redirect/decline -> caller posts canned);
        placeholders only of kinds {{you}} {{top1}} {{top2}} {{top3}} {{de:<id>}} with approved ids;
        forbidden markup; emoji allowlist (wordlists.VOICE_EMOJI_ALLOWLIST, max 2, in text and `reaction`);
        length (request persona.max_chars, default 300) and at most two sentences, no code or backticks;
        number grounding when a STAT word is present (request['card'] numbers only) - ordinary numbers are fine in
        general chat when no stat word is present;
        reward/program lint (strong reward words always while rewards unannounced; weak win words only allowed in
        chat mode when user_text contains them; CLAIM_PATTERNS always rejected; PROGRAM_WORDS_OUT; URLs);
        rank lint (rank/place/top/#n needs flags.mentions_rank true and a private reply);
        roast lint (flags.roasted must be false unless persona.roast_allowed);
        threat lint (THREAT_WORDS rejected unless the exact goose joke);
        banned idioms and profanity;
        German only via placeholders (any GERMAN_STOPWORDS word outside a placeholder rejects);
        flag truth (uses_german must equal placeholder presence);
        no identical text posted in the same channel in the last 24 hours (`recent_texts`, from the caller);
        the distress check runs again on the final text.
        On success: FilterResult(ok=True, text=<escaped, placeholders expanded>, reaction=<allowlisted or ''>, de_ids=[...]).
        On failure: FilterResult(ok=False, reason=<short code like 'markup', 'length', 'number', 'reward'>).
        Never raises."""
        try:
            return self._check(response, request, user_text, names, private, recent_texts)
        except _Fail as failure:
            return FilterResult(ok=False, reason=failure.reason)
        except Exception:
            log.warning("poster: filter error, reply rejected")
            return FilterResult(ok=False, reason="internal")

    def _check(self, response: Any, request: Any, user_text: str, names: Optional[Dict[str, str]],
               private: bool, recent_texts: Optional[List[str]]) -> FilterResult:
        req = request if isinstance(request, dict) else {}
        persona = req.get("persona") if isinstance(req.get("persona"), dict) else {}
        card = req.get("card") if isinstance(req.get("card"), dict) else {}
        private = bool(private) or req.get("delivery") == "private"

        # 1. strict JSON shape
        if not isinstance(response, dict):
            raise _Fail("json")
        keys = set(response.keys())
        if keys - RESPONSE_KEYS or REQUIRED_KEYS - keys:
            raise _Fail("shape")
        version = req.get("v", VOICE_VERSION)
        if type(response["v"]) is not int or response["v"] != version or version != VOICE_VERSION:
            raise _Fail("version")
        rid = req.get("request_id")
        if not isinstance(rid, str) or not rid or response["request_id"] != rid:
            raise _Fail("request_id")
        action = response["action"]
        text = response["text"]
        if action not in ACTIONS or not isinstance(text, str):
            raise _Fail("shape")
        reaction = response.get("reaction")
        if reaction is not None and not isinstance(reaction, str):
            raise _Fail("shape")
        flags = response["flags"]
        if not isinstance(flags, dict) or set(flags) != FLAG_KEYS:
            raise _Fail("shape")
        if not all(isinstance(flags[k], bool) for k in ("roasted", "mentions_rank", "uses_german")):
            raise _Fail("shape")
        if not isinstance(flags["numbers_used"], list):
            raise _Fail("shape")

        # 2. action handling
        if action == "silent":
            return FilterResult(ok=True, text="", reaction="", de_ids=[])
        if action in ("redirect", "decline"):
            raise _Fail(action)
        if not text.strip():
            raise _Fail("empty")

        # 2. placeholders: only the listed kinds, approved German ids only
        approved = set(self.lines.german_ids())
        for m in PLACEHOLDER_RE.finditer(text):
            kind = m.group(1).strip()
            if kind.startswith("de:"):
                if kind[3:].strip() not in approved:
                    raise _Fail("placeholder")
            elif kind not in PLACEHOLDER_KINDS:
                raise _Fail("placeholder")
        plain = PLACEHOLDER_RE.sub(" ", text)
        if "{" in plain or "}" in plain:
            raise _Fail("placeholder")
        plain_l = plain.lower()
        user_l = user_text.lower() if isinstance(user_text, str) else ""

        # 3. forbidden markup and control characters
        if has_forbidden_markup(text):
            raise _Fail("markup")
        if _bad_control(text):
            raise _Fail("control")

        # 4. emoji allowlist, at most two (text shortcodes and the reaction together)
        if any(_is_emoji_char(ch) for ch in plain):
            raise _Fail("emoji")
        shortcodes = [m.lower() for m in SHORTCODE_RE.findall(plain)]
        if any(code not in _EMOJI_ALLOWED for code in shortcodes):
            raise _Fail("emoji")
        if reaction and reaction not in _EMOJI_ALLOWED:
            raise _Fail("emoji")
        if len(shortcodes) + (1 if reaction else 0) > 2:
            raise _Fail("emoji")

        # 5. code, length after expansion, at most two sentences
        if "`" in text:
            raise _Fail("code")
        final = self._build(text, names)
        max_chars = persona.get("max_chars", DEFAULT_MAX_CHARS)
        if type(max_chars) is not int:
            max_chars = DEFAULT_MAX_CHARS
        if len(final) > max_chars:
            raise _Fail("length")
        sentences = [s for s in SENTENCE_SPLIT_RE.split(final.strip()) if s.strip()]
        if len(sentences) > 2:
            raise _Fail("sentences")

        # 6. number grounding, only when a stat word is present
        if _first_word(plain_l, W.STAT_WORDS):
            values = [int(x.replace(",", "")) for x in NUMBER_RE.findall(plain)]
            values += [NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", plain_l) if w in NUMBER_WORDS]
            allowed = _card_numbers(card)
            if any(v not in allowed for v in values):
                raise _Fail("number")

        # 7. reward and program lint, claims, URLs
        if card.get("rewards_announced") is not True:
            if _first_word(plain_l, W.REWARD_CORE):
                raise _Fail("reward")
            generic = _first_word(plain_l, W.REWARD_GENERIC)
            if generic and not (req.get("mode") == "chat" and _phrase_re(generic).search(user_l)):
                raise _Fail("reward")
        for pattern in W.CLAIM_PATTERNS:
            if re.search(pattern, plain_l):
                raise _Fail("claim")
        # weak win words are fine in general chat when the user's own message has a word starting with "win"
        user_has_win = re.search(r"\bwin\w*", user_l) is not None
        for word in W.REWARD_WEAK:
            if _phrase_re(word).search(plain_l):
                if not (req.get("mode") == "chat" and user_has_win):
                    raise _Fail("reward")
        if _first_word(plain_l, self._program):
            raise _Fail("program")
        if req.get("mode") != "chat" and _first_word(plain_l, EXTRA_PROGRAM_WORDS):
            raise _Fail("program")
        # Defense in depth: the input gate sends program questions to a fixed redirect and never calls the voice.
        # If one slips through, a spoken answer is rejected so the voice can never state program facts.
        if user_text and is_program_question(user_text):
            raise _Fail("program")
        if URL_RE.search(plain_l):
            raise _Fail("url")

        # 8. rank lint ("top N" counts only next to a community stat word)
        plain_rank = [w for w in RANK_WORDS if w not in RANK_TOP_PHRASES]
        top_phrase = _first_word(plain_l, RANK_TOP_PHRASES)
        if (_first_word(plain_l, plain_rank) or (top_phrase and _first_word(plain_l, W.STAT_WORDS))
                or RANK_HASH_RE.search(plain)):
            if flags["mentions_rank"] is not True:
                raise _Fail("rank")
            if not private:
                raise _Fail("rank_public")

        # 9. roast lint
        if flags["roasted"]:
            if persona.get("roast_allowed") is not True:
                raise _Fail("roast")
            if _first_word(plain_l, ROAST_TOPICS):
                raise _Fail("roast_topic")

        # 10. threat lint: only the exact approved goose joke may carry a threat word
        if _first_word(plain_l, W.THREAT_WORDS) and text.strip() != APPROVED_TIMEOUT_JOKE:
            raise _Fail("threat")

        # 11. banned idioms, profanity, insults, third-party roasts, animal insults aimed at a person
        if _first_word(plain_l, W.BANNED_IDIOMS):
            raise _Fail("idiom")
        if _first_word(plain_l, W.PROFANITY):
            raise _Fail("profanity")
        user_names = _user_names(user_text if isinstance(user_text, str) else "")
        if user_names:
            for sentence in SENTENCE_SPLIT_RE.split(plain_l):
                if _first_word(sentence, user_names) and _first_word(sentence, THIRD_PARTY_TRIGGER_WORDS + W.INSULT_WORDS):
                    raise _Fail("third_party")
        if _first_word(plain_l, W.INSULT_WORDS):
            raise _Fail("insult")
        if INSULT_RE.search(plain):
            raise _Fail("insult")

        # 12. German only through placeholders
        de_ids = _unique([m.group(1).strip()[3:].strip() for m in PLACEHOLDER_RE.finditer(text)
                          if m.group(1).strip().startswith("de:")])
        tokens = re.findall(r"[^\W\d_]+", plain_l)
        if any(t in _GERMAN_SET for t in tokens):
            raise _Fail("german")
        if de_ids and persona.get("german_allowed") is not True:
            raise _Fail("german")

        # 13. flag truth
        if flags["uses_german"] != bool(de_ids):
            raise _Fail("flags")

        # 14. no repeat within the channel's recent texts
        if recent_texts:
            seen = {t.strip().casefold() for t in recent_texts if isinstance(t, str)}
            if final.strip().casefold() in seen:
                raise _Fail("repeat")

        # 15. distress check again, on the final text
        if _first_word(final.lower(), self._distress):
            raise _Fail("distress")

        return FilterResult(ok=True, text=final, reaction=reaction or "", de_ids=de_ids)

    def _build(self, text: str, names: Optional[Dict[str, str]]) -> str:
        """Escape the literal model text, then insert mention tokens, then expand German. Order matters."""
        pieces: List[str] = []
        pos = 0
        for m in PLACEHOLDER_RE.finditer(text):
            pieces.append(_escape_literal(text[pos:m.start()]))
            kind = m.group(1).strip()
            if kind.startswith("de:"):
                pieces.append(self.lines.expand("{de:%s}" % kind[3:].strip()))
            else:
                pieces.append(_name_token(names, kind))
            pos = m.end()
        pieces.append(_escape_literal(text[pos:]))
        return "".join(pieces)


def finalize(job: VoiceJob, result: VoiceResult, post_filter: PostFilter, lines: LineBank,
             names: Optional[Dict[str, str]] = None, recent_texts: Optional[List[str]] = None) -> List[Action]:
    """Turn a finished voice job into Slack actions. Voice ok and filter ok -> a post (thread_ts from the job, or an
    ephemeral action when job.ephemeral) plus an optional reaction action. Anything else -> the job's canned
    fallback, expanded and escaped, posted the same way. action 'silent' -> []. Never raises."""
    try:
        return _finalize(job, result, post_filter, lines, names, recent_texts)
    except Exception:
        log.warning("poster: finalize failed, nothing posted")
        return []


def _finalize(job: VoiceJob, result: Optional[VoiceResult], post_filter: PostFilter, lines: LineBank,
              names: Optional[Dict[str, str]], recent_texts: Optional[List[str]]) -> List[Action]:
    request = job.request if isinstance(job.request, dict) else {}
    if result is None or not result.ok or not isinstance(result.response, dict):
        return _fallback(job, lines, (result.reason if result is not None else "") or "voice")
    fr = post_filter.check(result.response, request, user_text=_user_text(request), names=names,
                           private=bool(job.ephemeral), recent_texts=recent_texts)
    if not fr.ok:
        return _fallback(job, lines, fr.reason)
    if result.response.get("action") == "silent":
        return []
    return _deliver(job, fr.text, fr.reaction)


def _fallback(job: VoiceJob, lines: LineBank, reason: str) -> List[Action]:
    log.info("poster: canned fallback (%s)", reason)
    if not job.fallback:
        return []
    text = render_canned(job.fallback, lines)
    if not text.strip():
        return []
    return _deliver(job, text, "")


def _deliver(job: VoiceJob, text: str, reaction: str) -> List[Action]:
    if job.ephemeral:
        if not job.user:
            return []                      # a private reply with no recipient is never posted publicly
        actions = [Action(kind="ephemeral", channel=job.channel, user=job.user, text=text, thread_ts=job.thread_ts)]
    else:
        actions = [Action(kind="post", channel=job.channel, text=text, thread_ts=job.thread_ts)]
    if reaction and job.message_ts:
        actions.append(Action(kind="reaction", channel=job.channel, message_ts=job.message_ts, emoji=reaction))
    return actions
