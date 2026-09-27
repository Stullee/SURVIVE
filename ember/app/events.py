"""Mirror warnings and errors from the Python log into the ``events`` table.

This is how "all errors are logged and shown in the dashboard" holds for
problems nobody anticipated: anything logged at WARNING or above, from any
module or library, lands in the dashboard's system log.

Logging must never block or fail the code that logs, so records are handed to
a background writer thread; the caller never touches SQLite (it may be the web
server's event loop, or hold the database lock inside a transaction).

Some warnings can be triggered from outside (a misbehaving client on the
internal network), so the mirror is bounded: a repeated message is written at
most once per window, the number of rows per window is capped (the rest is
counted and reported with the next row), and old rows are pruned.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from .db import Database
from .logging_setup import redact

_LEVEL_NAMES = {logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}

REPEAT_WINDOW_SECONDS = 300.0
RATE_WINDOW_SECONDS = 600.0
MAX_EVENTS_PER_WINDOW = 60
KEEP_EVENTS = 5000
PRUNE_EVERY = 200

_STOP = object()
# LogRecord attribute set by record(): the event is already in the database.
RECORDED = "ember_recorded"
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class DatabaseLogHandler(logging.Handler):
    def __init__(self, db: Database, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(level=logging.WARNING)
        self.db = db
        self._clock = clock
        self._guard = threading.Lock()
        self._last_seen: dict[tuple[str, int, str, str], float] = {}
        self._window_start = clock()
        self._window_count = 0
        self._suppressed = 0
        self._writes = 0
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._writer = threading.Thread(target=self._write_loop, name="ember-event-writer", daemon=True)
        self._writer.start()

    # --- called by any thread that logs ---

    def emit(self, record: logging.LogRecord) -> None:
        if threading.current_thread() is self._writer:
            return  # never mirror problems of the mirror itself
        if getattr(record, RECORDED, False):
            return  # already written to the events table by record()
        try:
            message = redact(record.getMessage())
            exc_name = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
            suppressed = self._admit((record.name, record.levelno, message[:300], exc_name))
            if suppressed is None:
                return
            details: dict[str, object] = {}
            if record.exc_info:
                details["traceback"] = redact(logging.Formatter().formatException(record.exc_info))
            if suppressed:
                details["suppressed_before_this"] = suppressed
            level = _LEVEL_NAMES.get(record.levelno, "warning")
            self._queue.put((level, f"{record.name}: {message}", details or None))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API name
        # The default prints the raw record (message arguments included) to stderr,
        # which would bypass redaction. Report only that something went wrong.
        _report_failure("could not mirror a log record to the database", sys.exc_info()[1])

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until everything logged so far has been written. Returns False on timeout."""
        if not self._writer.is_alive():
            return False
        done = threading.Event()
        self._queue.put(done)
        return done.wait(timeout)

    def close(self) -> None:
        if self._writer.is_alive():
            self._queue.put(_STOP)
            self._writer.join(timeout=5)
        super().close()

    def _admit(self, key: tuple[str, int, str, str]) -> int | None:
        """Decide whether to write this record. Returns how many were suppressed before it, or None to drop it."""
        now = self._clock()
        with self._guard:
            last = self._last_seen.get(key)
            if last is not None and now - last < REPEAT_WINDOW_SECONDS:
                self._suppressed += 1
                return None
            if now - self._window_start >= RATE_WINDOW_SECONDS:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= MAX_EVENTS_PER_WINDOW:
                self._suppressed += 1
                return None
            self._window_count += 1
            if len(self._last_seen) > 1000:
                self._last_seen.clear()
            self._last_seen[key] = now
            suppressed, self._suppressed = self._suppressed, 0
            return suppressed

    # --- the writer thread ---

    def _write_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            level, message, details = item
            try:
                self.db.add_event(level, "log", message, details)
                self._writes += 1
                if self._writes % PRUNE_EVERY == 0:
                    self.db.prune_events(KEEP_EVENTS)
            except Exception as exc:  # noqa: BLE001 - keep the writer alive
                _report_failure("could not write an event to the database", exc)


def _report_failure(what: str, exc: BaseException | None) -> None:
    try:
        reason = type(exc).__name__ if exc else "unknown error"
        sys.stderr.write(f"Ember: {what} ({reason})\n")
    except Exception:  # noqa: BLE001, S110 - nothing sensible left to do
        pass


def record(
    db: Database,
    level: str,
    kind: str,
    message: str,
    details: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Write an event for the dashboard now (inside the caller's transaction, if any) and print it to the app log.

    Use this for events that belong to what just happened (a grant, a refused
    call, a death), so they commit or roll back together with it.
    """
    db.add_event(level, kind, message, details)
    (logger or logging.getLogger("app.events")).log(
        _LEVELS.get(level, logging.INFO), "%s", message, extra={RECORDED: True}
    )


def install(db: Database) -> DatabaseLogHandler:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, DatabaseLogHandler):
            root.removeHandler(handler)
            handler.close()
    handler = DatabaseLogHandler(db)
    root.addHandler(handler)
    return handler


def uninstall(handler: DatabaseLogHandler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.flush()
    handler.close()
