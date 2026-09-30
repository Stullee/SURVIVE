"""0.12.0: a full venture knowledge file (64 KB) made venture_update fail entirely, its scores, stage and note lost with
it, and the failure counted as a refused file operation. Now the file continues in a new part, and a write that fails
anyway is reported while the rest of the update is saved."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent import tools, ventures
from app.agent.sandbox import Jail, Limits
from tests.test_agent import make_agent, plan, rows

ETSY = 1  # the first seeded venture


def context_of(agent: Any, workspace: Jail | None = None) -> tools.ToolContext:
    return tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=workspace or agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )


def update(ctx: tools.ToolContext, **args: Any) -> tools.Outcome:
    llm_call = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, "venture_update", {"venture_id": ETSY, **args}, "toolu_update", llm_call, "act")


def rows_of(ctx: tools.ToolContext, sql: str) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def test_a_full_knowledge_file_continues_in_a_new_part(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    ctx = context_of(agent)
    title = rows(agent, f"SELECT title FROM ventures WHERE id = {ETSY}")[0]["title"]
    first = ventures.file_of(ETSY, title)
    ctx.workspace.write(first, "x" * (ctx.workspace.limits.max_file_bytes - 20))  # no room for a finding
    done = update(ctx, note="Checked what CV templates sell for.", learned="Buyers pay 4 to 6 EUR (etsy.com search).")
    assert done.ok, done.text
    second = ventures.file_of(ETSY, title, 2)
    assert f"What you learned is in {second} (" in done.text and f"continued from {first})." in done.text
    assert (
        "Checked what CV templates sell for."
        in rows(agent, f"SELECT notes FROM ventures WHERE id = {ETSY}")[0]["notes"]
    )
    text = ctx.workspace.read(second)
    assert text.startswith(f"# Venture #{ETSY}: {title}\n") and f"(Continued from {first}.)" in text
    assert text.rstrip().endswith("Buyers pay 4 to 6 EUR (etsy.com search).")
    again = update(ctx, learned="A second finding.")
    assert f"What you learned is in {second} (" in again.text and "continued" not in again.text  # part 2 has room
    parts = ventures.knowledge_parts(ctx.workspace, ETSY, title)
    assert parts == [first, second]
    row = rows(agent, f"SELECT * FROM ventures WHERE id = {ETSY}")[0]
    focus = ventures.focus_text(row, ventures.Money(), ctx.workspace.size_of(second, "text"), [], parts)
    assert f"{second} (" in focus and f"; its earlier parts: {first}" in focus
    shown = next(v for v in agent.ventures()["items"] if v["id"] == ETSY)
    assert shown["file"] == second


def test_a_full_workspace_keeps_the_rest_of_the_update(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    full = Jail(agent.roots()[0].root, Limits(max_total_bytes=10))  # no room for any text
    ctx = context_of(agent, full)
    done = update(ctx, note="Priced the templates.", learned="They sell for 4 to 6 EUR.")
    assert done.ok and "What you learned was NOT saved (text files take at most" in done.text
    assert "the rest of the update was" in done.text
    assert "Priced the templates." in rows(agent, f"SELECT notes FROM ventures WHERE id = {ETSY}")[0]["notes"]
    assert ctx.state.strikes == 0  # a full workspace is no refused file operation
