"""Logging with secret redaction.

Every log line passes through :class:`RedactingFormatter`, which removes the
configured API key and anything shaped like an Anthropic key, so a key can't
reach the app log even if some library logs a request header.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")
_secrets: set[str] = set()

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}

# Warnings uvicorn logs for malformed requests, before the app sees them. Any
# client on the internal network can trigger them, so they are rate-limited.
CLIENT_TRIGGERED_WARNINGS = (
    "Invalid HTTP request received",
    "Unsupported upgrade request",
    "No supported WebSocket library detected",
)


def register_secret(value: str) -> None:
    """Add a value that must never appear in logs (ignored if too short to be a real secret)."""
    value = value.strip()
    if len(value) >= 8:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "***")
    return _KEY_PATTERN.sub("sk-ant-***", text)


def printable(text: str, limit: int = 200) -> str:
    """Make client-supplied text safe to put in a log line.

    Control characters (newlines in particular) are escaped so a request can't
    forge extra log lines, and the length is capped.
    """
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in text[:limit])


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class SafeStreamHandler(logging.StreamHandler):
    """A stream handler whose error path can't leak secrets.

    logging's default handleError prints the raw record, message arguments
    included, which would bypass the redacting formatter.
    """

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API name
        try:
            reason = type(sys.exc_info()[1]).__name__
            sys.stderr.write(f"Ember: could not write a log line from {record.name} ({reason})\n")
        except Exception:  # noqa: BLE001, S110 - nothing sensible left to do
            pass


class RepeatedWarningFilter(logging.Filter):
    """Lets each client-triggered uvicorn warning through at most once per window."""

    def __init__(self, window_seconds: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.window = window_seconds
        self._clock = clock
        self._last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for prefix in CLIENT_TRIGGERED_WARNINGS:
            if message.startswith(prefix):
                now = self._clock()
                last = self._last.get(prefix)
                if last is not None and now - last < self.window:
                    return False
                self._last[prefix] = now
        return True


def setup_logging(level: str = "info") -> None:
    """Send logs to stdout (the Home Assistant app log) at the chosen level.

    The root logger itself stays at WARNING or lower, so warnings and errors
    always reach the dashboard's system log even if the owner quiets the app log.
    """
    chosen = _LEVELS.get(level, logging.INFO)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "ember_stdout", False):
            root.removeHandler(handler)
    handler = SafeStreamHandler(sys.stdout)
    handler.ember_stdout = True  # type: ignore[attr-defined]
    handler.setLevel(chosen)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(min(chosen, logging.WARNING))
    # Route uvicorn's own loggers through the root handler so they are redacted too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    uvicorn_error = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, RepeatedWarningFilter) for f in uvicorn_error.filters):
        uvicorn_error.addFilter(RepeatedWarningFilter())
    # HTTP clients log every request line at INFO; keep that out of normal logs.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
