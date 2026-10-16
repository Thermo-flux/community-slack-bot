"""A separate, hash-chained audit log for organizer actions. OWNER: agent C1 (safe-output).

One JSON object per line: {"n", "ts", "actor", "action", "target", "detail", "prev", "hash"}. hash = sha256 of the
canonical JSON of the other fields including prev. Tampering with any line breaks verify().
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, List

GENESIS = "0" * 64
DETAIL_MAX = 500
HASHED_FIELDS = ("n", "ts", "actor", "action", "target", "detail", "prev")
ENTRY_FIELDS = frozenset(HASHED_FIELDS + ("hash",))


def _digest(entry: Dict[str, Any]) -> str:
    canonical = json.dumps({k: entry[k] for k in HASHED_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str):
        """Creates the file and its folder if needed. Reads the last line to continue the chain."""
        self.path = path
        self._lock = threading.Lock()
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self._n = 0
        self._prev = GENESIS
        lines = self._read_lines()
        if lines:
            try:
                last = json.loads(lines[-1])
            except ValueError:
                raise ValueError("audit log: last line is not valid JSON, refusing to continue the chain")
            if not isinstance(last, dict) or set(last) != ENTRY_FIELDS:
                raise ValueError("audit log: last line has the wrong shape, refusing to continue the chain")
            self._n = int(last["n"])
            self._prev = str(last["hash"])
        else:
            open(self.path, "a", encoding="utf-8").close()

    def _read_lines(self) -> List[str]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [line for line in fh.read().split("\n") if line.strip()]

    def append(self, actor: str, action: str, target: str = "", detail: str = "", ts: float = 0.0) -> Dict[str, Any]:
        """Append one entry (fsync-flush). Thread-safe. Detail is cut to 500 chars; no secrets or message text."""
        with self._lock:
            entry: Dict[str, Any] = {
                "n": self._n + 1,
                "ts": float(ts),
                "actor": str(actor),
                "action": str(action),
                "target": str(target),
                "detail": str(detail)[:DETAIL_MAX],
                "prev": self._prev,
            }
            digest = _digest(entry)
            entry["hash"] = digest
            line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._n += 1
            self._prev = digest
            return entry

    def tail(self, count: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            lines = self._read_lines()
        if count <= 0:
            return []
        out: List[Dict[str, Any]] = []
        for raw in lines[-count:]:
            out.append(json.loads(raw))
        return out

    def verify(self) -> bool:
        """Walk the whole file. False if a line was edited, removed, reordered, inserted or is unreadable."""
        with self._lock:
            try:
                lines = self._read_lines()
            except (OSError, UnicodeDecodeError):
                return False
        prev = GENESIS
        for number, raw in enumerate(lines, start=1):
            try:
                entry = json.loads(raw)
            except ValueError:
                return False
            if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
                return False
            if type(entry["n"]) is not int or entry["n"] != number:
                return False
            if entry["prev"] != prev:
                return False
            if _digest(entry) != entry["hash"]:
                return False
            prev = entry["hash"]
        return True
