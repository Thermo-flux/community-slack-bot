"""Create and verify a consistent SQLite backup.

The live database is never modified: the SQLite online backup API copies it to
a timestamped file, and the copy is verified with ``PRAGMA integrity_check``.
Only the resulting path is printed, never row contents.

Usage:
    python tools/backup_db.py data/goose.sqlite3 --destination /backups
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def backup_database(
    source: str | Path,
    destination_dir: str | Path | None = None,
    *,
    timestamp: str | None = None,
) -> Path:
    source_path = Path(source)
    if not source_path.exists():
        raise FileNotFoundError(f"database not found: {source_path}")
    target_dir = Path(destination_dir) if destination_dir else source_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = target_dir / f"{source_path.stem}-{stamp}.db"
    with sqlite3.connect(str(source_path)) as origin, sqlite3.connect(str(destination)) as copy:
        origin.backup(copy)
        copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return destination


def verify_backup(path: str | Path) -> bool:
    backup_path = Path(path)
    if not backup_path.exists():
        return False
    with sqlite3.connect(str(backup_path)) as db:
        row = db.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and row[0] == "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the goose-lee SQLite ledger.")
    parser.add_argument("source", help="path to the live SQLite database")
    parser.add_argument("--destination", default=None, help="directory for the backup copy")
    parser.add_argument("--timestamp", default=None, help="override the filename timestamp")
    args = parser.parse_args(argv)
    destination = backup_database(args.source, args.destination, timestamp=args.timestamp)
    healthy = verify_backup(destination)
    print(f"backup written to {destination} (integrity: {'ok' if healthy else 'FAILED'})")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
