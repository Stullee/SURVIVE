"""0.37.1: a promise to the owner weighs about what a product does, and is urgent only near its day.

Live on 2026-10-09 (the diagnostics of 18:06, the first cycle of 0.37.0), every open promise or decision of the owner's
weighed at least 5 × (1 + 2) = 15, and no product step more than about 12: the promises still came first whatever else
waited. Nine were open (four of them one KDP job, two of them one report, the latest due in 9 days), so the pins the
owner had put first for the week ranked tenth, and none of the 24 cycles recorded took one. analysis-0.37.0 rebuilt
that evening's ranking from the report. Now a promise is worth weights.PROMISE_WORTH (3; its product's worth when that
is more) and urgent only from weights.PROMISE_NEAR_DAYS before its day, and one taken plan.PROMISE_TRIES times in a
day without being kept (a decision once) weighs its worth alone until the day is over, where 0.37.0 kept the floor.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, weights  # noqa: E402
from tests.test_fixes_0340 import keep  # noqa: E402
from tests.test_fixes_0350 import steered  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402
from tests.test_fixes_0353 import PINS, launched  # noqa: E402

KDP = 14

# --- the weights: the evening of 2026-10-09 ---


def _owed(step_id: int, product: int | None, days: int) -> weights.Step:
    """A promise due in ``days`` days (plan.py counts its day as ½), worth the owner's word: the KDP book in release
    (2.5) and the Owner project are worth less than a promise."""
    urgency = weights.promise_urgency(days + 0.5)
    return weights.Step(step_id, product, f"promise {step_id}", "ship", weights.PROMISE_WORTH, urgency=urgency)


def _october_9_evening() -> list[weights.Step]:
    """The candidates of 2026-10-09 18:06, as analysis-0.37.0 rebuilt them: the KDP book's promises #45 and #46 (due
    10-11) and the owner's decision #44 on its rejection (taken twice today: its worth alone); the Owner project's
    report promises, due 10-11 (#20, #28), 10-13 (#27), 10-14 (#21, #43) and 10-18 (#18); five live products' first
    pins and the critic's suggestions, ready since 10-08 (line #3 worked this morning); the brainstorm."""
    steps = [
        weights.Step(137, KDP, "decision #44", "fix", weights.PROMISE_WORTH, age_days=0.006),
        _owed(138, KDP, 2),
        _owed(148, KDP, 2),
        _owed(143, None, 2),
        _owed(146, None, 2),
        _owed(145, None, 4),
        _owed(144, None, 5),
        _owed(147, None, 5),
        _owed(142, None, 9),
    ]
    for product, pins, critic in ((4, 26, 29), (6, 56, 59), (7, 71, 74), (8, 87, 89)):
        steps.append(
            weights.Step(pins, product, f"pins #{product}", "market", 2.0, urgency=weights.REACH, age_days=1.38)
        )
        steps.append(
            weights.Step(critic, product, f"critic #{product}", "fix", 2.0, urgency=weights.IMPROVE, age_days=1.38)
        )
    steps.append(weights.Step(12, 3, "pins #3", "market", 2.0, urgency=weights.REACH, age_days=0.35))
    steps.append(weights.Step(14, 3, "critic #3", "fix", 2.0, urgency=weights.IMPROVE, age_days=0.35))
    steps.append(weights.Step(140, None, "brainstorm", "create", weights.EXPLORE_WORTH))
    return steps


def test_october_9_the_kdp_redo_then_the_pins_then_each_report_as_its_day_nears() -> None:
    pick = weights.choose(_october_9_evening(), KDP, 2)  # cycles #175 and #176 were on the KDP book
    # the redo the owner wanted by 10-10, due in two days and on the line worked on: 3 × (1 + 2 + 1) = 12
    assert pick.step is not None and pick.step.id in (138, 148) and pick.decided == "weight"
    order = [s.id for s, _ in pick.ranked]
    first_pins = min(order.index(i) for i in (26, 56, 71, 87, 12))
    soon = [order.index(i) for i in (143, 146)]  # the reports due in two days: 3 × (1 + 2) = 9
    later = [order.index(i) for i in (142, 144, 145, 147)]  # due in 4 to 9 days: their worth alone, 3
    assert first_pins == 2 < min(soon) and max(soon) < min(later)  # 0.37.0: the first pins tenth, behind nine at 15+
    assert order.index(137) > first_pins  # the decision taken twice today waits behind the pins


def test_a_promise_rises_above_a_live_products_first_pins_as_its_day_nears() -> None:
    pins = weights.weigh(weights.Step(1, 1, "pins", "market", 2.0, urgency=weights.REACH)).total  # 2 × (1 + 3) = 8

    def kept(days: int) -> float:  # the promise's weight ``days`` days before its day
        return weights.weigh(_owed(2, None, days)).total

    assert [kept(d) for d in (9, 3, 2, 1, 0)] == pytest.approx([3, 3, 9, 12, 30])
    assert kept(3) < pins < kept(2)


def test_a_promises_urgency_by_its_day_and_how_often_cycles_took_it() -> None:
    today = date(2026, 10, 9)

    def urgency(due: str | None, kind: str = "promise", tries: int = 0) -> float:
        return plan._owed_urgency({"id": 1, "kind": kind, "due": due}, today, {1: tries})

    assert urgency("2026-10-18") == urgency("2026-10-12") == 0.0  # more than two days off: its worth alone
    assert urgency("2026-10-11") == weights.PROMISE_FLOOR  # two days before its day
    assert (urgency("2026-10-10"), urgency("2026-10-09")) == (3.0, 9.0)  # the day before, its day
    assert urgency("2026-10-08") == 9.0 + weights.PROMISE_SLIP  # slipped: urgent whenever its day was
    assert urgency("2026-10-09", tries=plan.PROMISE_TRIES - 1) == 9.0
    # taken three times today without being kept: none until the day is over (0.37.0: the floor, still above every
    # product step, so a promise Ember couldn't keep yet took every cycle); a decision of the owner's once
    assert urgency("2026-10-09", tries=plan.PROMISE_TRIES) == 0.0
    assert urgency("2026-10-09", kind="fix", tries=1) == 0.0
    assert urgency(None) == weights.PROMISE_FLOOR  # a promise without a day (none is made so) keeps the floor


# --- the tree: a live product's pins and a report promised for later ---


def test_a_live_products_first_pins_come_before_a_report_due_later_and_after_it_the_day_before(data_dir: Path) -> None:
    agent, line, _ = launched(data_dir)
    report = promise(agent, "Report the Bluesky reactions and the view deltas of the week", days=9)
    keep(agent)
    first = steered(agent)
    assert first.step is not None and first.step.title == PINS  # 0.37.0: the report, 5 × (1 + 2) = 15 to the pins' 8
    [owed] = [c.step for c in first.found if c.step.title.startswith(f"Keep promise #{report}: ")]
    assert (owed.worth, owed.urgency, owed.product) == (weights.PROMISE_WORTH, 0.0, None)
    agent.clock.advance(days=8)  # the day before its day: urgent, and it waited as long as the pins
    keep(agent)
    due = steered(agent)
    assert due.step is not None and due.step.title.startswith(f"Keep promise #{report}: ")
    assert due.pick.parts is not None and due.pick.parts.urgency == 3.0
