"""Shared fixtures. Written by the lead. Agents must not edit this file; put extra fixtures in your own test files."""
from __future__ import annotations

import os
import random

import pytest

from goose.config import default_config
from goose.core.clock import FakeClock
from goose.core.db import Database
from goose.types import Event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(ROOT, "config", "lines.yaml")

# Thursday 2025-10-09 08:53:20 UTC. The week starts Monday 2025-10-06 (week id "2025-10-06").
START = 1_760_000_000.0


@pytest.fixture
def clock():
    return FakeClock(start=START)


@pytest.fixture
def rng():
    return random.Random(1234)


@pytest.fixture
def cfg():
    c = default_config()
    c.channels.scored = ["C1"]
    c.channels.organizer = "CORG"
    c.people.organizers = ["UORG1", "UORG2", "UORG3"]
    c.people.owner = "UOWNER"
    c.scoring.hold_seconds = 600
    return c


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def ledger(db, cfg, clock):
    from goose.core.ledger import Ledger
    return Ledger(db, cfg, clock)


@pytest.fixture
def users(db, cfg, clock, ledger):
    from goose.core.users import Users
    return Users(db, cfg, clock, ledger)


@pytest.fixture
def levels(cfg):
    from goose.core.levels import Levels
    return Levels(cfg)


@pytest.fixture
def weekly(db, cfg, clock, ledger, users, levels):
    from goose.core.weekly import Weekly
    return Weekly(db, cfg, clock, ledger, users, levels)


@pytest.fixture
def lines():
    from goose.core.lines import LineBank
    return LineBank.from_file(LINES_PATH)


@pytest.fixture
def ctx(cfg, clock, rng, lines, tmp_path):
    from goose.core.context import build_context
    c = build_context(cfg, db_path=":memory:", audit_path=str(tmp_path / "audit.log"), lines=lines,
                      clock=clock, rng=rng, bot_user_id="UBOT")
    yield c
    c.db.close()


@pytest.fixture
def ev(clock):
    """ev(kind="message", user="U1", text="...", **fields) -> Event with ts = now and sensible defaults."""
    counter = {"n": 0}

    def make(kind: str = "message", **kw) -> Event:
        counter["n"] += 1
        kw.setdefault("ts", clock.now())
        kw.setdefault("channel", "C1")
        kw.setdefault("event_id", "Ev%04d" % counter["n"])
        if kind in ("message", "message_changed", "reaction_added", "reaction_removed"):
            kw.setdefault("message_ts", "%.6f" % (clock.now() + counter["n"] / 1000.0))
        return Event(kind=kind, **kw)

    return make


def join(users, clock, *user_ids, show=True):
    """Helper for tests: join people and (optionally) turn show on."""
    for uid in user_ids:
        users.join(uid, clock.now())
        if show:
            users.set_show(uid, True)
