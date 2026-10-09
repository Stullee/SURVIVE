"""0.35.1: a promise comes first, and no cycle sleeps hours while the plan has work.

Live, the first cycles on 0.35.0 were venture cycles that slept 6 hours, then 3, while the plan had 8 steps ready and a
KDP book was promised to the owner for 10-10: a venture cycle's sleep was never cut, a cut one kept 3 hours, and the
promise, made without naming its product, was no step of the plan at all. The owner: "a promise should alter the plan",
"I want it asap", "time is money". Now a promise without its product gets the one its words name (a listing's
number, KDP), it is a step of its own taken before the heaviest step and the ventures' turn until it is kept, and any
cycle whose plan had steps ready sleeps the owner's shortest sleep.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import obligations, plan, store, weights  # noqa: E402
from app.agent.fake_llm import FakeTransport, Plan, Reply  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, started  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now, working  # noqa: E402
from tests.test_fixes_0340 import ALL, BOOK, keep, project, request  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import JOURNAL, VENTURING  # noqa: E402


def promise(agent: Agent, what: str, days: int = 2, line: int | None = None) -> int:
    """A promise to the owner, due in ``days`` days, naming ``line`` (None: no product, as message_owner made them)."""
    with agent.db.transaction() as conn:
        message = store.insert_message(conn, agent.scope(), 1, "On it.", now(agent))
        due = (agent.clock.today() + timedelta(days=days)).isoformat()
        return obligations.promise(conn, agent.scope(), 1, message, what, due, now(agent), project_id=line)


def steered(agent: Agent, exploring: bool = False) -> plan.Steer:
    with agent.db.connection() as conn:
        return plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL, exploring=exploring)


def lines_of(agent: Agent) -> dict[int, Any]:
    return {r["id"]: r["project_id"] for r in rows(agent, "SELECT id, project_id FROM obligations")}


def test_a_promise_without_its_product_gets_the_one_its_words_name(data_dir: Path) -> None:
    agent, line = started(data_dir)  # a product with a live listing
    [listing] = rows(agent, "SELECT listing_id FROM etsy_listings WHERE listing_id IS NOT NULL")
    book = project(agent, *BOOK)
    keep(agent)
    pins = promise(agent, f"2 Pinterest pins (DE+EN) live for listing #{listing['listing_id']}")
    kdp = promise(agent, "Propose the Haushaltsbuch 2027 KDP book")
    report = promise(agent, "Report Bluesky reactions and Etsy view deltas for posts #43/#44")
    said = keep(agent)
    assert lines_of(agent)[pins] == line and lines_of(agent)[kdp] == book
    assert lines_of(agent)[report] is None  # its words name no product
    assert f"Plan tree: promise #{pins} names line #{line}: a step of that product now." in said
    steps = {r["obligation_id"]: r for r in rows(agent, "SELECT * FROM plan_nodes WHERE obligation_id IS NOT NULL")}
    assert steps[pins]["channel"] == "pinterest" and steps[pins]["project_id"] == line
    assert steps[kdp]["channel"] is None and steps[kdp]["project_id"] == book
    # 0.37.0: a step of the Owner project, weighed like any (until then no step of the plan at all)
    [top] = rows(agent, "SELECT id FROM plan_nodes WHERE level = 'project' AND platform = 'owner'")
    assert (steps[report]["parent_id"], steps[report]["project_id"]) == (top["id"], None)
    assert f"Plan tree: obligation #{report} (promise, of no product) is a step of your owner's." in said


def test_message_owner_names_the_line_a_promise_is_about_from_its_words(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    outcome = call(
        working(agent, None),
        "message_owner",
        {"text": "The book goes out today.", "commits": "Send the KDP book's package", "due": now(agent)[:10]},
    )
    assert outcome.ok and f"on line #{book}: close it with obligation_done" in outcome.text, outcome.text
    assert [r["project_id"] for r in rows(agent, "SELECT project_id FROM obligations WHERE kind = 'promise'")] == [book]


def urgency_of(steer: plan.Steer, step_id: int) -> float:
    [found] = [c.step.urgency for c in steer.found if c.step.id == step_id]
    return found


@pytest.mark.exploring  # 0.37.0: a promise is weighed like any step (0.35.1 to 0.36.0 it came first)
def test_a_promise_is_weighed_like_any_step_more_as_its_day_nears(data_dir: Path) -> None:
    agent, _ = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist, each with steps ready
    book = project(agent, *BOOK)
    kdp = promise(agent, "Propose the Haushaltsbuch 2027 KDP book", days=5)
    keep(agent)
    first = steered(agent, exploring=True)
    assert first.pick.decided == "weight" and first.kind == "ordinary" and first.line == book
    assert first.step is not None and first.step.title.startswith(f"Keep promise #{kdp}: ")
    # worth the owner's word (its product could earn nothing yet), urgent at the floor while its day is 5 days off
    assert first.pick.parts is not None
    assert (first.pick.parts.worth, first.pick.parts.urgency) == (weights.PROMISE_WORTH, weights.PROMISE_FLOOR)
    with agent.db.connection() as conn:
        text = plan.step_text(conn, agent.scope(), first, explore=False)
    assert "Done when: you kept it and closed it with obligation_done." in text
    assert "Its product's open steps: #" in text and "Why: the heaviest step that is ready" in text
    agent.clock.advance(days=5)  # its day: more urgent
    due = steered(agent, exploring=True)
    assert due.step is not None and due.step.id == first.step.id
    assert urgency_of(due, first.step.id) == weights.promise_urgency(0.5)
    for _ in range(plan.PROMISE_TRIES):  # taken three times today without being kept: back to the floor, so a
        with agent.db.transaction() as conn:  # promise Ember can't keep yet doesn't take every cycle
            plan.record(conn, agent.scope(), cycle(agent, book), now(agent), due)
    assert urgency_of(steered(agent, exploring=True), first.step.id) == weights.PROMISE_FLOOR
    agent.clock.advance(hours=plan.OBLIGATION_HOURS + 1)  # a day later, overdue: urgent again, with its slip
    assert urgency_of(steered(agent, exploring=True), first.step.id) == weights.promise_urgency(0.5, slips=1)


def test_a_promise_of_pins_is_a_marketing_cycles_and_waits_while_its_request_does(data_dir: Path) -> None:
    agent, line = started(data_dir)
    [listing] = rows(agent, "SELECT listing_id FROM etsy_listings WHERE listing_id IS NOT NULL")
    pins = promise(agent, f"2 Pinterest pins live for listing #{listing['listing_id']}")
    keep(agent)
    marketing = steered(agent)
    assert marketing.kind == "marketing" and marketing.line == line
    assert marketing.step is not None and marketing.step.title.startswith(f"Keep promise #{pins}: ")
    agent.clock.advance(minutes=5)
    asked = request(agent, line, "pinterest_pin")  # the pins wait for the owner's decision
    waiting = steered(agent)
    assert waiting.step is None or not waiting.step.title.startswith(f"Keep promise #{pins}: ")
    assert [c.waiting for c in waiting.found if c.step.title.startswith(f"Keep promise #{pins}: ")] == ["owner"]
    assert owner(agent).decide(asked, {"decision": "reject", "comment": "Another picture."}, "Stefan").status == 200
    again = steered(agent).step
    assert again is not None and again.title.startswith(f"Keep promise #{pins}: ")  # decided: the promise again


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_venture_cycle_sleeps_the_owners_shortest_sleep_while_the_plan_has_steps_ready(data_dir: Path) -> None:
    fake = FakeTransport(script=[])
    agent, _ = run(data_dir, fake, cycles=0, settings=VENTURING)  # every cycle the ventures' turn
    cycle(agent)  # #1, for the project's records
    project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    venture = {
        "assessment": "ok",
        "goal": "Triage a venture",
        "money_path": "A business case my owner can back",
        "focus_project_id": None,
        "focus_venture_id": 1,
        "steps": ["research the venture"],
        "sleep_minutes": 360,
    }
    fake.script.extend([Plan(venture), Reply("Done."), JOURNAL])
    end = agent.run_cycle("schedule")
    assert end.status == "completed" and rows(agent, "SELECT kind FROM plan_picks") == [{"kind": "venture"}]
    assert (end.asked_minutes, end.sleep_minutes) == (360, agent.settings.min_sleep_minutes)
    reason = agent.db.get_meta(agent._key("next_wake_reason")) or ""
    assert "Ember chose 360 min" in reason and "cut it to 30 min: your plan has steps ready" in reason
