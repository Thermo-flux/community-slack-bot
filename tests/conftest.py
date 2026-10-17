"""Shared test setup: make sure the SQLite schema (including the newer tables) exists."""

import database


def pytest_configure(config):
    try:
        database.init_db()
    except Exception:
        pass
