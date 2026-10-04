"""0.19.4: the Projects and Inbox tabs, reworked. A project card showed its revenue and API cost, never its expenses
(Etsy's fees) or what it nets; and the Inbox showed a promise only on its message, which an older page may hold."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import obligations
from app.agent.fake_llm import FakeTransport
from app.economy.clock import to_iso
from tests.test_attribution import entry, project_with_a_sale
from tests.test_loop_shapes import run
from tests.test_money_goal import call
from tests.test_owner_api import post
from tests.test_roadmap import day, plan


def test_a_project_card_shows_its_expenses_and_what_it_nets(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent
    project_id = project_with_a_sale(agent, cost_micros=300_000)
    card = next(p for p in ingress_client.get("api/dashboard").json()["projects"] if p["id"] == project_id)
    assert (card["earned_usd"], card["expenses_usd"], card["net_usd"]) == (0, 0, pytest.approx(-0.3))

    sale = post(ingress_client, "api/ledger/revenue", entry("5.25", source="Etsy order 77", project_id=project_id))
    fee = post(ingress_client, "api/ledger/expense", entry("0.50", note="Etsy's fees", project_id=project_id))
    assert (sale.status_code, fee.status_code) == (201, 201)
    elsewhere = post(ingress_client, "api/ledger/expense", entry("2", note="Another project's", venture_id=2))
    assert elsewhere.status_code == 201
    card = next(p for p in ingress_client.get("api/dashboard").json()["projects"] if p["id"] == project_id)
    assert (card["earned_usd"], card["expenses_usd"]) == (5.25, 0.5)
    assert card["net_usd"] == pytest.approx(5.25 - 0.5 - 0.3)

    # A corrected fee counts what is left of it.
    fixed = post(ingress_client, f"api/ledger/{fee.json()['entry']['id']}/correct", entry("0.20", note="Refunded"))
    assert fixed.status_code == 201
    card = next(p for p in ingress_client.get("api/dashboard").json()["projects"] if p["id"] == project_id)
    assert card["expenses_usd"] == pytest.approx(0.3) and card["net_usd"] == pytest.approx(5.25 - 0.3 - 0.3)


def test_every_open_promise_reaches_the_dashboard_however_old_its_message(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    assert agent.dashboard()["promises_open"] == []
    made = call(agent, "message_owner", text="Drafts by Friday.", commits="Send you the three drafts", due=day(2))
    assert made.ok, made.text
    [promising] = [m for m in agent.dashboard()["inbox"] if m["text"] == "Drafts by Friday."]
    expected = [{"id": 1, "what": "Send you the three drafts", "due": day(2), "message_id": promising["id"]}]
    assert agent.dashboard()["promises_open"] == expected

    scope = agent.scope()
    with agent.db.transaction() as conn:  # newer messages push it off the Inbox's first page
        for _ in range(40):
            conn.execute(
                "INSERT INTO messages (mode, session, life_id, created_at, sender, cycle_id, text)"
                " VALUES (?, ?, ?, '2026-09-01T12:00:00Z', 'agent', 1, 'News.')",
                (scope.mode, scope.session, scope.life_id),
            )
    board = agent.dashboard()
    assert promising["id"] not in [m["id"] for m in board["inbox"]]
    assert board["promises_open"] == expected

    with agent.db.transaction() as conn:
        obligations.close_one(conn, 1, "Sent them", "agent", None, to_iso(agent.clock.now()))
    assert agent.dashboard()["promises_open"] == []
