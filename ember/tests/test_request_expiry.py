"""0.12.0: the agent's requests to its owner have a cap per type, can be withdrawn, and expire when nobody decides
them (one cap of 10 for all of them let waiting listings block an email reply, and requests waited for ever)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent import store
from tests.test_agent import make_agent, plan, rows, text, tools
from tests.test_owner_loop import owner
from tests.test_owner_news import first_text, section


def ask(kind: str, n: int) -> tuple[str, dict[str, Any]]:
    fields = {"title": f"{kind} {n}", "description": "d", "expected_cost": "none", "expected_benefit": "b"}
    return ("request_approval", {"type": kind, "payload": f"{kind} payload {n}", **fields})


def results(agent: Any, tool: str) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT status, result FROM tool_calls WHERE tool = '{tool}' ORDER BY id")


def test_each_type_of_request_has_its_own_cap(data_dir: Path) -> None:
    assert store.PENDING_CAPS["publish"] == 3
    first = [plan(), tools(ask("publish", 1), ask("publish", 2), ask("publish", 3)), text("done"), text("reflected")]
    second = [plan(), tools(ask("publish", 4), ask("other", 5)), text("done"), text("reflected")]
    agent, _ = make_agent(data_dir, [*first, *second])
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    asked = results(agent, "request_approval")
    assert [r["status"] for r in asked] == ["ok", "ok", "ok", "error", "ok"]
    assert asked[3]["result"] == (
        "Error: 3 publish requests are already waiting for your owner: withdraw one that is outdated "
        "(withdraw_request), or wait for their decision."
    )


def test_the_agent_withdraws_a_waiting_request(data_dir: Path) -> None:
    withdraw = [
        ("withdraw_request", {"request_id": 1, "reason": "A better one replaces it."}),
        ("withdraw_request", {"request_id": 1, "reason": "Again."}),
        ("withdraw_request", {"request_id": 99, "reason": "Unknown."}),
    ]
    agent, _ = make_agent(data_dir, [plan(), tools(ask("other", 1)), tools(*withdraw), text("done"), text("reflected")])
    agent.run_cycle("schedule")
    done = results(agent, "withdraw_request")
    assert [r["status"] for r in done] == ["ok", "error", "error"]
    assert done[0]["result"] == "Request #1 is withdrawn: it left your owner's queue."
    assert "request #1 is withdrawn already; only a waiting one" in done[1]["result"]
    assert "there is no request #99" in done[2]["result"]
    row = rows(agent, "SELECT status, decision_comment, seen_cycle_id, version FROM approvals")[0]
    assert row == {
        "status": "withdrawn",
        "decision_comment": "A better one replaces it.",
        "seen_cycle_id": 1,
        "version": 1,
    }
    decided = owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, "Stefan")
    assert decided.status == 409  # the owner's click on the withdrawn request changes nothing
    shown = agent.dashboard()["approvals"][0]
    assert shown["status"] == "withdrawn" and shown["expires_at"] is None


def test_a_request_nobody_decides_expires_and_the_agent_hears_it(data_dir: Path) -> None:
    first = [plan(), tools(ask("contact", 1), ask("other", 2)), text("done"), text("reflected")]
    agent, transport = make_agent(data_dir, [*first, plan(steps=[]), plan(steps=[])])
    agent.run_cycle("schedule")
    shown = {a["id"]: a for a in agent.dashboard()["approvals"]}
    made = shown[1]["created_at"]
    assert shown[1]["expires_at"] > made and shown[2]["expires_at"] > shown[1]["expires_at"]  # 7 and 30 days
    agent.run_cycle("schedule")
    waiting = section(first_text(transport.sent[-1]), "WAITING FOR YOUR OWNER") or ""
    assert f"#1 contact: contact 1 (expires {shown[1]['expires_at'][:10]})" in waiting
    agent.clock.advance(days=8)
    agent.run_cycle("schedule")
    assert [r["status"] for r in rows(agent, "SELECT status FROM approvals ORDER BY id")] == ["expired", "pending"]
    planned = first_text(transport.sent[-1])
    assert 'Request #1 (contact) "contact 1": expired after 7 days without a decision: ask again if it still' in planned
    assert "#1 contact" not in (section(planned, "WAITING FOR YOUR OWNER") or "")
    events = [e["message"] for e in agent.db.recent_events(limit=20)]
    assert "Request #1 expired: no decision in 7 days" in events
    assert agent.dashboard()["approvals"][1]["status"] == "expired"
