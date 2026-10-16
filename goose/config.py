"""Configuration: one YAML file, validated on start, fail closed.

Any unknown key, wrong type or invalid value raises ConfigError, so a typo can never silently reset a rule.
Secrets are NOT here. They come from environment variables.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, get_type_hints

import yaml


class ConfigError(Exception):
    """Raised for every config problem. The message lists all problems found."""


# A real Slack channel id: "C" then 8 or more capitals or digits. The placeholders from the shipped file never match.
CHANNEL_ID_RE = re.compile(r"C[A-Z0-9]{8,}")
PLACEHOLDER_CHANNEL_IDS = ("C_LOADOUT", "C_ORGANIZERS")


@dataclass
class ScoringConfig:
    message_curve: str = "log"           # "log" (gentle curve, default) | "linear" (pure volume)
    hold_seconds: int = 600              # a new fact stays "pending" this long before it counts
    min_words: int = 3
    min_chars: int = 12
    flood_seconds: int = 30              # technical flood limit between scored messages per person
    star_points: float = 25
    bigstar_points: float = 60


@dataclass
class LevelsConfig:
    thresholds: List[int] = field(default_factory=lambda: [0, 10, 25, 50, 100, 155, 235, 330, 445, 580, 735, 890])
    growth_above_top: float = 1.2
    titles: List[str] = field(default_factory=lambda: [
        "Hatchling", "Duckling", "Paddler", "Dabbler", "Mallard", "Diver",
        "Pond Scout", "Flock Leader", "Wing Commander", "Golden Beak", "Pond Elder", "Head Duck"])
    # activity bands from last confirmed week crumbs: below steady = quiet, then steady, active, very_active
    band_steady: float = 15
    band_active: float = 50
    band_very_active: float = 100


@dataclass
class ChannelsConfig:
    scored: List[str] = field(default_factory=lambda: ["C_LOADOUT"])   # channel ids that are scored and answered in
    organizer: str = "C_ORGANIZERS"                                    # private channel for organizer posts


@dataclass
class PeopleConfig:
    organizers: List[str] = field(default_factory=list)   # Slack user ids
    owner: str = ""                                       # Slack user id, can confirm the final result
    confirmations_required: int = 1


@dataclass
class VibesConfig:
    bot_name: str = "goose-lee"
    mascot: str = "duck"
    humor_level: int = 2
    german_rate: float = 0.25
    german_gloss: str = "on_request"      # "on_request" | "first_time" | "always"
    roast_rate: float = 0.1
    roast_person_gap_s: int = 86400
    newcomer_days: int = 14
    reactions_per_hour: int = 6
    reaction_person_gap_s: int = 600
    name_reaction_chance: float = 0.33
    ack_person_gap_s: int = 600
    rank_remark_chance: float = 0.0667    # about 1 in 15
    rank_remark_gap_s: int = 86400
    explain_message_gap_s: int = 600
    explain_channel_per_10min: int = 3


@dataclass
class MoodConfig:
    window_s: int = 1800
    half_life_s: int = 600
    first_add: float = 1.5
    repeat_add: float = 0.75
    person_gap_s: int = 600
    person_cap: float = 3.0
    gag_calls: int = 3
    gag_window_s: int = 300
    gag_duration_s: int = 1800
    annoyed_at: float = 2.5
    angry_at: float = 5.0
    timeout_at: float = 8.0
    honk_chance: float = 0.1667
    honk_gap_s: int = 1200
    anger_line_gap_s: int = 120
    timeout_silent_s: int = 600
    timeout_gap_s: int = 1800
    timeout_per_day: int = 1
    cooling_gap_s: int = 300
    cooling_max_s: int = 1800
    calm_line_chance: float = 0.25


@dataclass
class VoiceConfig:
    enabled: bool = False                  # the voice stays OFF until the gates in the plan are met
    url: str = "http://voice:8081"
    secret_env: str = "VOICE_SHARED_SECRET"
    model: str = ""                        # must be on the voice's allowlist
    messages_per_person_per_hour: int = 12   # the message limit (replaces spend caps)
    notice_version: str = "2"              # members must have accepted this privacy notice (via /goose join) before their messages go to the voice
    person_gap_s: int = 5
    reply_max_chars: int = 300
    elaborate_max_chars: int = 700
    max_tokens: int = 100
    elaborate_max_tokens: int = 200
    input_max_chars: int = 500
    thread_max_messages: int = 6
    thread_item_chars: int = 200
    thread_total_chars: int = 1200
    timeout_s: float = 5.0
    concurrency: int = 2
    queue_size: int = 5
    breaker_failures: int = 3
    breaker_window_s: int = 300
    breaker_open_s: int = 900
    junk_window: int = 50
    junk_ratio: float = 0.2
    junk_off_s: int = 3600
    slow_p95_s: float = 3.0
    slow_for_s: int = 600
    event_remarks_per_hour: int = 4
    event_remarks_per_day: int = 12
    alert_tokens_per_day: Optional[int] = None   # optional heads-up only, never a cap


@dataclass
class RewardsConfig:
    announced: bool = False


@dataclass
class ScheduleConfig:
    timezone: str = "UTC"
    launch_date: Optional[str] = None      # "YYYY-MM-DD"
    end_date: Optional[str] = None         # "YYYY-MM-DD", set by the owner; default launch + program_weeks
    program_weeks: int = 13
    snapshot_delay_s: int = 900            # the week is snapshotted at Monday 00:15
    confirm_deadline_hour: int = 12        # Tuesday 12:00 reminder
    tick_seconds: int = 30


@dataclass
class SafetyConfig:
    distress_words: List[str] = field(default_factory=list)   # extra words, added to the built-in draft list
    sad_words: List[str] = field(default_factory=list)
    contact_text: str = "Please talk to a trusted adult, or ask one of the organizers for help."


@dataclass
class PathsConfig:
    db: str = "data/goose.db"
    audit: str = "data/audit.log"
    lines: str = "config/lines.yaml"


@dataclass
class Config:
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    levels: LevelsConfig = field(default_factory=LevelsConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    people: PeopleConfig = field(default_factory=PeopleConfig)
    vibes: VibesConfig = field(default_factory=VibesConfig)
    mood: MoodConfig = field(default_factory=MoodConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    rewards: RewardsConfig = field(default_factory=RewardsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    def is_organizer(self, user_id: str) -> bool:
        return bool(user_id) and (user_id in self.people.organizers or user_id == self.people.owner)

    def is_owner(self, user_id: str) -> bool:
        return bool(user_id) and user_id == self.people.owner

    def is_scored_channel(self, channel: str) -> bool:
        return channel in self.channels.scored


def default_config() -> Config:
    return Config()


def _coerce(value: Any, tp: Any, path: str, problems: List[str]) -> Any:
    origin = getattr(tp, "__origin__", None)
    if dataclasses.is_dataclass(tp):
        if value is None:
            value = {}
        if not isinstance(value, dict):
            problems.append("%s: expected a mapping" % path)
            return tp()
        return _build(tp, value, path, problems)
    if origin is list:
        if not isinstance(value, list):
            problems.append("%s: expected a list" % path)
            return []
        inner = tp.__args__[0]
        return [_coerce(v, inner, "%s[%d]" % (path, i), problems) for i, v in enumerate(value)]
    if origin is not None and type(None) in getattr(tp, "__args__", ()):   # Optional[X]
        if value is None:
            return None
        inner = [a for a in tp.__args__ if a is not type(None)][0]
        return _coerce(value, inner, path, problems)
    if tp is bool:
        if not isinstance(value, bool):
            problems.append("%s: expected true or false" % path)
            return False
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append("%s: expected a whole number" % path)
            return 0
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append("%s: expected a number" % path)
            return 0.0
        return float(value)
    if tp is str:
        if value is None:
            return ""
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            problems.append("%s: expected text" % path)
            return ""
        return str(value)
    return value


def _build(cls: Any, data: Dict[str, Any], path: str, problems: List[str]) -> Any:
    hints = get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    for key in data:
        if key not in known:
            problems.append("%s%s: unknown setting (typo?)" % (path + "." if path else "", key))
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], hints[f.name], (path + "." if path else "") + f.name, problems)
    return cls(**kwargs)


def _validate(cfg: Config, problems: List[str]) -> None:
    t = cfg.levels.thresholds
    if len(t) < 2 or t[0] != 0 or any(b <= a for a, b in zip(t, t[1:])):
        problems.append("levels.thresholds: must start at 0 and strictly increase")
    if len(cfg.levels.titles) != len(t):
        problems.append("levels.titles: needs exactly one title per threshold (%d)" % len(t))
    if cfg.levels.growth_above_top <= 1.0:
        problems.append("levels.growth_above_top: must be above 1.0")
    if cfg.scoring.message_curve not in ("log", "linear"):
        problems.append("scoring.message_curve: must be log or linear")
    if cfg.vibes.german_gloss not in ("on_request", "first_time", "always"):
        problems.append("vibes.german_gloss: must be on_request, first_time or always")
    if cfg.vibes.humor_level not in (1, 2, 3):
        problems.append("vibes.humor_level: must be 1, 2 or 3")
    for name in ("german_rate", "roast_rate", "name_reaction_chance", "rank_remark_chance"):
        v = getattr(cfg.vibes, name)
        if not 0.0 <= v <= 1.0:
            problems.append("vibes.%s: must be between 0 and 1" % name)
    if cfg.people.confirmations_required < 1:
        problems.append("people.confirmations_required: must be at least 1")
    if not cfg.channels.scored:
        problems.append("channels.scored: needs at least one channel id")
    for cid in cfg.channels.scored:
        if str(cid).strip() in ("", "*"):
            problems.append("channels.scored: channel ids must be real ids, not empty or '*'")
            break
    if str(cfg.channels.organizer).strip() in ("", "*"):
        problems.append("channels.organizer: must be a real channel id, not empty or '*'")
    if cfg.voice.messages_per_person_per_hour < 1:
        problems.append("voice.messages_per_person_per_hour: must be at least 1")
    if cfg.voice.reply_max_chars < 20 or cfg.voice.elaborate_max_chars < cfg.voice.reply_max_chars:
        problems.append("voice.reply_max_chars/elaborate_max_chars: invalid lengths")
    if cfg.schedule.program_weeks < 1:
        problems.append("schedule.program_weeks: must be at least 1")
    for name in ("launch_date", "end_date"):
        v = getattr(cfg.schedule, name)
        if v is not None:
            import datetime as _dt
            try:
                _dt.date.fromisoformat(v)
            except ValueError:
                problems.append("schedule.%s: must look like 2026-10-19" % name)
    from goose.core.clock import Clock
    try:
        Clock(cfg.schedule.timezone)
    except ValueError as e:
        problems.append("schedule.timezone: %s" % e)


def _is_real_channel_id(value: str) -> bool:
    return value not in PLACEHOLDER_CHANNEL_IDS and CHANNEL_ID_RE.fullmatch(value or "") is not None


def missing_setup(cfg: Config) -> List[str]:
    """Human-readable reasons the config is valid but not yet filled in for a real community (empty list when ready).
    These are not validation errors: the file loads, but the gateway refuses to start until they are fixed."""
    problems: List[str] = []
    if not cfg.people.organizers:
        problems.append("people.organizers: no organizer is listed yet")
    if not cfg.people.owner:
        problems.append("people.owner: not set yet")
    for cid in cfg.channels.scored:
        if not _is_real_channel_id(cid):
            problems.append("channels.scored: %s is not a real channel id yet" % cid)
    if not _is_real_channel_id(cfg.channels.organizer):
        problems.append("channels.organizer: %s is not a real channel id yet" % cfg.channels.organizer)
    if not str(cfg.voice.notice_version or "").strip():
        problems.append("voice.notice_version: empty")
    return problems


def config_from_dict(data: Optional[Dict[str, Any]]) -> Config:
    """Build and validate a Config. Raises ConfigError listing every problem."""
    problems: List[str] = []
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("the config file must be a mapping at the top level")
    cfg = _build(Config, data, "", problems)
    _validate(cfg, problems)
    if problems:
        raise ConfigError("config problems:\n  - " + "\n  - ".join(problems))
    return cfg


def load_config(path: str) -> Config:
    """Read a YAML file (a UTF-8 BOM is accepted) and validate it. Missing file or bad YAML raises ConfigError."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except yaml.YAMLError as e:
        raise ConfigError("config file is not valid YAML: %s" % e)
    if data is None:
        raise ConfigError("config file is empty")
    return config_from_dict(data)
