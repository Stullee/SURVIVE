"""The warning/error mirror into the dashboard's system log: bounded, non-blocking, leak-free."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import events
from app.db import Database, migrate
from app.logging_setup import setup_logging

KEY = "sk-ant-api03-eventsecretkey-0123456789"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    yield database
    database.close()


@pytest.fixture
def mirror(database: Database) -> Iterator[tuple[logging.Logger, FakeClock, events.DatabaseLogHandler]]:
    clock = FakeClock()
    handler = events.DatabaseLogHandler(database, clock=clock)
    logger = logging.getLogger("test.events")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, clock, handler
    logger.removeHandler(handler)
    logger.propagate = True
    handler.close()


def _messages(database: Database, handler: events.DatabaseLogHandler) -> list[str]:
    assert handler.flush()
    return [e["message"] for e in database.recent_events(limit=1000, min_level="debug")]


def test_only_warnings_and_errors_are_mirrored(database: Database, mirror) -> None:
    logger, _, handler = mirror
    logger.info("just info")
    logger.warning("a warning")
    logger.error("an error")
    assert _messages(database, handler) == ["test.events: an error", "test.events: a warning"]


def test_repeated_message_is_written_once_per_window(database: Database, mirror) -> None:
    logger, clock, handler = mirror
    for _ in range(50):
        logger.warning("Unsupported upgrade request.")
    assert len(_messages(database, handler)) == 1

    clock.now += events.REPEAT_WINDOW_SECONDS + 1
    logger.warning("Unsupported upgrade request.")
    handler.flush()
    rows = database.recent_events(limit=10)
    assert len(rows) == 2
    assert rows[0]["details"] == {"suppressed_before_this": 49}


def test_flood_of_distinct_messages_is_capped(database: Database, mirror) -> None:
    logger, clock, handler = mirror
    for i in range(500):
        logger.warning("garbage request %d", i)
    assert len(_messages(database, handler)) == events.MAX_EVENTS_PER_WINDOW

    clock.now += events.RATE_WINDOW_SECONDS + 1
    logger.error("real problem")
    handler.flush()
    newest = database.recent_events(limit=1)[0]
    assert newest["message"] == "test.events: real problem"
    assert newest["details"] == {"suppressed_before_this": 500 - events.MAX_EVENTS_PER_WINDOW}


def test_old_events_are_pruned(database: Database, mirror, monkeypatch) -> None:
    monkeypatch.setattr(events, "KEEP_EVENTS", 10)
    monkeypatch.setattr(events, "PRUNE_EVERY", 5)
    monkeypatch.setattr(events, "MAX_EVENTS_PER_WINDOW", 1000)
    logger, _, handler = mirror
    for i in range(23):
        logger.warning("event %d", i)
    messages = _messages(database, handler)
    assert len(messages) <= 10 + 5
    assert messages[0] == "test.events: event 22"


def test_tracebacks_are_kept_and_redacted(database: Database, mirror) -> None:
    logger, _, handler = mirror
    try:
        raise ValueError(f"bad {KEY}")
    except ValueError:
        logger.exception("it failed")
    handler.flush()
    row = database.recent_events(limit=1)[0]
    assert "ValueError" in row["details"]["traceback"]
    assert KEY not in row["details"]["traceback"]


def test_logging_inside_a_transaction_does_not_block(database: Database, mirror) -> None:
    """Code holding the write lock (e.g. the budget guard) may log; the event is written after."""
    logger, _, handler = mirror
    started = time.monotonic()
    with database.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('k', 'v', 'now')")
        logger.warning("cap reached")
        database.add_event("info", "system", "written inside the transaction")
    assert time.monotonic() - started < 1
    assert database.get_meta("k") == "v"
    assert "test.events: cap reached" in _messages(database, handler)


def test_logging_from_many_threads(database: Database, mirror) -> None:
    logger, _, handler = mirror

    def work(n: int) -> None:
        for i in range(5):
            logger.warning("thread %d message %d", n, i)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_messages(database, handler)) == 40


def test_write_failures_never_raise_or_leak(tmp_path: Path, capsys) -> None:
    broken = Database(tmp_path / "missing-dir" / "nowhere.db")
    handler = events.DatabaseLogHandler(broken)
    logger = logging.getLogger("test.events.broken")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("request headers %s", {"x-api-key": KEY})  # must not raise
        handler.flush()
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
        handler.close()
    err = capsys.readouterr().err
    assert "could not write an event to the database" in err
    assert KEY not in err


def test_log_level_option_does_not_hide_warnings_from_the_dashboard(database: Database) -> None:
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        setup_logging("error")  # the owner quiets the app log
        handler = events.install(database)
        logging.getLogger("probe.level").warning("someone probed the app")
        assert "probe.level: someone probed the app" in _messages(database, handler)
        events.uninstall(handler)
    finally:
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
        root.setLevel(saved_level)
