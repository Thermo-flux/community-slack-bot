"""Shared data types. Every module imports these; do not change a field without telling the owner (the lead)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

EVENT_KINDS = (
    "message", "message_changed", "message_deleted",
    "reaction_added", "reaction_removed", "command", "member_joined",
)
FACT_KINDS = ("msg", "thanks", "helpful", "star", "bigstar")
ACTION_KINDS = ("post", "ephemeral", "reaction", "organizer_post", "file")

MEMBER_COMMANDS = (
    "help", "join", "leave", "show", "hide", "rest", "quiet", "me", "top", "all",
    "explain", "pun", "quack", "report", "appeal",
)
ORGANIZER_COMMANDS = (
    "results", "winners", "stars", "roster", "audit", "export", "star", "bigstar",
    "revoke", "confirm-week", "disqualify", "skip-week", "confirm-final", "set-end",
    "freeze", "unfreeze", "reports", "resolve", "prompt", "announce", "pause",
    "resume", "voice", "config",
)


@dataclass
class Event:
    """A normalized Slack event. Built by gateway/slack_io.py, consumed by core/router.py."""
    kind: str
    ts: float                       # event time, unix seconds
    channel: str = ""
    user: str = ""                  # actor's Slack user id ("" if none)
    event_id: str = ""
    message_ts: str = ""            # message events: the message ts. Reactions: the reacted-to message ts
    thread_ts: str = ""             # parent ts when this is a thread reply, else ""
    text: str = ""                  # message text as Slack sends it, or the whole command argument text
    item_user: str = ""             # reactions: author of the reacted-to message
    reaction: str = ""              # reaction name without colons, e.g. "duck"
    is_bot: bool = False            # sent by any bot, including this one
    bot_mentioned: bool = False     # text pings this bot
    command: str = ""               # kind "command": the subcommand word, e.g. "me", "star"
    args: str = ""                  # kind "command": the rest of the text after the subcommand
    mentions: List[str] = field(default_factory=list)   # user ids found in <@U123|name> tokens in text/args
    thread_parent_user: str = ""    # author of the thread's parent message, if known
    previous_text: str = ""         # message_changed: the old text
    deleted_ts: str = ""            # message_deleted: ts of the deleted message


@dataclass
class Action:
    """Something to do in Slack. Built by core, executed by gateway/slack_io.py."""
    kind: str                       # one of ACTION_KINDS
    channel: str = ""
    text: str = ""
    user: str = ""                  # "ephemeral": who sees it
    thread_ts: str = ""
    message_ts: str = ""            # "reaction": which message
    emoji: str = ""                 # "reaction": emoji name without colons
    filename: str = ""              # "file"
    content: str = ""               # "file": body, e.g. CSV


@dataclass
class VoiceJob:
    """A voice request the gateway runs asynchronously. `fallback` is posted when the voice fails."""
    request: Dict[str, Any]
    channel: str
    user: str
    thread_ts: str
    message_ts: str
    ephemeral: bool
    fallback: str                   # canned text, already rendered, may still contain {de:id} placeholders
    mode: str = "chat"              # "chat" | "event"
    kind: str = "addressed"         # "addressed" | "event"
    allow_german: bool = True
    allow_roast: bool = False
    reply_cap: int = 300


@dataclass
class Result:
    actions: List[Action] = field(default_factory=list)
    jobs: List[VoiceJob] = field(default_factory=list)

    def extend(self, other: "Result") -> "Result":
        self.actions.extend(other.actions)
        self.jobs.extend(other.jobs)
        return self

    @classmethod
    def of(cls, *actions: Action) -> "Result":
        return cls(actions=list(actions))


@dataclass
class VoiceResult:
    ok: bool
    response: Optional[Dict[str, Any]] = None
    reason: str = ""                # "", "timeout", "breaker_open", "disabled", "junk", "http_500", ...
    latency: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class FilterResult:
    ok: bool
    text: str = ""                  # final text, escaped and with German placeholders expanded
    reaction: str = ""
    reason: str = ""                # why it was rejected
    de_ids: List[str] = field(default_factory=list)


@dataclass
class Breakdown:
    """One person's crumbs for one week."""
    chat: float = 0.0
    thanks: float = 0.0
    helpful: float = 0.0
    stars: float = 0.0
    total: float = 0.0
    n_msgs: int = 0
    active_days: int = 0
    n_thanks: int = 0
    n_helpful: int = 0
    star_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RankRow:
    user_id: str
    total: float
    breakdown: Breakdown
    stars: List[Dict[str, Any]] = field(default_factory=list)   # {id, amount, actor, reason, link, kind}
    recognizers: int = 0            # distinct people who thanked or marked
    joined_ts: float = 0.0
    flags: List[str] = field(default_factory=list)
