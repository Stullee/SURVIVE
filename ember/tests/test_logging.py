from __future__ import annotations

import io
import logging

from app.logging_setup import RedactingFormatter, RepeatedWarningFilter, printable, redact, register_secret

KEY = "sk-ant-api03-another-secret-key-000111222"


def test_redacts_anthropic_key_shapes() -> None:
    assert redact(f"header x-api-key: {KEY}") == "header x-api-key: sk-ant-***"


def test_redacts_registered_secret() -> None:
    register_secret("my-custom-secret-token")
    assert "my-custom-secret-token" not in redact("token=my-custom-secret-token")


def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact("abc") == "abc"


def test_formatter_redacts_messages_args_and_tracebacks() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("calling with %s", KEY)
        try:
            raise ValueError(f"bad key {KEY}")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert KEY not in output
    assert "sk-ant-***" in output
    assert "ValueError" in output


def test_printable_escapes_control_characters() -> None:
    assert printable("a\nb\r\tc\x00") == r"a\nb\r\tc\x00"
    assert printable("Grüße /x") == "Grüße /x"
    assert printable("x" * 500, 10) == "x" * 10


def test_client_triggered_uvicorn_warnings_are_rate_limited() -> None:
    now = [0.0]
    flt = RepeatedWarningFilter(window_seconds=300, clock=lambda: now[0])

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", logging.WARNING, __file__, 1, message, None, None)

    assert flt.filter(record("Unsupported upgrade request.")) is True
    assert flt.filter(record("Unsupported upgrade request.")) is False
    assert flt.filter(record("Invalid HTTP request received.")) is True
    now[0] = 301
    assert flt.filter(record("Unsupported upgrade request.")) is True
    # Anything else from uvicorn is never filtered.
    assert flt.filter(record("Something else")) is True
    assert flt.filter(record("Something else")) is True


def test_app_log_on_stdout_is_redacted(capsys) -> None:
    from app.logging_setup import setup_logging

    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        setup_logging("info")
        logging.getLogger("some.library").warning("headers %s", {"x-api-key": KEY})
        logging.getLogger("uvicorn.error").error("Exception in ASGI application: %s", KEY)
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        root.setLevel(saved_level)
    out = capsys.readouterr().out
    assert "some.library: headers" in out and "uvicorn.error: Exception in ASGI application" in out
    assert KEY not in out
