"""0.13.0: the decision desk. A venture cycle's planner chose freely what to look at, so research went where the plan's
mood took it and the brainstorm never ran live. Now Ember's code ranks the ventures' next decisions (READY: a backed
venture without a project, the owner's wishes, deadlines, the critic's flags, the other appraisals by expected net,
triage of ideas, a brainstorm when the funnel is thin), and each venture plan takes one or says why it takes none.
Ember's code checks the pick, aims the cycle at its venture and keeps each pick with the list it came from. 0.37.0:
each venture's next decision is a step of the plan tree, weighed like any step (the owner's wish and a park soon make
it urgent); a venture cycle takes it as YOUR STEP, and Ember's code aims the cycle at its venture."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import critic, desk, econ, ventures, weights
from app.agent import plan as plan_tree
from app.agent.fake_llm import FakeTransport, Reply, request_kind
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.test_agent import rows
from tests.test_critic import proposed
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_ventures import (
    COMPANION,
    DROPSHIPPING,
    ETSY,
    JOURNAL,
    PINTEREST,
    PRINT,
    RECRUITING,
    VENTURING,
    WEBSITE,
    aim,
    plan,
)


def needs(agent: Agent) -> dict[int, desk.Item]:
    """Each venture's next decision as Ember's code names it (desk.item; READY ranked them until 0.36.0)."""
    with agent.db.connection() as conn:
        found = ventures.all_ventures(conn, agent.scope())
        room = sum(1 for v in found if v["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE
        items = {int(v["id"]): desk.item(conn, v, today=agent.clock.today(), cash_eur=20.0, room=room) for v in found}
    return {vid: item for vid, item in items.items() if item is not None}


def kinds(agent: Agent) -> dict[int, tuple[str, str]]:
    return {vid: (item.kind, item.tier) for vid, item in needs(agent).items()}


def steps(agent: Agent) -> dict[int | None, plan_tree.Candidate]:
    """0.37.0: each venture's step as the plan weighs it (None: the brainstorm), the tree kept first."""
    now, today = to_iso(agent.clock.now()), agent.clock.today()
    with agent.db.transaction() as conn:
        plan_tree.keep(conn, agent.scope(), now, today, {})
        _, found = plan_tree.choose(conn, agent.scope(), now, today, {}, True)
        return {plan_tree.venture_of(conn, agent.scope(), c.step.id): c for c in found if c.stage == "venture"}


@pytest.mark.exploring  # 0.37.0: each venture's next decision is a step of the plan, weighed (READY's order until then)
def test_each_ventures_next_decision_is_named_and_weighed(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)  # the tree is planted
    # Etsy is being researched (no listing is live yet) and six ideas wait: each its research or its triage
    ideas = (PINTEREST, DROPSHIPPING, PRINT, WEBSITE, COMPANION, RECRUITING)
    assert kinds(agent) == {ETSY: ("appraise", ""), **{vid: ("triage", "") for vid in ideas}}
    now = to_iso(agent.clock.now())
    guessed = {"revenue": 5, "doability": 5, "difficulty": 1, "risk": 1, "speed": 5, "cost": 1}
    with agent.db.transaction() as conn:
        ventures.update(conn, COMPANION, now, scores_by="brainstorm", **guessed)  # the heaviest idea
        ventures.update(conn, DROPSHIPPING, now, stage="researching")
        ventures.add_research(conn, DROPSHIPPING, 1, None, "q", None, 1, 460_000, now)  # $0.14 of $0.60 left
    actions = owner(agent)
    assert actions.decide_venture(RECRUITING, {"action": "research"}, "Stefan").status == 200  # the owner's wish
    assert (
        actions.decide_venture(PRINT, {"action": "back", "confirm": True}, "Stefan").status == 200
    )  # backed, no project yet
    assert kinds(agent) == {
        ETSY: ("appraise", ""),
        DROPSHIPPING: ("appraise", "urgent"),  # its research budget is nearly used
        RECRUITING: ("appraise", "wish"),  # the owner's wish
        PINTEREST: ("triage", ""),
        WEBSITE: ("triage", ""),
        COMPANION: ("triage", ""),
    }  # 0.19.3: the backed venture is its project's work, in ordinary cycles: no next decision here
    items = needs(agent)
    assert items[DROPSHIPPING].text.startswith(
        "Dropshipping store · needs 2 research calls for it that found something (it has 1)"
    )
    assert "research budget: $0.14 of $0.60 left" in items[DROPSHIPPING].text
    assert items[RECRUITING].text.startswith("your owner's wish: Recruiting and headhunting service · needs")
    # The plan weighs them: the owner's wish and a park soon are urgent, the backed venture's node is done, and a
    # brainstorm is due (fewer than 5 ideas wait, and none has its numbers)
    weighed = steps(agent)
    assert weighed[RECRUITING].step.urgency == weights.ASKED
    assert weighed[DROPSHIPPING].step.urgency == weights.PARK_SOON
    assert weighed[ETSY].step.urgency == 0 and PRINT not in weighed and None in weighed
    # the heaviest idea is researched first: an idea is worth half to one and a half of an unknown venture's, by its
    # scores (an unscored one's as middling)
    assert weighed[COMPANION].step.worth == weights.EXPLORE_WORTH * 1.5
    assert weighed[PINTEREST].step.worth < weighed[COMPANION].step.worth
    for vid in (PINTEREST, WEBSITE, COMPANION):  # the ideas go
        assert actions.decide_venture(vid, {"action": "park"}, "Stefan").status == 200
    weighed = steps(agent)
    assert None in weighed and not {PINTEREST, WEBSITE, COMPANION} & set(weighed)


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_proposed_venture_the_critic_doubts_is_to_be_answered(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    proposed(agent)  # DROPSHIPPING, with its numbers
    assert DROPSHIPPING not in needs(agent)  # no critique yet: its step is the owner's decision
    assert steps(agent)[DROPSHIPPING].waiting == "owner"
    theirs = econ.Case("etsy_digital", 4.9, 0.0, 0.0, (0, 1, 3), 10.0, 2.0, 91, 3.0)
    with agent.db.transaction() as conn:
        texts = {"verdict": "park", "fatal_flaw": "Nobody searches for it.", "change_mind": "Ten sales a month."}
        critic.add(conn, DROPSHIPPING, 1, None, texts, theirs, econ.compute(theirs), to_iso(agent.clock.now()))
    answer = needs(agent)[DROPSHIPPING]
    assert answer.kind == "answer" and (
        "the critic says park (case #1): Nobody searches for it. · answer it with evidence or new numbers"
        in answer.text
    )
    step = steps(agent)[DROPSHIPPING]  # 0.37.0: its decision's step moves on: Ember answers the critic
    assert step.waiting is None and step.step.title == plan_tree.VENTURE_STEPS["answer"][2]


@pytest.mark.exploring  # 0.37.0: each venture's next decision is a step of the plan (READY until 0.36.0)
def test_a_venture_cycle_takes_its_venture_step_from_the_plan(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)  # the heaviest venture step: Etsy's appraisal, the oldest
    planner = [r for r in fake.sent if request_kind(r) == "plan"][-1]
    asked = planner["messages"][0]["content"][0]["text"]
    assert (
        f'of venture #{ETSY} "Etsy digital products" (researching): Research its next question; then its business'
        " case, or park it\nNow: Etsy digital products · " in asked
    )
    assert "\n== YOUR STEP ==\nStep #" in asked and "\n== READY ==" not in asked
    assert "ready" not in planner["output_config"]["format"]["schema"]["properties"]  # no READY item to name
    assert rows(agent, "SELECT venture_id FROM cycles WHERE id = 1") == [{"venture_id": ETSY}]
    aim(DROPSHIPPING)(agent)  # the owner's Explore next on Dropshipping: its triage, whatever the plan names
    fake.script.extend([plan(steps=["Triage it"], venture=ETSY), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    brief = [r for r in fake.sent if request_kind(r) == "work"][-1]["messages"][0]["content"][0]["text"]
    assert "\nYour step: #" in brief and "Triage it: research it, or park it with why" in brief
    assert f"Focus venture: #{DROPSHIPPING} Dropshipping store" in brief
    assert rows(agent, "SELECT venture_id FROM cycles WHERE id = 2") == [{"venture_id": DROPSHIPPING}]
    assert rows(agent, "SELECT cycle_id, kind, decided FROM plan_picks ORDER BY id") == [
        {"cycle_id": 1, "kind": "venture", "decided": "weight"},
        {"cycle_id": 2, "kind": "venture", "decided": "pin"},  # and the pin is spent
    ]
    assert rows(agent, "SELECT COUNT(*) AS n FROM desk_picks") == [{"n": 0}]  # READY's picks ended with 0.36.0
    with pytest.raises(sqlite3.IntegrityError, match="a pick never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE plan_picks SET decided = 'none'")


def test_an_items_text_is_kept_short() -> None:
    assert len(desk.Item("appraise", 1, "x" * 500).text) == desk.ITEM_CHARS
