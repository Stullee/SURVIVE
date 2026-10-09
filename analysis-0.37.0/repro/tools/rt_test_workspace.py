"""Review reproductions: workspace_write restore/edit, draft overwrite versions, strikes for missing files.

Run from ember/ with:  python -m pytest -p tests.conftest -q -s <this file>
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent import tools
from tests.test_agent import make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls


def ctx_of(agent: Any) -> tools.ToolContext:
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    return tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=cycle,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )


def write(ctx: tools.ToolContext, **args: Any) -> tools.Outcome:
    llm = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, "workspace_write", args, "toolu_x", llm, "act")


def rows_of(ctx: tools.ToolContext, sql: str) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def a_cycle(data_dir: Path) -> Any:
    agent, _ = make_agent(data_dir, [plan(), text("done"), text("reflected")])
    agent.run_cycle("schedule")
    return agent


def test_restore_reaches_only_the_newest_kept_text(data_dir: Path) -> None:
    agent = a_cycle(data_dir)
    ctx = ctx_of(agent)
    ctx.workspace.write("book/interior.md", "V0 the 24 KB interior\n")
    print(write(ctx, path="book/interior.md", mode="overwrite", content="V1 header only\n").text)
    print(write(ctx, path="book/interior.md", mode="overwrite", content="V2 another rewrite\n").text)
    kept = rows_of(ctx, "SELECT id, content FROM workspace_versions ORDER BY id")
    print("kept versions:", [r["content"].strip() for r in kept])
    seen = []
    for _ in range(4):
        out = write(ctx, path="book/interior.md", mode="restore")
        seen.append(ctx.workspace.read("book/interior.md").strip())
        print("restore ->", out.ok, seen[-1])
    print("texts reachable by restore:", sorted(set(seen)))
    assert "V0 the 24 KB interior" not in seen  # kept, but restore can never reach it


def test_draft_overwrite_keeps_no_earlier_text(data_dir: Path) -> None:
    agent = a_cycle(data_dir)
    ctx = ctx_of(agent)
    ctx.workspace.write("drafts/guide.md", "ORIGINAL hand-written guide\n")
    ctx.draft = lambda brief, sources: tools.Drafted("DRAFTED replacement\n", False, 20_000)
    llm = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    out = tools.run(ctx, "draft", {"path": "drafts/guide.md", "brief": "Rewrite it.", "mode": "overwrite"}, "t", llm, "act")
    print("draft:", out.ok, out.text[:120])
    print("versions kept:", rows_of(ctx, "SELECT path, reason FROM workspace_versions"))
    restored = write(ctx, path="drafts/guide.md", mode="restore")
    print("restore:", restored.ok, restored.text)
    assert not rows_of(ctx, "SELECT 1 FROM workspace_versions")
    assert not restored.ok


def test_edit_cannot_remove_a_passage(data_dir: Path) -> None:
    agent = a_cycle(data_dir)
    ctx = ctx_of(agent)
    ctx.workspace.write("drafts/plan.md", "Keep this.\nDUPLICATE PARAGRAPH\nKeep that.\n")
    out = write(ctx, path="drafts/plan.md", mode="edit", find="DUPLICATE PARAGRAPH\n", content="")
    print("edit with empty content:", out.ok, out.text)
    assert not out.ok


def test_three_missing_files_end_the_work(data_dir: Path) -> None:
    calls = [tool_calls(("workspace_read", {"path": f"notes/typo-{n}.md"})) for n in range(3)]
    agent, _ = make_agent(
        data_dir,
        [plan(), *calls, tool_calls(("workspace_list", {})), text("done"), text("reflected")],
    )
    agent.run_cycle("schedule")
    results = rows(agent, "SELECT tool, status, result FROM tool_calls ORDER BY id")
    for r in results:
        print(r["tool"], r["status"], r["result"][:80])
    print("act_end_reason:", rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"])
    assert rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"] == "too many refused file operations"


def test_draft_append_overflow_and_precheck(data_dir: Path) -> None:
    agent = a_cycle(data_dir)
    ctx = ctx_of(agent)
    ctx.workspace.write("drafts/book.md", "x" * 30_720)
    paid = []
    ctx.draft = lambda brief, sources: paid.append(1) or tools.Drafted("y" * 36_000 + "\n", False, 50_000)
    llm = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    out = tools.run(ctx, "draft", {"path": "drafts/book.md", "brief": "More.", "mode": "append"}, "t1", llm, "act")
    print("overflow:", out.ok, out.text[:160])
    ctx.workspace.write("drafts/full.md", "x" * 34_000)
    out2 = tools.run(ctx, "draft", {"path": "drafts/full.md", "brief": "More.", "mode": "append"}, "t2", llm, "act")
    print("precheck:", out2.ok, out2.text[:160], "paid calls:", len(paid))
    assert ctx.workspace.size_of("drafts/book-2.md") and len(paid) == 1


def test_ordinary_mistakes_are_strikes(data_dir: Path) -> None:
    calls = [
        tool_calls(("workspace_write", {"path": "notes.md", "mode": "create", "content": "again"})),
        tool_calls(("look", {"path": "shop/photo-1.png"})),
        tool_calls(("make_document", {"source": "drafts/guide.md", "output": "shop/guide.pdf"})),
        tool_calls(("workspace_list", {})),
    ]
    agent, _ = make_agent(data_dir, [plan(), *calls, text("done"), text("reflected")])
    agent.roots()[0].write("notes.md", "first")
    agent.run_cycle("schedule")
    for r in rows(agent, "SELECT tool, phase, status, result FROM tool_calls ORDER BY id"):
        print(r["tool"], r["phase"], r["status"], r["result"][:90])
    print("act_end_reason:", rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"])


def test_repeated_restores_push_the_oldest_text_out(data_dir: Path) -> None:
    agent = a_cycle(data_dir)
    ctx = ctx_of(agent)
    ctx.workspace.write("book/interior.md", "V0 the 24 KB interior\n")
    write(ctx, path="book/interior.md", mode="overwrite", content="V1 header only\n")
    write(ctx, path="book/interior.md", mode="overwrite", content="V2 another rewrite\n")
    for n in range(1, 5):
        write(ctx, path="book/interior.md", mode="restore")
        kept = [r["content"].split()[0] for r in rows_of(ctx, "SELECT content FROM workspace_versions ORDER BY id")]
        print(f"after restore {n}: file={ctx.workspace.read('book/interior.md').split()[0]} kept={kept}")
    assert "V0" not in kept
