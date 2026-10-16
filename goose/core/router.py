"""The single event router. OWNER: agent C2a (router). Behavior: docs/PLAN.md sections 3, 4, 6, 10.3, 10.10, 11.

handle(event) is pure with respect to Slack: it reads/writes the database and returns a Result (actions + voice jobs).
It never calls the voice. The gateway runs the jobs and posts the actions.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from goose.core import commands_member
from goose.core.card import build_card
from goose.core.commands_member import index_of, row_user_id
from goose.core.context import AppContext
from goose.core.rules import (
    extract_user_ids, is_elaborate_request, is_explain_request, is_near_duplicate, mentions_goose,
    qualify_message, simhash,
)
from goose.core.safety import is_distress, is_program_question, is_sad, safety_reply
from goose.types import ORGANIZER_COMMANDS, Action, Event, Result, VoiceJob

_HOUR = 3600
_CLASSES = {"message": "new", "message_changed": "changed", "message_deleted": "deleted"}
_STAGE_MOMENT = {"calm": "calm", "annoyed": "annoyed", "angry": "angry", "timeout": "timeout", "cooling": "cooling"}
_REACTIONS = ("reaction_added", "reaction_removed")
_SHORTCUT_COMMANDS = ("me", "top")

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
    return t.strip()[:limit]


class Router:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def handle(self, event: Event) -> Result:
        """Order of work for every event:
        1. Ignore events from bots (event.is_bot) and from the bot's own user id. Dedupe: skip if `processed` has
           event_id, or (channel, class, message_ts) where class is new|changed|deleted; record it.
        2. kind 'command' -> member or organizer handler (types.MEMBER_COMMANDS / ORGANIZER_COMMANDS).
        3. Only act in config.channels.scored: messages, mentions and member commands. Organizer commands run in the
           organizer channel.
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
        level-up note in Users last_channel (moment 'level_up' with {n}, {title}) and update announced_level."""
        ctx = self.ctx
        cfg = ctx.config
        res = Result()
        for uid in user_ids or []:
            if not uid or not ctx.users.is_joined(uid):
                continue
            user = ctx.users.get(uid) or {}
            level = ctx.levels.level_for(ctx.ledger.lifetime(uid))
            announced = int(user.get("announced_level") or 1)
            if level <= announced:
                continue
            channel = user.get("last_channel") or (cfg.channels.scored[0] if cfg.channels.scored else "")
            if not channel:
                continue
            title = ctx.levels.title(level)
            moment = "level_12" if level == 12 else "level_up"
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
        keys = self._dedupe_keys(event)
        if not keys:
            return True
        fresh = True
        now = ctx.clock.now()
        for key in keys:
            cur = ctx.db.execute("INSERT OR IGNORE INTO processed (key, ts) VALUES (?, ?)", (key, now))
            if cur.rowcount != 1:
                fresh = False
        return fresh

    def _dedupe_keys(self, event: Event) -> List[str]:
        """Every key that makes this event unique. A later delivery carrying any of them is ignored.

        Messages use the event id and (channel, class, message ts). When Slack gives no event id (or the kind is not a
        message), a key is derived from what the event carries so an exact redelivery is still ignored once."""
        keys: List[str] = []
        if event.event_id:
            keys.append("evt:" + event.event_id)
        cls = _CLASSES.get(event.kind)
        if cls:
            ts = event.deleted_ts if (cls == "deleted" and event.deleted_ts) else event.message_ts
            if ts:
                keys.append("msg:%s:%s:%s" % (event.channel, cls, ts))
            return keys
        if not event.event_id:
            if event.kind in _REACTIONS and event.message_ts:
                keys.append("react:%s:%s:%s:%s:%s" % (event.channel, event.kind, event.message_ts, event.user,
                                                      event.reaction))
            elif event.kind == "command" and (event.command or event.args):
                keys.append("cmd:%s:%s:%s:%s" % (event.channel, event.user, event.command, event.args))
            elif event.kind == "member_joined":
                keys.append("join:%s:%s" % (event.channel, event.user))
        return keys

    def _paused(self) -> bool:
        return self.ctx.db.get_setting("paused", "0") == "1"

    def _command(self, event: Event) -> Result:
        cmd = (event.command or "").strip().lower()
        if cmd in ORGANIZER_COMMANDS:
            from goose.core import commands_organizer
            return commands_organizer.handle(self.ctx, event)
        if self._paused():
            return Result()
        if not self.ctx.config.is_scored_channel(event.channel):
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

    def _mention_shortcut(self, ev: Event) -> Optional[Tuple[str, str]]:
        """A ping that starts with `me` or `top` (`@goose-lee me`) -> (command, args), else None.

        Only the two shortcuts in PLAN section 8. The bot mention is read as an id; any label is ignored."""
        text = (ev.text or "").lstrip()
        mention = re.compile(r"<@%s(?:\|[^>]*)?>" % re.escape(self.ctx.bot_user_id))
        match = mention.match(text)
        if match is None:
            return None
        rest = text[match.end():].strip()
        if not rest:
            return None
        parts = rest.split(None, 1)
        command = parts[0].lower()
        if command not in _SHORTCUT_COMMANDS:
            return None
        return command, (parts[1].strip() if len(parts) > 1 else "")

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
        addr = ctx.vibes.classify_addressing(ev, ctx.bot_user_id)
        if addr == "orry":
            return Result()
        flags = self._flags(ev.user, joined)
        text = ev.text or ""
        in_thread = bool(ev.thread_ts) and ev.thread_parent_user == ctx.bot_user_id
        ping = addr == "ping"
        name = addr == "name"
        addressed = ping or in_thread
        if not addressed and not name:
            return self._ambient(ev, now, flags)

        sad = is_sad(text, cfg)
        if is_distress(text, cfg):
            return self._distress(ev)
        if sad:
            return Result.of(self._eph(ev, safety_reply(cfg))) if addressed else Result()

        if name and not addressed:
            if self._is_goose_call(ev, text, flags):
                return self._goose(ev, now)
            res = Result()
            emoji = ctx.vibes.name_reaction(text, ev.user, now, quiet=flags.quiet, sad=False)
            if emoji:
                res.actions.append(self._reaction(ev, emoji))
            return res

        if self._is_goose_call(ev, text, flags):
            return self._goose(ev, now)

        res = Result()
        if not flags.quiet:
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
        if ping:
            shortcut = self._mention_shortcut(ev)
            if shortcut is not None:
                command_event = Event(kind="command", ts=ev.ts, channel=ev.channel, user=ev.user,
                                      command=shortcut[0], args=shortcut[1])
                res.extend(commands_member.handle(ctx, command_event))
                return res
        self._chat(ev, now, res, ping=ping, in_thread=in_thread, flags=flags, text=text)
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
              text: str) -> None:
        """Addressed general chat. Voice on and within the message limit -> a VoiceJob. Otherwise a canned line,
        gated so repeats stay quiet (ack_allowed)."""
        ctx = self.ctx
        if not self._voice_on(now):
            if ctx.vibes.ack_allowed(ev.user, now):
                res.actions.append(self._post(ev, self._line("ack" if ping else "chat_fallback", allow_german=True)))
            return
        allowed, why = ctx.voice_client.allow_person(ev.user, now)
        if not allowed:
            if why == "limit" and ctx.vibes.ack_allowed(ev.user, now):
                res.actions.append(self._post(ev, self._line("limit_reached")))
            return
        res.jobs.append(self._voice_job(ev, now, text, in_thread, flags))
        ctx.voice_client.record_person(ev.user, now)

    def _voice_job(self, ev: Event, now: float, text: str, in_thread: bool, flags: _Flags) -> VoiceJob:
        ctx = self.ctx
        cfg = ctx.config
        elaborate = in_thread and is_elaborate_request(text)
        reply_cap = cfg.voice.elaborate_max_chars if elaborate else cfg.voice.reply_max_chars
        allow_german = ctx.rng.random() < cfg.vibes.german_rate
        newcomer = ctx.users.is_newcomer(ev.user, now)
        allow_roast = bool(_ROAST_ME.search(text)) and not flags.quiet and not newcomer
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
                "thread": [],
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
        ctx = self.ctx
        return Result(actions=[
            Action(kind="ephemeral", channel=ev.channel, user=ev.user, text=safety_reply(ctx.config),
                   thread_ts=self._thread(ev)),
            Action(kind="organizer_post", channel=ctx.organizer_channel,
                   text=("Possible distress in a message addressed to the duck. User `%s`, channel `%s`. "
                         "Please check in. The message text is not stored or shown here.") % (ev.user, ev.channel)),
        ])

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
        return res

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
        if user.get("rest"):
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
        ts = ev.ts or now
        if ev.reaction == "duck":
            ctx.ledger.add_fact(idem_key="thanks:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user),
                                user_id=author, kind="thanks", ts=ts, counterparty=ev.user, actor=ev.user,
                                channel=ev.channel, message_ts=ev.message_ts, state="pending")
        elif (ev.reaction == "white_check_mark" and ev.thread_ts and ev.thread_ts != ev.message_ts
              and ev.thread_parent_user and ev.user == ev.thread_parent_user):
            ctx.ledger.add_fact(idem_key="helpful:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user),
                                user_id=author, kind="helpful", ts=ts, counterparty=ev.user, actor=ev.user,
                                channel=ev.channel, message_ts=ev.message_ts, state="pending")
        return res

    def _reaction_removed(self, ev: Event, now: float) -> Result:
        ctx = self.ctx
        if ev.reaction == "duck":
            key = "thanks:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user)
        elif ev.reaction == "white_check_mark":
            key = "helpful:%s:%s:%s" % (ev.channel, ev.message_ts, ev.user)
        else:
            return Result()
        ctx.ledger.void_fact(idem_key=key, actor=ev.user, reason="reaction removed", now=now)
        return Result()

    def _member_joined(self, ev: Event) -> Result:
        text = self._line("welcome", allow_german=True)
        return Result.of(Action(kind="ephemeral", channel=ev.channel, user=ev.user, text=text))
