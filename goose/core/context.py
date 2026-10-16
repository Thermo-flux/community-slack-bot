"""AppContext: the bag of services every handler receives. Written by the lead; do not change field names."""
from __future__ import annotations

import os
import random
import tempfile
from dataclasses import dataclass
from typing import Any, Optional, Union

from goose.config import Config, default_config
from goose.core.clock import Clock
from goose.core.db import Database
from goose.core.explain import ExplainService
from goose.core.ledger import Ledger
from goose.core.levels import Levels
from goose.core.lines import LineBank
from goose.core.mood import Mood
from goose.core.users import Users
from goose.core.vibes import Vibes
from goose.core.weekly import Weekly
from goose.gateway.audit import AuditLog


@dataclass
class AppContext:
    config: Config
    db: Database
    clock: Clock
    rng: random.Random
    lines: LineBank
    ledger: Ledger
    users: Users
    levels: Levels
    weekly: Weekly
    mood: Mood
    vibes: Vibes
    explain: ExplainService
    audit: AuditLog
    voice_client: Optional[Any] = None      # goose.gateway.voice_client.VoiceClient or None (voice not wired)
    bot_user_id: str = "UBOT"
    organizer_channel: str = ""


def build_context(config: Optional[Config] = None, *, db_path: str = ":memory:", audit_path: Optional[str] = None,
                  lines: Union[LineBank, str, None] = None, clock: Optional[Clock] = None,
                  rng: Optional[random.Random] = None, voice_client: Optional[Any] = None,
                  bot_user_id: str = "UBOT") -> AppContext:
    """Wire everything. Tests pass a FakeClock, a seeded Random, db_path ':memory:' and a LineBank (or a path)."""
    cfg = config or default_config()
    clk = clock or Clock(cfg.schedule.timezone)
    rnd = rng or random.Random()
    db = Database(db_path)
    if isinstance(lines, LineBank):
        bank = lines
    else:
        bank = LineBank.from_file(lines or cfg.paths.lines)
    if audit_path is None:
        audit_path = os.path.join(tempfile.mkdtemp(prefix="goose-audit-"), "audit.log")
    ledger = Ledger(db, cfg, clk)
    users = Users(db, cfg, clk, ledger)
    levels = Levels(cfg)
    weekly = Weekly(db, cfg, clk, ledger, users, levels)
    mood = Mood(db, cfg, clk, rnd)
    vibes = Vibes(db, cfg, clk, bank, rnd)
    explain = ExplainService(db, cfg, clk, bank, rnd)
    audit = AuditLog(audit_path)
    return AppContext(config=cfg, db=db, clock=clk, rng=rnd, lines=bank, ledger=ledger, users=users, levels=levels,
                      weekly=weekly, mood=mood, vibes=vibes, explain=explain, audit=audit,
                      voice_client=voice_client, bot_user_id=bot_user_id, organizer_channel=cfg.channels.organizer)
