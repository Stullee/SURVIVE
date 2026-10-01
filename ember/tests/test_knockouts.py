"""0.13.0: knock-outs in code. What ruled a venture out was the agent's judgement, and a dropshipping case resting on
vendors' pages reached the owner. Now Ember's code checks each business case before it is proposed: cold outreach,
accounts Ember would create, cash beyond the owner's venture budget, a first sale later than half the net runway, a
sale that loses money, and demand without an independent page. A knocked-out venture isn't proposed until the case
changes or the owner lifts that knock-out for it (reversibly, kept as history)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import econ, evidence, knockouts, ventures, views
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_ventures import CASE, DROPSHIPPING, JOURNAL, SCORES, VENTURING, plan, tool_results

PAGE = "https://example.invalid/forum/sales"


def case(agent: Agent, **changes: object) -> None:
    numbers = {
        "channel": "etsy_digital",
        "price_eur": 4.9,
        "unit_cost_eur": 0.0,
        "monthly_costs_eur": 0.0,
        "sales": (2, 10, 30),
        "setup_eur": 10.0,
        "owner_hours": 2.0,
        "first_sale_days": 30,
        "api_usd": 3.0,
        **{k: v for k, v in changes.items() if k != "needs"},
    }
    made = econ.Case(**numbers)  # type: ignore[arg-type]
    with agent.db.transaction() as conn:
        now = to_iso(agent.clock.now())
        ventures.add_case(conn, DROPSHIPPING, None, made, econ.compute(made), now, str(changes.get("needs", "")))


def independent(agent: Agent) -> None:
    with agent.db.transaction() as conn:
        now = to_iso(agent.clock.now())
        evidence.record_sources(conn, agent.scope(), None, None, [PAGE], now)
        evidence.add(
            conn,
            agent.scope(),
            DROPSHIPPING,
            None,
            "Shops sell 200 a month.",
            "sales",
            200,
            200,
            "orders",
            "DE",
            PAGE,
            now,
        )


def found(agent: Agent, net_days: float | None = 100.0) -> list[tuple[str, bool]]:
    with agent.db.connection() as conn:
        row = ventures.get(conn, agent.scope(), DROPSHIPPING)
        assert row is not None
        return [(k.rule, k.overridden) for k in knockouts.check(conn, row, cash_eur=20.0, net_days=net_days)]


def test_each_knock_out_is_found_from_the_case(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    assert found(agent) == [("vendor_only", False)]  # no case, no evidence: its demand has no independent page
    independent(agent)
    case(agent)
    assert found(agent) == []
    case(agent, setup_eur=50.0, first_sale_days=61, unit_cost_eur=6.0, needs="cold_outreach,ember_accounts")
    assert [rule for rule, _ in found(agent, net_days=100.0)] == [
        "cold_outreach",
        "ember_accounts",
        "cash",
        "slow",  # 61 days, half the runway is 50 days
        "losing",
    ]
    assert "slow" not in [rule for rule, _ in found(agent, net_days=None)]  # it earns what it spends
    case(agent)
    with agent.db.transaction() as conn:
        ventures.update(conn, DROPSHIPPING, to_iso(agent.clock.now()), setup="Cold emails to shop owners in Berlin")
    assert found(agent) == [("cold_outreach", False)]  # the case's words plan it


def test_a_knocked_out_venture_is_proposed_only_once_the_owner_lifts_it(data_dir: Path) -> None:
    propose = ToolCalls([("venture_update", {"venture_id": DROPSHIPPING, "stage": "proposed"})])
    fake = FakeTransport(script=[plan(steps=[]), plan(venture=DROPSHIPPING), propose, Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=VENTURING)  # a first cycle, for the research rows to belong to
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # a researched venture with a complete case
        for sources in (1, 2):
            ventures.add_research(conn, DROPSHIPPING, 1, None, "q", None, sources, 10, now)
        ventures.update(conn, DROPSHIPPING, now, stage="researching", scores_by="research", **SCORES, **CASE)
    independent(agent)
    case(agent, setup_eur=80.0)  # more than the budget of EUR 20
    agent.run_cycle("schedule")
    [refused] = tool_results(agent, "venture_update")
    assert refused["status"] == "error"
    assert (
        "is knocked out by Ember's code: cash beyond the budget (EUR 80 to start, the budget is EUR 20)"
        in (refused["result"])
    )
    actions = owner(agent)
    assert actions.override_knockout(DROPSHIPPING, {"rule": "cash", "lift": True}, "Stefan").status == 200
    assert actions.override_knockout(DROPSHIPPING, {"rule": "cash", "lift": True}, "Stefan").status == 409
    assert actions.override_knockout(DROPSHIPPING, {"rule": "nope", "lift": True}, "Stefan").status == 422
    assert found(agent) == [("cash", True)]
    shown = next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)
    assert [(k["rule"], k["overridden"]) for k in shown["knockouts"]] == [("cash", True)]
    assert rows(agent, f"SELECT owner_action, owner_comment FROM ventures WHERE id = {DROPSHIPPING}") == [
        {"owner_action": "note", "owner_comment": "Lifted the knock-out 'cash beyond the budget'"}
    ]
    fake.script.extend([plan(venture=DROPSHIPPING), propose, Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    assert tool_results(agent, "venture_update")[-1]["status"] == "ok"
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {DROPSHIPPING}") == [{"stage": "proposed"}]
    assert actions.override_knockout(DROPSHIPPING, {"rule": "cash", "lift": False}, "Stefan").status == 200
    assert found(agent) == [("cash", False)]  # restored: a new proposal would be refused again
    with pytest.raises(sqlite3.IntegrityError, match="history cannot change"), agent.db.transaction() as conn:
        conn.execute("DELETE FROM knockout_overrides")
