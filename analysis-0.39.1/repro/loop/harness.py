"""Shared harness for the loop review: a fresh data dir, a fake clock, an Agent with scripted model replies."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRATCH = Path(__file__).resolve().parent
os.environ.setdefault("EMBER_SCHEDULER", "off")
os.environ.setdefault("EMBER_FAKE_DELAY_MS", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ember"))  # the repository's ember/


def fresh_dir(name: str) -> Path:
    base = Path(tempfile.gettempdir()) / "ember-repro-loop" / name  # outside the repository
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    os.environ["EMBER_DATA_DIR"] = str(base)
    return base


from app.agent.service import Agent  # noqa: E402
from app.config import LoadedSettings, Settings  # noqa: E402
from app.economy.metering import Completed  # noqa: E402
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy  # noqa: E402

_ids = itertools.count(1)

ROOMY = Settings(
    starting_balance_usd=50,
    daily_spend_cap_usd=5,
    cycle_spend_cap_usd=1,
    owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",),
)


def reply(content: list[dict[str, Any]], stop: str, output_tokens: int = 100, input_tokens: int = 900) -> Completed:
    body = {
        "id": f"msg_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    return Completed(body, f"req_{next(_ids)}")


def plan(steps: list[str] | None = None, focus: int | None = None, sleep: int = 120, **extra: Any) -> Completed:
    data = {
        "assessment": "Fresh start.",
        "goal": "Try one idea",
        "money_path": "",
        "focus_project_id": focus,
        "focus_venture_id": None,
        "focus_milestone_id": None,
        "steps": ["look around", "start a project"] if steps is None else steps,
        "sleep_minutes": sleep,
        **extra,
    }
    return reply([{"type": "text", "text": json.dumps(data)}], "end_turn")


def tools(*calls: tuple[str, dict[str, Any]], stop: str = "tool_use") -> Completed:
    return reply([{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": n, "input": i} for n, i in calls], stop)


def text(words: str, stop: str = "end_turn") -> Completed:
    return reply([{"type": "text", "text": words}], stop)


def make_agent(
    data_dir: Path, outcomes: list[Any], settings: Settings = ROOMY, clock: FakeClock | None = None
) -> tuple[Agent, ScriptedTransport]:
    economy = make_economy(data_dir, settings, clock=clock)
    transport = ScriptedTransport(simulated=True, outcomes=outcomes)
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=transport, cycles_enabled=True)
    agent.recover()
    return agent, transport


def rows(agent: Agent, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def meta(agent: Agent, name: str) -> str | None:
    return agent.db.get_meta(f"agent.{agent.mode}.{name}")
