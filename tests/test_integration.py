"""End-to-end scenarios for goose-lee (docs/PLAN.md sections 3, 4, 6, 7, 10).

The real components are wired with core.context.build_context: Router, Weekly, Ledger, Scheduler, the member and
organizer commands, PostFilter and VoiceClient. The only fakes are a Slack client that records calls and the HTTP
function injected into VoiceClient (http_post). The clock is a FakeClock, the rng is seeded, and nothing sleeps.

Events enter through the same functions the gateway uses (slack_io.normalize_* and app.build_handlers), so a scenario
is a Slack-shaped payload from start to posted text. Where the gateway does not fill a field the router needs, the test
sets it on the normalized Event and says so in a comment; the report lists those gaps as bugs.
"""
from __future__ import annotations

import json
import os
import random
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import pytest

from goose.config import default_config, load_config
from goose.core.clock import FakeClock
from goose.core.context import build_context
from goose.core.lines import LineBank
from goose.core.router import Router
from goose.core.safety import safety_reply
from goose.gateway import app, slack_io
from goose.gateway.poster import escape_text, render_canned
from goose.gateway.scheduler import Scheduler
from goose.gateway.voice_client import VoiceClient
from goose.types import Event, Result
from goose.voice.prompt import APPROVED_DE_IDS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")
CONFIG_PATH = os.path.join(ROOT, "config", "goose.yaml")
MANIFEST_PATH = os.path.join(ROOT, "slack", "manifest.json")

START = 1_760_000_000.0          # Thursday 2025-10-09 08:53:20 UTC, inside the week of 2025-10-06
WEEK = "2025-10-06"
PREV_WEEK = "2025-09-29"
CHANNEL = "C1"                   # the one scored channel in these tests
ORG = "CORG"                     # the organizer channel
BOT = "UBOT"
ORGANIZER = "UORG1"

SENTENCES = [
    "the pond water looks calm this morning",
    "anyone tried the new bridge build yet",
    "my cat knocked the lamp off the shelf again",
    "we should plan a movie night on friday",
    "reading about glacier melt and sea level rise",
    "the kettle decided to boil over during lunch",
    "found a great lentil soup recipe today",
    "the late train gave me time to write",
    "who else is watching the rain on the window",
    "building a small wooden birdhouse for the garden",
    "sharing a playlist that fixed my whole monday",
    "the library reopened its back study rooms",
]

GAG_MOMENTS = {
    "calm": "anger_calm",
    "annoyed": "anger_annoyed",
    "angry": "anger_angry",
    "timeout": "anger_timeout",
    "cooling": "anger_cooling",
}


# ------------------------------------------------------------------------------------------------ fakes

class FakeSlack:
    """Stands in for slack_sdk's WebClient. Records every call; returns nothing."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def chat_postMessage(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postMessage", kwargs))

    def chat_postEphemeral(self, **kwargs: Any) -> None:
        self.calls.append(("chat_postEphemeral", kwargs))

    def reactions_add(self, **kwargs: Any) -> None:
        self.calls.append(("reactions_add", kwargs))

    def files_upload_v2(self, **kwargs: Any) -> None:
        self.calls.append(("files_upload_v2", kwargs))


class FakeVoiceHTTP:
    """The http_post injected into VoiceClient. `answer` decides the reply from the request body."""

    def __init__(self, answer: Callable[[Dict[str, Any]], Tuple[int, Dict[str, Any]]]) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.answer = answer

    def __call__(self, url: str, body: Any, headers: Dict[str, str], timeout: float):
        # VoiceClient posts the signed JSON as bytes; decode it so the recorded request is the dict the voice sees.
        if isinstance(body, (bytes, bytearray)):
            body = json.loads(bytes(body).decode("utf-8"))
        self.requests.append(body)
        return self.answer(body)


def speak(text: str, numbers: Sequence[str] = ()) -> Callable[[Dict[str, Any]], Tuple[int, Dict[str, Any]]]:
    """A well-formed voice answer with a fixed text."""
    def answer(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        return 200, {
            "v": 1, "request_id": body["request_id"], "action": "speak", "text": text, "reaction": None,
            "flags": {"roasted": False, "mentions_rank": False, "uses_german": False,
                      "numbers_used": list(numbers)},
            "usage": {"tokens_in": 40, "tokens_out": 20},
        }
    return answer


def timeout_answer(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    raise TimeoutError("the voice did not answer in time")


def server_error(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 500, {"error": "internal"}


# ------------------------------------------------------------------------------------------------ world

class World:
    """One bot: the real context, router, scheduler and gateway handlers, with the two fakes wired in."""

    def __init__(self, tmp_path, *, voice: bool = False,
                 tune: Optional[Callable[[Any], None]] = None) -> None:
        cfg = default_config()
        cfg.channels.scored = [CHANNEL]
        cfg.channels.organizer = ORG
        cfg.people.organizers = [ORGANIZER, "UORG2"]
        cfg.people.owner = "UOWNER"
        cfg.voice.enabled = voice
        if tune is not None:
            tune(cfg)
        self.cfg = cfg
        self.clock = FakeClock(start=START)
        self.lines = LineBank.from_file(LINES_PATH)
        self.ctx = build_context(cfg, db_path=":memory:", audit_path=str(tmp_path / "audit.log"),
                                 lines=self.lines, clock=self.clock, rng=random.Random(1234), bot_user_id=BOT)
        self.router = Router(self.ctx)
        self.slack = FakeSlack()
        self.http = FakeVoiceHTTP(speak("Short answer: yes."))
        self.ctx.voice_client = VoiceClient(cfg, self.ctx.db, self.clock, secret="test-secret",
                                            http_post=self.http)
        self.scheduler = Scheduler(self.ctx, self.router)
        self.handlers = app.build_handlers(self.ctx, self.router, self.slack)
        self._n = 0

    # ---- ids, envelopes -------------------------------------------------------------------------------------

    def _next(self) -> int:
        self._n += 1
        return self._n

    def _ts(self) -> str:
        return "%.6f" % (self.clock.now() + self._next() * 0.001)

    def _envelope(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "event_callback", "event_id": "Ev%06d" % self._next(), "event": payload}

    # ---- inputs -----------------------------------------------------------------------------------------------

    def join(self, *users: str, show: bool = True) -> None:
        for uid in users:
            self.command("join", uid)
            if show:
                self.command("show", uid)

    def post_message(self, user: str, text: str, *, thread_ts: str = "", channel: str = CHANNEL) -> str:
        """A Slack message through build_handlers (the full gateway path). Returns the message ts."""
        ts = self._ts()
        payload: Dict[str, Any] = {"type": "message", "channel": channel, "user": user, "ts": ts, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        self.handlers["message"](self._envelope(payload))
        return ts

    def reaction(self, reactor: str, item_user: str, msg_ts: str, emoji: str, *, added: bool = True) -> None:
        payload = {"type": "reaction_added" if added else "reaction_removed", "user": reactor, "reaction": emoji,
                   "item_user": item_user, "event_ts": self._ts(),
                   "item": {"type": "message", "channel": CHANNEL, "ts": msg_ts}}
        name = "reaction_added" if added else "reaction_removed"
        self.handlers[name](self._envelope(payload))

    def command(self, text: str, user: str, channel: str = CHANNEL) -> None:
        body = {"text": text, "user_id": user, "channel_id": channel, "trigger_id": "T%06d" % self._next()}
        self.handlers["command"](body, ack=lambda: None)

    def make_event(self, user: str, text: str, *, thread_ts: str = "", thread_parent_user: str = "") -> Event:
        """A normalized message Event for router-level calls. A thread reply can be marked with the parent's author:
        slack_io never fills thread_parent_user (the gateway would need conversations.replies), so tests set it here."""
        payload: Dict[str, Any] = {"type": "message", "channel": CHANNEL, "user": user, "ts": self._ts(), "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        ev = slack_io.normalize_message(self._envelope(payload), BOT)
        assert ev is not None
        ev.thread_parent_user = thread_parent_user
        return ev

    def make_reaction(self, reactor: str, item_user: str, msg_ts: str, emoji: str, *, thread_ts: str = "",
                      thread_parent_user: str = "") -> Event:
        payload = {"type": "reaction_added", "user": reactor, "reaction": emoji, "item_user": item_user,
                   "event_ts": self._ts(), "item": {"type": "message", "channel": CHANNEL, "ts": msg_ts}}
        ev = slack_io.normalize_reaction(self._envelope(payload), True)
        assert ev is not None
        ev.thread_ts = thread_ts
        ev.thread_parent_user = thread_parent_user
        return ev

    def route_message(self, user: str, text: str) -> Result:
        """Router only, no execution: the caller inspects the actions and the VoiceJobs."""
        return self.router.handle(self.make_event(user, text))

    def dispatch(self, event: Event) -> Result:
        """What app.build_handlers does with a routed event: post the actions, run the voice jobs inline."""
        res = self.router.handle(event)
        slack_io.execute(self.slack, res.actions, event.channel)
        for job in res.jobs:
            app.run_voice_job(self.ctx, self.slack, job)
        return res

    def tick(self) -> Result:
        res = self.scheduler.tick(self.clock.now())
        app.make_deliver(self.ctx, self.slack)(res)
        return res

    # ---- outputs ------------------------------------------------------------------------------------------------

    def mark(self) -> int:
        return len(self.slack.calls)

    def since(self, mark: int) -> List[Tuple[str, Dict[str, Any]]]:
        return self.slack.calls[mark:]

    def said(self, mark: int = 0, channel: str = CHANNEL) -> List[str]:
        return posted(self.since(mark), channel)

    def count(self, table: str) -> int:
        return int(self.ctx.db.one("SELECT COUNT(*) AS n FROM %s" % table)["n"])

    def facts(self, user: str, kind: str = "msg") -> int:
        row = self.ctx.db.one("SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND kind = ? AND state != 'voided'",
                              (user, kind))
        return int(row["n"])

    def canned(self, moment: str) -> Set[str]:
        """Every line of a moment as it may appear in Slack: expanded, and expanded plus escaped."""
        out: Set[str] = set()
        for line in self.lines.lines(moment):
            out.add(self.lines.expand(line.text))
            out.add(render_canned(line.text, self.lines))
        return out

    def advance_to_monday_close(self) -> None:
        """Monday 00:15:01 UTC of the week after WEEK: the snapshot time."""
        self.clock.set(self.clock.date_to_ts("2025-10-13") + 901)


def posted(calls: Sequence[Tuple[str, Dict[str, Any]]], channel: str = CHANNEL) -> List[str]:
    return [kw["text"] for name, kw in calls if name == "chat_postMessage" and kw.get("channel") == channel]


def ephemerals(calls: Sequence[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, str]]:
    return [(kw["user"], kw["text"]) for name, kw in calls if name == "chat_postEphemeral"]


def reactions(calls: Sequence[Tuple[str, Dict[str, Any]]]) -> List[str]:
    return [kw["name"] for name, kw in calls if name == "reactions_add"]


def week_results(w: World, week: str = WEEK) -> List[str]:
    rows = w.ctx.db.query("SELECT user_id FROM results WHERE scope = 'week' AND week_id = ? ORDER BY rank", (week,))
    return [r["user_id"] for r in rows]


def table_counts(w: World) -> Dict[str, int]:
    return {t: w.count(t) for t in ("facts", "flags", "week_status", "results", "users")}


def run_week_close(w: World) -> None:
    """Scheduler tick at the snapshot time, then the organizer confirms the week."""
    w.advance_to_monday_close()
    w.tick()
    w.command("confirm-week", ORGANIZER)


# ============================================================================================== scenario 1

def test_weekly_lifecycle_close_confirm_winners_and_disqualify(tmp_path):
    w = World(tmp_path)
    w.join("UWIN1", "UMID2", "UMID3")
    sentences = iter(SENTENCES * 10)
    first_winner_msg = ""
    for _day in range(3):
        for _block in range(2):
            for uid, count in (("UWIN1", 3), ("UMID2", 2), ("UMID3", 1)):
                for _ in range(count):
                    ts = w.post_message(uid, next(sentences))
                    if uid == "UWIN1" and not first_winner_msg:
                        first_winner_msg = ts
                    w.clock.advance(45)
            w.clock.advance(2 * 3600)
        w.clock.advance(12 * 3600)

    # Thanks: two people give the winner a duck. Helpful: UMID2 asks, the winner answers, UMID2 marks the answer.
    # The helpful mark needs the thread parent author, which slack_io does not fill, so it is set on the Event.
    w.reaction("UMID2", "UWIN1", first_winner_msg, "duck")
    w.reaction("UMID3", "UWIN1", first_winner_msg, "duck")
    parent = w.post_message("UMID2", "does anyone know a good way to clean the filter")
    reply = w.post_message("UWIN1", "yes, rinse it under cold water first", thread_ts=parent)
    w.dispatch(w.make_reaction("UMID2", "UWIN1", reply, "white_check_mark", thread_ts=parent,
                               thread_parent_user="UMID2"))

    mark = w.mark()
    w.command("star <@UWIN1> active helped with the setup", ORGANIZER)
    assert any("Recorded" in text for _user, text in ephemerals(w.since(mark)))

    # Monday 00:15 of the next week: the scheduler closes the week and posts the provisional list to organizers.
    w.advance_to_monday_close()
    mark = w.mark()
    w.tick()
    alerts = [t for t in posted(w.since(mark), ORG) if "is closed" in t]
    assert len(alerts) == 1
    assert "Week %s is closed" % WEEK in alerts[0]
    assert "1. <@UWIN1>" in alerts[0]
    snapshot = [r["user_id"] for r in w.ctx.weekly.snapshot(WEEK)]
    assert snapshot[0] == "UWIN1"
    assert set(snapshot) == {"UWIN1", "UMID2", "UMID3"}

    # The organizer confirms: the Pond Report goes to the scored channel and names only people with show on.
    mark = w.mark()
    w.command("confirm-week", ORGANIZER)
    report = [t for t in posted(w.since(mark), CHANNEL) if "<@" in t]
    assert len(report) == 1
    named = set(re.findall(r"<@(U[A-Z0-9]+)>", report[0]))
    assert named == {"UWIN1", "UMID2", "UMID3"}
    results = week_results(w)
    assert results[0] == "UWIN1" and set(results) == {"UWIN1", "UMID2", "UMID3"}

    w.command("winners", ORGANIZER)
    winners_text = ephemerals(w.since(mark))[-1][1]
    assert "Week %s: 1. <@UWIN1>" % WEEK in winners_text

    # A second tick posts nothing new.
    mark = w.mark()
    w.tick()
    assert w.since(mark) == []

    # Disqualify the winner: the stored ranking re-orders and the results rows change.
    before_snapshot = [r["user_id"] for r in w.ctx.weekly.snapshot(WEEK)]
    before_results = week_results(w)
    assert before_results == before_snapshot[:3]
    w.command("disqualify <@UWIN1> 2025-10-06 star was a mistake", ORGANIZER)
    after_snapshot = [r["user_id"] for r in w.ctx.weekly.snapshot(WEEK)]
    assert "UWIN1" not in after_snapshot
    assert after_snapshot == before_snapshot[1:]
    assert week_results(w) == before_results[1:]
    mark = w.mark()
    w.command("winners", ORGANIZER)
    assert "Week %s: 1. <@%s>" % (WEEK, before_snapshot[1]) in ephemerals(w.since(mark))[-1][1]


# ============================================================================================== scenario 2

BRUCE_QUESTION = "<@UBOT> do you know who is Bruce Lee?"
WINS_QUESTION = "<@UBOT> you vs him. who wins?"


def kernel_answer(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    text = str(body["input"]["message"]["text"]).lower()
    if "bruce" in text:
        return speak("Yes. Bruce Lee was a martial artist and film star, born in 1940.", ("1940",))(body)
    return speak("Short answer: he wins on legend, you on style.")(body)


def test_kernel_pasted_chat_gets_short_in_character_voice_replies(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UASKER")
    w.http.answer = kernel_answer

    first = w.route_message("UASKER", BRUCE_QUESTION)
    w.clock.advance(10)                     # person_gap_s is 5 seconds
    second = w.route_message("UASKER", WINS_QUESTION)
    assert len(first.jobs) == 1 and len(second.jobs) == 1

    for job in first.jobs + second.jobs:
        app.run_voice_job(w.ctx, w.slack, job)

    said = w.said()
    assert said == [
        escape_text("Yes. Bruce Lee was a martial artist and film star, born in 1940."),
        escape_text("Short answer: he wins on legend, you on style."),
    ]
    assert all(len(t) < 300 for t in said)
    assert "wins" in said[1] and "1940" in said[0]
    assert len(w.http.requests) == 2


@pytest.mark.parametrize("bad", [
    "<!channel> the duck says hi to everyone",
    "Read more at https://example.com/duck",
    "Congrats, you won the prize!",
])
def test_kernel_unsafe_voice_text_falls_back_to_canned_line_and_counts_junk(tmp_path, bad):
    w = World(tmp_path, voice=True)
    w.join("UASKER")
    w.http.answer = speak(bad)
    res = w.route_message("UASKER", BRUCE_QUESTION)
    assert len(res.jobs) == 1
    for job in res.jobs:
        app.run_voice_job(w.ctx, w.slack, job)
    said = w.said()
    assert len(said) == 1
    assert said[0] in w.canned("chat_fallback")
    assert bad not in said[0]
    assert w.ctx.voice_client.status(w.clock.now())["junk_ratio"] > 0


# ============================================================================================== scenario 3

@pytest.mark.parametrize("mode", ["voice_off", "timeout", "http_500"])
def test_addressed_message_gets_one_canned_reply_when_voice_is_off_or_failing(tmp_path, mode):
    w = World(tmp_path, voice=(mode != "voice_off"))
    w.join("UPING")
    if mode == "timeout":
        w.http.answer = timeout_answer
    elif mode == "http_500":
        w.http.answer = server_error
    mark = w.mark()
    w.post_message("UPING", "<@UBOT> is this pond still open for new members")
    replies = w.said(mark)
    assert len(replies) == 1
    expected = w.canned("ack") if mode == "voice_off" else w.canned("chat_fallback")
    assert replies[0] in expected
    assert w.facts("UPING") == 1                       # scoring is unaffected
    if mode == "voice_off":
        assert w.http.requests == []
    else:
        assert len(w.http.requests) == 1               # tried once, no retry


# ============================================================================================== scenario 4

def test_message_limit_is_per_hour_then_one_canned_line(tmp_path):
    """Limit comes from config (200 an hour by default). Pings are 5 seconds apart so the whole burst fits in one
    rolling hour; the first over-limit ping gets one canned line, and nothing more until the hour rolls."""
    w = World(tmp_path, voice=True)
    w.join("UBUSY")
    limit = w.cfg.voice.messages_per_person_per_hour
    limit_texts = w.canned("limit_reached")
    jobs = 0
    for i in range(limit + 1):
        if i:
            w.clock.advance(5)
        res = w.route_message("UBUSY", "<@UBOT> ping %d while the pond is busy" % i)
        jobs += len(res.jobs)
        limit_lines = [a.text for a in res.actions if a.kind == "post" and a.text in limit_texts]
        if i < limit:
            assert limit_lines == [], "ping %d" % i
        else:
            assert len(limit_lines) == 1, "ping %d" % i
    assert jobs == limit

    # Still inside the first hour: no job and no second limit line.
    over = w.clock.now()
    for offset in (60, 300, 1200, 2400):
        w.clock.set(over + offset)
        res = w.route_message("UBUSY", "<@UBOT> still here, still busy at minute %d" % (offset // 60))
        assert res.jobs == [], "offset %d" % offset
        assert [a for a in res.actions if a.kind == "post" and a.text in limit_texts] == [], "offset %d" % offset

    # Once the first ping is more than an hour old, the window has room again.
    w.clock.set(w.clock.now() + 3600)
    res = w.route_message("UBUSY", "<@UBOT> the hour has rolled over")
    assert len(res.jobs) == 1


def test_message_limit_frees_up_when_the_hour_rolls_and_is_per_person(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UBUSY", "UCALM")
    start = w.clock.now()
    limit = w.cfg.voice.messages_per_person_per_hour
    for i in range(limit):
        w.clock.set(start + 5 * i)
        assert len(w.route_message("UBUSY", "<@UBOT> ping %d about the pond" % i).jobs) == 1
    w.clock.set(start + 5 * limit)
    assert w.route_message("UBUSY", "<@UBOT> ping %d about the pond" % limit).jobs == []
    # The other person is unaffected at the same moment.
    assert len(w.route_message("UCALM", "<@UBOT> is the pond open today").jobs) == 1
    # The first ping ages out of the hour at start + 3600, so the next ping gets a job again.
    w.clock.set(start + 3601)
    assert len(w.route_message("UBUSY", "<@UBOT> back after the hour rolled").jobs) == 1


def test_no_daily_or_monthly_cap_for_many_people_over_a_day(tmp_path):
    w = World(tmp_path, voice=True)
    people = ["UDAY%02d" % n for n in range(10)]
    w.join(*people)
    jobs = 0
    total = 0
    for _block in range(4):
        for uid in people:
            total += 1
            jobs += len(w.route_message(uid, "<@UBOT> is there news about the pond today").jobs)
            w.clock.advance(330)
    assert total == 40
    assert jobs == 40


# ============================================================================================== scenario 5

def test_request_card_has_no_ids_names_or_provisional_ranking(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UVOICEPRIVA", "UVOICEPRIVB")
    w.join("UVOICEHIDE", show=False)
    for i in range(6):
        w.post_message("UVOICEPRIVB", SENTENCES[i])
        w.clock.advance(45)
    for i in range(8):
        w.post_message("UVOICEHIDE", SENTENCES[i + 2])
        w.clock.advance(45)
    w.post_message("UVOICEPRIVA", SENTENCES[11])
    w.advance_to_monday_close()
    w.tick()                                           # provisional only: nothing confirmed yet
    assert w.ctx.weekly.snapshot(WEEK)[0]["user_id"] == "UVOICEPRIVB"

    res = w.route_message("UVOICEPRIVA", BRUCE_QUESTION)
    assert len(res.jobs) == 1
    dumped = json.dumps(res.jobs[0].request)
    for forbidden in ("UVOICEPRIVA", "UVOICEPRIVB", "UVOICEHIDE", BOT, "pond water looks calm"):
        assert forbidden not in dumped
    assert res.jobs[0].request["card"]["week"] == {}

    w.command("confirm-week", ORGANIZER)
    w.clock.advance(10)
    res = w.route_message("UVOICEPRIVA", WINS_QUESTION)
    dumped = json.dumps(res.jobs[0].request)
    for forbidden in ("UVOICEPRIVA", "UVOICEPRIVB", "UVOICEHIDE", BOT):
        assert forbidden not in dumped
    assert "standing" not in res.jobs[0].request["card"]["you"]


def test_hidden_person_never_appears_in_pond_report_or_top_replies(tmp_path):
    w = World(tmp_path)
    w.join("UWIN1", "UMID2")
    w.join("UHIDE", show=False)
    for i in range(10):                                # the hidden person posts the most
        w.post_message("UHIDE", SENTENCES[i % 12])
        w.clock.advance(45)
    for i in range(3):
        w.post_message("UWIN1", SENTENCES[(i + 3) % 12])
        w.clock.advance(45)
    w.post_message("UMID2", SENTENCES[7])
    w.clock.advance(3600)
    w.advance_to_monday_close()
    mark = w.mark()
    w.tick()
    organizer_text = " ".join(posted(w.since(mark), ORG))
    assert "UHIDE" not in organizer_text
    mark = w.mark()
    w.command("confirm-week", ORGANIZER)
    calls = w.since(mark)
    assert "UHIDE" not in " ".join(posted(calls, CHANNEL))
    assert "<@UWIN1>" in " ".join(posted(calls, CHANNEL))
    mark = w.mark()
    w.command("top", "UWIN1")
    w.command("all", "UWIN1")
    replies = " ".join(text for _user, text in ephemerals(w.since(mark)))
    assert "UHIDE" not in replies
    assert "<@UWIN1>" in replies


def test_quiet_person_gets_no_reaction_and_no_roast(tmp_path):
    def tune(cfg):
        cfg.vibes.roast_rate = 1.0                     # so a roast is certain for anyone allowed one

    w = World(tmp_path, tune=tune)
    old = START - 30 * 86400                           # joined long enough ago to not be a newcomer
    for uid in ("UQUIET", "UCONTROL"):
        w.ctx.users.join(uid, old)
        w.ctx.users.set_show(uid, True)
    w.command("quiet on", "UQUIET")

    mark = w.mark()
    w.post_message("UQUIET", "<@UBOT> is the pond loud today")
    assert reactions(w.since(mark)) == []
    mark = w.mark()
    w.post_message("UCONTROL", "<@UBOT> is the pond loud today")
    assert reactions(w.since(mark)) == ["duck"]        # the control shows the reaction path works

    mark = w.mark()
    w.post_message("UQUIET", "wow!!!!! lol lol lol this pond is great")
    assert w.since(mark) == []
    w.clock.advance(60)
    mark = w.mark()
    w.post_message("UCONTROL", "wow!!!!! lol lol lol this pond is great")
    assert len(w.said(mark)) == 1                      # the roast reaches the control only


def test_resting_person_is_not_remarked_on(tmp_path):
    def tune(cfg):
        cfg.vibes.rank_remark_chance = 1.0             # a remark is certain for anyone allowed one
        cfg.levels.band_steady = 0.0                   # every closed week counts as very active
        cfg.levels.band_active = 0.0
        cfg.levels.band_very_active = 0.0

    w = World(tmp_path, tune=tune)
    w.join("UREST", "UACTIVE")
    w.command("rest on", "UREST")
    w.ctx.weekly.close_week(PREV_WEEK, w.clock.now())   # the previous closed week gives the band its input

    mark = w.mark()
    w.post_message("UREST", "this is a nice pond day for everyone")
    assert w.said(mark) == []
    mark = w.mark()
    w.post_message("UACTIVE", "this is a nice pond day for everyone")
    assert len(w.said(mark)) == 1
    assert w.said(mark)[0] in w.canned("rank_remark_active")


# ============================================================================================== scenario 6

GEESE = ["UGOOSE1", "UGOOSE2", "UGOOSE3", "UGOOSE4"]


def test_goose_gag_escalates_to_self_timeout_then_goes_silent_and_touches_no_scores(tmp_path):
    def tune(cfg):
        cfg.mood.calm_line_chance = 1.0                # a calm line on the first call, so the sequence is visible
        cfg.mood.honk_chance = 0.0                     # no honk slip, so each stage has its own line

    w = World(tmp_path, tune=tune)
    w.join(*GEESE)
    before = table_counts(w)
    stage_lines = {stage: w.canned(moment) for stage, moment in GAG_MOMENTS.items()}

    def stage_of(text: str) -> Optional[str]:
        for stage, lines in stage_lines.items():
            if text in lines:
                return stage
        return None

    stages: List[Optional[str]] = []
    for n in range(60):
        mark = w.mark()
        w.post_message(GEESE[n % 4], "<@UBOT> goose")
        stages.extend(stage_of(text) for text in w.said(mark))
        if "timeout" in stages:
            break
        w.clock.advance(40)
    assert None not in stages, stages
    assert "calm" in stages and "annoyed" in stages and "angry" in stages and "timeout" in stages
    assert stages.index("calm") < stages.index("annoyed") < stages.index("angry") < stages.index("timeout")
    assert "cooling" not in stages[: stages.index("timeout")]

    # Inside the 10 minute silence after the timeout, goose calls get nothing: no line, no reaction.
    for uid in GEESE[:2]:
        w.clock.advance(40)
        mark = w.mark()
        w.post_message(uid, "<@UBOT> goose")
        assert w.since(mark) == []

    # The lines themselves: no "you", no names or mentions in any anger line.
    for moment in GAG_MOMENTS.values():
        for line in w.lines.lines(moment):
            assert not re.search(r"\byou\b", line.text, re.IGNORECASE), line.id
            assert "<@" not in line.text

    assert table_counts(w) == before                   # scores, flags and the rest are unchanged by the gag


# ============================================================================================== scenario 7

DUCK_TS = "1760000100.000100"
DUCK_TS_2 = "1760000200.000100"


def test_german_is_explained_on_request_in_a_thread_and_by_question_reaction(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UASKER", "UREACT")
    w.ctx.explain.record(CHANNEL, DUCK_TS, ["ente_gut"], w.clock.now())
    w.ctx.explain.record(CHANNEL, DUCK_TS_2, ["quak"], w.clock.now())
    expected = w.lines.expand(w.lines.explain_text("ente_gut"))

    mark = w.mark()
    res = w.dispatch(w.make_event("UASKER", "what does that mean", thread_ts=DUCK_TS,
                                  thread_parent_user=BOT))
    assert res.jobs == []
    said = w.said(mark)
    assert said == [expected]
    thread_posts = [kw for name, kw in w.since(mark) if name == "chat_postMessage"]
    assert thread_posts[0]["thread_ts"] == DUCK_TS
    assert "what does that mean" not in said[0].lower()
    assert w.http.requests == []                       # no voice call for an explanation

    w.clock.advance(60)                                # the same ask a minute later
    mark = w.mark()
    w.dispatch(w.make_event("UASKER", "what does that mean", thread_ts=DUCK_TS, thread_parent_user=BOT))
    assert w.said(mark) == []

    mark = w.mark()
    w.handlers["reaction_added"](w._envelope({
        "type": "reaction_added", "user": "UREACT", "reaction": "question", "item_user": BOT,
        "event_ts": w._ts(), "item": {"type": "message", "channel": CHANNEL, "ts": DUCK_TS_2}}))
    private = ephemerals(w.since(mark))
    assert private == [("UREACT", w.lines.expand(w.lines.explain_text("quak")))]
    assert w.said(mark) == []


# ============================================================================================== scenario 8

def test_distress_and_program_questions_never_reach_the_voice(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UKID")

    mark = w.mark()
    res = w.dispatch(w.make_event("UKID", "<@UBOT> I want to die"))
    assert res.jobs == []
    assert [a.kind for a in res.actions] == ["ephemeral", "organizer_post"]
    assert res.actions[0].text == safety_reply(w.cfg)
    assert res.actions[1].channel == ORG
    names = [name for name, _kw in w.since(mark)]
    assert names == ["chat_postEphemeral", "chat_postMessage"]
    assert reactions(w.since(mark)) == []
    assert "{de:" not in res.actions[0].text
    assert w.http.requests == []

    w.clock.advance(10)
    mark = w.mark()
    res = w.dispatch(w.make_event("UKID", "<@UBOT> how much ore per hour?"))
    assert res.jobs == []
    assert w.http.requests == []
    said = w.said(mark)
    assert len(said) == 1 and said[0] in w.canned("program_redirect")


# ============================================================================================== scenario 9

def test_leave_scrubs_the_person_from_facts_snapshots_confirmers_and_results(tmp_path):
    w = World(tmp_path)
    w.join("ULEAVER", "UKEEP")
    for i in range(3):
        w.post_message("ULEAVER", SENTENCES[i])
        w.clock.advance(45)
    for i in range(2):
        w.post_message("UKEEP", SENTENCES[i + 5])
        w.clock.advance(45)
    run_week_close(w)
    assert "ULEAVER" in week_results(w)                # the leaver has a confirmed result

    w.command("leave", "ULEAVER")
    assert not w.ctx.users.is_joined("ULEAVER")        # the leave itself went through

    leftovers: Dict[str, Any] = {}
    fact_rows = w.ctx.db.query(
        "SELECT id FROM facts WHERE user_id = ? OR counterparty = ? OR actor = ?", ("ULEAVER", "ULEAVER", "ULEAVER"))
    if fact_rows:
        leftovers["facts"] = [r["id"] for r in fact_rows]
    for row in w.ctx.db.query("SELECT week_id, snapshot_json, confirmers_json FROM week_status"):
        if "ULEAVER" in (row["snapshot_json"] or ""):
            leftovers["snapshot_json:%s" % row["week_id"]] = True
        if "ULEAVER" in (row["confirmers_json"] or ""):
            leftovers["confirmers_json:%s" % row["week_id"]] = True
    if week_results(w):
        if "ULEAVER" in week_results(w):
            leftovers["results"] = week_results(w)
    assert leftovers == {}


# ============================================================================================== scenario 10

def test_pause_and_voice_kill_switch_then_resume_restores_scoring_and_replies(tmp_path):
    w = World(tmp_path, voice=True)
    w.join("UPAUSE", "UOTHER")
    w.http.answer = speak("Short answer: yes.")

    # Voice off while the duck is running: scoring continues, addressed replies become canned lines.
    w.command("voice off", ORGANIZER)
    mark = w.mark()
    msg_a = w.post_message("UPAUSE", "a perfectly ordinary message here")
    assert w.facts("UPAUSE") == 1
    w.post_message("UPAUSE", "<@UBOT> is anyone around the pond")
    assert w.said(mark) and w.said(mark)[-1] in w.canned("ack")
    assert w.http.requests == []

    # Paused: messages, pings and reactions are ignored entirely.
    w.clock.advance(700)
    w.command("pause", ORGANIZER)
    counts = (w.facts("UPAUSE"), w.count("facts"))
    mark = w.mark()
    w.post_message("UPAUSE", "nothing counts while the duck is paused")
    w.post_message("UPAUSE", "<@UBOT> hello while paused")
    w.reaction("UOTHER", "UPAUSE", msg_a, "duck")
    assert w.since(mark) == []
    assert (w.facts("UPAUSE"), w.count("facts")) == counts
    assert w.facts("UPAUSE", "thanks") == 0

    # Resume: scoring and reactions come back. Voice stays off until someone turns it on.
    w.clock.advance(700)
    w.command("resume", ORGANIZER)
    msg_b = w.post_message("UPAUSE", "back again after the pause today")
    assert w.facts("UPAUSE") == counts[0] + 1
    w.reaction("UOTHER", "UPAUSE", msg_b, "duck")
    assert w.facts("UPAUSE", "thanks") == 1
    w.clock.advance(700)
    mark = w.mark()
    w.post_message("UPAUSE", "<@UBOT> is anyone around the pond")
    assert w.said(mark)[-1] in w.canned("ack")
    assert w.http.requests == []

    w.command("voice on", "UOWNER")   # voice on is owner-only (plan 7)
    w.clock.advance(700)
    mark = w.mark()
    w.post_message("UPAUSE", "<@UBOT> and now with the voice back on")
    assert len(w.http.requests) == 1
    assert w.said(mark) == [escape_text("Short answer: yes.")]


# ============================================================================================== scenario 11

@pytest.mark.parametrize("first_kind", ["message", "app_mention"])
def test_message_and_app_mention_twins_make_one_fact_and_at_most_one_reply(tmp_path, first_kind):
    w = World(tmp_path)
    w.join("UTWIN")
    ts = "%.6f" % w.clock.now()
    text = "<@UBOT> this is a perfectly ordinary chat message"
    message = w._envelope({"type": "message", "channel": CHANNEL, "user": "UTWIN", "ts": ts, "text": text})
    mention = w._envelope({"type": "app_mention", "channel": CHANNEL, "user": "UTWIN", "ts": ts, "text": text})
    bodies = {"message": message, "app_mention": mention}
    order = [first_kind, "app_mention" if first_kind == "message" else "message"]

    mark = w.mark()
    for kind in order:
        w.handlers[kind](bodies[kind])
    assert w.facts("UTWIN") == 1
    assert len(w.said(mark)) <= 1


# ============================================================================================== scenario 12

def test_config_loads_and_every_german_id_the_voice_may_use_is_in_the_line_bank():
    cfg = load_config(CONFIG_PATH)
    assert cfg.voice.messages_per_person_per_hour == 200
    bank = LineBank.from_file(LINES_PATH)
    missing = sorted(set(APPROVED_DE_IDS) - set(bank.german_ids()))
    assert missing == [], "approved German ids with no line bank entry: %s" % missing


def test_manifest_has_the_scopes_the_code_needs():
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        manifest = json.load(fh)
    granted = set(manifest["oauth_config"]["scopes"]["bot"])
    needed = {"chat:write", "commands", "app_mentions:read", "channels:history", "channels:read",
              "reactions:read", "reactions:write", "files:write"}
    assert needed - granted == set()
    assert "/goose" in [c["command"] for c in manifest["features"]["slash_commands"]]
