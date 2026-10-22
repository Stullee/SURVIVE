"""0.37.0: everything Ember does is a step of the plan, weighed like any other.

The owner: "couldn't we just integrate potential ventures as a sub goal into the plan and remove the ventures tab
completely?", and "shouldn't promises just move into the plan and be evaluated for weight?". In 0.36.0 one Explore
step stood for every venture, and a venture cycle's plan then picked one venture from READY, a list Ember's code
ranked by rules of its own; a promise to the owner, or their decision, came first by a rule of its own too. Now each
venture being explored is a node of the plan's Ventures, its next decision a step weighed like any (worth what its
case expects, an idea by its scores; the owner's wish and a park soon urgent), and a venture cycle is aimed at its
step's venture. A promise or decision of no product is a step of the Owner project, and every promise and decision is
weighed, worth at least PROMISE_WORTH and urgent as its day nears. The Ventures tab is the Plan tab's.

Tests from 0.37.0 and 0.39.1 (formerly test_fixes_0370.py, test_fixes_0391.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from datetime import timedelta  # noqa: E402
from typing import Any  # noqa: E402

from app import paths  # noqa: E402
from app.agent import desk, obligations, plan, ventures, views, weights  # noqa: E402  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.db import discover_migrations, migrate  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.etsy_helpers import call  # noqa: E402
from tests.plan.test_cycle_line import cycle, lined, now, working  # noqa: E402  # noqa: E402
from tests.plan.test_exploring import NOW, explored, exploring_agent  # noqa: E402
from tests.plan.test_plan_tree import (  # noqa: E402
    TRACKER,
    keep,
    project,
)
from tests.plan.test_promises import promise, steered  # noqa: E402
from tests.plan.test_tree_steers import step_id  # noqa: E402
from tests.ventures.test_ventures import DROPSHIPPING, ETSY, FIVERR  # noqa: E402

SCRIPT = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")


def venture_nodes(agent: Agent) -> dict[int, dict[str, object]]:
    """Each venture's node of the plan (the newest), with its open steps' templates."""
    found: dict[int, dict[str, object]] = {}
    for n in rows(agent, "SELECT * FROM plan_nodes WHERE level = 'venture' ORDER BY id"):
        steps = rows(agent, f"SELECT template FROM plan_nodes WHERE parent_id = {n['id']} AND status = 'open'")
        found[int(n["venture_id"])] = {**n, "steps": [s["template"] for s in steps]}
    return found


def waits(agent: Agent) -> dict[int | None, str | None]:
    """What each venture's step waits on now (None: ready), as the next cycle's choice sees it."""
    steer = explored(agent)
    with agent.db.connection() as conn:
        return {plan.venture_of(conn, agent.scope(), c.step.id): c.waiting for c in steer.found if c.stage == "venture"}


# --- the migration ---


def test_the_migration_drops_the_one_explore_step_and_a_venture_becomes_a_node(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 90], backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(db_file)
    node = (
        "INSERT INTO plan_nodes (id, mode, session, parent_id, level, platform, template, kind, title, source, pinned,"
        " created_at, updated_at) VALUES (?, 'live', 0, ?, ?, ?, ?, ?, ?, 'code', ?, ?, ?)"
    )
    conn.execute(node, (1, None, "project", "ventures", None, None, "Ventures", 0, NOW, NOW))
    conn.execute(node, (2, 1, "step", None, "explore@1", "create", "Explore your ventures", 1, NOW, NOW))
    conn.commit()
    conn.close()
    assert migrate(db_file, discover_migrations(), backup_dir=tmp_path / "backups") == [
        m.version for m in discover_migrations() if m.version > 90
    ]
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    explore = dict(conn.execute("SELECT status, closed_by, pinned, result FROM plan_nodes WHERE id = 2").fetchone())
    assert explore == {
        "status": "dropped",
        "closed_by": "code",
        "pinned": 0,
        "result": "0.37.0: each venture's next decision is a step of its own",
    }
    venture = (
        "INSERT INTO plan_nodes (mode, session, parent_id, level, venture_id, title, source, owner_worth, hold_reason,"
        " hold_by, created_at, updated_at) VALUES ('live', 0, 1, 'venture', ?, 'Phone cases', 'code', ?, ?, ?, ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # a venture's node names its venture
        conn.execute(venture, (None, None, None, None, NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # its worth may be the owner's, a hold is no venture's
        conn.execute(venture, (3, 4.0, "nothing new", "owner", NOW, NOW))
    conn.execute(venture, (3, 4.0, None, None, NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="plan_nodes_one_venture|UNIQUE"):  # one open node a venture
        conn.execute(venture, (3, None, None, None, NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="title and check are fixed"):
        conn.execute("UPDATE plan_nodes SET venture_id = 4 WHERE venture_id = 3")
    owner_top = (
        "INSERT INTO plan_nodes (mode, session, level, platform, title, source, owner_worth, created_at, updated_at)"
        " VALUES ('live', 0, 'project', 'owner', 'Your owner', 'code', ?, ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # what the owner is owed is worth PROMISE_WORTH
        conn.execute(owner_top, (2.0, NOW, NOW))
    conn.execute(owner_top, (None, NOW, NOW))
    conn.close()


# --- the ventures: each a node of the plan, its next decision a step ---


@pytest.mark.exploring
def test_each_venture_being_explored_is_a_node_its_next_decision_a_step(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    laid = venture_nodes(agent)
    with agent.db.connection() as conn:
        top = plan.ventures_node(conn, agent.scope())
    assert top is not None and {n["parent_id"] for n in laid.values()} == {top["id"]}
    assert FIVERR not in laid  # the owner parked it: nothing to decide
    assert laid[ETSY]["steps"] == ["appraise@1"] and laid[DROPSHIPPING]["steps"] == ["triage@1"]
    assert all(len(n["steps"]) == 1 for n in laid.values())  # one next decision each
    first = laid[DROPSHIPPING]["id"]
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "research"}, "Stefan").status == 200
    keep(agent)  # researched now: its triage is done and its appraisal the next step, under the same node
    assert venture_nodes(agent)[DROPSHIPPING]["steps"] == ["appraise@1"]
    [triage] = rows(agent, f"SELECT status, result FROM plan_nodes WHERE parent_id = {first} AND template = 'triage@1'")
    assert triage == {"status": "done", "result": f"venture #{DROPSHIPPING} is researching now"}
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "park"}, "Stefan").status == 200
    # until the next cycle's keeper closes it, its step waits (the Plan tab says it closes then)
    assert waits(agent)[DROPSHIPPING] == "moved" and waits(agent)[ETSY] is None
    keep(agent)  # parked: its node and its step are dropped
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {first}") == [
        {"status": "dropped", "result": f"venture #{DROPSHIPPING} is parked"}
    ]
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "research"}, "Stefan").status == 200
    keep(agent)  # taken up again: a new node (a closed one is final)
    again = venture_nodes(agent)[DROPSHIPPING]
    assert again["id"] != first and again["status"] == "open" and again["steps"] == ["appraise@1"]
    with agent.db.connection() as conn:
        said = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
    assert "Ventures, each next decision a step of yours: #" in said


@pytest.mark.exploring
def test_a_ventures_worth_is_what_its_case_expects_or_the_owners(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    weighed = {c.step.id: c for c in explored(agent).found if c.stage == "venture"}
    step = int(venture_nodes(agent)[ETSY]["id"])
    [etsy_step] = rows(agent, f"SELECT id FROM plan_nodes WHERE parent_id = {step} AND status = 'open'")
    assert weighed[etsy_step["id"]].step.worth == weights.EXPLORE_WORTH  # no case yet
    with agent.db.transaction() as conn:  # the owner's worth for this venture alone
        plan.set_worth(conn, agent.scope(), step, 7.0, "Owner", now(agent))
    weighed = {c.step.id: c for c in explored(agent).found if c.stage == "venture"}
    assert weighed[etsy_step["id"]].step.worth == 7.0
    with agent.db.transaction() as conn, pytest.raises(plan.PlanError, match="a worth is a product's"):
        plan.set_worth(conn, agent.scope(), etsy_step["id"], 7.0, "Owner", now(agent))  # a step has none


@pytest.mark.exploring(turns=True)
def test_the_owners_explore_next_doesnt_wait_for_a_new_products_turn(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)  # no product yet, and the cycle before explored: a new product's turn
    turn = explored(agent)
    assert turn.step is None and {c.waiting for c in turn.found if c.stage == "venture"} == {"turn"}
    step = int(
        rows(agent, f"SELECT id FROM plan_nodes WHERE parent_id = {venture_nodes(agent)[DROPSHIPPING]['id']}")[0]["id"]
    )
    with agent.db.transaction() as conn:  # found live: Explore next did nothing while the turn was a new product's
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    pinned = explored(agent)
    assert (pinned.kind, pinned.pick.decided, pinned.venture) == ("venture", "pin", DROPSHIPPING)
    assert {c.waiting for c in pinned.found if c.stage == "venture" and c.step.id != step} == {"turn"}


# --- what the owner is owed: steps of the plan, weighed ---


def test_a_promise_of_no_product_is_a_step_of_the_owner_project_weighed_like_any(data_dir: Path) -> None:
    agent, _ = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist, each with steps ready
    report = promise(agent, "Report the Bluesky reactions of the week", days=3)
    said = keep(agent)
    assert f"Plan tree: obligation #{report} (promise, of no product) is a step of your owner's." in said
    [top] = rows(agent, "SELECT id, title, owner_worth, hold_reason FROM plan_nodes WHERE platform = 'owner'")
    assert top == {"id": top["id"], "title": "Your owner", "owner_worth": None, "hold_reason": None}
    [step] = rows(agent, f"SELECT * FROM plan_nodes WHERE obligation_id = {report}")
    assert (step["parent_id"], step["project_id"]) == (top["id"], None)
    assert (step["kind"], step["source"]) == ("promise", "promise")
    assert step["title"] == f"Keep promise #{report}: Report the Bluesky reactions of the week"
    taken = steered(agent)  # worth the owner's word; 0.37.1: 3 days out, no urgency yet (0.37.0: the floor)
    assert taken.step is not None and taken.step.id == step["id"] and taken.pick.decided == "weight"
    assert taken.kind == "ordinary" and taken.line is None
    assert taken.pick.parts is not None
    assert (taken.pick.parts.worth, taken.pick.parts.urgency) == (weights.PROMISE_WORTH, 0.0)
    with agent.db.connection() as conn:
        text = plan.step_text(conn, agent.scope(), taken, explore=False)
    assert text.startswith(f"Step #{step['id']}: Keep promise #{report}: ")
    assert "Done when: you kept it and closed it with obligation_done." in text
    with agent.db.transaction() as conn:
        obligations.close_one(conn, report, "sent the report", "agent", None, now(agent))
    keep(agent)
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {step['id']}") == [
        {"status": "done", "result": "Ember's code: the promise was kept"}
    ]


# --- the Plan tab: the ventures are its own ---


@pytest.mark.exploring
def test_the_plan_tab_lists_each_ventures_step_and_names_a_steps_venture(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    shown = views.plan_view(agent)
    assert (shown["now"]["venture"], shown["now"]["venture_title"]) == (ETSY, "Etsy digital products")
    assert all(s["venture"] is not None for s in shown["next"])  # no product yet: every step is a venture's
    [picked] = shown["picks"]  # the cycle the agent ran: a venture cycle, on Etsy's step
    assert (picked["kind"], picked["venture"]) == ("venture", ETSY)
    ventures = shown["ventures"]
    assert (ventures["hold"], ventures["owner_worth"]) == (None, None)
    by_venture = {v["venture"]: v for v in ventures["ventures"]}
    etsy = by_venture[ETSY]
    assert (etsy["stage"], etsy["step"]["decision"], etsy["worth"]) == ("researching", "appraise", 2.0)
    assert etsy["needs"].startswith("needs ")  # what it needs now, without its title (its row names it)
    assert etsy["step"]["weight"] is not None and etsy["step"]["why"].startswith("worth 2 × kind 0.8")
    assert FIVERR not in by_venture
    assert ventures["brainstorm"] is None  # six ideas wait: no brainstorm is due
    shop = views.ventures_view(agent)  # the venture tree's own view: no decision desk any more
    assert "desk" not in shop and shop["forecasts"] is None
    assert shop["decided_week"] == 1  # the owner's Fiverr, parked when the tree was planted


def test_the_ventures_steps_list_and_the_plan_tabs_links() -> None:
    steps = SCRIPT[SCRIPT.index("  function renderVentureSteps(d) {") : SCRIPT.index("  // 0.36.0: the owner's freeze")]
    assert 'planButton(st.pinned ? "Unpin" : "Explore next"' in steps  # a pin: the next venture cycle takes it
    assert 'st.decision !== "decide"' in steps  # the owner's own decision is made on the venture's card
    assert "showVentureCard(x.venture)" in steps
    render = SCRIPT[SCRIPT.index("  function renderPlan() {") : SCRIPT.index("  function renderPlanNow(d) {")]
    assert "renderVentureSteps(d);" in render and "renderVentureDesk" not in SCRIPT
    assert 'if (s.venture) return "venture #" + s.venture' in SCRIPT  # a venture's step names its venture
    assert 'if (name === "plan") { loadPlan(); loadRoadmap(); loadVentures(); }' in SCRIPT


# --- From 0.39.1 (formerly test_fixes_0391.py) ---


# --- 3. the ventures' steps ---


def idea_steps(agent: Agent) -> dict[int, int]:
    """The open triage step of each idea, by venture."""
    with agent.db.connection() as conn:
        found = plan.nodes(conn, agent.scope(), "level = 'step' AND status = 'open' AND template = 'triage@1'")
        return {int(plan.venture_of(conn, agent.scope(), int(s["id"]))): int(s["id"]) for s in found}


@pytest.mark.exploring
def test_a_triage_names_the_other_ideas_waiting_and_a_ventures_step_waits_for_its_day(data_dir: Path) -> None:
    """Live, six cycles in a row each parked one idea of a brainstorm, and an idea "paused until 10-20" came back the
    next day: nothing could make a venture's step wait."""
    agent = exploring_agent(data_dir)
    triages = idea_steps(agent)
    assert DROPSHIPPING in triages and len(triages) >= 4
    step = triages[DROPSHIPPING]
    with agent.db.transaction() as conn:  # the owner's Explore next: this cycle's step
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    steer = explored(agent)
    assert steer.kind == "venture" and steer.step is not None and steer.step.id == step

    def your_step() -> str:
        with agent.db.connection() as conn:
            return plan.step_text(conn, agent.scope(), steer, explore=True, today=agent.clock.today())

    others = [v for v in sorted(triages) if v != DROPSHIPPING]
    titles = {v["id"]: v["title"] for v in rows(agent, "SELECT id, title FROM ventures")}
    named = " · ".join(f'#{v} "{titles[v]}"' for v in others)
    assert (
        f"Other ideas wait for their triage: {named}: triage them in this cycle too (venture_update each: park the"
        " ones you would park, with why; research the one that deserves it most). A cycle for each costs more than"
        " its decision."
    ) in your_step()
    ctx = working(agent)
    ctx.venture, ctx.state.step_id = True, step  # a venture cycle: no product line
    later = triages[others[0]]
    day = (agent.clock.today() + timedelta(days=5)).isoformat()
    waited = call(
        ctx, "plan_step", {"action": "wait", "step_id": later, "on": "date", "until": day, "why": "after the freeze"}
    )
    assert waited.ok and waited.text == (  # 0.42.6: with its title
        f'Step #{later} ("Triage it: research it, or park it with why") waits on {day} (1 of {plan.WAITS_A_DAY} '
        "waits today)."
    )
    assert f"#{others[0]} " not in your_step().split("Other ideas wait for their triage: ")[1]
    assert {c.step.id: c.waiting for c in explored(agent).found}[later] == "date"
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    product = call(ctx, "plan_step", {"action": "wait", "step_id": demand, "on": "date", "until": day, "why": "x"})
    assert not product.ok and f"step #{demand} isn't a step of your ventures" in product.text
    done = call(ctx, "plan_step", {"action": "done", "why": "x"})
    assert not done.ok and "action must be one of wait" in done.text  # a venture cycle's plan_step waits only
    pinned = call(ctx, "plan_step", {"action": "wait", "on": "date", "until": day, "why": "after the freeze"})
    assert not pinned.ok and f"your owner pinned step #{step}" in pinned.text  # 0.40.3: the owner's pin holds
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), step, False, "Owner", now(agent))
    own = call(ctx, "plan_step", {"action": "wait", "on": "date", "until": day, "why": "after the freeze"})
    assert own.ok and own.text.startswith(f'Step #{step} ("')  # YOUR STEP, named
    assert own.text.endswith(f'") waits on {day} (2 of {plan.WAITS_A_DAY} waits today).')
    agent.clock.advance(days=5)
    lifted = keep(agent)
    assert f"Plan tree: step #{later} is ready again ({day} has come)." in lifted
    assert f"Plan tree: step #{step} is ready again ({day} has come)." in lifted


def brainstorm_steps(agent: Agent) -> list[dict[str, Any]]:
    return rows(
        agent, f"SELECT status, result FROM plan_nodes WHERE template = '{plan.BRAINSTORM_TEMPLATE}' ORDER BY id"
    )


@pytest.mark.exploring
def test_no_brainstorm_comes_within_a_week_of_one_whose_ideas_had_no_research(data_dir: Path) -> None:
    """Live, a brainstorm's six ideas were all parked within a day, and the funnel they left empty brought the next
    brainstorm at once: $1.76 in two days, nothing researched."""
    agent = exploring_agent(data_dir)
    for v in rows(agent, "SELECT id FROM ventures WHERE stage = 'idea'"):  # the owner parks the ideas: a thin funnel
        assert owner(agent).decide_venture(v["id"], {"action": "park"}, "Stefan").status == 200
    keep(agent)
    assert [s["status"] for s in brainstorm_steps(agent)] == ["open"]
    brainstormed = cycle(agent)
    with agent.db.transaction() as conn:  # a brainstorm's six ideas, each parked by a triage before the next keeper
        for n in range(6):
            made = ventures.create(
                conn,
                agent.scope(),
                title=f"Idea {n}",
                pitch="A guess.",
                stage="idea",
                now=now(agent),
                cycle_id=brainstormed,
            )
            ventures.update(conn, made, now(agent), stage="parked", parked_by="agent")
    today = agent.clock.today()
    ends = (today + timedelta(days=desk.BRAINSTORM_REST_DAYS)).isoformat()
    rest = (
        f"none of the last brainstorm's 6 ideas ({today.isoformat()}) went on past triage: the next brainstorm comes on"
        f" {ends}, or once one of them is being researched"
    )
    keep(agent)
    assert brainstorm_steps(agent) == [{"status": "dropped", "result": rest}]
    agent.clock.advance(days=desk.BRAINSTORM_REST_DAYS - 1)
    keep(agent)
    assert len(brainstorm_steps(agent)) == 1  # not yet
    agent.clock.advance(days=1)
    keep(agent)
    assert [s["status"] for s in brainstorm_steps(agent)] == ["dropped", "open"]


def test_the_brainstorms_rest_reads_its_last_batch_of_ideas() -> None:
    def idea(n: int, cycle_id: int, stage: str = "parked", researched: int = 0, by: str = "agent") -> dict[str, Any]:
        return {
            "id": n,
            "created_by": by,
            "created_cycle_id": cycle_id,
            "created_at": "2026-10-12T21:40:00Z",
            "stage": stage,
            "researched": researched,
        }

    batch = [idea(n, 3711) for n in range(1, 7)]
    later = "2026-10-14T09:00:00Z"
    said = desk.brainstorm_rests(batch, later)
    assert said == (
        "none of the last brainstorm's 6 ideas (2026-10-12) went on past triage: the next brainstorm comes on"
        " 2026-10-19, or once one of them is being researched"
    )
    assert desk.brainstorm_rests(batch, "2026-10-19T00:00:00Z") == ""  # a week on
    # 0.40.2 (analysis-0.40.1, 3.4): an idea researched and parked, or researched and still an idea, doesn't end it
    assert desk.brainstorm_rests([*batch[1:], idea(1, 3711, researched=1)], later) == said
    assert desk.brainstorm_rests([*batch[1:], idea(1, 3711, stage="idea", researched=1)], later) == said
    assert desk.brainstorm_rests([*batch[1:], idea(1, 3711, stage="researching")], later) == ""
    assert desk.brainstorm_rests([idea(1, 3711), idea(2, 3700)], later) == ""  # one idea a cycle: no brainstorm
    assert desk.brainstorm_rests([*batch, idea(9, 3720, by="owner"), idea(10, 3721)], later) == said  # not theirs
