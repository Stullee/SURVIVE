"""0.37.0: everything Ember does is a step of the plan, weighed like any other.

The owner: "couldn't we just integrate potential ventures as a sub goal into the plan and remove the ventures tab
completely?", and "shouldn't promises just move into the plan and be evaluated for weight?". In 0.36.0 one Explore
step stood for every venture, and a venture cycle's plan then picked one venture from READY, a list Ember's code
ranked by rules of its own; a promise to the owner, or their decision, came first by a rule of its own too. Now each
venture being explored is a node of the plan's Ventures, its next decision a step weighed like any (worth what its
case expects, an idea by its scores; the owner's wish and a park soon urgent), and a venture cycle is aimed at its
step's venture. A promise or decision of no product is a step of the Owner project, and every promise and decision is
weighed, worth at least PROMISE_WORTH and urgent as its day nears. The Ventures tab is the Plan tab's.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import obligations, plan, views, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.db import discover_migrations, migrate  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_fixes_0280 import lined, now  # noqa: E402
from tests.test_fixes_0340 import keep  # noqa: E402
from tests.test_fixes_0351 import promise, steered  # noqa: E402
from tests.test_fixes_0360 import NOW, explored, exploring_agent  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import DROPSHIPPING, ETSY, FIVERR  # noqa: E402

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
    assert migrate(db_file, discover_migrations(), backup_dir=tmp_path / "backups") == [91]
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
