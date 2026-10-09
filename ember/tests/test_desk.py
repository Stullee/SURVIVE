"""0.13.0: the decision desk. A venture cycle's planner chose freely what to look at, so research went where the plan's
mood took it and the brainstorm never ran live. Now Ember's code ranks the ventures' next decisions (READY: a backed
venture without a project, the owner's wishes, deadlines, the critic's flags, the other appraisals by expected net,
triage of ideas, a brainstorm when the funnel is thin), and each venture plan takes one or says why it takes none.
Ember's code checks the pick, aims the cycle at its venture and keeps each pick with the list it came from."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent import critic, desk, econ, ventures, views
from app.agent.fake_llm import FakeTransport, Plan, Reply, request_kind
from app.agent.service import Agent
from app.economy import burn
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
    plan,
)


def keys(agent: Agent, mode: str = burn.EXPLORE) -> list[str]:
    with agent.db.connection() as conn:
        found = desk.ready(conn, agent.scope(), mode=mode, today=agent.clock.today(), cash_eur=20.0, net_days=None)
    return [item.key for item in found]


def taking(ready: str, venture: int | None = None) -> Plan:
    return Plan({**dict(plan(venture=venture).plan), "ready": ready})  # type: ignore[arg-type]


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_ready_ranks_the_ventures_next_decisions(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)  # the tree is planted
    # Etsy is being researched (no listing is live yet) and six ideas wait: the research first, then the ideas
    assert keys(agent) == [f"appraise #{ETSY}", *(f"triage #{v}" for v in (PINTEREST, DROPSHIPPING, PRINT, WEBSITE))]
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
    assert keys(agent) == [
        f"appraise #{RECRUITING}",  # the owner's wish
        f"appraise #{DROPSHIPPING}",  # its research budget is nearly used: urgent
        f"appraise #{ETSY}",
        f"triage #{COMPANION}",  # weight 100 before the unscored ideas
        "brainstorm",  # 0.19.3: due, it keeps the last place (triage #PINTEREST came fifth before)
    ]
    # 0.19.3: the backed venture is its project's work, in ordinary cycles: no "build", and no venture cycle in focus
    assert keys(agent, burn.FOCUS) == []
    with agent.db.connection() as conn:
        items = desk.ready(
            conn, agent.scope(), mode=burn.EXPLORE, today=agent.clock.today(), cash_eur=20.0, net_days=None
        )
    assert items[1].text.startswith(
        "Dropshipping store · needs 2 research calls for it that found something (it has 1)"
    )
    assert "research budget: $0.14 of $0.60 left" in items[1].text
    assert items[0].text.startswith("your owner's wish: Recruiting and headhunting service · needs")
    for vid in (PINTEREST, WEBSITE, COMPANION):  # the ideas go: fewer than 5 wait, and none has its numbers
        assert actions.decide_venture(vid, {"action": "park"}, "Stefan").status == 200
    assert keys(agent)[-1] == "brainstorm"
    assert items[-1].kind == "brainstorm" and len(items) == desk.MAX_ITEMS


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_proposed_venture_the_critic_doubts_is_answered_first(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    proposed(agent)  # DROPSHIPPING, with its numbers
    assert f"answer #{DROPSHIPPING}" not in keys(agent)  # no critique yet
    theirs = econ.Case("etsy_digital", 4.9, 0.0, 0.0, (0, 1, 3), 10.0, 2.0, 91, 3.0)
    with agent.db.transaction() as conn:
        texts = {"verdict": "park", "fatal_flaw": "Nobody searches for it.", "change_mind": "Ten sales a month."}
        critic.add(conn, DROPSHIPPING, 1, None, texts, theirs, econ.compute(theirs), to_iso(agent.clock.now()))
    assert keys(agent)[:2] == [f"answer #{DROPSHIPPING}", f"appraise #{ETSY}"]
    with agent.db.connection() as conn:
        [answer] = [
            i
            for i in desk.ready(
                conn, agent.scope(), mode=burn.EXPLORE, today=agent.clock.today(), cash_eur=20.0, net_days=None
            )
            if i.kind == "answer"
        ]
    assert (
        "the critic says park (case #1): Nobody searches for it. · answer it with evidence or new numbers"
        in answer.text
    )


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_venture_plan_takes_a_ready_item_or_says_why_none(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)  # its plan names none: kept as such
    fake.script.extend([taking(f"Triage #{DROPSHIPPING}: the heaviest open question"), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    planner = [r for r in fake.sent if request_kind(r) == "plan"][-1]
    asked = planner["messages"][0]["content"][0]["text"]
    assert (
        f"\n== READY ==\nRanked by Ember's code. Take one (ready: its key), or say why none:\n1. appraise #{ETSY}: "
        in asked
    )
    assert "ready" in planner["output_config"]["format"]["schema"]["required"]
    brief = [r for r in fake.sent if request_kind(r) == "work"][-1]["messages"][0]["content"][0]["text"]
    assert f"\nDecision desk: you took triage #{DROPSHIPPING}: Dropshipping store · an idea" in brief
    assert f"Focus venture: #{DROPSHIPPING} Dropshipping store" in brief  # the plan named no venture: the pick's
    assert rows(agent, "SELECT venture_id FROM cycles WHERE id = 2") == [{"venture_id": DROPSHIPPING}]
    fake.script.extend([taking("appraise #99"), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")  # 0.36.0: a wrong or missing answer only loses the cycle: the Explore step is ready
    agent.run_cycle("schedule")  # the fake model plans by itself: it takes READY's first item
    fake.script.extend([taking("none: my owner wants the Etsy research finished first"), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")  # 0.36.0: "none" leaves the plan's Explore step waiting until tomorrow
    picks = rows(agent, "SELECT cycle_id, pick, venture_id, why_not, items FROM desk_picks ORDER BY id")
    assert [(p["cycle_id"], p["pick"], p["venture_id"], p["why_not"]) for p in picks] == [
        (1, None, None, "the plan named none"),
        (2, f"triage #{DROPSHIPPING}", DROPSHIPPING, None),
        (3, None, None, "'appraise #99' isn't on the READY list"),
        (4, json.loads(picks[3]["items"])[0]["key"], json.loads(picks[3]["items"])[0]["venture_id"], None),
        (5, None, None, "my owner wants the Etsy research finished first"),
    ]
    tomorrow = (agent.clock.today() + timedelta(days=1)).isoformat()
    assert rows(agent, "SELECT waiting, wait_until FROM plan_nodes WHERE template = 'explore@1'") == [
        {"waiting": "date", "wait_until": tomorrow}
    ]
    assert [i["key"] for i in json.loads(picks[1]["items"])][:2] == [f"appraise #{ETSY}", f"triage #{PINTEREST}"]
    shown = views.ventures_view(agent)["desk"]
    assert [p["cycle_id"] for p in shown["picks"]] == [5, 4, 3, 2, 1] and shown["picks"][2]["shown"] == 5
    assert shown["mode"] == burn.EXPLORE and shown["ready"][0]["key"] == keys(agent)[0]
    with pytest.raises(sqlite3.IntegrityError, match="a pick never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE desk_picks SET pick = NULL")


def test_the_plans_answer_is_read_by_code() -> None:
    items = [
        desk.Item("appraise", 1, "Etsy"),
        desk.Item("triage", 3, "Dropshipping"),
        desk.Item("brainstorm", None, ""),
    ]
    assert desk.choose(items, "Appraise #1") == (items[0], "")
    assert desk.choose(items, "triage 3: the heaviest") == (items[1], "")
    assert desk.choose(items, "brainstorm") == (items[2], "")
    assert desk.choose(items, "None") == (None, "no reason given")
    assert desk.choose(items, "none - my owner's message first") == (None, "my owner's message first")
    assert desk.choose(items, "triage #4") == (None, "'triage #4' isn't on the READY list")
    assert desk.choose(items, "") == (None, "the plan named none")
    assert len(desk.Item("appraise", 1, "x" * 500).text) == desk.ITEM_CHARS
