"""Review reproduction: a run refused for its price (nothing sent, nothing spent) uses up one of the owner's runs per
day, and doesn't count toward the tool's per-cycle limit, so retries in one cycle use up the day.

Run from ember/ with:  python -m pytest -p tests.conftest -q -s <this file>
"""

from __future__ import annotations

from pathlib import Path

from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls
from tests.test_agent import ROOMY, rows
from tests.test_loop_shapes import JOURNAL, PLAN, run


def test_price_refusals_use_up_the_runs_per_day(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"workshop_run_cap_usd": 0.05, "workshop_runs_per_day": 3})
    call = ("workshop", {"task": "Chart."})
    fake = FakeTransport(
        script=[Plan(PLAN), ToolCalls([call]), ToolCalls([call]), ToolCalls([call]), ToolCalls([call]), Reply("Ok."),
                JOURNAL]
    )
    agent, _ = run(data_dir, fake, settings=settings)
    for r in rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'workshop' ORDER BY id"):
        print(r["status"], "|", r["result"][:110])
    print("workshop_runs rows:", rows(agent, "SELECT status, cost_micros FROM workshop_runs"))
    print("paid workshop calls:", rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'workshop'"))
    last = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'workshop' ORDER BY id DESC LIMIT 1")[0]["result"]
    assert "the workshop runs at most 3 times a day" in last
