"""0.13.0: the venture stages' last two rules, kept by Ember's code. Triage: an idea of the agent's that no one takes up
(researches) within TRIAGE_DAYS is parked; the owner's ideas wait for them. Live: a live venture that earns more than
it costs gets a decision point to scale it, and one that has sold nothing LIVE_DAYS after it went live is parked. A
venture already in its stage when a rule came counts from then (rules_from), so none is parked at once."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.agent import desk, stages, ventures
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.economy import burn
from app.economy.clock import to_iso
from tests.economy_helpers import owner as owner_entry
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_venture_stages import keep
from tests.test_ventures import DROPSHIPPING, ETSY, VENTURING, plan, venture


def agent_with_tree(data_dir: Path) -> Agent:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    return agent


def idea(agent: Agent, title: str, by: str = "agent") -> int:
    with agent.db.transaction() as conn:
        return ventures.create(
            conn,
            agent.scope(),
            title=title,
            pitch="A small test of an idea.",
            stage="idea",
            now=to_iso(agent.clock.now()),
            created_by=by,
        )


def test_an_idea_no_one_takes_up_is_parked_after_triage_days(data_dir: Path) -> None:
    agent = agent_with_tree(data_dir)
    mine, theirs = idea(agent, "Printable chore charts"), idea(agent, "Owner's idea", by="owner")
    due = agent.clock.today() + timedelta(days=stages.TRIAGE_DAYS)
    assert ventures.triage_date(venture(agent, mine)) == due and ventures.triage_date(venture(agent, theirs)) is None
    assert ventures.stage_rule(venture(agent, mine)) == (
        f"an idea: research it by {due.isoformat()} or Ember's code parks it (triage)"
    )
    agent.clock.advance(days=stages.TRIAGE_DAYS - 1)
    assert not any(f"#{mine}" in line for line in keep(agent))
    agent.clock.advance(days=1)
    parked = [line for line in keep(agent) if f"venture #{mine}" in line]
    assert parked and "no one took the idea up within 30 days (triage)" in parked[0]
    row = venture(agent, mine)
    assert (row["stage"], row["parked_by"]) == ("parked", "code")
    assert venture(agent, theirs)["stage"] == "idea"  # the owner's ideas wait for them


def test_an_idea_close_to_its_triage_date_is_urgent_on_the_desk(data_dir: Path) -> None:
    agent = agent_with_tree(data_dir)
    mine = idea(agent, "Wedding game printables")
    agent.clock.advance(days=stages.TRIAGE_DAYS - desk.URGENT_DAYS)
    with agent.db.connection() as conn:
        items = desk.ready(
            conn, agent.scope(), mode=burn.EXPLORE, today=agent.clock.today(), cash_eur=20, net_days=None
        )
    mine_item = next(i for i in items if i.venture_id == mine)
    assert mine_item.kind == "triage" and "parked by Ember's code on" in mine_item.text
    assert items.index(mine_item) <= 1  # among the urgent ones, before the plain triage items


def test_a_venture_already_waiting_counts_from_when_the_rule_came(data_dir: Path) -> None:
    agent = agent_with_tree(data_dir)
    mine = idea(agent, "Old idea")
    later = agent.clock.now() + timedelta(days=20)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE ventures SET rules_from = ? WHERE id = ?", (to_iso(later), mine))
    agent.clock.advance(days=stages.TRIAGE_DAYS + 1)
    keep(agent)
    assert venture(agent, mine)["stage"] == "idea"  # 30 days from the rule's start, not from the idea's


def live(agent: Agent, venture_id: int) -> None:
    """A venture live now (the seeded Etsy leg is researching until a listing of its is live)."""
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE ventures SET stage = 'live', updated_at = ? WHERE id = ?", (to_iso(agent.clock.now()), venture_id)
        )


def record_revenue(agent: Agent, venture_id: int, amount: str) -> None:
    owner_entry(agent.economy, "revenue", amount, venture_id=venture_id, test_money=True)


def test_a_live_venture_that_earns_more_than_it_costs_gets_a_decision_point_to_scale(data_dir: Path) -> None:
    agent = agent_with_tree(data_dir)
    live(agent, ETSY)
    record_revenue(agent, ETSY, "50")
    happened = keep(agent)
    assert any(
        line.startswith("Ember's code set milestone #") and f"scale venture #{ETSY}" in line for line in happened
    )
    scale = venture(agent, ETSY)["scale_milestone_id"]
    [row] = rows(agent, f"SELECT * FROM milestones WHERE id = {scale}")
    assert (row["kind"], row["created_by"], row["venture_id"], row["parent_id"]) == ("decision", "code", ETSY, None)
    assert row["title"].startswith("Scale it: Etsy digital products") and "earns more than it costs" in row["measure"]
    assert ventures.stage_rule(venture(agent, ETSY)) == f"it earns more than it costs: milestone #{scale} scales it"
    assert not any("scale venture" in line for line in keep(agent))  # once


def test_a_live_venture_that_sells_nothing_is_parked_after_live_days(data_dir: Path) -> None:
    agent = agent_with_tree(data_dir)
    live(agent, ETSY)
    rule = ventures.stage_rule(venture(agent, ETSY))
    assert rule.startswith("live: nothing sold by ") and "sets a milestone to scale it" in rule
    agent.clock.advance(days=stages.LIVE_DAYS)
    parked = [line for line in keep(agent) if f"venture #{ETSY}" in line]
    assert parked and f"nothing sold {stages.LIVE_DAYS} days after it went live" in parked[0]
    assert (venture(agent, ETSY)["stage"], venture(agent, ETSY)["parked_by"]) == ("parked", "code")
    assert venture(agent, DROPSHIPPING)["stage"] == "idea"  # the owner's idea waits for them
