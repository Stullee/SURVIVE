"""Shared setup for the review scripts: an agent on a fresh data folder and a ToolContext, as the tests build them.

Run from the repository's ember/ folder with PYTHONPATH=. (so `import app` and `import tests` work).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(__file__).resolve().parent
_data = Path(tempfile.mkdtemp(prefix="ember-repro-tools-"))  # outside the repository
os.environ["EMBER_DATA_DIR"] = str(_data)
os.environ["EMBER_SCHEDULER"] = "off"
os.environ["EMBER_FAKE_DELAY_MS"] = "0"
os.environ.pop("EMBER_OPTIONS_PATH", None)
os.environ.pop("EMBER_DEV_MODE", None)

from app.agent import plan as _plan_tree  # noqa: E402
from app.agent import tools  # noqa: E402

# As tests/conftest.py does for every test not marked "exploring": no venture cycles, the plan is the products' alone.
ORIGINAL_VENTURES = _plan_tree._ventures
_plan_tree._ventures = lambda conn, scope, now: []
from app.agent.service import Agent  # noqa: E402
from app.config import LoadedSettings, Settings  # noqa: E402
from tests.economy_helpers import ScriptedTransport, make_economy  # noqa: E402

ROOMY = Settings(
    starting_balance_usd=50,
    daily_spend_cap_usd=5,
    cycle_spend_cap_usd=1,
    owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",),
)


def agent(settings: Settings = ROOMY, outcomes: list | None = None) -> tuple[Agent, ScriptedTransport]:
    """An agent that ran one quiet cycle, so a cycle and a model call exist for the tool calls' rows."""
    from tests.test_agent import plan, text

    economy = make_economy(_data, settings)
    transport = ScriptedTransport(simulated=True, outcomes=outcomes or [plan(steps=["look"]), text("Done."), text("R.")])
    made = Agent(economy.db, LoadedSettings(settings), economy, transport=transport, cycles_enabled=True)
    made.recover()
    made.run_cycle("schedule")
    return made, transport


def context(made: Agent, **extra) -> tools.ToolContext:
    return tools.ToolContext(
        db=made.db,
        clock=made.clock,
        scope=made.scope(),
        cycle_id=rows(made, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=made.roots()[0],
        memory=made.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
        **extra,
    )


def call(ctx: tools.ToolContext, name: str, args, phase: str = "act") -> tools.Outcome:
    with ctx.db.connection() as conn:
        llm = conn.execute("SELECT MAX(id) FROM llm_calls").fetchone()[0]
    return tools.run(ctx, name, args, "toolu_review", llm, phase)


def rows(made: Agent, sql: str, params: tuple = ()) -> list[dict]:
    with made.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def show(label: str, outcome: tools.Outcome) -> None:
    print(f"--- {label}: ok={outcome.ok}")
    print("   ", outcome.text[:600].replace("\n", "\n    "))


if __name__ == "__main__":
    a, _ = agent()
    c = context(a)
    show("workspace_list", call(c, "workspace_list", {}))
    print("data dir:", _data, file=sys.stderr)
