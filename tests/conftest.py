"""Shared test setup: isolate the default DB and make sure the schema exists."""

import os
import tempfile

_TEST_DB = os.path.join(tempfile.gettempdir(), f"goose_tests_{os.getpid()}.db")
os.environ["DB_PATH"] = _TEST_DB

import database


def pytest_configure(config):
    database.init_db()


def pytest_sessionfinish(session, exitstatus):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TEST_DB + suffix)
        except OSError:
            pass
