"""0.12.0: a P&L per venture and a net runway. A venture showed what it cost and the revenue recorded for it, never its
refunds, its expenses (Etsy's fees) or what it nets; and the runway counted only the API spending, never the revenue
and expenses of the same days."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agent import review, ventures, views
from app.agent.fake_llm import FakeTransport
from app.config import Settings
from tests.economy_helpers import make_economy, owner
from tests.test_life import spend
from tests.test_loop_shapes import run
from tests.test_ventures import DROPSHIPPING, VENTURING, plan

LIVE = Settings(dry_run=False, starting_balance_usd=20, daily_spend_cap_usd=5, cycle_spend_cap_usd=5)


def test_the_net_runway_counts_revenue_and_expenses(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE)
    spend(economy, output_tokens=99_800, calls=2)  # 2.00 USD today: 18.00 left
    status = economy.status()
    assert status.runway.days == pytest.approx(18.00 / 2.00)
    assert status.runway.net_days == status.runway.days and status.runway.window_net_in == 0
    assert review.runway_text(status.runway) == "9.0 days"  # nothing else came in or went out
    owner(economy, "revenue", "1.50")
    owner(economy, "expense", "0.50", note="Etsy's fees")
    status = economy.status()
    assert status.runway.days == pytest.approx(19.00 / 2.00)  # the life's runway: the API spending
    assert status.runway.window_net_in == 1_000_000
    assert status.runway.net_days == pytest.approx(19.00 / 1.00)
    assert review.runway_text(status.runway) == "9.5 days at your API spending; net of revenue and expenses, 19.0 days"
    board = economy.dashboard()["agent"]
    assert (board["net_runway_days"], board["runway_net_in_usd"]) == (19.0, 1.0)
    owner(economy, "revenue", "5")
    status = economy.status()
    assert status.runway.net_days is None and status.runway.net_note == "Earning at least what it spends (last 7 days)"
    assert review.runway_text(status.runway).endswith("net of revenue and expenses, you earn at least what you spend")
    assert economy.sensors()["net_runway_days"] == 365
    assert economy.dashboard()["agent"]["net_runway_days"] is None


def test_a_ventures_pnl_has_its_refunds_expenses_and_net(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    sale = owner(agent.economy, "revenue", "10", venture_id=DROPSHIPPING, test_money=True)
    refund = agent.economy.correct(
        sale["entry"]["id"], {"amount": "2", "note": "Refunded", "idempotency_key": uuid.uuid4().hex}
    )
    assert refund.status == 201, refund.body
    owner(agent.economy, "expense", "1.50", venture_id=DROPSHIPPING, test_money=True)
    with agent.db.connection() as conn:
        m = ventures.money(conn, agent.scope())[DROPSHIPPING]
    assert (m.revenue, m.refunds, m.earned, m.expenses) == (10_000_000, 2_000_000, 8_000_000, 1_500_000)
    assert m.net == 8_000_000 - 1_500_000 - m.spent
    assert ventures.money_text(m) == (
        f"spent {ventures.usd(m.spent)} · earned $8.00 less $1.50 of expenses · net +{ventures.usd(m.net)}"
    )
    assert ventures.money_text(ventures.Money(spent=420_000)) == "spent $0.42 · earned $0.00"  # nothing booked
    assert ventures.money_text(ventures.Money(420_000, 0, 0, 100_000)).endswith("· net -$0.52")
    [card] = [v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING]
    assert (card["revenue_usd"], card["refunds_usd"], card["earned_usd"], card["expenses_usd"]) == (10.0, 2.0, 8.0, 1.5)
    assert card["net_usd"] == pytest.approx((m.net) / 1_000_000)
