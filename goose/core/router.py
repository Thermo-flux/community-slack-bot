"""The single event router. OWNER: agent C2a (router). Behavior: docs/PLAN.md sections 3, 4, 6, 10.3, 10.10, 11.

handle(event) is pure with respect to Slack: it reads/writes the database and returns a Result (actions + voice jobs).
It never calls the voice. The gateway runs the jobs and posts the actions.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from goose.core import commands_member
from goose.core.card import build_card
from goose.core.commands_member import index_of, row_user_id
from goose.core.context import AppContext
from goose.core.rules import (
    extract_user_ids, is_calling_goose, is_elaborate_request, is_explain_request, is_near_duplicate, mentions_goose,
    qualify_message, simhash,
)
from goose.core.safety import is_distress, is_program_question, is_sad, safety_reply
from goose.core.wordlists import SECRET_PATTERNS
from goose.types import ORGANIZER_COMMANDS, Action, Event, Result, VoiceJob

_HOUR = 3600
_CLASSES = {"message": "new", "message_changed": "changed", "message_deleted": "deleted"}
_STAGE_MOMENT = {"calm": "calm", "annoyed": "annoyed", "angry": "angry", "timeout": "timeout", "cooling": "cooling"}

_REDACT_MENTION = re.compile(r"<@[UW][A-Za-z0-9]+(?:\|[^>]*)?>")
_REDACT_LINK = re.compile(r"<(?:https?|mailto):[^>]*>")
_BARE_URL = re.compile(r"https?://\S+")
_RANK_WORD = re.compile(r"\brank(?:ed|ing)?\b", re.IGNORECASE)
_SELF_WORDS = re.compile(r"\b(?:my|me|i|mine|am i|do i)\b", re.IGNORECASE)
_DUCK_WORDS = re.compile(r"\b(?:your|you|duck|goose-lee|goose lee|gooselee)\b", re.IGNORECASE)
_ROAST_ME = re.compile(r"\broast me\b", re.IGNORECASE)
_NOT_FUNNY = re.compile(r"\b(?:not funny|isn'?t funny|stop roasting|don'?t roast me)\b", re.IGNORECASE)


class _Flags:
    __slots__ = ("joined", "show", "rest", "quiet")

    def __init__(self, joined: bool, show: bool, rest: bool, quiet: bool):
        self.joined = joined
        self.show = show
        self.rest = rest
        self.quiet = quiet


def _redact(text: str, limit: int) -> str:
    t = _REDACT_MENTION.sub("someone", text)
    t = _REDACT_LINK.sub("[link]", t)
    t = _BARE_URL.sub("[link]", t)
    for pattern in SECRET_PATTERNS:
        t = pattern.sub("[redacted]", t)
    return t.strip()[:limit]


class Router:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def handle(self, event: Event) -> Result:
        """Order of work for every event:
        1. Ignore events from bots (event.is_bot) and from the bot's own user id. Dedupe: skip if `processed` has
           event_id, or (channel, class, message_ts) where class is new|changed|deleted; record it.
        2. kind 'command' -> member or organizer handler (types.MEMBER_COMMANDS / ORGANIZER_COMMANDS).
        3. Only act in config.channels.scored (commands work anywhere the bot is).
        4. kind 'message':
           - program-term gate, distress gate, sad flag (core.safety);
           - if the sender joined: flood limit (scoring.flood_seconds), qualify_message, similarity check against the
             sender's recent hashes (seen_hash, 1 hour), add a pending 'msg' fact (idem_key 'msg:<channel>:<ts>'),
             Users.set_last_channel;
           - goose gag: duck addressed + core.rules.mentions_goose + no other person named -> Mood.register ->
             reaction and a canned anger line (LineBank.canned_for_stage) per MoodResult;
           - ping/name: Vibes.classify_addressing -> 'orry' stays silent; 'ping' adds the :duck: reaction and, if
             the sender is not over the message limit, either a VoiceJob (voice enabled) or a canned ack
             (moment 'ack'); 'name' -> Vibes.name_reaction;
           - thread reply to a duck message with German + rules.is_explain_request -> ExplainService.explanation as
             a thread post;
           - word-trigger reaction (Vibes.reaction_for) when not addressed;
           - addressed + rank question ('rank', 'where do i rank', 'my rank') -> private answer (ephemeral) from the
             last confirmed snapshot: top 10 gets the number, others the pointer line; duck's own rank -> offended line.
           - general chat when addressed and not a command: VoiceJob mode 'chat' (reply cap 300, 700 if
             rules.is_elaborate_request in a thread) with a canned fallback; voice off or limit hit -> canned line;
        5. kind 'reaction_added': ':duck:' on someone else's message by a joined reactor (not the author, not a bot post)
           -> 'thanks' fact for item_user (idem 'thanks:<channel>:<message_ts>:<reactor>'), '' white_check_mark by the
           thread parent's author on a reply -> 'helpful' fact for item_user, ':question:' on a duck message with stored
           German -> private explanation (ephemeral) to the reactor.
        6. 'reaction_removed' -> void the matching thanks/helpful fact. 'message_deleted' -> void msg facts of that ts.
           'message_changed' -> re-qualify; void the fact if it no longer qualifies, ignore otherwise.
        7. 'member_joined' -> welcome ephemeral (moment 'welcome') if the channel is scored.
        Pings and commands from people who have not joined still get replies; scoring requires join."""
        ctx = self.ctx
        if event.is_bot or (event.user and event.user == ctx.bot_user_id):
            return Result()
        now = ctx.clock.now()
        with ctx.db.tx():
            if not self._claim(event):
                return Result()
            if event.kind == "command":
                return self._command(event)
            if self._paused():
                return Result()
            if not ctx.config.is_scored_channel(event.channel):
                return Result()
            if event.kind == "message":
                return self._message(event, now)
            if event.kind == "message_changed":
                return self._changed(event, now)
            if event.kind == "message_deleted":
                return self._deleted(event, now)
            if event.kind == "reaction_added":
                return self._reaction_added(event, now)
            if event.kind == "reaction_removed":
                return self._reaction_removed(event, now)
            if event.kind == "member_joined":
                return self._member_joined(event)
            return Result()

    def level_up_notes(self, user_ids) -> Result:
        """For people whose facts just became final: if their level rose above announced_level, an ephemeral
        level-up note in Users last_channel (moment 'level_up' with {n}, {title}) and update announced_level.
        Runs in one transaction, so the read of announced_level and its write cannot interleave with another event."""
        with self.ctx.db.tx():
            return self._level_up_notes(user_ids)

    def _level_up_notes(self, user_ids) -> Result:
        ctx = self.ctx
        cfg = ctx.config
        res = Result()
        seen = set()
        for uid in user_ids or []:
            if not uid or uid in seen or not ctx.users.is_joined(uid):
                continue
            seen.add(uid)
            user = ctx.users.get(uid) or {}
            level = ctx.levels.level_for(ctx.ledger.lifetime(uid))
            announced = int(user.get("announced_level") or 1)
            if level <= announced:
                continue
            channel = user.get("last_channel") or (cfg.channels.scored[0] if cfg.channels.scored else "")
            if not channel:
                continue
            title = ctx.levels.title(level)
            # The Head Duck line whenever the jump reaches level 12 (the reached level is 12, or the span includes it).
            moment = "level_12" if announced < 12 <= level else "level_up"
            line = ctx.lines.pick(moment, ctx.rng, slots={"n": level, "title": title}, allow_german=True,
                                  german_rate=cfg.vibes.german_rate)
            res.actions.append(Action(kind="ephemeral", channel=channel, user=uid,
                                      text=ctx.lines.expand(line.text)))
            ctx.users.set_announced_level(uid, level)
        return res

    # ------------------------------------------------------------ dispatch helpers

    def _claim(self, event: Event) -> bool:
        """Record the event keys in `processed`. False when any key was already there (a duplicate delivery)."""
        ctx = self.ctx
        keys: List[str] = []
        if event.event_id:
            keys.append("evt:" + event.event_id)
        cls = _CLASSES.get(event.kind)
        if cls:
            ts = event.deleted_ts if (cls == "deleted" and event.deleted_ts) else event.message_ts
            if ts:
                keys.append("msg:%s:%s:%s" % (event.channel, cls, ts))
        fresh = True
        now = ctx.clock.now()
        for key in keys:
            cur = ctx.db.execute("INSERT OR IGNORE INTO processed (key, ts) VALUES (?, ?)", (key, now))
            if cur.rowcount != 1:
                fresh = False
        return fresh

    def _paused(self) -> bool:
        return self.ctx.db.get_setting("paused", "0") == "1"

    def _command(self, event: Event) -> Result:
        cmd = (event.command or "").strip().lower()
        if cmd in ORGANIZER_COMMANDS:
            from goose.core import commands_organizer
            return commands_organizer.handle(self.ctx, event)
        if self._paused():
            return Result()
        return commands_member.handle(self.ctx, event)

    # ------------------------------------------------------------ actions

    def _reaction(self, ev: Event, emoji: str) -> Action:
        return Action(kind="reaction", channel=ev.channel, message_ts=ev.message_ts, emoji=emoji)

    def _thread(self, ev: Event) -> str:
        return ev.thread_ts or ev.message_ts

    def _post(self, ev: Event, text: str) -> Action:
        return Action(kind="post", channel=ev.channel, text=text, thread_ts=self._thread(ev))

    def _eph(self, ev: Event, text: str) -> Action:
        return Action(kind="ephemeral", channel=ev.channel, user=ev.user, text=text, thread_ts=self._thread(ev))

    def _line(self, moment: str, allow_german: bool = False, slots: Optional[Dict[str, Any]] = None) -> str:
        ctx = self.ctx
        line = ctx.lines.pick(moment, ctx.rng, slots=slots, allow_german=allow_german,
                              german_rate=ctx.config.vibes.german_rate)
        return ctx.lines.expand(line.text)

    def _expand(self, text: str) -> str:
        return self.ctx.lines.expand(text) if "{de:" in text else text

    def _flags(self, uid: str, joined: bool) -> _Flags:
        user = (self.ctx.users.get(uid) or {}) if joined else {}
        return _Flags(joined, bool(user.get("show")), bool(user.get("rest")), bool(user.get("quiet")))

    def _others(self, ev: Event, text: str) -> bool:
        ids = set(extract_user_ids(text)) | set(ev.mentions or [])
        ids.discard(self.ctx.bot_user_id)
        return bool(ids)

    def _check_in_chat_command(self, ev: Event, text: str, now: float) -> Optional[Result]:
        if not text or bool(ev.bot_mentioned):
            return None
        clean = text.strip()
        low = clean.lower()
        ctx = self.ctx

        def _ensure_joined() -> None:
            if not ctx.users.is_joined(ev.user):
                notice = str(ctx.config.voice.notice_version)
                ctx.users.join(ev.user, now, channel=ev.channel, consent_version=notice)

        if low.startswith(("!goose", "/goose")):
            _ensure_joined()
            parts = clean.split(None, 2)
            subcmd = parts[1].lower() if len(parts) > 1 else "help"
            args = parts[2].strip() if len(parts) > 2 else ""
            cmd_ev = Event(
                kind="command", ts=now, channel=ev.channel, user=ev.user,
                command=subcmd, args=args, thread_ts=self._thread(ev),
            )
            return self._convert_to_post(ev, self._command(cmd_ev))

        if low in ("!join", "/join", "join"):
            _ensure_joined()
            cmd_ev = Event(kind="command", ts=now, channel=ev.channel, user=ev.user, command="join", thread_ts=self._thread(ev))
            return self._convert_to_post(ev, self._command(cmd_ev))

        if low.startswith(("!level", "!rank", "!card", "!me", "!stats", "/level", "/rank", "/card")) or low in ("level", "rank", "card", "stats", "my level", "my rank"):
            _ensure_joined()
            parts = clean.split(None, 1)
            args = parts[1].strip() if len(parts) > 1 else ""
            cmd_ev = Event(kind="command", ts=now, channel=ev.channel, user=ev.user, command="me", args=args, thread_ts=self._thread(ev))
            return self._convert_to_post(ev, self._command(cmd_ev))

        if low.startswith(("!leaderboard", "!top", "!board", "/leaderboard", "/top")) or low in ("leaderboard", "top", "ranks", "scores"):
            _ensure_joined()
            parts = clean.split(None, 1)
            args = parts[1].strip() if len(parts) > 1 else ""
            cmd_ev = Event(kind="command", ts=now, channel=ev.channel, user=ev.user, command="top", args=args, thread_ts=self._thread(ev))
            return self._convert_to_post(ev, self._command(cmd_ev))

        if low in ("!help", "/help", "help"):
            cmd_ev = Event(kind="command", ts=now, channel=ev.channel, user=ev.user, command="help", thread_ts=self._thread(ev))
            return self._convert_to_post(ev, self._command(cmd_ev))

        return None

    def _convert_to_post(self, ev: Event, res: Result) -> Result:
        out = Result()
        for a in res.actions:
            if a.kind == "ephemeral":
                out.actions.append(Action(kind="post", channel=a.channel or ev.channel, text=a.text, thread_ts=self._thread(ev)))
            else:
                out.actions.append(a)
        out.jobs.extend(res.jobs)
        return out

    # ------------------------------------------------------------ messages

    def _message(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        cfg = ctx.config
        if not ev.user:
            return Result()
        ts = ev.ts or now
        joined = ctx.users.is_joined(ev.user)
        if joined:
            ctx.users.set_last_channel(ev.user, ev.channel)
            self._score_message(ev, ts, now)

        text = ev.text or ""
        cmd_result = self._check_in_chat_command(ev, text, now)
        if cmd_result is not None:
            return cmd_result

        addr = ctx.vibes.classify_addressing(ev, ctx.bot_user_id)
        in_thread = bool(ev.thread_ts) and (
            ev.thread_parent_user == ctx.bot_user_id or getattr(ev, "thread_has_bot", False)
        )
        ping = addr == "ping"
        name = addr == "name" or is_calling_goose(text)
        addressed = ping or in_thread or name
        # Distress comes first, before any other gate (including the Orry silence). Addressed messages (a ping, the
        # name, a reply in the thread) get the plain safety reply; an unaddressed message gets an organizer alert only.
        if is_distress(text, cfg):
            if addr in ("ping", "name", "orry") or in_thread or name:
                return self._distress(ev)
            return self._distress_alert_only(ev)
        if addr == "orry":
            return Result()
        flags = self._flags(ev.user, joined)
        if not addressed and not name:
            return self._ambient(ev, now, flags)

        # Sad words (grief, loneliness, trivia about death) are not distress: no reaction, no roast, no German,
        # no goose call, but an addressed sad message still goes to the voice.
        sad = is_sad(text, cfg)
        if not sad and self._is_goose_call(ev, text, flags):
            return self._goose(ev, now)

        res = Result()
        if not flags.quiet and not sad:
            res.actions.append(self._reaction(ev, "duck"))
        if is_program_question(text):
            res.actions.append(self._post(ev, self._line("program_redirect")))
            return res
        rank = self._rank_actions(ev, text, now)
        if rank is not None:
            res.actions.extend(rank)
            return res
        if in_thread and is_explain_request(text) and ctx.explain.lookup(ev.channel, ev.thread_ts):
            explained = ctx.explain.explanation(ev.channel, ev.thread_ts, now)
            if explained:
                res.actions.append(self._post(ev, self._expand(explained)))
            return res
        self._chat(ev, now, res, ping=ping, in_thread=in_thread, flags=flags, text=text, sad=sad)
        return res

    def _is_goose_call(self, ev: Event, text: str, flags: _Flags) -> bool:
        """A bare 'goose' aimed at the duck, naming nobody else. Quiet people get no teasing, so they never add heat."""
        return (not flags.quiet) and mentions_goose(text) and not self._others(ev, text)

    def _goose(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        mr = ctx.mood.register(ev.user, now)
        res = Result()
        if mr.react:
            res.actions.append(self._reaction(ev, "duck"))
        if mr.reply:
            stage = "honk" if mr.honk else _STAGE_MOMENT.get(mr.stage)
            if stage:
                line = ctx.lines.canned_for_stage(stage, ctx.rng)
                res.actions.append(Action(kind="post", channel=ev.channel, text=ctx.lines.expand(line.text)))
        return res

    def _voice_on(self, now: float) -> bool:
        vc = self.ctx.voice_client
        if vc is None or not vc.enabled():
            return False
        ok, _reason = vc.available(now)
        return bool(ok)

    def _chat(self, ev: Event, now: float, res: Result, *, ping: bool, in_thread: bool, flags: _Flags,
              text: str, sad: bool = False) -> None:
        """Addressed general chat. Voice on and within the message limit -> a VoiceJob. Otherwise a canned line,
        gated so repeats stay quiet (ack_allowed). A sad message gets no German and no roast."""
        ctx = self.ctx
        if not self._voice_on(now):
            if ctx.vibes.ack_allowed(ev.user, now):
                res.actions.append(self._post(ev, self._line("ack" if ping else "chat_fallback",
                                                              allow_german=not sad)))
            return
        if not self._consented(ev.user):
            if ctx.vibes.ack_allowed(ev.user, now):
                res.actions.append(self._post(ev, self._consent_text()))
            return
        allowed, why = ctx.voice_client.allow_person(ev.user, now)
        if not allowed:
            if why == "limit" and self._limit_line_allowed(ev.user, now):
                res.actions.append(self._post(ev, self._line("limit_reached")))
            return
        res.jobs.append(self._voice_job(ev, now, text, in_thread, flags, sad=sad))
        ctx.voice_client.record_person(ev.user, now)

    def _limit_line_allowed(self, user_id: str, now: float) -> bool:
        """Over the message limit the duck posts one 'limit_reached' line and then stays quiet for that person until
        the hour rolls over (docs/PLAN.md 10.8). Records the line in remark_log when it returns True."""
        ctx = self.ctx
        row = ctx.db.one("SELECT COUNT(*) AS n FROM remark_log WHERE user_id = ? AND ts > ? AND kind = 'limit'",
                         (user_id, float(now) - _HOUR))
        if row is not None and int(row["n"]) > 0:
            return False
        ctx.db.execute("INSERT INTO remark_log (user_id, ts, kind) VALUES (?, ?, 'limit')", (user_id, float(now)))
        return True

    def _consented(self, user_id: str) -> bool:
        """The person's stored privacy-notice version must match config.voice.notice_version before any message
        from them goes to the voice."""
        user = self.ctx.users.get(user_id)
        if not user:
            now = self.ctx.clock.now()
            notice = str(self.ctx.config.voice.notice_version)
            self.ctx.users.join(user_id, now, consent_version=notice)
            return True
        return str(user.get("consent_version") or "") == str(self.ctx.config.voice.notice_version)

    def _consent_text(self) -> str:
        ctx = self.ctx
        if ctx.lines.has_moment("consent_needed") and ctx.lines.lines("consent_needed"):
            return self._line("consent_needed")
        return "The duck needs you to re-accept the privacy notice first. Use /goose join to see it."

    def _thread_context(self, ev: Event) -> List[str]:
        """Up to thread_max_messages earlier thread texts (the most recent ones), oldest first, each redacted and cut
        to thread_item_chars, within thread_total_chars in all."""
        vc = self.ctx.config.voice
        items = [t for t in list(ev.thread_excerpt or []) if t][-vc.thread_max_messages:]
        out: List[str] = []
        total = 0
        for raw in items:
            room = vc.thread_total_chars - total
            if room <= 0:
                break
            piece = _redact(raw, min(vc.thread_item_chars, room))
            if not piece:
                continue
            out.append(piece)
            total += len(piece)
        return out

    def _voice_job(self, ev: Event, now: float, text: str, in_thread: bool, flags: _Flags,
                   sad: bool = False) -> VoiceJob:
        ctx = self.ctx
        cfg = ctx.config
        elaborate = in_thread and is_elaborate_request(text)
        reply_cap = cfg.voice.elaborate_max_chars if elaborate else cfg.voice.reply_max_chars
        allow_german = (not sad) and ctx.rng.random() < cfg.vibes.german_rate
        newcomer = ctx.users.is_newcomer(ev.user, now)
        allow_roast = bool(_ROAST_ME.search(text)) and not flags.quiet and not newcomer and not sad
        request: Dict[str, Any] = {
            "v": 1,
            "request_id": uuid.uuid4().hex,
            "kind": "addressed",
            "mode": "chat",
            "persona": {
                "humor_level": cfg.vibes.humor_level,
                "german_allowed": allow_german,
                "roast_allowed": allow_roast,
                "max_chars": reply_cap,
                "elaborate": elaborate,
            },
            "card": build_card(ctx, ev.user, False, now),
            "input": {
                "message": {"handle": "you", "text": _redact(text, cfg.voice.input_max_chars)},
                "thread": self._thread_context(ev),
            },
        }
        return VoiceJob(
            request=request,
            channel=ev.channel,
            user=ev.user,
            thread_ts=self._thread(ev),
            message_ts=ev.message_ts,
            ephemeral=False,
            fallback=self._line("chat_fallback", allow_german=allow_german),
            mode="chat",
            kind="addressed",
            allow_german=allow_german,
            allow_roast=allow_roast,
            reply_cap=reply_cap,
        )

    def _distress(self, ev: Event) -> Result:
        """An addressed distress message: the plain safety reply (no joke, no German, no voice) and an alert."""
        ctx = self.ctx
        return Result(actions=[
            Action(kind="ephemeral", channel=ev.channel, user=ev.user, text=safety_reply(ctx.config),
                   thread_ts=self._thread(ev)),
            self._distress_alert(ev, "replied with the safety text"),
        ])

    def _distress_alert_only(self, ev: Event) -> Result:
        """An unaddressed distress message in scored chat: the organizers are told, the member is not answered."""
        return Result.of(self._distress_alert(ev, "did not answer, the message was not addressed to the duck"))

    def _distress_alert(self, ev: Event, what: str) -> Action:
        """Channel, message ts and user id only. Never the message text."""
        ctx = self.ctx
        return Action(kind="organizer_post", channel=ctx.organizer_channel,
                      text=("Possible distress. Channel `%s`, message ts `%s`, user `%s`. The duck %s. Please check "
                            "in. The message text is not stored or shown here.") % (
                          ev.channel, ev.message_ts, ev.user, what))

    def _ambient(self, ev: Event, now: float, flags: _Flags) -> Result:
        """Unaddressed chat: word reactions, a rare roast, a rare admiring remark. Never a voice call."""
        ctx = self.ctx
        res = Result()
        text = ev.text or ""
        sad = is_sad(text, ctx.config)
        if flags.joined and _NOT_FUNNY.search(text) and not self._others(ev, text):
            ctx.users.set_quiet(ev.user, True)
            ctx.vibes.set_not_funny(ev.user)
            return res
        emoji = ctx.vibes.reaction_for(text, ev.user, now, quiet=flags.quiet, sad=sad)
        if emoji:
            res.actions.append(self._reaction(ev, emoji))
        if not flags.joined or sad:
            return res
        newcomer = ctx.users.is_newcomer(ev.user, now)
        roast = ctx.vibes.roast_template(text, ev.user, now, quiet=flags.quiet, sad=sad, newcomer=newcomer,
                                         others_teasing=0)
        if roast:
            res.actions.append(self._post(ev, self._expand(roast)))
            return res
        if flags.show and not flags.quiet and not flags.rest:
            band = self._band(ev.user, now)
            if ctx.vibes.rank_remark_allowed(ev.user, now, quiet=flags.quiet, shown=flags.show,
                                             resting=flags.rest, band=band):
                res.actions.append(self._post(ev, self._line("rank_remark_active", allow_german=True)))

        # Natural ambient intervention in scored channels (knows when to intervene)
        if self._should_ambient_intervene(ev, text, now, flags, sad):
            self._chat(ev, now, res, ping=False, in_thread=bool(ev.thread_ts), flags=flags, text=text, sad=sad)
            self._record_ambient_intervention(ev.channel, now)

        return res

    def _should_ambient_intervene(self, ev: Event, text: str, now: float, flags: _Flags, sad: bool) -> bool:
        if not self._voice_on(now) or flags.quiet or sad:
            return False
        ctx = self.ctx
        if not ctx.config.is_scored_channel(ev.channel):
            return False
        row = ctx.db.one("SELECT MAX(ts) AS t FROM remark_log WHERE kind = 'ambient_intervene' AND user_id = ?",
                         (ev.channel,))
        last_ts = float(row["t"]) if row is not None and row["t"] else 0.0
        if now - last_ts < 180:
            return False

        words = text.split()
        has_question = ("?" in text and len(words) >= 3) or bool(re.search(
            r"\b(?:anyone know|how do (?:we|i|you)|why (?:is|does)|what (?:is|are|should)|can someone|where (?:is|can)|is there a way)\b",
            text, re.I
        ))
        is_shipping = bool(re.search(
            r"\b(?:just shipped|finally shipped|it works|fixed the bug|deployed|merged the pr|pr is up|released|built the)\b",
            text, re.I
        ))
        is_dev_topic = bool(re.search(
            r"\b(?:loadout|duck|crumbs|hatchling|pond|slack bot|api key|database|schema|docker|systemd|deploy)\b",
            text, re.I
        )) and len(words) >= 4

        return has_question or is_shipping or is_dev_topic

    def _record_ambient_intervention(self, channel: str, now: float) -> None:
        self.ctx.db.execute("INSERT INTO remark_log (user_id, ts, kind) VALUES (?, ?, 'ambient_intervene')",
                            (channel, float(now)))

    def _band(self, uid: str, now: float) -> Optional[str]:
        card = build_card(self.ctx, uid, False, now)
        return (card.get("you") or {}).get("activity_band")

    def _rank_actions(self, ev: Event, text: str, now: float) -> Optional[List[Action]]:
        """None when the text is not a rank question. Otherwise private answers only."""
        if not _RANK_WORD.search(text):
            return None
        ctx = self.ctx
        if self._others(ev, text):
            return [self._eph(ev, self._line("rank_below"))]
        if _DUCK_WORDS.search(text) and not _SELF_WORDS.search(text):
            return [self._eph(ev, self._line("rank_duck", allow_german=True))]
        if not ctx.users.is_joined(ev.user):
            return [self._eph(ev, self._line("not_joined"))]
        last = ctx.weekly.last_confirmed()
        rows = list((last or {}).get("rows") or [])
        idx = index_of(rows, ev.user)
        if idx is not None and idx < 10 and row_user_id(rows[idx]) == ev.user:
            return [self._eph(ev, self._line("rank_top10", allow_german=True, slots={"rank": idx + 1}))]
        return [self._eph(ev, self._line("rank_below", allow_german=True))]

    def _score_message(self, ev: Event, ts: float, now: float) -> None:
        """Flood limit, qualification, near-duplicate check, then one pending 'msg' fact."""
        ctx = self.ctx
        cfg = ctx.config
        uid = ev.user
        user = ctx.users.get(uid) or {}
        if user.get("rest") or ctx.users.is_frozen(uid, now):
            return
        text = ev.text or ""
        if not qualify_message(text, cfg.scoring.min_words, cfg.scoring.min_chars):
            return
        last = ctx.db.one("SELECT MAX(ts) AS t FROM facts WHERE user_id = ? AND kind = 'msg' AND state != 'voided'",
                          (uid,))
        last_ts = last["t"] if last is not None else None
        if last_ts is not None and ts - last_ts < cfg.scoring.flood_seconds:
            return
        h = simhash(text)
        recent = [int(r["hash"]) for r in ctx.db.query(
            "SELECT hash FROM seen_hash WHERE user_id = ? AND ts >= ?", (uid, now - _HOUR))]
        if is_near_duplicate(h, recent):
            return
        fact_id = ctx.ledger.add_fact(idem_key="msg:%s:%s" % (ev.channel, ev.message_ts), user_id=uid, kind="msg",
                                      ts=ts, channel=ev.channel, message_ts=ev.message_ts, state="pending")
        if fact_id is None:
            return
        ctx.db.execute("INSERT INTO seen_hash (user_id, hash, ts) VALUES (?, ?, ?)", (uid, str(h), ts))
        ctx.db.execute("DELETE FROM seen_hash WHERE user_id = ? AND ts < ?", (uid, now - _HOUR))

    # ------------------------------------------------------------ edits, deletes, reactions, joins

    def _changed(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        if qualify_message(ev.text or "", ctx.config.scoring.min_words, ctx.config.scoring.min_chars):
            return Result()
        for row in ctx.ledger.find_by_message(ev.channel, ev.message_ts, kinds=("msg",)):
            ctx.ledger.void_fact(fact_id=row["id"], actor=ev.user, reason="edited to a short message", now=now)
        return Result()

    def _deleted(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        ts = ev.deleted_ts or ev.message_ts
        for row in ctx.ledger.find_by_message(ev.channel, ts, kinds=("msg",)):
            ctx.ledger.void_fact(fact_id=row["id"], actor=ev.user, reason="deleted", now=now)
        return Result()

    def _reaction_added(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        bot = ctx.bot_user_id
        res = Result()
        author = ev.item_user
        if ev.reaction == "question" and author == bot:
            explained = ctx.explain.explanation(ev.channel, ev.message_ts, now)
            if explained:
                res.actions.append(Action(kind="ephemeral", channel=ev.channel, user=ev.user,
                                          text=self._expand(explained)))
            return res
        if not author or author == bot or author == ev.user:
            return res
        if not (ctx.users.is_joined(ev.user) and ctx.users.is_joined(author)):
            return res
        if ctx.users.is_frozen(ev.user, now):
            return res
        ts = ev.ts or now
        if ev.reaction == "duck":
            self._credit("thanks:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user),
                         user_id=author, kind="thanks", ts=ts, counterparty=ev.user, actor=ev.user,
                         channel=ev.channel, message_ts=ev.message_ts, state="pending")
        elif (ev.reaction == "white_check_mark" and ev.thread_ts and ev.thread_ts != ev.message_ts
              and ev.thread_parent_user and ev.user == ev.thread_parent_user):
            self._credit("helpful:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user),
                         user_id=author, kind="helpful", ts=ts, counterparty=ev.user, actor=ev.user,
                         channel=ev.channel, message_ts=ev.message_ts, state="pending")
        return res

    def _credit(self, base_key: str, **fields: Any) -> None:
        """Add the fact under base_key. When that key exists and is live, nothing is added (a duplicate). When it
        exists only as a voided row (the reaction was removed), the fact goes under base_key '#2', then '#3', and so on,
        so a removed and re-added reaction counts again."""
        ctx = self.ctx
        n = 1
        while True:
            key = base_key if n == 1 else "%s#%d" % (base_key, n)
            row = ctx.db.one("SELECT state FROM facts WHERE idem_key = ?", (key,))
            if row is None:
                ctx.ledger.add_fact(idem_key=key, **fields)
                return
            if row["state"] != "voided":
                return
            n += 1

    def _reaction_removed(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        if ev.reaction == "duck":
            kind = "thanks"
        elif ev.reaction == "white_check_mark":
            kind = "helpful"
        else:
            return Result()
        # Void every live fact this reactor made on this message (the original and any re-credited '#n' copies).
        rows = ctx.db.query(
            "SELECT id FROM facts WHERE kind = ? AND channel = ? AND message_ts = ? AND counterparty = ? "
            "AND state != 'voided' ORDER BY id", (kind, ev.channel, ev.message_ts, ev.user))
        for row in rows:
            ctx.ledger.void_fact(fact_id=int(row["id"]), actor=ev.user, reason="reaction removed", now=now)
        return Result()

    def _member_joined(self, ev: Event) -> Result:
        text = self._line("welcome", allow_german=True)
        return Result.of(Action(kind="ephemeral", channel=ev.channel, user=ev.user, text=text))
