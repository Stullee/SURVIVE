"""Mirror warnings and errors from the Python log into the ``events`` table.

This is how "all errors are logged and shown in the dashboard" holds for
problems nobody anticipated: anything logged at WARNING or above, from any
module or library, lands in the dashboard's system log.
"""

from __future__ import annotations

import logging
import threading

from .db import Database
from .logging_setup import redact

_LEVEL_NAMES = {logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}


class DatabaseLogHandler(logging.Handler):
    def __init__(self, db: Database) -> None:
        super().__init__(level=logging.WARNING)
        self.db = db
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        # Guard against recursion if writing the event itself logs a warning.
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            message = redact(record.getMessage())
            details = None
            if record.exc_info:
                formatter = logging.Formatter()
                details = {"traceback": redact(formatter.formatException(record.exc_info))}
            self.db.add_event(_LEVEL_NAMES.get(record.levelno, "warning"), "log", f"{record.name}: {message}", details)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
        finally:
            self._local.busy = False


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
