"""Text rules: message qualification, near-duplicate hashing, mention and name detection. OWNER: agent B (vibes).

Pure functions, no database. Slack markup: <@U123> or <@U123|name> users, <#C123|chan> channels, <http://x|label> links,
<!here> specials, :emoji:, `code`, ```code blocks```, > quotes, &amp; &lt; &gt; entities.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable, List

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_ANGLE_RE = re.compile(r"<[^<>\n]*>")
_RAW_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMOJI_RE = re.compile(r":[A-Za-z0-9_+\-]+:")
_QUOTE_LINE_RE = re.compile(r"^\s*(?:>|&gt;).*$", re.M)
_WORD_RE = re.compile(r"[^\W_]+")
_USER_TOKEN_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_REPEAT_RE = re.compile(r"([^\W\d_])\1{2,}")
_PUNCT_RE = re.compile(r"[^\w\s]")

_EXPLAIN_ENGLISH = ["what does that mean", "what does it mean", "whats that mean", "what does that say",
                    "translate", "explain"]
_EXPLAIN_GERMAN = ["was heisst das", "was bedeutet das", "bedeutet das"]
_EXPLAIN_PATTERNS = [r"\buebersetz"]
_EXPLAIN_PROGRAM_WORDS = {"ore", "docs", "github", "track", "build", "error", "function", "code", "python"}
_ELABORATE_PHRASES = ["more", "explain", "why", "go on", "elaborate", "tell me more", "details"]


def _visible(text: str) -> str:
    """The text a person can read as plain words: no code, quote lines, links or Slack tokens."""
    t = text or ""
    t = _CODE_BLOCK_RE.sub(" ", t)
    t = _CODE_SPAN_RE.sub(" ", t)
    t = _QUOTE_LINE_RE.sub(" ", t)
    t = _ANGLE_RE.sub(" ", t)
    t = _RAW_URL_RE.sub(" ", t)
    return t


def strip_markup(text: str) -> str:
    """Remove user/channel/link/special tokens, code spans and blocks, :emoji: and entities. Keep plain words."""
    t = _visible(text)
    t = _EMOJI_RE.sub(" ", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(t.split())


def word_count(text: str) -> int:
    """Words in strip_markup(text). A word contains at least one letter or digit."""
    return len(_WORD_RE.findall(strip_markup(text)))


def qualify_message(text: str, min_words: int = 3, min_chars: int = 12) -> bool:
    """True when the stripped text has at least min_words words and min_chars characters.
    Emoji-only, link-only and mention-only messages do not qualify. Short chat like 'ok sounds good to me' does."""
    stripped = strip_markup(text)
    return len(_WORD_RE.findall(stripped)) >= min_words and len(stripped) >= min_chars


def normalize_text(text: str) -> str:
    """Lowercase, strip markup and punctuation, collapse whitespace and repeated letters (3+ -> 2)."""
    s = strip_markup(text).lower()
    s = s.replace("'", "").replace("’", "").replace('"', "").replace("`", "")
    s = _PUNCT_RE.sub(" ", s)
    s = _REPEAT_RE.sub(r"\1\1", s)
    return " ".join(s.split())


def simhash(text: str) -> int:
    """64-bit similarity hash over normalized word 2-shingles. Deterministic. Similar texts differ in few bits."""
    words = normalize_text(text).split()
    if not words:
        return 0
    if len(words) == 1:
        shingles = words
    else:
        shingles = [words[i] + " " + words[i + 1] for i in range(len(words) - 1)]
    weights = [0] * 64
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    return value


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def is_near_duplicate(h: int, recent: Iterable[int], max_distance: int = 3) -> bool:
    return any(hamming(h, r) <= max_distance for r in recent)


def extract_user_ids(text: str) -> List[str]:
    """User ids from <@U123> and <@U123|name> tokens (ids start with U or W). Order kept, no duplicates."""
    return list(dict.fromkeys(_USER_TOKEN_RE.findall(text or "")))


def contains_ping(text: str, bot_user_id: str) -> bool:
    """True when <@bot_user_id> or <@bot_user_id|...> appears."""
    if not bot_user_id:
        return False
    return re.search(r"<@%s(?:\|[^>]*)?>" % re.escape(bot_user_id), text or "") is not None


def mentions_name(text: str, names: Iterable[str] = ("goose-lee", "goose lee", "gooselee")) -> bool:
    """Whole-word, case-insensitive match of the bot's name. Ignored inside code spans, quotes (lines starting with >)
    and URLs. A bare 'goose' is NOT the name."""
    vis = _visible(text)
    for name in names:
        pattern = r"(?<![\w-])" + r"\s+".join(re.escape(p) for p in name.split()) + r"(?![\w-])"
        if re.search(pattern, vis, re.I):
            return True
    return False


def mentions_goose(text: str) -> bool:
    """A bare whole word 'goose' (or 'geese'/'gooses' no). Not 'goose-lee', not 'Mother Goose', 'goosebumps',
    'gooseberry', 'goose bumps'. Ignored inside code spans, quotes and URLs."""
    vis = _visible(text)
    for m in re.finditer(r"(?<![\w-])goose(?![\w-])", vis, re.I):
        if re.search(r"mother\s*$", vis[:m.start()], re.I):
            continue
        if re.match(r"\s+bumps\b", vis[m.end():], re.I):
            continue
        return True
    return False


def _explain_norm(text: str) -> str:
    s = (text or "").lower()
    s = s.replace("ß", "ss").replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")
    s = s.replace("'", "").replace("’", "")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def is_explain_request(text: str) -> bool:
    """A thread reply asking what German meant. Lowercase, ß->ss, ü->ue before matching.
    English: 'what does that mean', 'what does it mean', "what's that mean", 'what does that say', 'translate',
    'explain', 'meaning?'. German: 'was heisst das', 'was bedeutet das', 'bedeutet das', 'uebersetz'.
    False for code, links, program words (ore, docs, github, track, build, error, function, code, python), quotes,
    and for messages over 12 words that contain no question mark."""
    raw = text or ""
    if "`" in raw:
        return False
    low = raw.lower()
    if re.search(r"https?://|<http|www\.", low):
        return False
    if any(line.lstrip().startswith((">", "&gt;")) for line in raw.splitlines()):
        return False
    norm = _explain_norm(raw)
    words = norm.split()
    if _EXPLAIN_PROGRAM_WORDS.intersection(words):
        return False
    has_question = "?" in raw
    if len(words) > 12 and not has_question:
        return False
    pad = " " + " ".join(words) + " "
    for phrase in _EXPLAIN_ENGLISH + _EXPLAIN_GERMAN:
        if (" " + phrase + " ") in pad:
            return True
    for pattern in _EXPLAIN_PATTERNS:
        if re.search(pattern, norm):
            return True
    return re.search(r"\bmeaning\s*\?", low) is not None


def is_elaborate_request(text: str) -> bool:
    """'more', 'explain', 'why', 'go on', 'elaborate', 'tell me more', 'details' as the main point of a short reply."""
    words = _explain_norm(text).split()
    if not words or len(words) > 6:
        return False
    pad = " " + " ".join(words) + " "
    return any((" " + phrase + " ") in pad for phrase in _ELABORATE_PHRASES)


def count_exclamations(text: str) -> int:
    return strip_markup(text).count("!")
