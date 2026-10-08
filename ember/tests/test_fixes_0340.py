"""0.34.0: the plan tree, in the shadow (Release 2a).

On 2026-10-07 twelve cycles went to eight things: READY's ranking, the obligations, the spending shares and the daily
review each decided part of what Ember worked on, and the Haushaltsbuch book promised three times got no cycle. One
tree under the owner's goal now holds the plan: projects (platforms), their products (the lines), each product's
stages and small steps with checks Ember's code reads. In 0.34.0 the tree is laid out and kept every cycle and records
the step it would have picked next to what READY picked; Ember's cycles don't change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import obligations, plan, store, templates  # noqa: E402
from app.agent.fake_llm import Reply, ToolCalls  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now, take  # noqa: E402
from tests.test_owner_api import post  # noqa: E402
from tests.test_ventures import JOURNAL  # noqa: E402

ALL = {"pinterest": True, "bluesky": True, "blog": True}
BOOK = ("Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch für 2027 mit Monatsübersicht")
TRACKER = ("Bewerbungs-Tracker", "Eine Vorlage für Bewerbungen und Absagen")


def project(agent: Agent, title: str, hypothesis: str = "Someone pays 5 EUR for it", status: str = "active") -> int:
    with agent.db.transaction() as conn:
        return store.create_project(
            conn,
            agent.scope(),
            cycle_id=1,
            title=title,
            hypothesis=hypothesis,
            next_step="make it",
            status=status,
            now=now(agent),
        )


def keep(agent: Agent, channels: dict[str, bool] = ALL) -> list[str]:
    with agent.db.transaction() as conn:
        return plan.keep(conn, agent.scope(), now(agent), agent.clock.today(), channels)


def tree(agent: Agent) -> list[dict[str, Any]]:
    return rows(
        agent,
        "SELECT id, parent_id, level, platform, project_id, stage, kind, title, status, waiting, template, audience"
        " FROM plan_nodes ORDER BY id",
    )


def steps_of(agent: Agent, line: int) -> dict[str, str]:
    return {n["title"]: n["status"] for n in tree(agent) if n["project_id"] == line and n["level"] == "step"}


def request(agent: Agent, line: int, executor: str) -> int:
    with agent.db.transaction() as conn:
        return store.insert_approval(
            conn,
            agent.scope(),
            1,
            now(agent),
            project_id=line,
            type="publish",
            title="A request",
            description="for the owner",
            payload="{}",
            expected_cost="nothing",
            expected_benefit="sales",
            executor=executor,
            action="{}",
        )


# --- the tree is laid out ---


def test_every_open_line_is_a_product_of_its_platform_laid_out_from_its_template(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    poster = project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    tracker = project(agent, *TRACKER)
    sticker = project(agent, "Stickers", status="idea")
    said = keep(agent)
    assert any(f"line #{book} laid out" in s for s in said)
    nodes = tree(agent)
    assert {n["platform"] for n in nodes if n["level"] == "project"} == {"kdp", "printify", "etsy", "other"}
    products = {n["project_id"]: n for n in nodes if n["level"] == "product"}
    assert products[book]["template"] == "kdp_book@1" and products[poster]["template"] == "printify_pod@1"
    assert products[tracker]["template"] == "etsy_digital@1" and products[sticker]["template"] == "generic@1"
    assert [n["stage"] for n in nodes if n["level"] == "stage" and n["project_id"] == book] == list(templates.STAGES)
    steps = steps_of(agent, book)
    assert {"Propose the book", "You publish it at KDP and add its Amazon link"} <= set(steps)
    blog = next(n for n in nodes if n["project_id"] == book and n["title"].startswith("A German blog post"))
    assert blog["waiting"] == "upgrade"  # pins, posts and the blog can't link Amazon yet
    # German words and no English ones: the tracker speaks German, so no Bluesky step (Bluesky is English only)
    assert products[tracker]["audience"] == "de"
    assert not [t for t in steps_of(agent, tracker) if "Bluesky" in t]
    assert not [s for s in keep(agent) if "laid out" in s]  # once


def test_a_stage_closes_when_its_check_passes_and_a_later_stage_done_closes_the_ones_before(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    assert steps_of(agent, book)["Write the demand note: Amazon searches, competing books"] == "open"
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO demand_notes (mode, session, cycle_id, project_id, created_at, keywords, demand, source)"
            " VALUES (?, ?, 1, ?, ?, 'haushaltsbuch 2027', 'Amazon lists 40 planners for 2027', 'amazon.de')",
            (agent.scope().mode, agent.scope().session, book, now(agent)),
        )
    assert any("research stage is done" in s for s in keep(agent))
    request(agent, book, "kdp_package")  # the book proposed: create is done (its steps moot), release waits
    keep(agent)
    steps = steps_of(agent, book)
    assert steps["Make the front picture"] == "done" and steps["Propose the book"] == "done"
    assert steps["You publish it at KDP and add its Amazon link"] == "open"
    picture = f"SELECT result FROM plan_nodes WHERE project_id = {book} AND title = 'Make the front picture'"
    assert rows(agent, picture) == [{"result": "Ember's code: the stage is done"}]
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    publish = next(c for c in found if c.step.title.startswith("You publish"))
    assert publish.waiting == "owner" and publish.step.blocked  # the owner's step: it waits on them, never ages


def test_a_promise_naming_a_product_is_its_step_and_the_step_in_front_carries_it_to_the_cycle(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    with agent.db.transaction() as conn:
        message = store.insert_message(conn, agent.scope(), 1, "The book comes today.", now(agent))
        promise = obligations.promise(
            conn,
            agent.scope(),
            1,
            message,
            "Propose the Haushaltsbuch 2027 KDP book",
            agent.clock.today().isoformat(),
            now(agent),
            project_id=book,
        )
    assert any(f"promise #{promise} is a step of product" in s for s in keep(agent))
    [step] = rows(agent, f"SELECT id, kind, status, due FROM plan_nodes WHERE obligation_id = {promise}")
    assert step["kind"] == "promise" and step["due"] == agent.clock.today().isoformat()
    with agent.db.connection() as conn:
        pick, _ = plan.choose(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    assert pick.decided == "promise" and pick.step is not None and pick.step.product == book
    assert pick.parts is not None and pick.parts.carried_from == step["id"]  # the demand note carries the promise
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE obligations SET status = 'closed', closed_at = ?, closed_by = 'agent' WHERE id = ?",
            (now(agent), promise),
        )
    keep(agent)
    assert rows(agent, f"SELECT status FROM plan_nodes WHERE obligation_id = {promise}") == [{"status": "done"}]
    with agent.db.transaction() as conn:
        store.update_project(conn, tracker, now(agent), status="abandoned")
    assert any(f"closed with line #{tracker}" in s for s in keep(agent))
    assert {r["status"] for r in rows(agent, f"SELECT status FROM plan_nodes WHERE project_id = {tracker}")} == {
        "dropped"
    }


def test_a_live_products_recurring_marketing_comes_once_its_launch_is_done(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    poster = project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    keep(agent)
    with agent.db.transaction() as conn:
        stages = plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'stage'", (poster,))
        for stage in stages:
            if stage["stage"] != "maintain":
                for step in plan.nodes(conn, agent.scope(), "parent_id = ?", (stage["id"],)):
                    plan._close(conn, step["id"], now(agent), "done", "test", by="owner")
                plan._close(conn, stage["id"], now(agent), "done", "test", by="owner")
    no_bluesky = {"pinterest": True, "bluesky": False, "blog": True}
    said = keep(agent, no_bluesky)
    recurring = [n for n in tree(agent) if n["project_id"] == poster and (n["template"] or "").startswith("recurring/")]
    assert [n["title"] for n in recurring] == ["This week's pin for Bauhaus posters"]  # Bluesky is off; no blog (en)
    assert any("pinterest step for this period" in s for s in said)
    assert not [s for s in keep(agent, no_bluesky) if "for this period" in s]  # one open step per duty


# --- the pick ---


def test_each_cycle_records_the_trees_pick(data_dir: Path) -> None:
    agent, fake = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist, after an idle cycle
    fake.script.extend([take(3), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL])
    assert agent.run_cycle("schedule").status == "completed"
    picks = rows(agent, "SELECT cycle_id, kind, line, decided, node_id, product, ranked FROM plan_picks ORDER BY id")
    assert [(p["cycle_id"], p["decided"]) for p in picks][0] == (1, "none")  # no line yet in the first cycle
    last = picks[-1]
    assert (last["cycle_id"], last["kind"]) == (2, "ordinary") and last["line"] in (1, 2, 3)
    assert last["decided"] in ("weight", "margin") and last["node_id"] is not None and last["product"] is not None
    assert rows(agent, "SELECT COUNT(*) AS n FROM plan_nodes WHERE level = 'product'") == [{"n": 3}]
    # 0.35.0: the tree's line is the cycle's, whatever the plan named
    assert rows(agent, "SELECT project_id FROM cycles WHERE id = 2") == [{"project_id": last["line"]}]
    failed = [r for r in rows(agent, "SELECT level, message FROM events") if "plan tree" in r["message"].lower()]
    assert [r for r in failed if r["level"] != "info"] == []


# --- the owner's Plan tab ---


def test_the_plan_tab_shows_the_tree_and_takes_a_pin_and_a_worth(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    cycle(agent)
    book = project(agent, *BOOK)
    keep(agent)
    shown = ingress_client.get("api/plan").json()
    assert [p["platform"] for p in shown["projects"]] == ["kdp"]
    [product] = shown["projects"][0]["products"]
    assert product["line"] == book and product["template"] == "KDP book" and product["stage"] == "research"
    assert [s["stage"] for s in product["stages"]] == list(templates.STAGES)
    demand = product["stages"][0]["steps"][0]
    assert demand["status"] == "open" and demand["waiting"] is None and "worth" in demand["why"]
    assert shown["now"]["id"] == demand["id"] and shown["now"]["decided"] == "weight"
    assert post(ingress_client, f"api/plan/steps/{demand['id']}/pin", {"pinned": True}).json() == {
        "id": demand["id"],
        "pinned": True,
    }
    again = ingress_client.get("api/plan").json()
    assert again["now"]["decided"] == "pin" and again["stamp"] != shown["stamp"]
    stage_id = product["stages"][0]["id"]
    assert post(ingress_client, f"api/plan/steps/{stage_id}/pin", {"pinned": True}).status_code == 422
    assert post(ingress_client, "api/plan/steps/9999/pin", {"pinned": True}).status_code == 404
    assert post(ingress_client, f"api/plan/products/{product['id']}/worth", {"worth": 7}).json()["worth"] == 7
    assert ingress_client.get("api/plan").json()["projects"][0]["products"][0]["worth"] == 7
    wrong = post(ingress_client, f"api/plan/products/{product['id']}/worth", {"worth": 40})
    assert wrong.status_code == 422 and wrong.json()["field"] == "worth"
    assert post(ingress_client, f"api/plan/products/{product['id']}/worth", {"worth": None}).json()["worth"] is None
    words = rows(agent, "SELECT action, value FROM plan_words ORDER BY id")
    assert [w["action"] for w in words] == ["pin", "worth", "clear_worth"]
    html = ingress_client.get("/").text
    assert 'id="tab-plan"' in html and 'id="panel-plan"' in html
    report = ingress_client.get("api/diagnostics").text
    assert "-- plan_nodes" in report and "-- plan_words" in report
    assert "-- plan tree: the ranking now (the next cycle takes: pin)" in report  # the demand note is still pinned
    assert ingress_client.get("api/dashboard").json()["plan_stamp"] == ingress_client.get("api/plan").json()["stamp"]
