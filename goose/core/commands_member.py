"""Member slash commands (/goose ...). OWNER: agent C2a (router). Behavior: docs/PLAN.md section 8.

Replies are private (ephemeral) unless stated. Plain, no jokes in errors and privacy text.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from goose.core.context import AppContext
from goose.core.rules import extract_user_ids
from goose.core.safety import is_program_question
from goose.types import Action, Event, Result

PRIVACY_LINE = (
    "`/goose me` shows your page. Organizers can look at it too when they review a week or a report. "
    "When you ping the duck or reply in its thread, that message goes to an AI provider so the duck can answer. "
    "Ambient chat is never sent. The duck stores no message text."
)

REPORT_WINDOW_S = 72 * 3600
_SLACK_LINK = re.compile(r"archives/([A-Z0-9]+)/p(\d{7,})")

# Plain structural replies. Not jokes, no German. Kept here so they are easy to move into lines.yaml as moments.
PLAIN = {
    "show_on": "You are on the boards now. Only the top 3 are ever named.",
    "show_off": "You are off the boards. You are still scored.",
    "rest_on": "Your scoring is paused. `/goose rest off` turns it back on.",
    "rest_off": "Your scoring is on again.",
    "quiet_on": "Quiet is on. No teasing and no reactions to your messages.",
    "quiet_off": "Quiet is off.",
    "usage_rest": "Use `/goose rest on` or `/goose rest off`.",
    "usage_quiet": "Use `/goose quiet on` or `/goose quiet off`.",
    "left": "You have left. Your rows are deleted.",
    "leave_refused": "You cannot leave right now. An organizer has an open note on your account.",
    "no_week": "No week is confirmed yet, so there is no board to show.",
    "no_board": "Nobody is on the board yet.",
    "not_organizer": "Only organizers can look at someone else's page.",
    "no_link": "Give me a Slack message link, for example `/goose explain <link to a duck message>`.",
    "receipt_report": "Report #%d received. Organizers read every report and reply within 72 hours.",
    "receipt_appeal": ("Appeal #%d received. Appeals are open for 7 days after a decision, "
                       "and organizers reply within 72 hours."),
    "not_on_board": "You are not on the all-time board yet.",
    "not_top10": "You are not in the top 10 this week.",
}

HELP_TEXT = "\n".join([
    "Commands (replies are private):",
    "`/goose join` opts you in. `/goose leave` deletes your rows and leaves.",
    "`/goose me` shows your page. `/goose top` and `/goose all` show the top 3.",
    "`/goose show` or `/goose hide` puts you on the boards or takes you off.",
    "`/goose rest on` or `off` pauses your own scoring. `/goose quiet on` or `off` stops teasing and reactions.",
    "`/goose explain <message link>` explains a German phrase the duck used.",
    "`/goose pun` and `/goose quack` are for fun.",
    "`/goose report <text>` or `/goose appeal <text>` tells the organizers.",
    "`@goose-lee me` and `@goose-lee top` work too.",
    "",
    PRIVACY_LINE,
])


def row_user_id(row: Any) -> Optional[str]:
    """user_id of a snapshot dict or a RankRow."""
    if isinstance(row, dict):
        return row.get("user_id")
    return getattr(row, "user_id", None)


def index_of(rows: List[Any], user_id: str) -> Optional[int]:
    for i, row in enumerate(rows):
        if row_user_id(row) == user_id:
            return i
    return None


def handle(ctx: AppContext, event: Event) -> Result:
    """event.kind == 'command', event.command in types.MEMBER_COMMANDS, event.args holds the rest.
    help: commands plus the privacy line. join: Users.join (welcome line, notes roasts can be stopped with quiet).
    leave: Users.leave (explain if refused). show/hide/rest/quiet on|off. me: the person's pond level and title,
    crumbs by source this week (Ledger.week_breakdown with pending), lifetime progress, next level, and why anything
    did not count (e.g. messages too short, held for review), private; `me` of someone else is refused for members.
    top / all: top 3 this week (last confirmed week) / all-time top 3, only people with show on, names as Slack mentions
    without pinging (use display via <@U..>), plus the caller's own place privately. explain <message link>: private
    explanation from ExplainService. pun / quack: a German pun with its explanation / a quack, private by default.
    report / appeal: insert into reports with due_ts now+72h (appeals 7 days window noted), reply with a receipt,
    alert the organizer channel via an 'organizer_post' action. Unknown subcommand -> help text.
    Rank answers use only the last confirmed snapshot. Program words in args -> the fixed redirect line."""
    cmd = (event.command or "").strip().lower()
    args = (event.args or "").strip()
    if cmd in ("report", "appeal"):
        return _report(ctx, event, cmd, args)
    if args and is_program_question(args):
        return Result.of(_eph(event, _line(ctx, "program_redirect")))
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return Result.of(_eph(event, HELP_TEXT))
    return handler(ctx, event, args)


# ---------------------------------------------------------------- helpers

def _eph(event: Event, text: str, thread_ts: str = "") -> Action:
    return Action(kind="ephemeral", channel=event.channel, user=event.user, text=text, thread_ts=thread_ts)


def _line(ctx: AppContext, moment: str, allow_german: bool = False, slots: Optional[Dict[str, Any]] = None) -> str:
    line = ctx.lines.pick(moment, ctx.rng, slots=slots, allow_german=allow_german,
                          german_rate=ctx.config.vibes.german_rate)
    return ctx.lines.expand(line.text)


def _clean(ctx: AppContext, text: str) -> str:
    return ctx.lines.expand(text) if "{de:" in text else text


def _on_off(args: str, default: Optional[bool]) -> Optional[bool]:
    """'on' -> True, 'off' -> False, empty -> default, anything else -> None (usage)."""
    parts = args.split()
    if not parts:
        return default
    word = parts[0].lower()
    if word in ("on", "yes"):
        return True
    if word in ("off", "no"):
        return False
    return None


def _not_joined(ctx: AppContext, event: Event) -> Result:
    return Result.of(_eph(event, _line(ctx, "not_joined")))


# ---------------------------------------------------------------- handlers

def _help(ctx: AppContext, event: Event, args: str) -> Result:
    return Result.of(_eph(event, HELP_TEXT))


def _join(ctx: AppContext, event: Event, args: str) -> Result:
    # Joining accepts the current privacy notice. A person who joined under an older notice re-accepts here.
    now = ctx.clock.now()
    notice = str(ctx.config.voice.notice_version)
    if not ctx.users.join(event.user, now, channel=event.channel, consent_version=notice):
        _accept_notice(ctx, event.user, notice, now)
    return Result.of(_eph(event, _line(ctx, "join", allow_german=True) + "\n\n" + PRIVACY_LINE))


def _accept_notice(ctx: AppContext, uid: str, version: str, now: float) -> None:
    """Store the accepted notice version (and a consents row) for someone already joined under an older version."""
    user = ctx.users.get(uid) or {}
    if str(user.get("consent_version") or "") == version:
        return
    with ctx.db.tx():
        ctx.db.execute("UPDATE users SET consent_version = ? WHERE user_id = ?", (version, uid))
        ctx.db.execute("INSERT INTO consents (user_id, version, ts) VALUES (?,?,?)", (uid, version, float(now)))


def _leave(ctx: AppContext, event: Event, args: str) -> Result:
    if not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    if ctx.users.leave(event.user, ctx.clock.now()):
        return Result.of(_eph(event, PLAIN["left"]))
    return Result.of(_eph(event, PLAIN["leave_refused"]))


def _show(ctx: AppContext, event: Event, args: str) -> Result:
    if not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    ctx.users.set_show(event.user, True)
    return Result.of(_eph(event, PLAIN["show_on"]))


def _hide(ctx: AppContext, event: Event, args: str) -> Result:
    if not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    ctx.users.set_show(event.user, False)
    return Result.of(_eph(event, PLAIN["show_off"]))


def _rest(ctx: AppContext, event: Event, args: str) -> Result:
    value = _on_off(args, default=True)
    if value is None:
        return Result.of(_eph(event, PLAIN["usage_rest"]))
    if not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    ctx.users.set_rest(event.user, value)
    return Result.of(_eph(event, PLAIN["rest_on" if value else "rest_off"]))


def _quiet(ctx: AppContext, event: Event, args: str) -> Result:
    value = _on_off(args, default=None)
    if value is None:
        return Result.of(_eph(event, PLAIN["usage_quiet"]))
    if not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    ctx.users.set_quiet(event.user, value)
    return Result.of(_eph(event, PLAIN["quiet_on" if value else "quiet_off"]))


def _me_text(ctx: AppContext, uid: str, now: float) -> str:
    cfg = ctx.config
    week = ctx.clock.week_id(now)
    lifetime = ctx.ledger.lifetime(uid, include_pending=True)
    prog = ctx.levels.progress(lifetime)
    bd = ctx.ledger.week_breakdown(uid, week, include_pending=True)
    final = ctx.ledger.week_breakdown(uid, week, include_pending=False)
    pending = max(0.0, bd.total - final.total)
    voided = sum(1 for f in ctx.ledger.all_facts(week_id=week, kinds=("msg",))
                 if f.get("user_id") == uid and f.get("state") == "voided")
    user = ctx.users.get(uid) or {}
    out = [
        "Pond level %d, %s." % (prog["level"], prog["title"]),
        "Lifetime crumbs: %.1f." % lifetime,
    ]
    if prog.get("next_level"):
        out.append("Next: level %d, %s, at %.0f crumbs (%.1f to go)." % (
            prog["next_level"], prog["next_title"], prog["next_threshold"], prog["to_next"]))
    else:
        out.append("You have reached the top level.")
    out += [
        "",
        "This week (%s): %.1f crumbs." % (week, bd.total),
        "  Chat: %.1f from %d messages on %d days." % (bd.chat, bd.n_msgs, bd.active_days),
        "  Thanks: %.1f from %d." % (bd.thanks, bd.n_thanks),
        "  Helpful: %.1f from %d." % (bd.helpful, bd.n_helpful),
        "  Stars: %.1f from %d." % (bd.stars, bd.star_count),
        "  Waiting to count: %.1f." % pending,
        "",
        ("Not counted: messages under %d words or %d characters, messages within %d seconds of your last counted "
         "one, near copies of your own recent messages, and edited or deleted messages (%d this week).") % (
            cfg.scoring.min_words, cfg.scoring.min_chars, cfg.scoring.flood_seconds, voided),
        "Board: %s. Quiet: %s. Rest: %s." % (
            "shown" if user.get("show") else "hidden",
            "on" if user.get("quiet") else "off",
            "on" if user.get("rest") else "off"),
    ]
    return "\n".join(out)


def _me(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    target = event.user
    header = ""
    ids = extract_user_ids(args)
    if ids and ids[0] != event.user:
        if not ctx.config.is_organizer(event.user):
            return Result.of(_eph(event, PLAIN["not_organizer"]))
        target = ids[0]
        if not ctx.users.is_joined(target):
            return Result.of(_eph(event, _line(ctx, "unknown_person")))
        header = "Page for <@%s>.\n" % target
    elif not ctx.users.is_joined(event.user):
        return _not_joined(ctx, event)
    return Result.of(_eph(event, header + _me_text(ctx, target, now)))


def _top(ctx: AppContext, event: Event, args: str) -> Result:
    last = ctx.weekly.last_confirmed()
    if not last:
        return Result.of(_eph(event, PLAIN["no_week"]))
    out = ["Top 3 this week (week %s):" % last.get("week_id", "")]
    top = list(last.get("top") or [])[:3]
    named = [(i, uid) for i, uid in enumerate(top, 1) if _shown_now(ctx, uid)]
    if not named:
        out.append(PLAIN["no_board"])
    for i, uid in named:
        out.append("%d. <@%s>" % (i, uid))
    rows = list(last.get("rows") or [])
    idx = index_of(rows, event.user)
    if idx is not None and idx < 10:
        out.append("Your place: #%d." % (idx + 1))
    else:
        out.append(PLAIN["not_top10"])
    return Result.of(_eph(event, "\n".join(out)))


def _shown_now(ctx: AppContext, uid: Optional[str]) -> bool:
    """Only people whose CURRENT show flag is on are named on a board (a snapshot can be older than a hide)."""
    row = ctx.users.get(uid or "")
    return bool(row and row.get("show"))


def _all(ctx: AppContext, event: Event, args: str) -> Result:
    full = ctx.weekly.alltime_ranking(limit=500)
    top = [row for row in full if _shown_now(ctx, row_user_id(row))][:3]
    out = ["All-time top 3:"]
    if not top:
        out.append(PLAIN["no_board"])
    for i, row in enumerate(top, 1):
        out.append("%d. <@%s>" % (i, row_user_id(row)))
    idx = index_of(full, event.user)
    out.append("Your place: #%d." % (idx + 1) if idx is not None else PLAIN["not_on_board"])
    return Result.of(_eph(event, "\n".join(out)))


def _explain(ctx: AppContext, event: Event, args: str) -> Result:
    m = _SLACK_LINK.search(args)
    if not m:
        return Result.of(_eph(event, PLAIN["no_link"]))
    channel, digits = m.group(1), m.group(2)
    message_ts = "%s.%s" % (digits[:-6], digits[-6:])
    if not ctx.explain.lookup(channel, message_ts):
        return Result.of(_eph(event, _clean(ctx, ctx.explain.unknown_reply())))
    text = ctx.explain.explanation(channel, message_ts, ctx.clock.now())
    return Result.of(_eph(event, _clean(ctx, text or ctx.explain.unknown_reply())))


def _pun(ctx: AppContext, event: Event, args: str) -> Result:
    line = ctx.lines.pick("pun", ctx.rng, allow_german=True, german_rate=1.0)
    text = ctx.lines.expand(line.text)
    for de_id in ctx.lines.de_ids_in(line.text)[:1]:
        text += "\n" + ctx.lines.explain_text(de_id)
    return Result.of(_eph(event, text))


def _quack(ctx: AppContext, event: Event, args: str) -> Result:
    return Result.of(_eph(event, _line(ctx, "quack", allow_german=True)))


def _report(ctx: AppContext, event: Event, kind: str, args: str) -> Result:
    now = ctx.clock.now()
    text = args[:1000]
    cur = ctx.db.execute(
        "INSERT INTO reports (user_id, kind, text, opened_ts, due_ts) VALUES (?, ?, ?, ?, ?)",
        (event.user, kind, text, now, now + REPORT_WINDOW_S))
    rid = int(cur.lastrowid)
    receipt = PLAIN["receipt_report"] % rid if kind == "report" else PLAIN["receipt_appeal"] % rid
    alert = Action(kind="organizer_post", channel=ctx.organizer_channel,
                   text="New %s #%d from `%s`. Read it with the reports command." % (kind, rid, event.user))
    return Result(actions=[_eph(event, receipt), alert])


_HANDLERS: Dict[str, Callable[[AppContext, Event, str], Result]] = {
    "help": _help,
    "join": _join,
    "leave": _leave,
    "show": _show,
    "hide": _hide,
    "rest": _rest,
    "quiet": _quiet,
    "me": _me,
    "top": _top,
    "all": _all,
    "explain": _explain,
    "pun": _pun,
    "quack": _quack,
}
