"""0.13.0: knock-outs in code. What ruled a venture out was the agent's judgement, and a dropshipping case resting on
vendors' pages reached the owner. Now Ember's code checks each business case before it is proposed: cold outreach,
accounts Ember would create, cash beyond the owner's venture budget, a first sale later than half the net runway (all
of it while the owner invests, 0.42.6), a sale that loses money, and demand without an independent page. A knocked-out
venture isn't proposed until the case changes or the owner lifts that knock-out for it (reversibly, kept as history).

Also tests from 0.24.0 and 0.32.0 (formerly test_fixes_0240.py, test_fixes_0320.py), and 0.42.6."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import econ, evidence, knockouts, tools, ventures, views
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.agent_helpers import owner, rows, run
from tests.etsy_helpers import call, shop_context, started
from tests.tools.test_workspace_tools import calling
from tests.ventures import test_knockouts, test_venture_stages, test_ventures
from tests.ventures.test_ventures import CASE, DROPSHIPPING, JOURNAL, NUMBERS, SCORES, VENTURING, plan, tool_results

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


def found(agent: Agent, net_days: float | None = 100.0, whole_runway: bool = False) -> list[tuple[str, bool]]:
    with agent.db.connection() as conn:
        row = ventures.get(conn, agent.scope(), DROPSHIPPING)
        assert row is not None
        found = knockouts.check(conn, row, cash_eur=20.0, net_days=net_days, whole_runway=whole_runway)
        return [(k.rule, k.overridden) for k in found]


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
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


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
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


# --- From 0.24.0 (formerly test_fixes_0240.py) ---


def now(agent: Any) -> str:
    return to_iso(agent.clock.now())


# --- ventures: the knock-outs come with the numbers ------------------------------------------------------------------


def test_a_venture_case_says_at_once_what_knocks_it_out(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    ctx = shop_context(agent)
    ctx.venture, ctx.net_runway_days = True, 32.0
    with agent.db.transaction() as conn:
        vid = ventures.create(
            conn, agent.scope(), title="Worksheets on eduki", pitch="p.", stage="idea", now=now(agent)
        )
    made = call(ctx, "venture_case", {**NUMBERS, "venture_id": vid, "first_sale_days": 30})
    assert made.ok and "Knocked out by Ember's code (it isn't proposed while one stands): " in made.text
    assert "a first sale too late for the runway (its first sale in 30 days, half the runway is 16 days)" in made.text


# --- From 0.32.0 (formerly test_fixes_0320.py) ---


# --- 3. a refused proposal keeps the rest of its update, and names every gap and knock-out at once -------------------


def in_a_venture_cycle(agent: Any, tool: str, **args: Any) -> tools.Outcome:
    """A call of a venture cycle's (its tools take a venture's scores and case)."""
    return calling(agent, tool, venture=True, **args)


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_refused_proposal_keeps_its_case_and_names_every_gap_and_knock_out_at_once(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=test_ventures.VENTURING)
    vid = test_ventures.DROPSHIPPING
    # live (venture #23): its whole case, its scores before any research and the stage, in one call
    first = in_a_venture_cycle(
        agent, "venture_update", venture_id=vid, stage="proposed", **test_ventures.SCORES, **test_ventures.CASE
    )
    assert first.ok and first.summary == f"venture #{vid} updated (not all of it)", first.text
    assert "Not done (the rest is saved): scores come from research: research venture #3 first" in first.text
    assert (
        "Not done (the rest is saved): venture #3 can't be proposed yet: a business case needs 2 research calls for "
        "it that found something (it has 0); scores for revenue, doability, difficulty, risk, speed, cost; its numbers "
        "(venture_case). Ember's code knocks it out too: no independent source for its demand (it has no evidence "
        "yet: save an independent page's demand numbers with evidence)." in first.text
    )
    kept = test_ventures.venture(agent, vid)
    assert {name: kept[name] for name in ventures.CASE_FIELDS} == test_ventures.CASE  # live: thrown away each time
    assert (kept["stage"], kept["revenue"], kept["proposed_at"]) == ("idea", None, None)
    # what it still needs, and nothing it sent before: research, its numbers, an independent page, the scores
    for _ in range(ventures.RESEARCH_TO_PROPOSE):
        test_venture_stages.researched(agent, vid)
    test_knockouts.case(agent)
    test_knockouts.independent(agent)
    proposed = in_a_venture_cycle(agent, "venture_update", venture_id=vid, stage="proposed", **test_ventures.SCORES)
    assert proposed.ok and "Venture #3: idea → proposed." in proposed.text and "Not done" not in proposed.text
    assert test_ventures.venture(agent, vid)["stage"] == "proposed"
    # a refusal with nothing else in the call is a refusal, as before
    again = in_a_venture_cycle(agent, "venture_update", venture_id=vid, stage="live")
    assert not again.ok and "a venture goes live once your owner backed it" in again.text


# --- From 0.42.6: the owner who invests gives a first sale the whole runway ------------------------------------------


def test_the_owner_who_invests_gives_a_first_sale_the_whole_runway(data_dir: Path) -> None:
    """Live (analysis-0.42.3, 3.2), the invest stance explored with 24 days of runway, and half of it knocked out every
    case (a first sale takes at least 14 days): 9 venture cycles ($2.48) ended parked, none reached the owner."""
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    independent(agent)
    case(agent, first_sale_days=30)
    assert found(agent, net_days=40.0) == [("slow", False)]  # half the runway is 20 days
    assert found(agent, net_days=40.0, whole_runway=True) == []
    with agent.db.connection() as conn:
        row = ventures.get(conn, agent.scope(), DROPSHIPPING)
        assert row is not None
        [slow] = knockouts.check(conn, row, cash_eur=20.0, net_days=25.0, whole_runway=True)
    assert (slow.label, slow.why) == (
        "a first sale too late for the runway",
        "its first sale in 30 days, the whole runway is 25 days, as your owner invests",
    )


def test_a_venture_case_meets_the_whole_runway_while_the_owner_invests(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    ctx = shop_context(agent)  # the loop sets whole_runway from the owner's stance
    ctx.venture, ctx.net_runway_days, ctx.whole_runway = True, 40.0, True
    with agent.db.transaction() as conn:
        vid = ventures.create(conn, agent.scope(), title="Worksheets", pitch="p.", stage="idea", now=now(agent))
    made = call(ctx, "venture_case", {**NUMBERS, "venture_id": vid, "first_sale_days": 30})
    assert made.ok and "too late for the runway" not in made.text, made.text
    ctx.whole_runway = False
    made = call(ctx, "venture_case", {**NUMBERS, "venture_id": vid, "first_sale_days": 30})
    assert "a first sale too late for the runway (its first sale in 30 days, half the runway is 20 days)" in made.text


@pytest.mark.parametrize(("stance", "slow"), [("invest", False), ("steady", True)])
def test_the_ventures_tab_and_the_owners_back_follow_the_stance(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, stance: str, slow: bool
) -> None:
    agent, _ = run(
        data_dir, FakeTransport(), cycles=0, settings=VENTURING.model_copy(update={"spending_stance": stance})
    )
    independent(agent)
    case(agent, first_sale_days=30)
    evaluate = agent.economy.life.evaluate

    def forty_days() -> Any:  # a runway of 40 days: a first sale in 30 is within it, not within half of it
        status = evaluate()
        return dataclasses.replace(status, runway=dataclasses.replace(status.runway, net_days=40.0))

    monkeypatch.setattr(agent.economy.life, "evaluate", forty_days)
    shown = next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)
    assert ("slow" in [k["rule"] for k in shown["knockouts"]]) is slow
    assert ("too late for the runway" in (shown["backing_problem"] or "")) is slow
    backed = owner(agent).decide_venture(DROPSHIPPING, {"action": "back"}, "Stefan")  # an idea: a problem asks first
    assert backed.status == (409 if slow else 200), backed.body
