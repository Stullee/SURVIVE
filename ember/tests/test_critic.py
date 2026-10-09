"""0.13.0: an independent critic. The agent argued its own business cases, so a case's weak point reached the owner only
if the agent itself saw it. Now, before the next plan after a venture is proposed, a separate call on the strategy model
reviews its newest case with its evidence: the fatal flaw, its own numbers for the same case, a verdict and what would
change its mind. Ember's code checks the answer, works out the economics of its numbers like the agent's and keeps it:
the owner sees it on the Ventures tab, the agent in FOCUS, and a venture ranks by the lower of the two expected nets."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.agent import critic, econ, prompts, ventures, views
from app.agent.fake_llm import FakeTransport, Reply, request_kind
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.test_agent import rows
from tests.test_knockouts import case, independent
from tests.test_loop_shapes import run
from tests.test_ventures import CASE, DROPSHIPPING, JOURNAL, SCORES, VENTURING, plan

ANSWER = {
    "fatal_flaw": "Buyers find the same printables for free: 4 sales a month, not 10.",
    "numbers": {
        "price_eur": 4.9,
        "unit_cost_eur": 0,
        "monthly_costs_eur": 0,
        "sales_low": 0,
        "sales_mid": 4,
        "sales_high": 12,
        "first_sale_months": 2,
    },
    "verdict": "test",
    "change_mind": "Three sales in the first two weeks of one listing.",
}
AGENTS = econ.Case("etsy_digital", 4.9, 0.0, 0.0, (2, 10, 30), 10.0, 2.0, 30, 3.0)  # test_knockouts.case's numbers
THEIRS = econ.Case(
    "etsy_digital", 4.9, 0.0, 0.0, (0, 4, 12), 10.0, 2.0, 61, 3.0
)  # its setup, hours and API: the agent's (0.15.0: its 2 months in days)


def proposed(agent: Agent) -> None:
    """DROPSHIPPING researched, with its business case, an independent page and its numbers, and proposed."""
    independent(agent)
    case(agent)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        for sources in (1, 2):
            ventures.add_research(conn, DROPSHIPPING, 1, None, "q", None, sources, 10, now)
        ventures.update(
            conn, DROPSHIPPING, now, stage="proposed", proposed_at=now, scores_by="research", **SCORES, **CASE
        )


def card(agent: Agent) -> dict:
    return next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_the_critic_reviews_a_proposed_case_before_the_plan(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)  # a first cycle, for the research rows to belong to
    proposed(agent)
    fake.script.extend([Reply(json.dumps(ANSWER)), plan(venture=DROPSHIPPING), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    assert [request_kind(r) for r in fake.sent][1:3] == ["critic", "plan"]
    request = fake.sent[1]
    assert request["model"] == VENTURING.planner_model  # the strategy model: the planner's, as none is set
    assert request["system"][-1]["text"] == prompts.CRITIC_RULES
    asked = request["messages"][0]["content"][0]["text"]
    assert f"Venture #{DROPSHIPPING}: " in asked and "\nFirst test: 10 products, 50 EUR of ads" in asked
    assert (
        "The agent's numbers (case #1): channel etsy_digital, price EUR 4.90, cost per sale EUR 0.00, fixed costs "
        "EUR 0.00 a month, sales a month 2 (P10) / 10 (P50) / 30 (P90), cash to start EUR 10.00" in asked
    )
    assert "Evidence: 1 claims (1 independent, 0 marketing, 0 unchecked); the newest:\n- [independent] sales" in asked
    got = econ.compute(THEIRS)
    [saved] = rows(
        agent,
        "SELECT venture_id, case_id, status, verdict, fatal_flaw, sales_mid, first_sale_months, net_eur, ev_eur"
        " FROM venture_critiques",
    )
    assert saved == {
        "venture_id": DROPSHIPPING,
        "case_id": 1,
        "status": "ok",
        "verdict": "test",
        "fatal_flaw": ANSWER["fatal_flaw"],
        "sales_mid": 4,
        "first_sale_months": 2,
        "net_eur": got.net_eur,
        "ev_eur": got.ev_eur,
    }
    assert rows(agent, "SELECT overhead FROM llm_calls WHERE purpose = 'critic'") == [{"overhead": 1}]
    [said] = rows(agent, "SELECT message FROM events WHERE message LIKE 'The critic%'")
    assert said["message"].startswith(
        f"The critic on venture #{DROPSHIPPING} (case #1): test; expected EUR {got.ev_eur:.0f} a month by its numbers."
    )
    brief = [r for r in fake.sent if request_kind(r) == "work"][-1]["messages"][0]["content"][0]["text"]
    # 0.15.0: the verdict and the flaw near the top of FOCUS, its numbers further down
    assert f"\nCritic (a separate call on case #1): test; fatal flaw: {ANSWER['fatal_flaw']}\n" in brief
    assert "\nCritic's numbers: a sale keeps EUR " in brief
    shown = card(agent)
    assert (shown["critique"]["verdict"], shown["critique"]["sales"]) == ("test", [0, 4, 12])
    assert shown["ranking_ev_eur"] == got.ev_eur < econ.compute(AGENTS).ev_eur  # the lower of the two
    fake.script.extend([plan(steps=[])])
    agent.run_cycle("schedule")  # the case has its critique: none again
    assert [request_kind(r) for r in fake.sent][-1:] == ["plan"] and len(fake.sent) == 6
    case(agent, sales=(1, 3, 8))  # a new case: a critique of its own
    assert (
        card(agent)["critique"] is None
        and card(agent)["ranking_ev_eur"]
        == econ.compute(econ.Case("etsy_digital", 4.9, 0.0, 0.0, (1, 3, 8), 10.0, 2.0, 30, 3.0)).ev_eur
    )
    fake.script.extend([Reply(json.dumps(ANSWER)), plan(steps=[])])
    agent.run_cycle("schedule")
    assert [request_kind(r) for r in fake.sent][-2:] == ["critic", "plan"]
    assert rows(agent, "SELECT case_id FROM venture_critiques") == [{"case_id": 1}, {"case_id": 2}]
    with pytest.raises(sqlite3.IntegrityError, match="a critique never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE venture_critiques SET verdict = 'back'")


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_failed_critique_is_kept_and_tried_again_once(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    proposed(agent)
    fake.script.extend(
        [
            Reply("I would test it first."),  # prose
            plan(steps=[]),
            Reply(json.dumps(ANSWER)[:40], "max_tokens"),
            plan(steps=[]),
            plan(steps=[]),  # critic.MAX_ATTEMPTS: the case goes without one
        ]
    )
    for _ in range(3):
        agent.run_cycle("schedule")
    assert [request_kind(r) for r in fake.sent][1:] == ["critic", "plan", "critic", "plan", "plan"]
    assert rows(agent, "SELECT status, note, verdict FROM venture_critiques") == [
        {"status": "failed", "note": "its answer wasn't usable", "verdict": None},
        {"status": "failed", "note": "it was cut off (max_tokens)", "verdict": None},
    ]
    warned = rows(agent, "SELECT message FROM events WHERE level = 'warning' AND message LIKE 'The critic%'")
    assert warned[0]["message"] == (
        f"The critic's review of venture #{DROPSHIPPING} (case #1) failed: its answer wasn't usable"
    )
    shown = card(agent)
    assert shown["critique"] == {"failed": 2, "gave_up": True}
    assert shown["ranking_ev_eur"] == econ.compute(AGENTS).ev_eur  # the agent's alone


def test_the_critics_answer_is_checked_by_code() -> None:
    parsed = critic.parse(ANSWER, AGENTS)
    assert parsed == (
        {"verdict": "test", "fatal_flaw": ANSWER["fatal_flaw"], "change_mind": ANSWER["change_mind"]},
        THEIRS,
    )
    numbers = ANSWER["numbers"]
    for wrong in (
        {**ANSWER, "verdict": "maybe"},
        {**ANSWER, "fatal_flaw": "  "},
        {**ANSWER, "numbers": {**numbers, "sales_low": 5}},  # not rising
        {**ANSWER, "numbers": {**numbers, "price_eur": 0}},
        {**ANSWER, "numbers": {**numbers, "first_sale_months": 30}},
        {**ANSWER, "numbers": {**numbers, "unit_cost_eur": "a lot"}},
        {**ANSWER, "numbers": {k: v for k, v in numbers.items() if k != "sales_mid"}},
        "Back it.",
    ):
        assert critic.parse(wrong, AGENTS) is None
    long = critic.parse({**ANSWER, "change_mind": "x " * 400}, AGENTS)
    assert long is not None and len(long[0]["change_mind"]) == critic.TEXT_CHARS
    assert critic.ranking_ev(None, None) is None
    assert critic.ranking_ev({"ev_eur": 12.0}, None) == 12.0
    assert critic.ranking_ev({"ev_eur": 12.0}, {"ev_eur": 3.5}) == 3.5
    assert critic.ranking_ev({"ev_eur": 2.0}, {"ev_eur": 3.5}) == 2.0


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_the_fake_models_critic_halves_the_agents_sales(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    proposed(agent)
    agent.run_cycle("schedule")  # the fake model answers everything itself now
    [saved] = rows(
        agent, "SELECT status, verdict, sales_low, sales_mid, sales_high, first_sale_months FROM venture_critiques"
    )
    assert saved == {
        "status": "ok",
        "verdict": "test",
        "sales_low": 1,
        "sales_mid": 5,
        "sales_high": 15,
        "first_sale_months": 2,
    }
