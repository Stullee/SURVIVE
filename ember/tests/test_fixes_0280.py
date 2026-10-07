"""0.28.0: one thing a wake cycle. An ordinary cycle worked on several product lines, an unbacked venture and marketing
at once (its rules said to juggle, its plan named a project, a venture and a milestone apart, and every tool took any
project): a licence bundle was listed in the cover-letter line, files and costs went to the wrong line. Now an
ordinary cycle works on one line it takes from READY (ranked by Ember's code), a marketing cycle (the owner's
marketing_share of each day's spending) brings buyers to one line's listings, a venture cycle decides one venture, and
the tools refuse another line's work for the rest of the cycle."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import (  # noqa: E402
    context,
    digest,
    lines,
    obligations,
    prompts,
    review,
    roadmap,
    store,
    tools,
    views,
    weekly,
)
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import ETSY, JOURNAL  # noqa: E402

# Every cycle a marketing cycle once a listing is live (nothing to market before: ordinary cycles, as with ROOMY, which
# has no marketing cycles)
MARKETS = ROOMY.model_copy(update={"marketing_share": 100})
IDLE = {"assessment": "ok", "goal": "Wait", "focus_project_id": None, "steps": [], "sleep_minutes": 120}


def now(agent: Agent) -> str:
    return to_iso(agent.clock.now())


def lined(data_dir: Path, titles: tuple[str, ...] = ("Planner", "Poster", "Checklist")) -> tuple[Agent, FakeTransport]:
    """A dry-run agent after one idle cycle, with an open product line for each title (#1, #2, ...)."""
    fake = FakeTransport(script=[Plan({**IDLE, "ready": "none: nothing to work on yet"})])
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


def ready(agent: Agent, explore: bool = True, **kw: Any) -> list[lines.Item]:
    with agent.db.connection() as conn:
        return lines.ready(conn, agent.scope(), today=agent.clock.today(), explore=explore, markets=False, **kw)


def texts(fake: FakeTransport, kind: str) -> list[str]:
    return [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == kind]


# --- what a wake cycle is ---


@pytest.mark.parametrize(("ventures", "share", "kept"), [(25, 20, 20), (90, 20, 10), (100, 20, 0), (0, 100, 100)])
def test_marketing_gets_the_owners_share_within_what_the_ventures_leave(ventures: int, share: int, kept: int) -> None:
    assert lines.marketing_share(ventures, share) == kept


@pytest.mark.parametrize(
    ("share", "spent", "marketed", "turn"),
    [
        (20, 0, 0, False),  # the day's first cycle is an ordinary one
        (20, 500, 0, True),
        (20, 500, 100, False),  # exactly the share
        (20, 501, 100, True),
        (0, 500, 0, False),  # switched off
        (100, 0, 0, True),  # every cycle
    ],
)
def test_a_cycle_is_a_marketing_cycle_while_marketing_is_below_its_share(
    share: int, spent: int, marketed: int, turn: bool
) -> None:
    assert lines.marketing_turn(share, spent, marketed) is turn


def kind(spends: tuple[int, int, int], **changes: Any) -> lines.Turn:
    """What a cycle is with the ventures' 25 % and marketing's 20 %, both running, and line #4 to market."""
    given: dict[str, Any] = {
        "venture_share": 25,
        "share": 20,
        "spends": spends,
        "ventures_run": True,
        "markets": True,
        "marketable": [4],
        "owed": [],
        "messages": False,
    }
    return lines.kind(**{**given, **changes})


PUSH = obligations.Owed(4, marketing=True)  # a push to bring buyers to line #4 (gates.MARKET)


@pytest.mark.parametrize(
    ("spends", "changes", "turn"),
    [
        ((1000, 250, 200), {}, lines.Turn(lines.ORDINARY)),  # both have their share
        ((1000, 100, 200), {}, lines.Turn(lines.VENTURE)),
        ((1000, 250, 100), {}, lines.Turn(lines.MARKETING)),
        ((1000, 200, 100), {}, lines.Turn(lines.MARKETING)),  # both behind: marketing by $0.10, ventures by $0.05
        ((1000, 100, 150), {}, lines.Turn(lines.VENTURE)),
        ((1000, 150, 100), {}, lines.Turn(lines.VENTURE)),  # as far behind: the ventures first
        ((1000, 100, 200), {"ventures_run": False}, lines.Turn(lines.ORDINARY)),  # not in the burn mode
        ((1000, 250, 100), {"markets": False}, lines.Turn(lines.ORDINARY)),  # nothing live to market
        # what the owner and pressing product work wait for comes first
        ((1000, 100, 100), {"messages": True}, lines.Turn(lines.ORDINARY, owed_first=True)),
        ((1000, 100, 100), {"owed": [obligations.Owed(4)]}, lines.Turn(lines.ORDINARY, owed_first=True)),
        ((1000, 100, 100), {"owed": [obligations.Owed(None)]}, lines.Turn(lines.ORDINARY, owed_first=True)),
        ((1000, 250, 200), {"owed": [obligations.Owed(None)]}, lines.Turn(lines.ORDINARY)),
        # a pressing push to bring buyers: a marketing cycle, whatever the shares
        ((1000, 100, 200), {"owed": [PUSH]}, lines.Turn(lines.MARKETING)),
        ((1000, 100, 200), {"owed": [PUSH], "markets": False}, lines.Turn(lines.ORDINARY, owed_first=True)),
        # a push for a line that can't be marketed: still before the ventures, and marketing's share decides
        ((1000, 100, 100), {"owed": [obligations.Owed(9, True)]}, lines.Turn(lines.MARKETING)),
        ((1000, 100, 200), {"owed": [obligations.Owed(9, True)]}, lines.Turn(lines.ORDINARY)),
    ],
)
def test_what_a_cycle_is(spends: tuple[int, int, int], changes: dict[str, Any], turn: lines.Turn) -> None:
    assert kind(spends, **changes) == turn


def test_the_options_carry_the_marketing_share() -> None:
    assert Settings().marketing_share == 20  # the default (the tests' ROOMY has none, as it has no ventures)
    with pytest.raises(ValueError, match="less than or equal to 100"):
        ROOMY.model_validate({**ROOMY.model_dump(), "marketing_share": 101})


# --- READY: the lines an ordinary plan takes one from ---


def test_ready_ranks_the_lines_by_what_they_owe_and_need(data_dir: Path) -> None:
    agent, _ = lined(data_dir, ("Waiting", "Owing", "Due", "Worked", "Fresh"))
    owed = request(agent, 2)
    due = milestone(agent, 3, 3, "A first sale of the checklist")
    milestone(agent, 5, 30, "Far away")  # not due within DUE_DAYS
    with agent.db.transaction() as conn:
        store.update_project(conn, 1, now(agent), status="waiting")
    cycle(agent, project=4)  # a cycle worked on line #4
    found = ready(agent)
    # 0.30.0: the line the last cycle worked on comes right after what is owed (0.28.0 put it after the ones never
    # worked on, so every cycle took another line)
    assert [i.key for i in found] == ["line #2", "line #4", "line #3", "line #5", "line #1", "new line"]
    assert [i.job for i in found] == [True, True, True, False, False, False]
    assert found[0].text.startswith(f"Owing [active] · owes obligation #{obligation_of(agent, owed)} · ")
    assert found[1].text.startswith("Worked [active] · in progress, cycle 2 of at most 3 in a row · ")
    assert found[1].text.endswith(f"last worked on {agent.clock.today().isoformat()}")
    assert f"milestone #{due} due " in found[2].text and "nothing live yet: build it" in found[2].text
    assert found[3].text.endswith("never worked on") and found[4].text.startswith("Waiting [waiting]")
    assert found[-1].text == "start a new product line (project_create): 4 in flight"
    assert [i.key for i in ready(agent, explore=False)] == [i.key for i in found[:-1]]  # no new line outside explore
    text = lines.text(found, ["Which line sells first?"])
    assert text.startswith(f"{lines.HEADING}\n1. line #2: Owing [active]")
    assert text.endswith(f"\n6. new line: {found[-1].text}\n{lines.QUESTIONS}\n- Which line sells first?")
    with agent.db.transaction() as conn:
        for project in (2, 3, 4, 5):
            store.update_project(conn, project, now(agent), status="waiting")
    assert ready(agent)[0].key == "new line"  # fewer than IN_FLIGHT lines in flight: a new one first


def test_ready_without_lines_offers_the_first_one_in_explore(data_dir: Path) -> None:
    agent, _ = lined(data_dir, ())
    assert ready(agent) == [lines.Item(lines.NEW, None, "start your first product line (project_create)")]
    assert ready(agent, explore=False) == []


def test_a_pressing_obligation_takes_its_line_once_a_day(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    owed = [obligations.Owed(3)]
    since = to_iso(agent.clock.now() - timedelta(hours=lines.PRESS_HOURS))
    pressed = ready(agent, owed=owed, since=since)
    assert [(i.key, i.pressed, i.job) for i in pressed] == [("line #3", True, True)]
    assert lines.text(pressed).startswith(f"{lines.PRESSED_HEADING}\n1. line #3: Checklist")
    with agent.db.connection() as conn:  # a push to bring buyers is a marketing cycle's while those run
        market = lines.ready(
            conn, agent.scope(), today=agent.clock.today(), explore=True, markets=True, owed=[PUSH], since=since
        )
    assert not any(i.pressed for i in market)
    agent.clock.advance(minutes=5)
    taken = cycle(agent, project=3)
    with agent.db.transaction() as conn:
        lines.record(conn, taken, "line", pressed, pressed[0], "", now(agent))
    agent.clock.advance(hours=1)
    again = ready(agent, owed=owed, since=to_iso(agent.clock.now() - timedelta(hours=lines.PRESS_HOURS)))
    assert len(again) == 4 and not any(i.pressed for i in again)  # taken once today: READY ranks as usual
    agent.clock.advance(hours=lines.PRESS_HOURS)
    assert ready(agent, owed=owed, since=to_iso(agent.clock.now() - timedelta(hours=lines.PRESS_HOURS)))[0].pressed


def test_the_plans_answer_is_read_by_code() -> None:
    items = [lines.Item(lines.LINE, 3, "Planner"), lines.Item(lines.LINE, 5, "Poster"), lines.Item(lines.NEW, None, "")]
    assert lines.choose(items, "line #5: it owes a reaction") == (items[1], "")
    assert lines.choose(items, " Line 3") == (items[0], "")
    assert lines.choose(items, "new line: both lines wait") == (items[2], "")
    assert lines.choose(items, "new") == (items[2], "")
    assert lines.choose(items, "none: my owner asked me to wait") == (None, "my owner asked me to wait")
    assert lines.choose(items, "none") == (None, "no reason given")
    assert lines.choose(items, "line #9") == (None, "'line #9' isn't on the READY list")
    assert lines.choose(items, "market #3") == (None, "'market #3' isn't on the READY list")
    assert lines.choose(items, "") == (None, "the plan named none")
    assert lines.choose([], "newline") == (None, "'newline' isn't on the READY list")


# --- the plan takes one line ---


def take(ready_: str, focus: int | None = None, **plan: Any) -> Plan:
    return Plan(
        {
            "assessment": "ok",
            "goal": "Work on one line",
            "money_path": "Buyers pay for what the line sells",
            "focus_project_id": focus,
            "steps": ["work on the line"],
            "sleep_minutes": 120,
            "ready": ready_,
            **plan,
        }
    )


def test_an_ordinary_plan_takes_one_line_and_the_cycle_counts_for_it(data_dir: Path) -> None:
    agent, fake = lined(data_dir)
    other = milestone(agent, 1, 5, "Ten views of the planner")
    own = milestone(agent, 3, 9, "A first sale of the checklist")
    fake.script.extend(
        [
            # a slip: the plan took line #3 but named line #1 its focus, with line #1's milestone and a venture
            take("line #3: its sale is due", focus=1, focus_venture_id=ETSY, focus_milestone_id=other),
            ToolCalls(
                [
                    ("project_update", {"project_id": 3, "next_step": "add a cover"}),
                    ("project_update", {"project_id": 2, "next_step": "redo the poster"}),
                    ("project_update", {"project_id": 1, "status": "abandoned", "note": "no demand"}),  # a close
                    ("project_create", {"title": "Stickers", "hypothesis": "h", "next_step": "n", "status": "idea"}),
                ]
            ),
            Reply("Done."),
            ToolCalls([("project_update", {"project_id": 2, "note": "waits"})]),  # the reflection too
        ]
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert rows(agent, "SELECT project_id, venture_id, milestone_id, venture, marketing FROM cycles WHERE id = 2") == [
        {"project_id": 3, "venture_id": None, "milestone_id": own, "venture": 0, "marketing": 0}
    ]
    answers = rows(agent, "SELECT tool, status, phase, result FROM tool_calls WHERE cycle_id = 2 ORDER BY id")
    assert [(a["tool"], a["status"], a["phase"]) for a in answers] == [
        ("project_update", "ok", "act"),
        ("project_update", "error", "act"),
        ("project_update", "ok", "act"),
        ("project_create", "error", "act"),
        ("project_update", "error", "reflect"),
    ]
    assert answers[1]["result"] == (
        "Error: this cycle works on product line #3: no update for project #2, whose work waits for a cycle of its "
        "own (say so in your journal's next)."
    )
    assert "a new product line is a cycle of its own" in answers[3]["result"]
    assert "this cycle works on product line #3" in answers[4]["result"]
    [pick] = rows(agent, "SELECT kind, pick, project_id, pressed, why_not, items FROM desk_picks WHERE cycle_id = 2")
    assert (pick["kind"], pick["pick"], pick["project_id"], pick["pressed"], pick["why_not"]) == (
        "line",
        "line #3",
        3,
        0,
        None,
    )
    # line #1's milestone is due first; a new line last (two lines are in flight)
    assert [i["key"] for i in json.loads(pick["items"])] == ["line #1", "line #3", "line #2", "new line"]
    with pytest.raises(sqlite3.IntegrityError, match="a pick never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE desk_picks SET project_id = 2 WHERE cycle_id = 2")  # kept as the plan took it
    [planner] = texts(fake, "plan")[-1:]
    assert f"\n== READY ==\n{lines.HEADING}\n1. line #1: Planner [active] · milestone #{other} due " in planner
    brief = texts(fake, "work")[-1]
    assert "Your line this cycle: project #3. Ember's code keeps your tools on it" in brief
    assert (
        f"Your plan aimed at milestone #{other}, which isn't line #3's: the cycle is aimed at #{own}, its milestone "
        "due first." in brief
    )
    assert rows(agent, "SELECT status FROM projects WHERE id = 1") == [{"status": "abandoned"}]


def test_a_plan_without_a_key_takes_the_line_it_named_or_none(data_dir: Path) -> None:
    agent, fake = lined(data_dir)
    fake.script.extend(
        [
            take("the poster", focus=2),  # no key: its focus, a line it may take
            ToolCalls([("project_list", {})]),
            Reply("Done."),
            JOURNAL,
            take("none: waiting for my owner's answer", focus=2),  # none: no line, whatever its focus
            ToolCalls([("project_update", {"project_id": 1, "note": "checked"})]),  # the first call takes line #1
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    picks = rows(agent, "SELECT cycle_id, pick, project_id, why_not FROM desk_picks WHERE cycle_id > 1 ORDER BY id")
    assert picks == [
        {"cycle_id": 2, "pick": "line #2", "project_id": 2, "why_not": None},
        {"cycle_id": 3, "pick": None, "project_id": None, "why_not": "waiting for my owner's answer"},
    ]
    assert rows(agent, "SELECT id, project_id FROM cycles WHERE id > 1 ORDER BY id") == [
        {"id": 2, "project_id": 2},
        {"id": 3, "project_id": 1},  # 0.28.0: the line its first call worked on (tools._lock)
    ]


def test_a_pressing_obligation_takes_its_line_whatever_the_plan_said(data_dir: Path) -> None:
    agent, fake = lined(data_dir)
    owed = obligation_of(agent, request(agent, 2))
    fake.script.extend([take("line #3", focus=3), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    planner = texts(fake, "plan")[-1]
    assert f"\n== READY ==\n{lines.PRESSED_HEADING}\n1. line #2: Poster [active] · owes obligation #{owed}" in planner
    assert rows(agent, "SELECT project_id FROM cycles WHERE id = 2") == [{"project_id": 2}]
    assert rows(agent, "SELECT pick, pressed FROM desk_picks WHERE cycle_id = 2") == [{"pick": "line #2", "pressed": 1}]
    brief = texts(fake, "work")[-1]
    assert "Ember's code took line #2 for this cycle: an obligation of it presses (OBLIGATIONS)." in brief
    assert "Deal with what line #2 owes first (OBLIGATIONS), then its next step" in brief


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
    assert call(ctx, "project_update", {"project_id": 2, "next_step": "a better cover"}).ok
    assert ctx.state.focus_project_id == 2
    assert rows(agent, f"SELECT project_id, milestone_id FROM cycles WHERE id = {ctx.cycle_id}") == [
        {"project_id": 2, "milestone_id": own}
    ]
    assert rows(agent, "SELECT path, project_id FROM workspace_files") == [{"path": "notes.md", "project_id": 2}]
    assert workspace.read("notes.md") == "# Notes\n"
    outcome = call(ctx, "project_update", {"project_id": 3, "next_step": "x"})
    assert outcome.text == (
        "Error: this cycle works on product line #2: no update for project #3, whose work waits for a cycle of its own "
        "(say so in your journal's next)."
    )
    assert (
        "a new product line is a cycle of its own"
        in call(
            ctx, "project_create", {"title": "Stickers", "hypothesis": "h", "next_step": "n", "status": "idea"}
        ).text
    )
    assert call(ctx, "project_update", {"project_id": 3, "status": "abandoned"}).ok  # closing another line
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
    assert call(venture_cycle, "project_update", {"project_id": 1, "next_step": "x"}).ok
    assert venture_cycle.state.focus_project_id is None


def test_a_new_line_is_the_cycle_s_line(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    aimed = milestone(agent, 1, 4, "Ten views of the planner")
    ctx = working(agent)
    with agent.db.transaction() as conn:
        store.update_cycle(conn, ctx.cycle_id, milestone_id=aimed)  # its plan aimed at line #1's milestone
    made = call(ctx, "project_create", {"title": "Stickers", "hypothesis": "h", "next_step": "n", "status": "idea"})
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
    assert found[rejected] == obligations.Owed(2)
    assert found[pin] == obligations.Owed(3, marketing=True)  # a pin's request is a marketing cycle's work
    assert obligations.Owed(1) in found.values()  # the missed milestone's line
    assert found[promised] == obligations.Owed(None)  # a promise is of no line
    assert {o for _, o in owed} == set(found.values())
    assert f"\n- [line #2] #{rejected} decision (" in listed and f"\n- [line #3] #{pin} decision (" in listed
    assert f"\n- #{promised} promise to your owner, due today" in listed  # of no line
    seen = obligations.for_line(listed, 1)
    assert "[line #2: waits for its own cycle]" in seen and "[line #1]" in seen
    ctx = working(agent, line=1)
    closing = call(ctx, "obligation_done", {"numbers": f"{rejected}, {pin}", "result": "answered in message #1"})
    assert closing.text == (
        f"Error: #{rejected} is line #2's: it closes in a cycle on that line; #{pin} is line #3's: it closes in a "
        "cycle on that line."
    )
    with agent.db.transaction() as conn:
        store.update_project(conn, 2, now(agent), status="abandoned")  # a closed line: any cycle may close its own
    assert call(ctx, "obligation_done", {"numbers": str(rejected), "result": "closed project #2"}).ok


# --- marketing cycles ---


def marketed(data_dir: Path) -> tuple[Agent, FakeTransport, int]:
    """A dry-run agent whose first listing is live, with marketing cycles on (every cycle one from now on); its
    product line."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4, settings=MARKETS)
    [listing] = rows(agent, "SELECT id, project_id FROM approvals WHERE executor = 'etsy_listing'")
    owner(agent).decide(listing["id"], {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(listing["id"], "active")]
    assert rows(agent, "SELECT marketing FROM cycles") == [{"marketing": 0}] * 4  # nothing was live to market
    return agent, fake, int(listing["project_id"])


def test_a_marketing_cycle_brings_buyers_to_one_line(data_dir: Path) -> None:
    agent, fake, line = marketed(data_dir)
    end = agent.run_cycle("schedule")
    assert end.status in ("completed", "idle"), end
    assert rows(agent, "SELECT marketing, venture, project_id FROM cycles WHERE id = 5") == [
        {"marketing": 1, "venture": 0, "project_id": line}
    ]
    planner = texts(fake, "plan")[-1]
    assert "Plan this marketing cycle." in planner and "This is a marketing cycle." in planner
    assert f"\n== READY ==\n{lines.HEADING}\n1. market #{line}: " in planner
    system = [r for r in fake.sent if request_kind(r) == "plan"][-1]["system"]
    assert [b["text"] for b in system][-2:] == [prompts.PLANNER_RULES, prompts.MARKETING_RULES]
    [pick] = rows(agent, "SELECT kind, pick, project_id FROM desk_picks WHERE cycle_id = 5")
    assert pick == {"kind": "market", "pick": f"market #{line}", "project_id": line}
    work = [r for r in fake.sent if request_kind(r) == "work"]
    if work:  # the fake worked: its tools are a marketing cycle's
        names = {t["name"] for t in work[-1]["tools"]}
        assert "propose_etsy_edit" in names and not names & tools.BUILDING_TOOLS
        brief = work[-1]["messages"][0]["content"][0]["text"]
        assert "- listing #900000001 " in brief
    activity = views.dashboard(agent)["activity"]
    newest = next(a for a in activity if a["cycle_id"] == 5)
    assert newest["kind"] == "marketing" and newest["about"]["type"] == "line" and newest["about"]["id"] == line
    desk = views.dashboard(agent)["lines"]
    assert (desk["share"], desk["owner_share"], desk["marketing_cycles"]) == (100, 100, 1)
    assert desk["picks"][0]["kind"] == "market" and desk["market"][0]["key"] == f"market #{line}"
    with agent.db.connection() as conn:
        assert " · a marketing cycle" in digest.build(conn, 5, "completed", None)[0]
        since = to_iso(agent.clock.now() - timedelta(days=1))
        assert "\n1 marketing cycle had $" in review._cycles(conn, agent.scope(), since)


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


def test_the_status_line_shows_the_marketing_share_only_while_marketing_cycles_run() -> None:
    snap = SimpleNamespace(
        venture_day=(2_000_000, 500_000),
        venture_share=25,
        marketing_share=20,
        marketing_spent=400_000,
        venture=False,
        marketing=True,
        marketing_apart=False,
    )
    assert context._share_line(snap) == (  # type: ignore[arg-type]
        "Your owner gives ventures 25% and marketing 20% of your spending: $0.50 and $0.40 of today's $2.00 so far."
        " This is a marketing cycle."
    )
    snap.marketing, snap.marketing_apart = False, True
    assert context._share_line(snap).endswith(  # type: ignore[arg-type]
        " Pins, Bluesky posts and blog posts belong to marketing cycles."
    )
    snap.marketing_share, snap.marketing_apart = 0, False  # none run: the loop gives no share
    assert context._share_line(snap) == (  # type: ignore[arg-type]
        "Your owner gives ventures 25% of your spending: $0.50 of today's $2.00 so far."
    )
