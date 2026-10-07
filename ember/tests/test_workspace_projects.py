"""0.26.0: each file of the workspace filed under the project or venture it was written for (agent/workfiles.py), so
the owner's Workspace tab groups the files by project and venture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent import store, workfiles
from app.economy.clock import to_iso
from tests.test_agent import make_agent, plan, rows, text, tools

PROJECT = {"title": "Meal planners", "hypothesis": "Families pay 4 EUR", "next_step": "make one", "status": "active"}
OTHER = {"title": "CV templates", "hypothesis": "Job seekers pay 6 EUR", "next_step": "draft", "status": "active"}


def write(path: str, content: str = "text", mode: str = "create") -> tuple[str, dict[str, Any]]:
    return "workspace_write", {"path": path, "mode": mode, "content": content}


def filed(agent: Any) -> dict[str, tuple[Any, ...]]:
    found = rows(agent, "SELECT path, project_id, venture_id, cycle_id, tool FROM workspace_files ORDER BY path")
    return {r["path"]: (r["project_id"], r["venture_id"], r["cycle_id"], r["tool"]) for r in found}


def listed(agent: Any) -> dict[str, tuple[Any, ...]]:
    return {f["path"]: (f["project_id"], f["venture_id"]) for f in agent.workspace()["files"]}


def test_a_file_is_filed_under_the_project_its_cycle_worked_on(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["start the planners"]),
            tools(("project_create", PROJECT)),  # no focus in the plan: the project it starts is the cycle's
            tools(write("drafts/planner.md", "# Planner\n"), write("notes/loose.md")),
            text("Wrote them."),
            text("Reflected."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert filed(agent) == {
        "drafts/planner.md": (1, None, 1, "workspace_write"),
        "notes/loose.md": (1, None, 1, "workspace_write"),
    }
    data = agent.workspace()
    assert listed(agent) == {"drafts/planner.md": (1, None), "notes/loose.md": (1, None)}
    assert data["files"][0]["cycle_id"] == 1 and data["files"][0]["tool"] == "workspace_write"
    assert data["owners"]["projects"] == [{"id": 1, "title": "Meal planners", "status": "active", "venture_id": None}]


def test_a_file_keeps_its_first_project_and_a_deleted_file_goes(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["the planners"]),
            tools(("project_create", PROJECT)),  # the cycle's
            tools(write("notes/a.md"), write("notes/gone.md")),
            text("Done."),
            text("Reflected."),
            plan(steps=["the CVs"]),
            tools(("project_create", OTHER)),  # 0.28.0: a new product line is a cycle of its own
            tools(write("notes/a.md", " more", "append"), write("notes/b.md"), write("notes/gone.md", "", "delete")),
            text("Done."),
            text("Reflected."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert agent.run_cycle("schedule").status == "completed"
    assert filed(agent) == {
        "notes/a.md": (1, None, 2, "workspace_write"),  # the project it was written for; the cycle that changed it
        "notes/b.md": (2, None, 2, "workspace_write"),
    }


def test_a_file_written_without_a_focus_takes_the_next_ones(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["look around"]),
            tools(write("notes/ideas-list.md")),
            text("Noted."),
            text("Reflected."),
            plan(steps=["start"]),
            tools(("project_create", PROJECT)),
            tools(write("notes/ideas-list.md", "\nmore", "append")),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.run_cycle("schedule")
    assert filed(agent) == {"notes/ideas-list.md": (None, None, 1, "workspace_write")}
    agent.run_cycle("schedule")
    assert filed(agent) == {"notes/ideas-list.md": (1, None, 2, "workspace_write")}


def test_a_product_and_the_files_made_with_it_are_its_projects(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["make it"]),
            tools(("project_create", PROJECT)),
            tools(write("shop/planner.md", "# Weekly planner\n\n- [ ] Plan\n")),
            tools(("make_document", {"source": "shop/planner.md", "output": "shop/planner.pdf"})),
            text("Made."),
            text("Reflected."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert {path: row[:2] for path, row in filed(agent).items()} == {
        "shop/planner-page1.png": (1, None),
        "shop/planner.docx": (1, None),
        "shop/planner.md": (1, None),
        "shop/planner.pdf": (1, None),
    }
    assert filed(agent)["shop/planner.pdf"][3] == "make_document"


def test_a_ventures_knowledge_file_is_that_ventures_whatever_wrote_it(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=["notes"]), tools(("project_create", PROJECT)), text("."), text(".")])
    agent.run_cycle("schedule")
    jail = agent.roots()[0]
    venture = rows(agent, "SELECT id, title FROM ventures ORDER BY id")[0]
    jail.write(f"ventures/{venture['id']}-what-we-learned.md", "# Venture\n")
    jail.write("ventures/ideas.md", "every brainstorm\n")
    with agent.db.transaction() as conn:  # as if a cycle of the project had written them
        noticed = [("write", f"ventures/{venture['id']}-what-we-learned.md"), ("write", "ventures/ideas.md")]
        workfiles.record(conn, agent.scope(), noticed, 1, None, 1, "workspace_write", "2026-10-06T10:00:00Z")
    assert listed(agent) == {
        f"ventures/{venture['id']}-what-we-learned.md": (None, venture["id"]),
        "ventures/ideas.md": (None, None),
    }
    assert {path: row[:2] for path, row in filed(agent).items()} == {  # filed so, so the counts say the same
        f"ventures/{venture['id']}-what-we-learned.md": (None, venture["id"]),
        "ventures/ideas.md": (None, None),
    }
    assert agent.workspace()["owners"]["ventures"] == [
        {
            "id": venture["id"],
            "title": venture["title"],
            "stage": rows(agent, f"SELECT stage FROM ventures WHERE id = {venture['id']}")[0]["stage"],
        }
    ]


def test_projects_and_ventures_count_their_files(data_dir: Path) -> None:
    started = [plan(steps=["start"]), tools(("project_create", PROJECT)), tools(write("a.md"), write("b.md"))]
    agent, _ = make_agent(data_dir, [*started, text("."), text(".")])
    agent.run_cycle("schedule")
    venture, other = (r["id"] for r in rows(agent, "SELECT id FROM ventures ORDER BY id LIMIT 2"))
    agent.roots()[0].write("notes/market.md", "who buys")
    agent.roots()[0].write("c.md", "both in focus")
    with agent.db.transaction() as conn:
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = 1", (venture,))
        noticed = [("write", "notes/market.md")]
        workfiles.record(conn, agent.scope(), noticed, None, venture, 1, "workspace_write", "now")
        # A cycle focused on the project and on another venture: the file is the project's, and so its venture's
        workfiles.record(conn, agent.scope(), [("write", "c.md")], 1, other, 1, "workspace_write", "now")
    assert [(p["id"], p["files"]) for p in agent.dashboard()["projects"]] == [(1, 3)]
    counted = {v["id"]: v["files"] for v in agent.ventures()["items"]}
    assert counted[venture] == 4  # its own and its project's
    assert not any(n for vid, n in counted.items() if vid != venture)


def test_files_from_before_are_filed_once_from_the_calls_that_wrote_them(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["make it"]),
            tools(("project_create", PROJECT)),
            tools(write("shop/planner.md", "# Weekly planner\n")),
            tools(("make_document", {"source": "shop/planner.md", "output": "shop/planner.pdf"})),
            text("Made."),
            text("Reflected."),
            plan(steps=["notes"], focus=1),
            tools(write("notes/kept.md"), write("notes/gone.md"), write("notes/gone.md", "", "delete")),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    jail = agent.roots()[0]
    jail.write_bytes("workshop/out/chart.png", b"\x89PNG made by a run")
    jail.write("notes/by-hand.md", "no call named it")
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        store.insert_workshop_run(
            conn, scope, 2, now, task="a chart", outputs=[{"path": "workshop/out/chart.png", "bytes": 18}], status="ok"
        )
        before = filed(agent)
        conn.execute("DELETE FROM workspace_files")  # as before 0.26.0
        conn.execute("DELETE FROM meta WHERE key LIKE '%workspace_files_filed'")
        assert workfiles.backfill(conn, scope, jail, now) == 6
        assert workfiles.backfill(conn, scope, jail, now) == 0  # once
    after = filed(agent)
    assert {path: row[:2] for path, row in after.items()} == {path: row[:2] for path, row in before.items()} | {
        "workshop/out/chart.png": (1, None)
    }
    assert (
        after["notes/kept.md"][2:] == (2, "workspace_write") and after["shop/planner-page1.png"][3] == "make_document"
    )
    assert "notes/by-hand.md" not in after and "notes/gone.md" not in after


def test_the_agent_starts_with_its_files_filed(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [plan(steps=["start"]), tools(("project_create", PROJECT)), tools(write("a.md")), text("."), text(".")],
    )
    agent.run_cycle("schedule")
    with agent.db.transaction() as conn:
        conn.execute("DELETE FROM workspace_files")
        conn.execute("DELETE FROM meta WHERE key LIKE '%workspace_files_filed'")
    agent.recover()  # an upgrade's first start
    assert filed(agent) == {"a.md": (1, None, 1, "workspace_write")}
