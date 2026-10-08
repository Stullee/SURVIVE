"""0.28.0: one thing a wake cycle. An ordinary cycle worked on several product lines, an unbacked venture and marketing
at once (its rules said to juggle, its plan named a project, a venture and a milestone apart, and every tool took any
project): a licence bundle was listed in the cover-letter line, files and costs went to the wrong line. Now a cycle
works on one line, a marketing cycle brings buyers to one line's listings, a venture cycle decides one venture, and
the tools refuse another line's work for the rest of the cycle. 0.35.0: the plan tree takes each cycle's step, its
line and its kind (tests/test_fixes_0350.py); READY's ranking and the marketing share retired."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import (  # noqa: E402
    context,
    obligations,
    roadmap,
    store,
    tools,
    weekly,
)
from app.agent.fake_llm import FakeTransport, Plan, request_kind  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402

IDLE = {"assessment": "ok", "goal": "Wait", "focus_project_id": None, "steps": [], "sleep_minutes": 120}


def now(agent: Agent) -> str:
    return to_iso(agent.clock.now())


def lined(data_dir: Path, titles: tuple[str, ...] = ("Planner", "Poster", "Checklist")) -> tuple[Agent, FakeTransport]:
    """A dry-run agent after one idle cycle, with an open product line for each title (#1, #2, ...)."""
    fake = FakeTransport(script=[Plan(IDLE)])
    agent, _ = run(data_dir, fake)
    with agent.db.transaction() as conn:
        for title in titles:
            store.create_project(
                conn,
                agent.scope(),
                cycle_id=1,
                title=title,
                hypothesis="Someone pays 5 EUR for it",
                next_step="make it",
                status="active",
                now=now(agent),
            )
    return agent, fake


def cycle(agent: Agent, project: int | None = None, status: str = "completed") -> int:
    """A cycle of the agent's (a running one, or one that worked on ``project`` and ended)."""
    scope, stamp = agent.scope(), now(agent)
    with agent.db.transaction() as conn:
        return int(
            conn.execute(
                "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
                " session, project_id) VALUES (?, 'b', ?, ?, ?, 'schedule', 1, 0, ?, ?)",
                (scope.life_id, stamp, None if status == "running" else stamp, status, scope.session, project),
            ).lastrowid
        )


def working(agent: Agent, line: int | None = None, one_line: bool = True) -> tools.ToolContext:
    """The tools' context in the agent's running cycle (a new one if none runs), on product ``line`` (None: none
    taken yet)."""
    running = rows(agent, "SELECT id FROM cycles WHERE status = 'running'")
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=running[0]["id"] if running else cycle(agent, status="running"),
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(focus_project_id=line, one_line=one_line),
    )
    return ctx


def request(agent: Agent, project: int, executor: str | None = None, status: str = "rejected") -> int:
    """A request of line ``project`` the owner decided, and the obligation Ember's code keeps of it."""
    scope, stamp = agent.scope(), now(agent)
    with agent.db.transaction() as conn:
        found = store.insert_approval(
            conn,
            scope,
            1,
            stamp,
            type="publish",
            title=f"Something for line #{project}",
            description="What and why.",
            payload=f"payload {project} {executor}",
            expected_cost="free",
            expected_benefit="views",
            project_id=project,
            executor=executor,
            action="{}" if executor else None,
        )
        conn.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?", (status, stamp, found))
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, approval_id)"
            " VALUES (?, ?, 'decision', ?, ?, ?, ?)",
            (scope.mode, scope.session, f"react to request #{found}", stamp[:10], stamp, found),
        )
    return found


def obligation_of(agent: Agent, approval_id: int) -> int:
    return rows(agent, f"SELECT id FROM obligations WHERE approval_id = {approval_id}")[0]["id"]


def milestone(agent: Agent, project: int | None, days: int, title: str) -> int:
    with agent.db.transaction() as conn:
        return roadmap.create(
            conn,
            agent.scope(),
            title=title,
            measure="Ember's code checks it",
            due=(agent.clock.today() + timedelta(days=days)).isoformat(),
            now=now(agent),
            project_id=project,
        )


def texts(fake: FakeTransport, kind: str) -> list[str]:
    return [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == kind]


def take(focus: int | None = None, **plan: Any) -> Plan:
    """An ordinary cycle's plan on line ``focus`` (0.35.0: the plan tree takes its step and line)."""
    return Plan(
        {
            "assessment": "ok",
            "goal": "Work on one line",
            "money_path": "Buyers pay for what the line sells",
            "focus_project_id": focus,
            "steps": ["work on the line"],
            "sleep_minutes": 120,
            **plan,
        }
    )


# --- the tools keep a cycle on its line ---


def test_a_cycle_without_a_line_takes_the_one_its_first_call_works_on(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    own = milestone(agent, 2, 4, "Ten views of the poster")
    ctx = working(agent)
    refused = call(ctx, "project_update", {"project_id": 2})  # nothing to change: refused, so no line taken
    assert not refused.ok and ctx.state.focus_project_id is None
    workspace = agent.roots()[0]
    assert call(ctx, "workspace_write", {"path": "notes.md", "mode": "create", "content": "# Notes\n"}).ok
    assert ctx.state.focus_project_id is None  # files are no line's work
    assert call(ctx, "project_update", {"project_id": 2, "note": "a better cover next"}).ok
    assert ctx.state.focus_project_id == 2
    assert rows(agent, f"SELECT project_id, milestone_id FROM cycles WHERE id = {ctx.cycle_id}") == [
        {"project_id": 2, "milestone_id": own}
    ]
    assert rows(agent, "SELECT path, project_id FROM workspace_files") == [{"path": "notes.md", "project_id": 2}]
    assert workspace.read("notes.md") == "# Notes\n"
    assert call(ctx, "project_update", {"project_id": 3, "note": "x"}).ok  # 0.33.0: its record, any cycle
    assert ctx.state.focus_project_id == 2
    outcome = call(ctx, "project_update", {"project_id": 3, "status": "active", "note": "back to it"})
    assert outcome.text == (
        "Error: this cycle works on product line #2: no update for project #3, whose work waits for a cycle of its own "
        "(say so in your journal's next)."
    )
    made = call(ctx, "project_create", {"title": "Stickers", "hypothesis": "h", "status": "idea"})
    assert "a new product line starts in a cycle of its own" in made.text
    assert call(ctx, "message_owner", {"text": "The poster line waits for its cycle."}).ok
    assert (
        "this cycle works on product line #2: no request for project #1"
        in call(
            ctx,
            "request_approval",
            {
                "type": "publish",
                "title": "A planner bundle",
                "description": "d",
                "payload": "p",
                "expected_cost": "free",
                "expected_benefit": "sales",
                "project_id": 1,
            },
        ).text
    )
    venture_cycle = working(agent, one_line=False)  # a venture cycle keeps every line's tools (it has none of them)
    assert call(venture_cycle, "project_update", {"project_id": 1, "hypothesis": "Teachers pay 5 EUR for it"}).ok
    assert venture_cycle.state.focus_project_id is None


def test_a_new_line_is_the_cycle_s_line(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    aimed = milestone(agent, 1, 4, "Ten views of the planner")
    ctx = working(agent)
    with agent.db.transaction() as conn:
        store.update_cycle(conn, ctx.cycle_id, milestone_id=aimed)  # its plan aimed at line #1's milestone
    made = call(ctx, "project_create", {"title": "Stickers", "hypothesis": "h", "status": "idea"})
    assert made.ok and ctx.state.focus_project_id == 4
    assert rows(agent, f"SELECT project_id, milestone_id FROM cycles WHERE id = {ctx.cycle_id}") == [
        {"project_id": 4, "milestone_id": None}  # line #1's milestone isn't the new line's
    ]


def test_obligations_name_their_line_and_close_in_its_cycle(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    rejected = obligation_of(agent, request(agent, 2))
    pin = obligation_of(agent, request(agent, 3, executor="pinterest_pin"))
    missed = milestone(agent, 1, -1, "Ten views of the planner")
    stamp = now(agent)
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, milestone_id)"
            " VALUES (?, ?, 'miss', 'decide', ?, ?, ?)",
            (agent.scope().mode, agent.scope().session, stamp[:10], stamp, missed),
        )
        message = store.insert_message(conn, agent.scope(), 1, "I'll send the numbers", stamp)
        promised = obligations.promise(conn, agent.scope(), 1, message, "send the numbers", stamp[:10], stamp)
    scope = agent.scope()
    with agent.db.connection() as conn:
        found = {int(r["id"]): obligations.owed(conn, scope, r) for r in obligations.open_rows(conn, scope)}
        owed = obligations.pressing_owed(conn, scope, agent.clock.today())
        listed = obligations.text(conn, scope, agent.clock.today())
    assert found[rejected] == obligations.Owed(2, forces=True)  # 0.33.0: the owner's decision
    assert found[pin] == obligations.Owed(3, marketing=True, forces=True)  # a pin's request: a marketing cycle's work
    assert obligations.Owed(1) in found.values()  # the missed milestone's line
    assert found[promised] == obligations.Owed(None, forces=True)  # a promise that names no line
    assert {o for _, o in owed} == set(found.values())
    assert f"\n- [line #2] #{rejected} decision (" in listed and f"\n- [line #3] #{pin} decision (" in listed
    assert f"\n- #{promised} promise to your owner, due today" in listed  # of no line
    seen = obligations.for_line(listed, 1)
    assert "[line #2: waits for its own cycle]" in seen and "[line #1]" in seen
    ctx = working(agent, line=1)
    # 0.33.0: what another line owes is met (or decided) in any cycle, with its evidence; its work stays its own
    closing = call(ctx, "obligation_done", {"numbers": f"{rejected}, {pin}", "result": "answered in message #1"})
    assert closing.text == f"Closed #{rejected}, #{pin}."


# --- marketing cycles ---


def test_a_marketing_cycle_refuses_building_and_an_ordinary_one_marketing(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent, line=1)
    ctx.marketing = True
    refused = call(ctx, "make_document", {"output": "a.pdf", "title": "A", "content": "x"})
    assert refused.text.startswith("Error: make_document is not one of your tools in a marketing cycle")
    ctx.marketing, ctx.marketing_apart = False, True
    refused = call(ctx, "propose_bluesky_post", {"text": "Hi", "reason": "reach"})
    assert refused.text.startswith("Error: propose_bluesky_post belongs to marketing cycles")
    on = {"mail": True, "workshop": True, "etsy": True, "venture": False, "library": True, "pinterest": True}
    on |= {"bluesky": True, "blog": True, "printify": True}
    assert tools.offered("propose_pin", **on) and tools.offered("propose_pin", marketing=True, **on)
    assert not tools.offered("propose_pin", marketing_apart=True, **on)
    assert tools.offered("propose_reddit_post", marketing_apart=True, **on)  # a first test of demand, too
    assert not tools.offered("propose_etsy_listing", marketing=True, **on)
    assert tools.offered("propose_etsy_edit", marketing=True, **on)


def test_reports_name_the_marketing_cycles() -> None:
    row = {"venture": 0, "project_id": 4, "marketing": 1}
    assert weekly._went_to(row) == "marketing #4"  # type: ignore[arg-type]
    assert weekly._went_to({**row, "marketing": 0}) == "project #4"  # type: ignore[arg-type]
    assert weekly._went_to({**row, "project_id": None}) == "marketing cycles"  # type: ignore[arg-type]
    assert weekly._went_to({"venture": 1, "project_id": None, "marketing": 0}) == "venture cycles"  # type: ignore[arg-type]


def test_the_status_line_shows_the_ventures_share_and_the_kind_of_cycle() -> None:
    snap = SimpleNamespace(
        venture_day=(2_000_000, 500_000), venture_share=25, venture=False, marketing=True, marketing_apart=True
    )
    assert context._share_line(snap) == (  # type: ignore[arg-type]
        "Your owner gives ventures 25% of your spending: $0.50 of today's $2.00 so far. This is a marketing cycle."
    )
    snap.marketing = False  # 0.35.0: the marketing share retired; a marketing step has a cycle of its own
    assert context._share_line(snap).endswith(  # type: ignore[arg-type]
        " Pins, Bluesky posts and blog posts belong to marketing cycles."
    )
