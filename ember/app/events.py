"""Mirror warnings and errors from the Python log into the ``events`` table.

This is how "all errors are logged and shown in the dashboard" holds for
problems nobody anticipated: anything logged at WARNING or above, from any
module or library, lands in the dashboard's system log.

Some warnings can be triggered from outside (a misbehaving client on the
internal network), so the mirror is bounded: a repeated message is written at
most once per window, the number of rows per window is capped (the rest is
counted and reported with the next row), and old rows are pruned.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .db import Database
from .logging_setup import redact

_LEVEL_NAMES = {logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}

REPEAT_WINDOW_SECONDS = 300.0
RATE_WINDOW_SECONDS = 600.0
MAX_EVENTS_PER_WINDOW = 60
KEEP_EVENTS = 5000
PRUNE_EVERY = 200


class DatabaseLogHandler(logging.Handler):
    def __init__(self, db: Database, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(level=logging.WARNING)
        self.db = db
        self._clock = clock
        self._local = threading.local()
        self._guard = threading.Lock()
        self._last_seen: dict[tuple[str, int, str, str], float] = {}
        self._window_start = clock()
        self._window_count = 0
        self._suppressed = 0
        self._writes = 0

    def emit(self, record: logging.LogRecord) -> None:
        # Guard against recursion if writing the event itself logs a warning.
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
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
            self.db.add_event(
                _LEVEL_NAMES.get(record.levelno, "warning"), "log", f"{record.name}: {message}", details or None
            )
            self._writes += 1
            if self._writes % PRUNE_EVERY == 0:
                self.db.prune_events(KEEP_EVENTS)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
        finally:
            self._local.busy = False

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


def install(db: Database) -> DatabaseLogHandler:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, DatabaseLogHandler):
            root.removeHandler(handler)
    handler = DatabaseLogHandler(db)
    root.addHandler(handler)
    return handler


def uninstall(handler: DatabaseLogHandler) -> None:
    logging.getLogger().removeHandler(handler)
