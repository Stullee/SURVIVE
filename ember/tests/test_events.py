"""The warning/error mirror into the dashboard's system log stays bounded."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app import events
from app.db import Database, migrate


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    return Database(db_file)


@pytest.fixture
def logger_with_handler(database: Database):
    clock = FakeClock()
    handler = events.DatabaseLogHandler(database, clock=clock)
    logger = logging.getLogger("test.events")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, clock
    logger.removeHandler(handler)


def _messages(database: Database) -> list[str]:
    return [e["message"] for e in database.recent_events(limit=1000, min_level="debug")]


def test_only_warnings_and_errors_are_mirrored(database: Database, logger_with_handler) -> None:
    logger, _ = logger_with_handler
    logger.info("just info")
    logger.warning("a warning")
    logger.error("an error")
    assert _messages(database) == ["test.events: an error", "test.events: a warning"]


def test_repeated_message_is_written_once_per_window(database: Database, logger_with_handler) -> None:
    logger, clock = logger_with_handler
    for _ in range(50):
        logger.warning("Unsupported upgrade request.")
    assert len(_messages(database)) == 1

    clock.now += events.REPEAT_WINDOW_SECONDS + 1
    logger.warning("Unsupported upgrade request.")
    rows = database.recent_events(limit=10)
    assert len(rows) == 2
    assert rows[0]["details"] == {"suppressed_before_this": 49}


def test_flood_of_distinct_messages_is_capped(database: Database, logger_with_handler) -> None:
    logger, clock = logger_with_handler
    for i in range(500):
        logger.warning("garbage request %d", i)
    assert len(_messages(database)) == events.MAX_EVENTS_PER_WINDOW

    clock.now += events.RATE_WINDOW_SECONDS + 1
    logger.error("real problem")
    newest = database.recent_events(limit=1)[0]
    assert newest["message"] == "test.events: real problem"
    assert newest["details"] == {"suppressed_before_this": 500 - events.MAX_EVENTS_PER_WINDOW}


def test_old_events_are_pruned(database: Database, logger_with_handler, monkeypatch) -> None:
    monkeypatch.setattr(events, "KEEP_EVENTS", 10)
    monkeypatch.setattr(events, "PRUNE_EVERY", 5)
    monkeypatch.setattr(events, "MAX_EVENTS_PER_WINDOW", 1000)
    logger, _ = logger_with_handler
    for i in range(23):
        logger.warning("event %d", i)
    messages = _messages(database)
    assert len(messages) <= 10 + 5
    assert messages[0] == "test.events: event 22"


def test_tracebacks_are_kept_and_redacted(database: Database, logger_with_handler) -> None:
    logger, _ = logger_with_handler
    try:
        raise ValueError("bad sk-ant-api03-abcdefghijklmnop")
    except ValueError:
        logger.exception("it failed")
    row = database.recent_events(limit=1)[0]
    assert "ValueError" in row["details"]["traceback"]
    assert "abcdefghijklmnop" not in row["details"]["traceback"]


def test_handler_never_raises(tmp_path: Path) -> None:
    broken = Database(tmp_path / "missing" / "nowhere.db")
    handler = events.DatabaseLogHandler(broken)
    handler.handleError = lambda record: None  # silence the default stderr report
    logger = logging.getLogger("test.events.broken")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("database is gone")  # must not raise
    finally:
        logger.removeHandler(handler)
