"""The canned line bank and the approved German table. OWNER: agent B (vibes). Data: config/lines.yaml.

lines.yaml shape (agent B writes the file; keep this shape):
  moments:
    level_up:                       # moment id
      - id: lu1
        text: "Pond level {n}, {title}. The duck wrote it down with a very serious face."
      - id: lu2
        text: "Level {n}. Mensch {de:mensch}!"      # {de:id} marks an approved German phrase
        de: [mensch]                                 # ids used (must match the placeholders)
  german:
    mensch: {text: "Mensch", meaning: "wow", wordplay: "", explain: "Mensch means 'man' and is used like 'wow!'."}
Slots use {name}. German placeholders are {de:id} in the bank and {{de:id}} in voice replies (normalize to {de:id}).
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from goose.core.wordlists import REWARD_STRONG, REWARD_WEAK

DE_RE = re.compile(r"\{de:([A-Za-z0-9_]+)\}")
DE_VOICE_RE = re.compile(r"\{\{de:([A-Za-z0-9_]+)\}\}")
SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
DASHES = ("\u2014", "\u2013")

# Gloss wrappers for the german_gloss dial (docs/PLAN.md section 2). {meaning} is filled in.
GLOSS_WRAPPERS = [
    "(for the non-German ducklets: {meaning})",
    "(translation for the flock: {meaning})",
    "(subtitles for the pond: {meaning})",
    "(no German required: {meaning})",
    "(pond English: {meaning})",
    "(bilingual pond note: {meaning})",
    "(that's German for {meaning}, for ducks of every language)",
    "(German for the curious: {meaning})",
]

_REWARD_RES = []
for _w in list(REWARD_STRONG) + list(REWARD_WEAK):
    _REWARD_RES.append((_w, re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(p) for p in _w.split()) + r"(?!\w)", re.IGNORECASE)))


@dataclass
class Line:
    id: str
    text: str                       # slots rendered, {de:id} placeholders still present
    de_ids: List[str] = field(default_factory=list)
    moment: str = ""


class LineBank:
    def __init__(self, moments: Optional[Dict[str, List[Line]]] = None,
                 german: Optional[Dict[str, Dict[str, str]]] = None):
        self._moments: Dict[str, List[Line]] = moments or {}
        self._german: Dict[str, Dict[str, str]] = german or {}

    @classmethod
    def from_file(cls, path: str) -> "LineBank":
        """Load and validate lines.yaml. Raises ValueError listing problems: every {de:id} must exist in `german`,
        `de` lists must match placeholders, ids unique, no line over 400 chars, no reward words
        (wordlists.REWARD_STRONG/WEAK) outside moments whose name starts with 'reward_', no em or en dashes."""
        import yaml
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = yaml.safe_load(fh)
        except FileNotFoundError:
            raise ValueError("lines file not found: %s" % path)
        except yaml.YAMLError as e:
            raise ValueError("lines file is not valid YAML: %s" % e)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LineBank":
        problems: List[str] = []
        if not isinstance(data, dict):
            raise ValueError("lines: the file must be a mapping with 'moments' and 'german'")
        moments_raw = data.get("moments")
        german_raw = data.get("german") or {}
        if not isinstance(moments_raw, dict) or not moments_raw:
            problems.append("moments: must be a non-empty mapping of moment names to lists")
            moments_raw = {}
        if not isinstance(german_raw, dict):
            problems.append("german: must be a mapping")
            german_raw = {}

        german: Dict[str, Dict[str, str]] = {}
        for gid, g in german_raw.items():
            where = "german.%s" % gid
            if not isinstance(gid, str) or not re.fullmatch(r"[A-Za-z0-9_]+", gid):
                problems.append("%s: id must be letters, digits and underscores" % where)
                continue
            if not isinstance(g, dict) or not isinstance(g.get("text"), str) or not g["text"].strip():
                problems.append("%s: needs a text" % where)
                continue
            entry = {
                "text": g["text"],
                "meaning": str(g.get("meaning") or ""),
                "wordplay": str(g.get("wordplay") or ""),
                "explain": str(g.get("explain") or ""),
            }
            if not entry["explain"]:
                problems.append("%s: needs an explain text" % where)
            for key in ("text", "meaning", "wordplay", "explain"):
                if any(d in entry[key] for d in DASHES):
                    problems.append("%s: no em or en dashes" % where)
            german[gid] = entry

        seen: set = set()
        moments: Dict[str, List[Line]] = {}
        for moment, items in moments_raw.items():
            name = str(moment)
            if not isinstance(items, list) or not items:
                problems.append("moments.%s: needs a non-empty list" % name)
                continue
            out: List[Line] = []
            for i, item in enumerate(items):
                where = "moments.%s[%d]" % (name, i)
                if not isinstance(item, dict):
                    problems.append("%s: must be a mapping with id and text" % where)
                    continue
                lid = item.get("id")
                if not isinstance(lid, str) or not lid:
                    problems.append("%s: needs an id" % where)
                    lid = "%s[%d]" % (name, i)
                elif lid in seen:
                    problems.append("%s: duplicate id %s" % (where, lid))
                seen.add(lid)
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    problems.append("%s: needs a text" % where)
                    continue
                if len(text) > 400:
                    problems.append("%s: over 400 characters" % where)
                if any(d in text for d in DASHES):
                    problems.append("%s: no em or en dashes" % where)
                if "{{" in text or "}}" in text:
                    problems.append("%s: use {de:id} in the bank, not {{de:id}}" % where)
                found = DE_RE.findall(text)
                for de_id in found:
                    if de_id not in german:
                        problems.append("%s: unknown German id %s" % (where, de_id))
                declared = item.get("de") or []
                if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
                    problems.append("%s: de must be a list of ids" % where)
                    declared = []
                if set(declared) != set(found):
                    problems.append("%s: de list %s does not match placeholders %s" % (
                        where, sorted(set(declared)), sorted(set(found))))
                if not name.startswith("reward_"):
                    for word, rx in _REWARD_RES:
                        if rx.search(text):
                            problems.append("%s: reward word %r outside a reward_ moment" % (where, word))
                            break
                out.append(Line(id=lid, text=text, de_ids=list(dict.fromkeys(found)), moment=name))
            moments[name] = out

        if problems:
            raise ValueError("lines.yaml problems:\n  - " + "\n  - ".join(problems))
        return cls(moments, german)

    def moments(self) -> List[str]:
        return list(self._moments.keys())

    def has_moment(self, moment: str) -> bool:
        return moment in self._moments

    def lines(self, moment: str) -> List[Line]:
        """Every line of a moment (copies of the list, the Line objects are shared)."""
        if moment not in self._moments:
            raise KeyError(moment)
        return list(self._moments[moment])

    def pick(self, moment: str, rng: random.Random, *, slots: Optional[Dict[str, Any]] = None,
             allow_german: bool = True, german_rate: float = 0.25, avoid_ids: Optional[List[str]] = None) -> Line:
        """Choose a line for the moment. With probability german_rate (and allow_german) choose among lines that carry
        German, otherwise among plain lines (fall back to any line if a group is empty). Skip avoid_ids when possible.
        Fill {slots}. Raises KeyError for an unknown moment."""
        if moment not in self._moments:
            raise KeyError(moment)
        lines = self._moments[moment]
        avoid = set(avoid_ids or [])
        pool = [line for line in lines if line.id not in avoid] or lines
        want_de = bool(allow_german) and rng.random() < german_rate
        group = [line for line in pool if bool(line.de_ids) == want_de] or pool
        chosen = rng.choice(group)
        return Line(id=chosen.id, text=self.render(chosen.text, slots), de_ids=list(chosen.de_ids), moment=moment)

    def render(self, text: str, slots: Optional[Dict[str, Any]] = None) -> str:
        """Fill {name} slots; leave {de:id} untouched; unknown slots stay as written."""
        if not slots:
            return text

        def sub(m):
            name = m.group(1)
            return str(slots[name]) if name in slots else m.group(0)

        return SLOT_RE.sub(sub, text)

    def normalize_placeholders(self, text: str) -> str:
        """Turn {{de:id}} into {de:id}."""
        return DE_VOICE_RE.sub(lambda m: "{de:%s}" % m.group(1), text)

    def expand(self, text: str) -> str:
        """Replace every {de:id} with the German text. Unknown ids raise KeyError."""
        text = self.normalize_placeholders(text)

        def sub(m):
            de_id = m.group(1)
            if de_id not in self._german:
                raise KeyError(de_id)
            return self._german[de_id]["text"]

        return DE_RE.sub(sub, text)

    def de_ids_in(self, text: str) -> List[str]:
        """German ids placed in the text, in order, without repeats."""
        found = DE_RE.findall(self.normalize_placeholders(text))
        return list(dict.fromkeys(found))

    def german(self, de_id: str) -> Dict[str, str]:
        """{"text","meaning","wordplay","explain"}"""
        if de_id not in self._german:
            raise KeyError(de_id)
        return dict(self._german[de_id])

    def german_ids(self) -> List[str]:
        return list(self._german.keys())

    def explain_text(self, de_id: str) -> str:
        """The playful explanation for the ID (the E1..E8 style in docs/APPENDIX_VOICE.md)."""
        return self.german(de_id)["explain"]

    def gloss_text(self, de_id: str, rng: random.Random) -> str:
        """A short gloss with a random wrapper, e.g. '(translation for the flock: look!)'."""
        meaning = self.german(de_id)["meaning"]
        return rng.choice(GLOSS_WRAPPERS).format(meaning=meaning)

    def canned_for_stage(self, stage: str, rng: random.Random) -> Line:
        """Anger meter lines: stage in calm, annoyed, angry, honk, timeout, cooling (moments anger_<stage>)."""
        return self.pick("anger_%s" % stage, rng)
