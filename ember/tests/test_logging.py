from __future__ import annotations

import io
import logging

from app.logging_setup import RedactingFormatter, redact, register_secret

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
