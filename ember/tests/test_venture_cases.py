"""0.13.0: a numeric business case. A venture's economics were prose, so two cases couldn't be compared, and a margin
that didn't survive the fees went unnoticed. Now the agent gives a venture's numbers (venture_case) and Ember's code
computes the rest the same way for every venture (agent/econ.py): Etsy's fees for Germany, what a sale keeps, the
break-even, the net a month at the low, likely and high estimate, and the expected net per API dollar and per hour of
the owner's. The newest case shows in FOCUS and on the Ventures tab, and a venture is proposed only with one."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import econ, tools, views
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls, request_kind
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_ventures import DROPSHIPPING, JOURNAL, NUMBERS, VENTURING, plan, tool_results


def test_the_numbers_are_worked_out_the_same_way_for_every_venture() -> None:
    # Etsy in Germany: USD 0.20 again at each sale, 6.5%, 4% + EUR 0.30, and 19% VAT on the listing and 6.5% fees
    assert econ.fees("etsy_digital", 3.0, 1.10) == pytest.approx(0.8684, abs=1e-4)
    assert econ.fees("other", 3.0, 1.10) == 0.0
    case = econ.Case("etsy_digital", 3.0, 0.0, 0.0, (2, 10, 30), 0.0, 2.0, 1, 3.0)
    got = econ.compute(case, 1.10)
    assert (got.fees_eur, got.net_eur) == (0.87, 2.13)
    assert got.break_even == 1.3  # the API spend (USD 3 a month) is its only fixed cost
    assert got.net == (1.54, 18.59, 61.22)
    # Swanson's rule (30/40/30) over six months, the first earning nothing: 5/6 of the expected month
    assert got.ev_eur == pytest.approx((0.3 * 1.54 + 0.4 * 18.59 + 0.3 * 61.22) * 5 / 6, abs=0.01)
    assert got.ev_per_hour == pytest.approx(got.ev_eur / 2, abs=0.01)
    assert got.ev_per_api_usd == pytest.approx(got.ev_eur * 1.10 / 3, abs=0.01)
    losing = econ.compute(econ.Case("other", 10.0, 12.0, 5.0, (0, 1, 2), 100.0, 0.0, 3, 0.0))
    assert losing.break_even is None and losing.ev_per_hour is None and losing.ev_per_api_usd is None
    assert losing.usd_per_eur == econ.DEFAULT_USD_PER_EUR  # the owner set no rate: the assumption
    assert "no break-even: a sale doesn't cover its own costs" in losing.text(
        econ.Case("other", 10.0, 12.0, 5.0, (0, 1, 2), 100.0, 0.0, 3, 0.0)
    )


def test_a_venture_case_is_saved_with_its_numbers_and_shown(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls(
                [
                    ("venture_case", {**NUMBERS, "sales_low": 9, "sales_mid": 6}),  # not rising
                    ("venture_case", {**NUMBERS, "price_eur": "0"}),
                    ("venture_case", {**NUMBERS, "owner_hours": "800"}),  # more hours than a month has
                    ("venture_case", {**NUMBERS, "venture_id": 99}),
                ]
            ),
            ToolCalls(
                [
                    ("venture_case", NUMBERS),
                    ("milestone_plan", {"milestones": [{"title": "A goal", "due": "2026-12-01"}]}),
                ]
            ),
            Reply("Done."),
            JOURNAL,
            plan(venture=DROPSHIPPING),  # the next cycle's FOCUS shows the numbers
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, cycles=2, settings=VENTURING)
    results = tool_results(agent, "venture_case")
    assert [r["status"] for r in results] == ["error"] * 4 + ["ok"]
    assert "sales must rise: sales_low (P10) <= sales_mid (P50) <= sales_high (P90)" in results[0]["result"]
    assert "price_eur must be above 0" in results[1]["result"]
    assert "owner_hours must be between 0 and 744" in results[2]["result"]
    assert "there is no venture #99" in results[3]["result"]
    assert results[4]["result"].startswith(
        f"Saved the numbers of venture #{DROPSHIPPING} as case #1 (at an assumed USD 1.10 per EUR: your owner set no "
        "exchange rate). A sale at EUR 25.00 keeps EUR 13.00 (fees EUR 0.00, cost EUR 12.00); break-even at 2.4 sales"
    )
    [planned] = tool_results(agent, "milestone_plan")  # 0.13.0: laying out the roadmap is an ordinary cycle's
    assert planned["status"] == "error" and "laying out the roadmap belong to ordinary cycles" in planned["result"]
    [saved] = rows(
        agent, "SELECT venture_id, channel, sales_low, sales_mid, sales_high, net_eur, break_even FROM venture_cases"
    )
    assert saved == {
        "venture_id": DROPSHIPPING,
        "channel": "other",
        "sales_low": 1,
        "sales_mid": 6,
        "sales_high": 20,
        "net_eur": 13.0,
        "break_even": 2.4,
    }
    brief = [r for r in fake.sent if request_kind(r) == "work"][-1]["messages"][0]["content"][0]["text"]
    assert "\nNumbers (case #1, 2026-09-01): A sale at EUR 25.00 keeps EUR 13.00" in brief
    card = next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)["numbers"]
    assert (card["id"], card["net_eur"], card["break_even"], card["sales"]) == (1, 13.0, 2.4, [1, 6, 20])
    others = [v["numbers"] for v in views.ventures_view(agent)["items"] if v["id"] != DROPSHIPPING]
    assert set(map(str, others)) == {"None"}
    refused = pytest.raises(sqlite3.IntegrityError, match="a case never changes")
    with refused, agent.db.transaction() as conn:
        conn.execute("UPDATE venture_cases SET price_eur = 99")


def test_venture_case_is_a_venture_cycles_tool() -> None:
    venture = {d["name"] for d in tools.definitions(etsy=True, venture=True)}
    ordinary = {d["name"] for d in tools.definitions(etsy=True, venture=False)}
    assert "venture_case" in venture and "venture_case" not in ordinary
    assert "milestone_plan" in ordinary and "milestone_plan" not in venture
    assert "milestone_update" in venture and "milestone_update" in ordinary
