"""Tests for the hash-chained audit log (goose/gateway/audit.py). No network, no real time."""
from __future__ import annotations

import json
import os
import threading

import pytest

from goose.gateway.audit import GENESIS, AuditLog


def _lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [line for line in fh.read().split("\n") if line.strip()]


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "nested" / "audit.log")


@pytest.fixture
def three(path):
    log = AuditLog(path)
    log.append("UORG1", "confirm-week", "2025-10-06", "one organizer confirmed", ts=1.0)
    log.append("UOWNER", "set-end", "", "end date moved", ts=2.0)
    log.append("UORG2", "freeze", "UBAD", "reason code: review", ts=3.0)
    return log


def test_creates_folder_and_file(path):
    log = AuditLog(path)
    assert os.path.exists(path)
    assert log.verify() is True
    assert log.tail(5) == []


def test_first_entry_chains_from_genesis(path):
    log = AuditLog(path)
    entry = log.append("UORG1", "results", "2025-10-06", "posted", ts=10.0)
    assert entry["n"] == 1
    assert entry["prev"] == GENESIS
    assert len(entry["hash"]) == 64
    assert set(entry) == {"n", "ts", "actor", "action", "target", "detail", "prev", "hash"}


def test_entries_link_to_previous_hash(three):
    rows = three.tail(10)
    assert [r["n"] for r in rows] == [1, 2, 3]
    assert rows[1]["prev"] == rows[0]["hash"]
    assert rows[2]["prev"] == rows[1]["hash"]
    assert three.verify() is True


def test_tail_returns_last_entries_in_order(three):
    last_two = three.tail(2)
    assert [r["action"] for r in last_two] == ["set-end", "freeze"]
    assert three.tail(0) == []
    assert len(three.tail(100)) == 3


def test_ts_and_fields_are_stored(path):
    log = AuditLog(path)
    entry = log.append("UORG1", "confirm-week", "W", "d", ts=1760000000.25)
    assert entry["ts"] == 1760000000.25
    assert log.tail(1)[0]["ts"] == 1760000000.25


def test_detail_is_cut_to_500_characters(path):
    log = AuditLog(path)
    entry = log.append("UORG1", "note", "", "x" * 900)
    assert len(entry["detail"]) == 500
    assert log.verify() is True


def test_reopening_continues_the_chain(path, three):
    last_hash = three.tail(1)[0]["hash"]
    reopened = AuditLog(path)
    entry = reopened.append("UORG3", "resume", "", "after restart", ts=4.0)
    assert entry["n"] == 4
    assert entry["prev"] == last_hash
    rows = AuditLog(path).tail(10)
    assert [r["n"] for r in rows] == [1, 2, 3, 4]
    assert rows[3]["prev"] == rows[2]["hash"]
    assert AuditLog(path).verify() is True


def test_reopening_an_empty_file_starts_at_genesis(path):
    AuditLog(path)
    entry = AuditLog(path).append("UORG1", "first", "", "")
    assert entry["n"] == 1 and entry["prev"] == GENESIS


def test_reopening_with_a_corrupt_last_line_refuses(path, three):
    lines = _lines(path)
    lines[-1] = lines[-1][:20]
    _write_lines(path, lines)
    with pytest.raises(ValueError):
        AuditLog(path)


def test_verify_detects_an_edited_detail(path, three):
    lines = _lines(path)
    assert '"detail":"end date moved"' in lines[1]
    lines[1] = lines[1].replace('"detail":"end date moved"', '"detail":"end date moved by someone else"')
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_an_edited_actor(path, three):
    lines = _lines(path)
    lines[0] = lines[0].replace('"actor":"UORG1"', '"actor":"UORG9"')
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_a_forged_hash(path, three):
    lines = _lines(path)
    row = json.loads(lines[2])
    row["hash"] = "f" * 64
    lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_a_removed_middle_line(path, three):
    lines = _lines(path)
    del lines[1]
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_reordered_lines(path, three):
    lines = _lines(path)
    lines[0], lines[1] = lines[1], lines[0]
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_an_inserted_line(path, three):
    lines = _lines(path)
    lines.insert(1, lines[0])
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_verify_detects_an_unreadable_line(path, three):
    lines = _lines(path)
    lines[1] = "not json at all"
    _write_lines(path, lines)
    assert AuditLog(path).verify() is False


def test_untouched_file_still_verifies(path, three):
    assert AuditLog(path).verify() is True


def test_concurrent_appends_keep_one_chain(path):
    log = AuditLog(path)

    def worker(tag):
        for i in range(25):
            log.append("UORG%d" % tag, "note", str(i), "thread %d" % tag, ts=float(i))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r["n"] for r in AuditLog(path).tail(500)] == list(range(1, 201))
    assert AuditLog(path).verify() is True
