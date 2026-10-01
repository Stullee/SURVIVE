"""The journal is never lost (0.12.0, FIX NOW 11): a cycle whose reflection wrote no journal got "Goal: …" alone, and
the limit of 4 tool calls per turn skipped a journal that came fifth (live: cycle #35's journal was lost)."""

from __future__ import annotations

from pathlib import Path

from app.agent.fake_llm import FakeTransport, Reply, ToolCalls
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_roadmap import plan, tool_results

JOURNAL = ("write_journal", {"summary": "Looked around", "entry": "Listed the workspace."})


def test_a_journal_that_comes_fifth_is_written(data_dir: Path) -> None:
    sleep = ("set_sleep", {"minutes": 60, "reason": "Nothing more to do today."})
    fake = FakeTransport(
        script=[
            plan(steps=["look around"]),
            Reply("Done."),
            ToolCalls([sleep] * 4 + [JOURNAL, sleep]),  # 0.14.0: the journal is the reflection's
        ]
    )
    agent, ends = run(data_dir, fake)
    assert ends[0].status == "completed"
    assert rows(agent, "SELECT author, summary FROM journal") == [{"author": "agent", "summary": "Looked around"}]
    slept = tool_results(agent, "set_sleep")
    assert [r["status"] for r in slept] == ["ok"] * 4 + ["skipped"]  # the limit still holds for the others


def test_a_cycle_without_a_journal_gets_one_from_its_records(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=["look around"]),
            ToolCalls([("workspace_list", {}), ("no_such_tool", {})]),
            Reply("Done."),
            ToolCalls([("set_sleep", {"minutes": 60, "reason": "Nothing more to do today."})]),  # no journal
        ]
    )
    agent, _ = run(data_dir, fake)
    [journal] = rows(agent, "SELECT author, summary, entry FROM journal")
    assert journal["author"] == "system" and journal["summary"].startswith("Cycle ended completed")
    lines = journal["entry"].split("\n")
    assert lines[:2] == ["Written by Ember's code: the reflection wrote no journal.", "Goal: Plan ahead"]
    assert lines[2].startswith("Done: workspace_list (")
    assert lines[3] == "Refused, failed or skipped (not done): no_such_tool (error)"
    assert lines[4].startswith("Cost: $")
