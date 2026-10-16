"""Word lists used by the gates and the post-filter. DRAFTS: the community must review the distress and sad lists.

All matching is whole-word and case-insensitive. Multi-word entries match as phrases.
"""
from __future__ import annotations

# Reward wording. The strong words are blocked in every voice mode while rewards.announced is false.
REWARD_CORE = [
    "reward", "rewards", "prize", "prizes", "trophy", "badge", "merch", "sticker", "stickers",
    "sponsor", "invitation", "giveaway", "swag",
]
# Generic words that also show up in ordinary chat ("a gift for mom", "hardware store"). In general chat they are
# allowed when the user's own message used the same word; in every other mode they are blocked like the core words.
REWARD_GENERIC = ["hardware", "crown", "perk", "perks", "bonus", "unlock", "claim", "gift", "sign up"]
REWARD_STRONG = REWARD_CORE + REWARD_GENERIC   # the line bank lint uses the whole list
# The weak words are fine in general chat when the user's own message used them ("who wins?").
REWARD_WEAK = ["win", "wins", "won", "winner", "winners", "winning"]

# A reply that says someone won, earned or will receive something is rejected in every mode.
CLAIM_PATTERNS = [
    r"\byou('ve| have)?\s+(won|earned|got|received|unlocked)\b",
    r"\byou('re| are)\s+(getting|receiving|the winner)\b",
    r"\b(is|are)\s+getting\s+(a|an|the)\b",
    r"\bcongrat\w*\s+on\s+winning\b",
]

# Words that make a reply "about the duck's own stats". Numbers near these must come from the card.
STAT_WORDS = [
    "crumbs", "crumb", "pond level", "rank", "ranks", "ranked", "ranking", "rankings", "standing", "standings",
    "leaderboard", "streak", "busiest", "top 3", "top three", "top 10", "top ten", "your level", "your week",
    "this week", "last week", "your score",
]

# Program words. Blocked in voice output, and they trigger the redirect gate on input.
PROGRAM_WORDS_OUT = ["ore", "track xp", "era points", "requisition", "requisitions", "orry", "ysws", "hackatime",
                     "loadout"]
PROGRAM_STRONG_PATTERNS = [
    r"\bore\b", r"track\s*xp", r"era\s+points?", r"requisitions?", r"\bhackatime\b", r"\bysws\b", r"\borry\b",
]
# These only count as a program question when the message also talks about loadout, the program or ysws.
PROGRAM_WEAK_PATTERNS = [
    r"\blaunch\w*", r"\bdeadline\w*", r"\beligib\w*", r"\bapply\b", r"\bjoin\b", r"\bprizes?\b", r"\brewards?\b",
    r"\bprice\w*", r"\bcost\w*", r"\brules?\b", r"\bsign\s*up\b", r"\bdates?\b", r"\bwhen\b", r"\bhours?\b",
]
PROGRAM_CONTEXT_PATTERNS = [r"\bloadout\b", r"\bthe program\b", r"\bysws\b"]

THREAT_WORDS = ["timeout", "time out", "ban", "banned", "mute", "muted", "suspend", "suspended", "punish", "punished"]

BANNED_IDIOMS = ["lame duck", "ugly duckling", "dumme gans", "ja, ja", "na ja", "alles klar", "passt"]
PROFANITY = ["damn", "hell", "shit", "fuck", "fucking", "bitch", "crap", "bastard", "asshole"]

# German words that must never appear outside an approved {de:id} placeholder in voice output.
GERMAN_STOPWORDS = [
    "der", "das", "ein", "eine", "und", "ist", "ich", "du", "nicht", "mit", "für", "auf", "dem", "des", "zu",
    "von", "wir", "sie", "aber", "oder", "auch", "noch", "nur", "schon", "sehr", "gut", "danke", "bitte",
    "hallo", "tschüss", "nein", "kein", "keine", "bist", "sind", "haben", "wird", "werden", "kann", "können",
    "muss", "über", "unter", "nach", "vor", "bei", "aus", "zum", "zur", "dass", "wenn", "weil", "denn", "doch",
    "ganz", "mal", "heute", "jetzt", "hier", "dort", "alles", "nichts", "viel", "mehr", "wirklich",
]

# DRAFT lists. Replace with the community's own words (plan section 11).
DISTRESS_WORDS = [
    "kill myself", "suicide", "suicidal", "self harm", "self-harm", "hurt myself", "want to die",
    "end it all", "cut myself", "being abused", "abused me", "no reason to live",
]
SAD_WORDS = [
    "died", "dead", "death", "funeral", "depressed", "depression", "lonely", "alone", "miss him", "miss her",
    "miss them", "crying", "cried", "anxious", "anxiety", "panic", "hate myself", "hopeless", "worthless",
    "grief", "grieving", "bullied", "bullying", "abuse", "abused",
]

# Emoji the voice may use. Excludes star and white_check_mark, which mean real things here.
VOICE_EMOJI_ALLOWLIST = ["duck", "tada", "wave", "thinking_face", "eyes", "bread", "feather", "partying_face", "zzz", "sunrise"]
