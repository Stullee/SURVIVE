"""Shared helpers for the economy tests: a controllable clock, a scripted transport, an economy."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from app.config import LoadedSettings, Settings
from app.db import Database, migrate
from app.economy.clock import Clock
from app.economy.metering import Completed, MeteredModel, Outcome
from app.economy.service import Economy

START = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock(Clock):
    def __init__(self, start: datetime = START, tz: tzinfo = UTC) -> None:
        self.current = start
        super().__init__(now=lambda: self.current, tz=tz)

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


def message(
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-5",
    **usage: Any,
) -> dict[str, Any]:
    """A Messages API response body."""
    return {
        "id": "msg_" + uuid.uuid4().hex[:12],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, **usage},
    }


class ScriptedTransport:
    """Returns prepared outcomes in order (a plain completion when the script runs out)."""

    def __init__(self, simulated: bool = True, outcomes: list[Outcome] | None = None, tokens: int = 1_000) -> None:
        self.simulated = simulated
        self.outcomes = list(outcomes or [])
        self.tokens = tokens
        self.sent: list[dict[str, Any]] = []

    def count_tokens(self, request: Mapping[str, Any]) -> int:
        return self.tokens

    def send(self, request: Mapping[str, Any]) -> Outcome:
        self.sent.append(dict(request))
        if self.outcomes:
            return self.outcomes.pop(0)
        return Completed(message(input_tokens=self.tokens), "req_1")


def request(max_tokens: int = 1_000, model: str = "claude-sonnet-5", **extra: Any) -> dict[str, Any]:
    return {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}], **extra}


def make_economy(
    data_dir: Path,
    settings: Settings | None = None,
    clock: FakeClock | None = None,
    safe_mode: bool = False,
) -> Economy:
    db = Database(data_dir / "ember.db")
    migrate(db.path)
    loaded = LoadedSettings(settings or Settings(), errors=["broken option"] if safe_mode else [])
    economy = Economy(db, loaded, clock=clock or FakeClock())
    economy.start()
    return economy


def restart(economy: Economy, settings: Settings | None = None) -> Economy:
    """The same data folder after a restart (a new process: new boot id)."""
    economy.stop()
    loaded = LoadedSettings(settings or economy.settings)
    fresh = Economy(economy.db, loaded, clock=economy.clock)
    fresh.start()
    return fresh


def metered(economy: Economy, transport: ScriptedTransport | None = None) -> tuple[MeteredModel, ScriptedTransport]:
    transport = transport or ScriptedTransport(simulated=economy.mode == "dry_run")
    return economy.metered(transport), transport


def owner(economy: Economy, kind: str, amount: str, **fields: Any) -> dict[str, Any]:
    """Record an owner entry, confirming any question; returns the reply body."""
    body = {"amount": amount, "idempotency_key": uuid.uuid4().hex, **fields}
    body.setdefault("confirm_state_change", True)
    body.setdefault("confirm_large", True)
    if kind in ("expense",):
        body.setdefault("note", "test expense")
    if kind == "revenue":
        body.setdefault("source", "test customer")
    if kind in ("adjustment", "api-correction"):
        body.setdefault("note", "test")
    reply = economy.record(kind, body)
    assert reply.status == 201, reply.body
    return reply.body
