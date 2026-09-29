"""0.12.0: a paid call (research, brainstorm, workshop) that was sent counts toward its tool's per-cycle limit, also
when it failed: only calls that worked counted, so retries of a failing call went on spending past the limit."""

from __future__ import annotations

from pathlib import Path

from app.agent.fake_llm import Fail, FakeTransport, Reply, ToolCalls
from app.config import Settings
from app.economy.metering import Interrupted
from tests.test_loop_shapes import run
from tests.test_ventures import JOURNAL, VENTURING, plan, tool_results

RESEARCH = ("research", {"question": "What do CV templates sell for?"})


def test_failed_research_counts_toward_the_limit(data_dir: Path) -> None:
    # A call cut off mid-answer: it cost money, and isn't retried.
    failing = Fail(Interrupted("stream broke", partial_usage={"input_tokens": 1_000, "output_tokens": 50}))
    fake = FakeTransport(
        script=[
            plan(steps=["research"]),
            ToolCalls([RESEARCH, RESEARCH, RESEARCH]),
            failing,
            failing,
            failing,
            ToolCalls([RESEARCH]),  # a fourth: refused before anything is sent
            Reply("Done."),
            JOURNAL,
        ]
    )
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, ends = run(data_dir, fake, settings=settings)
    assert ends[0].status == "completed"
    calls = tool_results(agent, "research")
    assert [c["status"] for c in calls] == ["error"] * 4
    assert all("research failed" in c["result"] for c in calls[:3])
    assert "research can be used at most 3 times per cycle" in calls[3]["result"]


def test_a_brainstorm_without_usable_ideas_counts(data_dir: Path) -> None:
    brainstorm = ("brainstorm", {})
    fake = FakeTransport(
        script=[
            plan(steps=["brainstorm"]),
            ToolCalls([brainstorm]),
            Reply("No ideas today, sorry."),  # the brainstorm call worked but brought nothing usable
            ToolCalls([brainstorm]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    calls = tool_results(agent, "brainstorm")
    assert [c["status"] for c in calls] == ["error", "error"]
    assert "the brainstorm brought no usable ideas" in calls[0]["result"]
    assert "brainstorm can be used at most 1 times per cycle" in calls[1]["result"]
