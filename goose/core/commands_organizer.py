"""Organizer slash commands. OWNER: agent C2b (ops-wiring). Behavior: docs/PLAN.md section 7.

Every command: check ctx.config.is_organizer(event.user) first (owner-only ones check is_owner); non-organizers get a plain
refusal. A reason is required for star, bigstar, revoke, disqualify and freeze. Every state-changing command writes to
ctx.audit and returns an 'organizer_post' action (to ctx.organizer_channel) as an alert. Replies are ephemeral.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from goose.core.context import AppContext
from goose.core.wordlists import REWARD_STRONG, REWARD_WEAK
from goose.types import Action, Event, Result

STAR_KINDS = ("active", "help", "made", "idea", "hosted", "other")
BIGSTAR_PENDING_TTL_S = 7 * 86400
PLAIN_HINT = ("Plain @names do not reach the duck. Type @ and pick the person from the mention menu, then try again.")
NOT_ORGANIZER = "That command is for organizers."
OWNER_ONLY = "Only the owner can do that."
MISSING_PERSON = "Name the person with a mention (type @ and pick them from the menu)."
NOT_JOINED = "That person has not joined goose-lee yet."
SLACK_LINK_RE = re.compile(r"^https://[A-Za-z0-9-]+\.slack\.com/archives/([A-Za-z0-9]+)/p(\d{7,})")
MENTION_TOKEN_RE = re.compile(r"^<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


class _Refuse(Exception):
    """A plain refusal for the organizer: the message is shown privately and nothing is changed."""


# ---------------------------------------------------------------------------------------------------------------- helpers

def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _n(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "0"
    if x == int(x):
        return str(int(x))
    return ("%.2f" % x).rstrip("0").rstrip(".")


def _private(event: Event, text: str) -> Result:
    return Result.of(Action(kind="ephemeral", channel=event.channel, user=event.user, text=text))


def _alert(ctx: AppContext, text: str) -> Action:
    return Action(kind="organizer_post", channel=ctx.organizer_channel, text=text)


def _tell(ctx: AppContext, user_id: str, text: str) -> Optional[Action]:
    """A private note to a member, in the channel where they were last seen, else the first scored channel."""
    if not user_id:
        return None
    channel = ctx.ledger.last_channel(user_id) or (ctx.config.channels.scored[0] if ctx.config.channels.scored else "")
    if not channel:
        return None
    return Action(kind="ephemeral", channel=channel, user=user_id, text=text)


def _has_reward_word(text: str, words: Optional[List[str]] = None) -> bool:
    words = words if words is not None else REWARD_STRONG
    pattern = r"\b(?:%s)\b" % "|".join(re.escape(w) for w in words)
    return re.search(pattern, text, re.IGNORECASE) is not None


def _take_person(args: str) -> Tuple[str, str, str]:
    """(user_id, rest, error). error is '' when a mention was found, 'plain' for a typed @name, 'missing' otherwise."""
    s = (args or "").strip()
    m = MENTION_TOKEN_RE.match(s)
    if m:
        return m.group(1), s[m.end():].strip(), ""
    if s.startswith("@"):
        return "", s, "plain"
    return "", s, "missing"


def _person(args: str) -> Tuple[str, str]:
    uid, rest, err = _take_person(args)
    if err == "plain":
        raise _Refuse(PLAIN_HINT)
    if err or not uid:
        raise _Refuse(MISSING_PERSON)
    return uid, rest


def _require_joined(ctx: AppContext, uid: str) -> None:
    if not ctx.users.is_joined(uid):
        raise _Refuse(NOT_JOINED)


def _week_from_date(ctx: AppContext, token: str) -> str:
    try:
        d = dt.date.fromisoformat(token.strip())
    except ValueError:
        raise _Refuse("Give a week as last, current, or a date like 2025-10-06 (the Monday).")
    return ctx.clock.week_id(ctx.clock.date_to_ts(d.isoformat()))


def _week_arg(ctx: AppContext, token: str, now: float) -> str:
    t = (token or "").strip().lower()
    current = ctx.clock.week_id(now)
    if t in ("", "current", "this", "now"):
        return current
    if t == "last":
        return ctx.clock.previous_week_id(current)
    return _week_from_date(ctx, token)


def _row(r: Any) -> Dict[str, Any]:
    """RankRow (dataclass) or its dict form (weekly snapshot) as one plain dict."""
    if isinstance(r, dict):
        bd = r.get("breakdown") or {}
        return {"user_id": r.get("user_id", ""), "total": float(r.get("total") or 0), "bd": dict(bd),
                "stars": list(r.get("stars") or []), "flags": list(r.get("flags") or []),
                "recognizers": int(r.get("recognizers") or 0)}
    bd = r.breakdown
    bd_dict = bd.as_dict() if hasattr(bd, "as_dict") else dict(bd or {})
    return {"user_id": r.user_id, "total": float(r.total), "bd": bd_dict, "stars": list(r.stars or []),
            "flags": list(r.flags or []), "recognizers": int(r.recognizers or 0)}


def format_rows(rows: List[Any], limit: int = 10) -> str:
    """Breakdown lines for a ranking (used by the organizer replies and the scheduler's organizer posts)."""
    lines = []
    for i, raw in enumerate(rows[:limit], start=1):
        r = _row(raw)
        bd = r["bd"]
        star_count = int(bd.get("star_count") or len(r["stars"]))
        line = "%d. <@%s> %s crumbs (chat %s, thanks %s, helpful %s, stars %s from %d star%s)" % (
            i, r["user_id"], _n(r["total"]), _n(bd.get("chat")), _n(bd.get("thanks")), _n(bd.get("helpful")),
            _n(bd.get("stars")), star_count, "" if star_count == 1 else "s")
        if r["flags"]:
            line += " flags: %s" % _esc(", ".join(str(f) for f in r["flags"]))
        lines.append(line)
    return "\n".join(lines) if lines else "No one on the board."


def _is_slack_link(token: str) -> bool:
    return bool(_link_from_token(token))


def _link_from_token(token: str) -> str:
    t = token.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].split("|", 1)[0]
    t = html.unescape(t)
    return t if t.startswith("https://") else ""


def _slack_message_ref(link: str) -> Tuple[str, str]:
    """('C123', '1760000000.123456') for https://<ws>.slack.com/archives/C123/p1760000000123456, else ('', '')."""
    m = SLACK_LINK_RE.match(link or "")
    if not m:
        return "", ""
    digits = m.group(2)
    return m.group(1), "%s.%s" % (digits[:-6], digits[-6:])


# ---------------------------------------------------------------------------------------------------------- commands

def _cmd_results(ctx: AppContext, event: Event, args: str) -> Result:
    tokens = args.split()
    sub = tokens[0].lower() if tokens else ""
    now = ctx.clock.now()
    if sub == "week":
        wid = _week_arg(ctx, tokens[1] if len(tokens) > 1 else "", now)
        status = ctx.weekly.status(wid)
        if status and status.get("state") in ("provisional", "confirmed"):
            rows = ctx.weekly.snapshot(wid)
            label = status["state"]
        elif status and status.get("state") == "skipped":
            return _private(event, "Week %s was skipped (%s)." % (wid, _esc(status.get("skipped_reason") or "no reason")))
        else:
            rows = ctx.weekly.ranking(wid, limit=10, include_pending=True, now=now)
            label = "live, not closed yet"
        return _private(event, "Week %s (%s)\n%s" % (wid, label, format_rows(rows, 10)))
    if sub == "all":
        count = 10
        if len(tokens) > 1:
            if not tokens[1].isdigit() or not 1 <= int(tokens[1]) <= 50:
                raise _Refuse("The count must be a number from 1 to 50.")
            count = int(tokens[1])
        rows = ctx.weekly.alltime_ranking(limit=count, now=now)
        return _private(event, "All-time top %d\n%s" % (count, format_rows(rows, count)))
    raise _Refuse("Usage: /goose results week [last|YYYY-MM-DD] or /goose results all [count].")


def _cmd_winners(ctx: AppContext, event: Event, args: str) -> Result:
    data = ctx.weekly.winners()
    lines = []
    for w in data.get("weeks", []):
        top = ", ".join("%d. <@%s> (%s)" % (t["rank"], t["user_id"], _n(t["total"])) for t in w.get("top3", []))
        lines.append("Week %s: %s" % (w.get("week_id"), top or "none"))
    if not lines:
        lines.append("No confirmed weekly winners yet.")
    for t in data.get("final", []):
        lines.append("Final %d. <@%s> (%s)" % (t["rank"], t["user_id"], _n(t["total"])))
    return _private(event, "\n".join(lines))


def _cmd_stars(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    tokens = args.split(None, 1)
    scope = "all"
    rest = args.strip()
    if tokens and tokens[0].lower() in ("week", "month", "all"):
        scope = tokens[0].lower()
        rest = tokens[1] if len(tokens) > 1 else ""
    uid = ""
    if rest.strip():
        uid, _rest, err = _take_person(rest)
        if err == "plain":
            raise _Refuse(PLAIN_HINT)
        if err == "missing":
            raise _Refuse("Usage: /goose stars [week|month|all] [@person].")
    this_week = ctx.clock.week_id(now)
    this_month = ctx.clock.local(now).strftime("%Y-%m")
    out = []
    for r in ctx.ledger.all_facts(kinds=("star", "bigstar")):
        if uid and r["user_id"] != uid:
            continue
        if scope == "week" and r["week_id"] != this_week:
            continue
        if scope == "month" and ctx.clock.local(r["ts"]).strftime("%Y-%m") != this_month:
            continue
        flag = " (voided: %s)" % _esc(r.get("void_reason") or "") if r.get("state") == "voided" else ""
        out.append("#%s <@%s> %s %s from <@%s>: %s%s%s" % (
            r["id"], r["user_id"], r["kind"], _n(r["amount"] or 0), r["counterparty"] or r["actor"],
            _esc(r["reason"]), (" " + _esc(r["link"])) if r.get("link") else "", flag))
        if len(out) >= 40:
            out.append("...more in /goose export stars")
            break
    return _private(event, "\n".join(out) if out else "No stars for that filter.")


def _cmd_roster(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    out = []
    for r in ctx.users.roster():
        joined = ctx.clock.local(r["joined_ts"]).date().isoformat()
        frozen = "yes" if float(r.get("frozen_until") or 0) > now else "no"
        flags = r.get("open_flags", r.get("flags", 0))
        out.append("<@%s> joined %s show:%s rest:%s quiet:%s frozen:%s flags:%s" % (
            r["user_id"], joined, _yn(r.get("show")), _yn(r.get("rest")), _yn(r.get("quiet")), frozen, flags))
        if len(out) >= 200:
            out.append("...list cut at 200")
            break
    return _private(event, "\n".join(out) if out else "Nobody has joined yet.")


def _yn(value: Any) -> str:
    return "yes" if value else "no"


def _cmd_audit(ctx: AppContext, event: Event, args: str) -> Result:
    count = 10
    tokens = args.split()
    if tokens:
        if not tokens[0].isdigit() or not 1 <= int(tokens[0]) <= 100:
            raise _Refuse("The count must be a number from 1 to 100.")
        count = int(tokens[0])
    lines = []
    for e in ctx.audit.tail(count):
        when = ctx.clock.local(float(e.get("ts") or 0)).strftime("%Y-%m-%d %H:%M")
        lines.append("%s #%s %s %s %s %s" % (when, e.get("n"), e.get("actor"), e.get("action"),
                                              e.get("target") or "-", _esc(e.get("detail") or "")))
    return _private(event, "\n".join(lines) if lines else "The audit log is empty.")


EXPORT_COLUMNS = ("id", "week_id", "user_id", "kind", "counterparty", "ts", "amount", "state", "actor", "reason",
                  "link", "channel", "message_ts", "void_reason", "void_actor")


def _csv_cell(value: Any) -> str:
    s = "" if value is None else str(value)
    if s.startswith(CSV_FORMULA_PREFIXES):
        s = "'" + s
    return s


def _cmd_export(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    tokens = args.split()
    sub = tokens[0].lower() if tokens else ""
    if sub == "week":
        wid = _week_arg(ctx, tokens[1] if len(tokens) > 1 else "", now)
        facts, name = ctx.ledger.all_facts(week_id=wid), "week-%s" % wid
    elif sub in ("all", "ledger"):
        facts, name = ctx.ledger.all_facts(), sub
    elif sub == "stars":
        facts, name = ctx.ledger.all_facts(kinds=("star", "bigstar")), "stars"
    else:
        raise _Refuse("Usage: /goose export week [last|YYYY-MM-DD] | all | stars | ledger.")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in facts:
        writer.writerow([_csv_cell(r.get(col)) for col in EXPORT_COLUMNS])
    filename = "goose-export-%s-%s.csv" % (name, dt.date.fromtimestamp(now).isoformat())
    ctx.audit.append(event.user, "export", target=name, detail="rows=%d" % len(facts), ts=now)
    return Result(actions=[
        Action(kind="file", channel=ctx.organizer_channel, filename=filename, content=buf.getvalue(),
               text="Export by <@%s>: %d rows." % (event.user, len(facts))),
        Action(kind="ephemeral", channel=event.channel, user=event.user,
               text="Export posted to the organizer channel."),
    ])


def _award(ctx: AppContext, event: Event, uid: str, kind: str, reason: str, link: str, fact_kind: str,
           points: float) -> Result:
    now = ctx.clock.now()
    actor = event.user
    chan, mts = _slack_message_ref(link)
    fid = ctx.ledger.add_fact(
        idem_key="star:%s" % uuid.uuid4().hex, user_id=uid, kind=fact_kind, ts=now, counterparty=actor,
        amount=points, actor=actor, reason="%s: %s" % (kind, reason), link=link, channel=chan, message_ts=mts,
        state="final")
    if fid is None:
        raise _Refuse("That star was already recorded.")
    ctx.audit.append(actor, fact_kind, target=uid, detail="kind=%s points=%s reason=%s link=%s" % (
        kind, _n(points), reason, link), ts=now)
    alert = _alert(ctx, "%s for <@%s> from <@%s>: %s, %s crumbs. Reason: %s%s" % (
        "Big star" if fact_kind == "bigstar" else "Star", uid, actor, kind, _n(points), _esc(reason),
        (" Link: " + _esc(link)) if link else ""))
    actions = [alert]
    if chan and mts:
        actions.append(Action(kind="reaction", channel=chan, message_ts=mts, emoji="star"))
    actions.append(Action(kind="ephemeral", channel=event.channel, user=actor,
                          text="Recorded: %s crumbs to <@%s> (final). The star reaction %s." % (
                              _n(points), uid, "is on the linked message" if chan and mts else "was not added (no Slack message link)")))
    return Result(actions=actions)


def _cmd_star(ctx: AppContext, event: Event, args: str, big: bool = False) -> Result:
    uid, rest = _person(args)
    if uid == event.user:
        raise _Refuse("You cannot give a star to yourself.")
    _require_joined(ctx, uid)
    tokens = rest.split()
    if not tokens:
        raise _Refuse("Usage: /goose %s @person <kind> <reason> [link]. Kinds: %s." % (
            "bigstar" if big else "star", ", ".join(STAR_KINDS)))
    kind = tokens[0].lower()
    if kind not in STAR_KINDS:
        raise _Refuse("Kinds: %s." % ", ".join(STAR_KINDS))
    tokens = tokens[1:]
    link = ""
    if tokens and _link_from_token(tokens[-1]):
        link = _link_from_token(tokens.pop())
    reason = " ".join(tokens).strip()
    if not reason:
        raise _Refuse("A reason is required: /goose %s @person <kind> <reason> [link]." % (
            "bigstar" if big else "star"))
    if big:
        return _bigstar(ctx, event, uid, kind, reason, link)
    return _award(ctx, event, uid, kind, reason, link, "star", ctx.config.scoring.star_points)


def _bigstar(ctx: AppContext, event: Event, uid: str, kind: str, reason: str, link: str) -> Result:
    """The first organizer records a pending approval. A different organizer's identical call completes it."""
    now = ctx.clock.now()
    actor = event.user
    key = "bigstar_pending:%s:%s" % (uid, kind)
    raw = ctx.db.get_setting(key, "")
    pending: Optional[Dict[str, Any]] = None
    if raw:
        try:
            pending = json.loads(raw)
        except ValueError:
            pending = None
        if pending and now - float(pending.get("ts") or 0) > BIGSTAR_PENDING_TTL_S:
            pending = None
    if pending is None:
        ctx.db.set_setting(key, json.dumps({"by": actor, "ts": now}))
        ctx.audit.append(actor, "bigstar_request", target=uid, detail="kind=%s reason=%s" % (kind, reason), ts=now)
        return Result(actions=[
            _alert(ctx, "Big star proposed for <@%s> (%s) by <@%s>: %s. A second organizer must repeat the command." % (
                uid, kind, actor, _esc(reason))),
            Action(kind="ephemeral", channel=event.channel, user=actor,
                   text="Recorded. A second organizer must repeat /goose bigstar with the same person and kind."),
        ])
    if pending.get("by") == actor:
        raise _Refuse("You already proposed this bigstar. A different organizer must repeat the command.")
    ctx.db.set_setting(key, "")
    ctx.audit.append(actor, "bigstar_approved", target=uid, detail="kind=%s second_approver" % kind, ts=now)
    return _award(ctx, event, uid, kind, reason, link, "bigstar", ctx.config.scoring.bigstar_points)


def _cmd_revoke(ctx: AppContext, event: Event, args: str) -> Result:
    tokens = args.split(None, 1)
    if not tokens or not tokens[0].isdigit():
        raise _Refuse("Usage: /goose revoke <fact id> <reason>.")
    fid = int(tokens[0])
    reason = tokens[1].strip() if len(tokens) > 1 else ""
    if not reason:
        raise _Refuse("A reason is required: /goose revoke <fact id> <reason>.")
    fact = ctx.ledger.get_fact(fid)
    if fact is None:
        raise _Refuse("No row has that id.")
    now = ctx.clock.now()
    if not ctx.ledger.void_fact(fact_id=fid, actor=event.user, reason=reason, now=now):
        raise _Refuse("That row is already voided.")
    ctx.audit.append(event.user, "revoke", target=str(fid), detail="user=%s reason=%s" % (
        fact["user_id"], reason), ts=now)
    actions = [_alert(ctx, "Row %d revoked by <@%s> (member <@%s>): %s" % (
        fid, event.user, fact["user_id"], _esc(reason)))]
    note = "An entry of yours was removed by an organizer. Reason: %s" % reason
    for target in (fact["user_id"], fact.get("counterparty") or ""):
        if target and target != event.user:
            told = _tell(ctx, target, note)
            if told:
                actions.append(told)
    actions.append(Action(kind="ephemeral", channel=event.channel, user=event.user, text="Row %d voided." % fid))
    return Result(actions=actions)


def _cmd_confirm_week(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    tokens = args.split()
    wid = _week_arg(ctx, tokens[0], now) if tokens else ctx.clock.previous_week_id(ctx.clock.week_id(now))
    status, info = ctx.weekly.confirm_week(wid, event.user, now)
    if status == "error":
        raise _Refuse("Week %s cannot be confirmed: %s." % (wid, _esc(str((info or {}).get("reason", "error")))))
    if status == "already":
        raise _Refuse("Week %s is already confirmed." % wid)
    if status == "need_more":
        ctx.audit.append(event.user, "confirm_week", target=wid, detail="recorded, more confirmations needed", ts=now)
        return Result(actions=[
            _alert(ctx, "Week %s: confirmation recorded by <@%s>. More are needed." % (wid, event.user)),
            Action(kind="ephemeral", channel=event.channel, user=event.user,
                   text="Recorded. More confirmations are needed before the Pond Report goes out."),
        ])
    ctx.audit.append(event.user, "confirm_week", target=wid, detail="confirmed", ts=now)
    actions = [_alert(ctx, "Week %s confirmed by <@%s>. The Pond Report is posted." % (wid, event.user))]
    actions.extend(pond_report(ctx, wid))
    actions.append(Action(kind="ephemeral", channel=event.channel, user=event.user, text="Week %s confirmed." % wid))
    return Result(actions=actions)


def _cmd_disqualify(ctx: AppContext, event: Event, args: str) -> Result:
    uid, rest = _person(args)
    tokens = rest.split(None, 1)
    if not tokens:
        raise _Refuse("Usage: /goose disqualify @person <week id or all> <reason>.")
    now = ctx.clock.now()
    scope = "all" if tokens[0].lower() == "all" else _week_arg(ctx, tokens[0], now)
    reason = tokens[1].strip() if len(tokens) > 1 else ""
    if not reason:
        raise _Refuse("A reason is required: /goose disqualify @person <week or all> <reason>.")
    if not ctx.weekly.disqualify(scope, uid, reason, event.user, now):
        raise _Refuse("Nothing to remove for that person in that ranking.")
    ctx.audit.append(event.user, "disqualify", target=uid, detail="scope=%s reason=%s" % (scope, reason), ts=now)
    actions = [_alert(ctx, "<@%s> disqualified for %s by <@%s>: %s" % (uid, scope, event.user, _esc(reason)))]
    told = _tell(ctx, uid, "You were removed from the %s ranking by an organizer. Reason: %s" % (scope, reason))
    if told:
        actions.append(told)
    actions.append(Action(kind="ephemeral", channel=event.channel, user=event.user, text="Disqualified for %s." % scope))
    return Result(actions=actions)


def _cmd_skip_week(ctx: AppContext, event: Event, args: str) -> Result:
    now = ctx.clock.now()
    tokens = args.split(None, 1)
    if tokens and (DATE_RE.match(tokens[0]) or tokens[0].lower() in ("last", "current")):
        wid = _week_arg(ctx, tokens[0], now)
        reason = tokens[1].strip() if len(tokens) > 1 else ""
    else:
        wid = ctx.clock.previous_week_id(ctx.clock.week_id(now))
        reason = args.strip()
    if not reason:
        raise _Refuse("A reason is required: /goose skip-week [id] <reason>.")
    if not ctx.weekly.skip_week(wid, reason, event.user, now):
        raise _Refuse("Week %s cannot be skipped (not closed yet, or already confirmed)." % wid)
    ctx.audit.append(event.user, "skip_week", target=wid, detail="reason=%s" % reason, ts=now)
    return Result(actions=[
        _alert(ctx, "Week %s skipped by <@%s>: %s" % (wid, event.user, _esc(reason))),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="Week %s skipped." % wid),
    ])


def _cmd_confirm_final(ctx: AppContext, event: Event, args: str) -> Result:
    if not ctx.config.is_owner(event.user):
        raise _Refuse(OWNER_ONLY)
    now = ctx.clock.now()
    status, info = ctx.weekly.confirm_final(event.user, now)
    if status == "error":
        raise _Refuse("The final result cannot be confirmed: %s." % _esc(str((info or {}).get("reason", "error"))))
    if status == "already":
        raise _Refuse("The final result is already confirmed.")
    ctx.audit.append(event.user, "confirm_final", target="final", detail="confirmed", ts=now)
    return Result(actions=[
        _alert(ctx, "Final result confirmed by the owner (<@%s>)." % event.user),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="Final result confirmed."),
    ])


def _cmd_set_end(ctx: AppContext, event: Event, args: str) -> Result:
    if not ctx.config.is_owner(event.user):
        raise _Refuse(OWNER_ONLY)
    token = args.split()[0] if args.split() else ""
    try:
        new_day = dt.date.fromisoformat(token)
    except ValueError:
        raise _Refuse("Usage: /goose set-end YYYY-MM-DD.")
    now = ctx.clock.now()
    old = ctx.weekly.end_ts()
    old_text = ctx.clock.local(old).date().isoformat() if old else "none"
    ctx.weekly.set_end(new_day.isoformat(), event.user)
    ctx.audit.append(event.user, "set_end", target="program", detail="from=%s to=%s" % (old_text, new_day.isoformat()),
                     ts=now)
    return Result(actions=[
        _alert(ctx, "End date changed from %s to %s by the owner." % (old_text, new_day.isoformat())),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="End date set to %s." % new_day.isoformat()),
    ])


def _cmd_freeze(ctx: AppContext, event: Event, args: str) -> Result:
    uid, rest = _person(args)
    tokens = rest.split(None, 1)
    if not tokens or not tokens[0].isdigit() or not 1 <= int(tokens[0]) <= 72:
        raise _Refuse("Usage: /goose freeze @person <hours 1 to 72> <reason>.")
    hours = int(tokens[0])
    reason = tokens[1].strip() if len(tokens) > 1 else ""
    if not reason:
        raise _Refuse("A reason is required: /goose freeze @person <hours> <reason>.")
    _require_joined(ctx, uid)
    now = ctx.clock.now()
    ctx.users.freeze(uid, now + hours * 3600, reason, event.user, now)
    ctx.audit.append(event.user, "freeze", target=uid, detail="hours=%d reason=%s" % (hours, reason), ts=now)
    actions = [_alert(ctx, "<@%s> frozen for %d hours by <@%s>: %s" % (uid, hours, event.user, _esc(reason)))]
    told = _tell(ctx, uid, "Scoring is paused for you for %d hours. Reason: %s" % (hours, reason))
    if told:
        actions.append(told)
    actions.append(Action(kind="ephemeral", channel=event.channel, user=event.user, text="Frozen for %d hours." % hours))
    return Result(actions=actions)


def _cmd_unfreeze(ctx: AppContext, event: Event, args: str) -> Result:
    uid, _rest = _person(args)
    now = ctx.clock.now()
    if not ctx.users.is_frozen(uid, now):
        raise _Refuse("That person is not frozen.")
    if not ctx.users.unfreeze(uid, event.user, now):
        raise _Refuse("That person is not frozen.")
    ctx.audit.append(event.user, "unfreeze", target=uid, detail="", ts=now)
    actions = [_alert(ctx, "<@%s> unfrozen by <@%s>." % (uid, event.user))]
    told = _tell(ctx, uid, "Scoring is back on for you.")
    if told:
        actions.append(told)
    actions.append(Action(kind="ephemeral", channel=event.channel, user=event.user, text="Unfrozen."))
    return Result(actions=actions)


def _cmd_reports(ctx: AppContext, event: Event, args: str) -> Result:
    rows = ctx.db.query(
        "SELECT id, user_id, kind, text, opened_ts, due_ts FROM reports WHERE status='open' ORDER BY id LIMIT 50")
    lines = []
    for r in rows:
        lines.append("#%d %s from <@%s> opened %s due %s: %s" % (
            r["id"], r["kind"], r["user_id"], ctx.clock.local(r["opened_ts"]).date().isoformat(),
            ctx.clock.local(r["due_ts"]).date().isoformat(), _esc(r["text"])))
    return _private(event, "\n".join(lines) if lines else "No open reports or appeals.")


def _cmd_resolve(ctx: AppContext, event: Event, args: str) -> Result:
    tokens = args.split(None, 1)
    if not tokens or not tokens[0].isdigit():
        raise _Refuse("Usage: /goose resolve <id> <note>.")
    rid = int(tokens[0])
    note = tokens[1].strip() if len(tokens) > 1 else ""
    if not note:
        raise _Refuse("A note is required: /goose resolve <id> <note>.")
    now = ctx.clock.now()
    with ctx.db.tx():
        cur = ctx.db.execute(
            "UPDATE reports SET status='resolved', outcome=?, resolved_by=? WHERE id=? AND status='open'",
            (note, event.user, rid))
        changed = cur.rowcount
    if not changed:
        raise _Refuse("No open report has that id.")
    ctx.audit.append(event.user, "resolve", target=str(rid), detail="note=%s" % note, ts=now)
    return Result(actions=[
        _alert(ctx, "Report %d resolved by <@%s>: %s" % (rid, event.user, _esc(note))),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="Report %d resolved." % rid),
    ])


def _cmd_prompt(ctx: AppContext, event: Event, args: str) -> Result:
    text = args.strip()
    if not text:
        raise _Refuse("Usage: /goose prompt <text>.")
    if len(text) > 300:
        raise _Refuse("The prompt must be 300 characters or fewer.")
    ctx.db.set_setting("prompt", text)
    ctx.audit.append(event.user, "prompt", target="weekly", detail="len=%d" % len(text), ts=ctx.clock.now())
    return _private(event, "Prompt saved. It shows in the next Pond Report.")


def _cmd_announce(ctx: AppContext, event: Event, args: str) -> Result:
    text = args.strip()
    if not text:
        raise _Refuse("Usage: /goose announce <text>.")
    if len(text) > 1000:
        raise _Refuse("The announcement must be 1000 characters or fewer.")
    if re.search(r"@(channel|here|everyone)", text, re.IGNORECASE):
        raise _Refuse("Broadcast mentions are not allowed in announcements.")
    if not ctx.config.rewards.announced and _has_reward_word(text, REWARD_STRONG):
        raise _Refuse("Reward wording is not allowed while rewards are unannounced.")
    if not ctx.config.channels.scored:
        raise _Refuse("No scored channel is configured.")
    channel = ctx.config.channels.scored[0]
    ctx.audit.append(event.user, "announce", target=channel, detail="len=%d" % len(text), ts=ctx.clock.now())
    return Result(actions=[
        Action(kind="post", channel=channel, text=_esc(text)),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="Announced."),
    ])


def _cmd_pause(ctx: AppContext, event: Event, args: str) -> Result:
    return _set_paused(ctx, event, True)


def _cmd_resume(ctx: AppContext, event: Event, args: str) -> Result:
    return _set_paused(ctx, event, False)


def _set_paused(ctx: AppContext, event: Event, paused: bool) -> Result:
    ctx.db.set_setting("paused", "1" if paused else "0")
    word = "paused" if paused else "resumed"
    ctx.audit.append(event.user, word, target="scoring", detail="", ts=ctx.clock.now())
    return Result(actions=[
        _alert(ctx, "The duck is %s by <@%s>." % (word, event.user)),
        Action(kind="ephemeral", channel=event.channel, user=event.user, text="The duck is %s." % word),
    ])


def _cmd_voice(ctx: AppContext, event: Event, args: str) -> Result:
    tokens = args.split()
    sub = tokens[0].lower() if tokens else "status"
    vc = ctx.voice_client
    if vc is None:
        return _private(event, "The voice is not wired in this process. The core keeps working without it.")
    now = ctx.clock.now()
    if sub in ("on", "off"):
        vc.set_enabled(sub == "on")
        ctx.audit.append(event.user, "voice_" + sub, target="voice", detail="", ts=now)
        return Result(actions=[
            _alert(ctx, "Voice switched %s by <@%s>." % (sub, event.user)),
            Action(kind="ephemeral", channel=event.channel, user=event.user, text="Voice is %s." % sub),
        ])
    if sub == "status":
        st = vc.status(now)
        return _private(event, "\n".join("%s: %s" % (k, _esc(str(st[k]))) for k in sorted(st)))
    raise _Refuse("Usage: /goose voice on|off|status.")


def _cmd_config(ctx: AppContext, event: Event, args: str) -> Result:
    if args.strip().lower() not in ("", "show"):
        raise _Refuse("Usage: /goose config show.")
    cfg = ctx.config
    vc = ctx.voice_client
    end = ctx.weekly.end_ts()
    end_text = ctx.clock.local(end).date().isoformat() if end else "not set"
    lines = [
        "Scored channels: %s" % ", ".join(cfg.channels.scored),
        "Organizer channel: %s" % cfg.channels.organizer,
        "Organizers: %d, owner set: %s" % (len(cfg.people.organizers), "yes" if cfg.people.owner else "no"),
        "Confirmations required: %d" % cfg.people.confirmations_required,
        "Voice enabled in config: %s, runtime: %s" % (_yn(cfg.voice.enabled),
                                                      _yn(vc.enabled()) if vc is not None else "not wired"),
        "Voice model: %s" % (cfg.voice.model or "none"),
        "Rewards announced: %s" % _yn(cfg.rewards.announced),
        "Timezone: %s, launch %s, end %s" % (cfg.schedule.timezone, cfg.schedule.launch_date or "not set", end_text),
        "Humor level %d, german rate %s, roast rate %s" % (cfg.vibes.humor_level, cfg.vibes.german_rate,
                                                          cfg.vibes.roast_rate),
        "Paused: %s" % _yn(ctx.db.get_setting("paused", "0") == "1"),
    ]
    return _private(event, "\n".join(lines))


# ---------------------------------------------------------------------------------------------------------- dispatch

_OWNER_COMMANDS = ("confirm-final", "set-end")

_DISPATCH: Dict[str, Callable[[AppContext, Event, str], Result]] = {
    "results": _cmd_results,
    "winners": _cmd_winners,
    "stars": _cmd_stars,
    "roster": _cmd_roster,
    "audit": _cmd_audit,
    "export": _cmd_export,
    "star": lambda ctx, event, args: _cmd_star(ctx, event, args, big=False),
    "bigstar": lambda ctx, event, args: _cmd_star(ctx, event, args, big=True),
    "revoke": _cmd_revoke,
    "confirm-week": _cmd_confirm_week,
    "disqualify": _cmd_disqualify,
    "skip-week": _cmd_skip_week,
    "confirm-final": _cmd_confirm_final,
    "set-end": _cmd_set_end,
    "freeze": _cmd_freeze,
    "unfreeze": _cmd_unfreeze,
    "reports": _cmd_reports,
    "resolve": _cmd_resolve,
    "prompt": _cmd_prompt,
    "announce": _cmd_announce,
    "pause": _cmd_pause,
    "resume": _cmd_resume,
    "voice": _cmd_voice,
    "config": _cmd_config,
}


def handle(ctx: AppContext, event: Event) -> Result:
    """event.command in types.ORGANIZER_COMMANDS. Commands (args shown after the name):
    results week [last|YYYY-MM-DD]   top 10 for a week with breakdowns (provisional or confirmed)
    results all [count]              all-time top N with breakdowns
    winners                          confirmed weekly winners and the final result
    stars [week|month|all] [@person] stars with giver, kind, reason, link
    roster                           everyone joined: join date, show, rest, quiet, flags
    audit [count]                    last N audit entries
    export week [id] | all | stars | ledger   -> a 'file' action with CSV; cells starting with = + - @ get a leading '
    star @person <kind> <reason> [link]      25 crumbs; kinds: active, help, made, idea, hosted, other; adds the :star:
                                             reaction on the linked message when the link is a Slack message link
    bigstar @person <kind> <reason> [link]   60 crumbs
    revoke <fact id> <reason>
    confirm-week [id]                Weekly.confirm_week; on 'confirmed' also return the Pond Report post for the scored channel
    disqualify @person <week id|all> <reason>
    skip-week [id] <reason>
    confirm-final                    owner only
    set-end <YYYY-MM-DD>             owner only; audited
    freeze @person <hours up to 72> <reason> / unfreeze @person
    reports / resolve <id> <note>
    prompt <text>                    stores the weekly prompt (settings 'prompt') shown in the Pond Report
    announce <text>                  post as the duck in the first scored channel; escaped; reward words refused while unannounced
    pause / resume                   settings 'paused': when paused the router does nothing except these commands
    voice on|off|status              VoiceClient.set_enabled / status; works with the voice off
    config show                      non-secret settings summary
    Mentions arrive as <@U123|name> tokens in args (event.mentions has the ids). A star needs a joined recipient."""
    command = (event.command or "").lower()
    if not ctx.config.is_organizer(event.user):
        return _private(event, NOT_ORGANIZER)
    fn = _DISPATCH.get(command)
    if fn is None:
        return _private(event, "Unknown organizer command. Try /goose help.")
    args = (event.args or "").strip()
    try:
        return fn(ctx, event, args)
    except _Refuse as refusal:
        return _private(event, str(refusal))


def _first_scored(ctx: AppContext) -> str:
    return ctx.config.channels.scored[0] if ctx.config.channels.scored else ""


def _show_on(ctx: AppContext, user_id: str) -> bool:
    row = ctx.users.get(user_id)
    return bool(row and row.get("show"))


def _busiest(ctx: AppContext, week_id: str) -> Optional[str]:
    """The person with the most qualified messages that week, or None when it is nobody (or a hidden or quiet person)."""
    best_uid, best_n = "", 0
    for uid, bd in sorted(ctx.ledger.week_breakdowns(week_id).items()):
        n = int(bd.n_msgs)
        if n > best_n:
            best_uid, best_n = uid, n
    if not best_uid or best_n <= 0:
        return None
    row = ctx.users.get(best_uid) or {}
    if not row.get("show") or row.get("quiet"):
        return None
    return best_uid


def pond_report(ctx: AppContext, week_id: str):
    """The Pond Report text for a confirmed week (header, top 3 as <@U> mentions for people with show on, busiest duck,
    prompt, footer) from the line bank, with rewards wording only if config.rewards.announced. Returns a list of Actions."""
    channel = _first_scored(ctx)
    if not channel:
        return []
    announced = bool(ctx.config.rewards.announced)
    rate = ctx.config.vibes.german_rate
    week_no = dt.date.fromisoformat(week_id).isocalendar()[1]
    parts: List[str] = []

    def add(text: str) -> None:
        if not announced and _has_reward_word(text, REWARD_STRONG + REWARD_WEAK):
            return
        parts.append(text)

    def line(moment: str, slots: Optional[Dict[str, Any]] = None) -> str:
        picked = ctx.lines.pick(moment, ctx.rng, slots=slots, allow_german=True, german_rate=rate)
        return ctx.lines.expand(picked.text)

    add(line("pond_report_header", {"n": week_no}))
    for place, row in enumerate(ctx.weekly.snapshot(week_id)[:3], start=1):
        uid = row.get("user_id", "")
        if uid and _show_on(ctx, uid):
            add("%d. <@%s>" % (place, uid))
    add(line("pond_report_result"))
    busiest = _busiest(ctx, week_id)
    if busiest:
        add(line("busiest_duck", {"name": "<@%s>" % busiest}))
    prompt = ctx.db.get_setting("prompt", "")
    if prompt:
        add("This week's prompt: %s" % _esc(prompt))
    add(line("pond_report_footer"))
    if not parts:
        return []
    return [Action(kind="post", channel=channel, text="\n".join(parts))]
