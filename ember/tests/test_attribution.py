"""Revenue and expenses belong to the project and venture that earned or spent them (0.12.0, FIX NOW 1).

The ledger had a project_id that nothing wrote: every project and venture showed "earned $0.00", and no project could
ever succeed, because success needed revenue recorded for it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import tools, ventures
from app.agent.service import Agent
from app.economy.clock import to_iso
from app.integrations import etsy_publisher
from tests.test_agent import rows
from tests.test_owner_api import post

ETSY = 1  # the seeded tree's first venture: "Etsy digital products"
LISTING = 4_401_234_567


def entry(amount: str, **extra: Any) -> dict[str, Any]:
    return {"amount": amount, "idempotency_key": uuid.uuid4().hex, **extra}


def project_with_a_sale(agent: Agent, cost_micros: int = 0) -> int:
    """A project whose cycle cost ``cost_micros`` and asked for an Etsy listing that sold (returns the project's id)."""
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle_id = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros, session)"
            " VALUES (?, 'b', ?, 'running', 'schedule', 1, 0, ?)",
            (scope.life_id, now, scope.session),
        ).lastrowid
        project_id = conn.execute(
            "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
            " status, venture_id) VALUES (?, ?, ?, ?, ?, ?, 'Meal planner', 'Parents pay 5 EUR', 'active', ?)",
            (scope.mode, scope.session, scope.life_id, cycle_id, now, now, ETSY),
        ).lastrowid
        conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day, cost_micros)"
            " VALUES ('b', ?, 'work', 'scripted', 1, 'ok', ?, ?, ?)",
            (cycle_id, now, agent.clock.today().isoformat(), cost_micros),
        )
        conn.execute("UPDATE cycles SET project_id = ? WHERE id = ?", (project_id, cycle_id))
        conn.execute("UPDATE cycles SET status = 'completed', ended_at = ? WHERE id = ?", (now, cycle_id))
        approval_id = conn.execute(
            "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
            " payload_sha256, expected_cost, expected_benefit, executor, action)"
            " VALUES (?, ?, ?, ?, ?, 'sell', 'Etsy listing: planner', 'd', 'p', 's', 'c', 'b', 'etsy_listing', '{}')",
            (scope.mode, scope.session, scope.life_id, cycle_id, now),
        ).lastrowid
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, title, listing_id)"
            " VALUES (?, ?, ?, ?, ?, 'active', 'Planner', ?)",
            (scope.mode, scope.session, approval_id, now, now, LISTING),
        )
        conn.execute(
            "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency, items,"
            " synced_at) VALUES (?, ?, 77, ?, '4.90 EUR', 490, 'EUR', ?, ?)",
            (scope.mode, scope.session, now, json.dumps([{"listing_id": LISTING, "title": "Planner"}]), now),
        )
    return int(project_id)


def test_revenue_belongs_to_a_project_and_its_venture(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent
    project_id = project_with_a_sale(agent)
    with agent.db.connection() as conn:  # Record as revenue suggests the project whose listing sold
        order = etsy_publisher.orders_json(conn, agent.scope())[0]
    assert (order["project_id"], order["venture_id"]) == (project_id, ETSY)

    sale = entry("5.25", source="Etsy order 77", project_id=project_id)
    recorded = post(ingress_client, "api/ledger/revenue", sale)
    assert recorded.status_code == 201
    assert (recorded.json()["entry"]["project_id"], recorded.json()["entry"]["venture_id"]) == (project_id, ETSY)
    assert post(ingress_client, "api/ledger/revenue", sale).status_code == 200  # a retry is the same entry
    moved = post(ingress_client, "api/ledger/revenue", {**sale, "project_id": None, "venture_id": 2})
    assert moved.status_code == 409  # the same key for a different entry

    dashboard = ingress_client.get("api/dashboard").json()
    card = next(p for p in dashboard["projects"] if p["id"] == project_id)
    assert card["earned_usd"] == 5.25 and card["venture_id"] == ETSY
    assert {"id": ETSY, "title": "Etsy digital products", "stage": "researching"} in dashboard["venture_choices"]
    with agent.db.connection() as conn:
        assert ventures.money(conn, agent.scope())[ETSY].earned == 5_250_000

    # A correction belongs where the entry it corrects belongs.
    entry_id = recorded.json()["entry"]["id"]
    corrected = post(ingress_client, f"api/ledger/{entry_id}/correct", entry("1.25", note="Etsy refunded a part"))
    assert corrected.status_code == 201 and corrected.json()["entry"]["project_id"] == project_id
    card = next(p for p in ingress_client.get("api/dashboard").json()["projects"] if p["id"] == project_id)
    assert card["earned_usd"] == 4.0

    # An expense can belong to a venture alone.
    fee = post(ingress_client, "api/ledger/expense", entry("0.20", note="Listing fee", venture_id=2))
    assert fee.status_code == 201 and (fee.json()["entry"]["project_id"], fee.json()["entry"]["venture_id"]) == (
        None,
        2,
    )


@pytest.mark.parametrize(
    ("kind", "extra", "field", "message"),
    [
        ("revenue", {"source": "x", "project_id": 999}, "project_id", "there is no project #999"),
        ("revenue", {"source": "x", "venture_id": 999}, "venture_id", "there is no venture #999"),
        ("revenue", {"source": "x", "project_id": "1"}, "project_id", "project_id must be the number of a project"),
        ("revenue", {"source": "x", "project_id": True}, "project_id", "project_id must be the number of a project"),
        ("grant", {"venture_id": 1}, "venture_id", "only revenue and expenses belong to a project or venture"),
        ("adjustment", {"direction": "add", "note": "n", "project_id": 1}, "project_id", "only revenue and expenses"),
    ],
)
def test_attribution_is_checked(ingress_client: TestClient, kind: str, extra: dict, field: str, message: str) -> None:
    refused = post(ingress_client, f"api/ledger/{kind}", entry("1", **extra))
    assert refused.status_code == 422 and refused.json()["field"] == field and message in refused.json()["error"]


def test_a_projects_venture_comes_along_and_must_agree(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent
    project_id = project_with_a_sale(agent)
    other = post(ingress_client, "api/ledger/revenue", entry("1", source="x", project_id=project_id, venture_id=2))
    assert other.status_code == 422 and other.json()["error"] == f"project #{project_id} belongs to venture #{ETSY}"
    both = post(ingress_client, "api/ledger/revenue", entry("1", source="x", project_id=project_id, venture_id=ETSY))
    assert both.status_code == 201


def test_only_revenue_and_expenses_can_carry_attribution_in_the_database(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent
    with pytest.raises(sqlite3.IntegrityError, match="only revenue and expenses"), agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO ledger (ts, occurred_on, type, amount_micros, created_by, idempotency_key, venture_id)"
            " VALUES ('t', '2026-09-29', 'owner_grant', 1, 'owner', 'k', 1)"
        )


def test_a_project_succeeds_only_when_it_earned_more_than_it_cost(ingress_client: TestClient) -> None:
    """0.11.1: project_update(succeeded) always failed, because no revenue could name its project."""
    agent = ingress_client.app.state.ember.agent
    project_id = project_with_a_sale(agent, cost_micros=500_000)  # its cycle cost $0.50
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=0,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )

    def succeed() -> str:
        with agent.db.transaction() as conn:
            return tools.HANDLERS["project_update"](ctx, {"project_id": project_id, "status": "succeeded"}, conn).text

    with pytest.raises(tools.ToolError, match="once your owner has recorded revenue for it"):
        succeed()
    assert (
        post(ingress_client, "api/ledger/revenue", entry("0.60", source="s", project_id=project_id)).status_code == 201
    )
    assert (
        post(ingress_client, "api/ledger/expense", entry("0.20", note="fee", project_id=project_id)).status_code == 201
    )
    with pytest.raises(tools.ToolError, match=r"earned \$0\.40 \(revenue less its expenses\) and cost \$0\.50"):
        succeed()
    assert (
        post(ingress_client, "api/ledger/revenue", entry("0.20", source="s", project_id=project_id)).status_code == 201
    )
    assert succeed() == f"Project #{project_id}: active → succeeded."
    assert rows(agent, f"SELECT status FROM projects WHERE id = {project_id}")[0]["status"] == "succeeded"
