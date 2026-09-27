"""Logging with secret redaction.

Every log line passes through :class:`RedactingFormatter`, which removes the
configured API key and anything shaped like an Anthropic key, so a key can't
reach the app log even if some library logs a request header.
"""

from __future__ import annotations

import logging
import re
import sys

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")
_secrets: set[str] = set()

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


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


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "info") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "ember_stdout", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.ember_stdout = True  # type: ignore[attr-defined]
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(_LEVELS.get(level, logging.INFO))
    # Route uvicorn's own loggers through the root handler so they are redacted too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # HTTP clients log every request line at INFO; keep that out of normal logs.
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
