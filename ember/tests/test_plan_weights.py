"""0.34.0: the plan tree's weights (agent/weights.py), without a database: the parts, the order each cycle takes, and
the guards (no thrash, no monopoly past the streak, no starvation, age keeps importance first, blocked steps don't
age), with the cycles of 2026-10-07 as the acceptance case."""

from __future__ import annotations

import random

import pytest

from app.agent import weights
from app.agent.weights import Step


def rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - generated days, not security


def step(id_: int, product: int | None, worth: float = 3.0, kind: str = "ship", **more: object) -> Step:
    return Step(id_, product, f"step {id_}", kind, worth, **more)  # type: ignore[arg-type]


# --- worth ---


@pytest.mark.parametrize(("usd", "worth"), [(0, 1.0), (5, 2.0), (20, 4.64), (60, 7.4), (155, 10.0), (10_000, 10.0)])
def test_what_a_product_could_earn_is_damped_into_its_worth(usd: float, worth: float) -> None:
    assert weights.prior(usd) == pytest.approx(worth, abs=0.01)


def test_unseen_is_not_unwanted_and_evidence_moves_only_after_30_views() -> None:
    assert weights.evidence(29, 0, 0) == 1.0
    assert weights.evidence(60, 0, 0) == pytest.approx(1 / 2.2, abs=0.01)  # seen, neither liked nor bought
    assert weights.evidence(60, 8, 1) == pytest.approx(4 / 2.2, abs=0.01)
    assert weights.evidence(10_000, 0, 0) == weights.EVIDENCE_MIN
    assert weights.evidence(40, 0, 50) == weights.EVIDENCE_MAX


def test_chance_rises_as_the_stages_finish_and_the_owners_worth_replaces_the_codes() -> None:
    chances = [weights.chance(s) for s in ("research", "create", "release", "launch", "maintain")]
    assert chances == sorted(chances) and chances[0] == 0.4 and chances[-1] == 1.0
    assert weights.worth(20, "create") == pytest.approx(4.64 * 0.6, abs=0.01)
    assert weights.worth(20, "create", owner=7) == 7
    assert weights.worth(20, "create", owner=40) == weights.OWNER_WORTH_MAX


def test_urgency_of_promises() -> None:
    assert weights.promise_urgency(10) == weights.PROMISE_FLOOR  # at least 2 from the moment of the promise
    assert weights.promise_urgency(1) == 4.5
    assert weights.promise_urgency(3, slips=1) == 4.0  # +2 for each slip
    assert weights.promise_urgency(0.01, slips=9) == weights.URGENCY_CAP


# --- the weight and its parts ---


def test_weight_is_worth_times_kind_times_channel_times_one_plus_urgency_age_and_momentum() -> None:
    marketing = step(1, 7, worth=4.0, kind="market", channel=0.5, urgency=1, age_days=2)
    parts = weights.weigh(marketing, last_product=7, streak=1)
    assert (parts.urgency, parts.age, parts.momentum) == (1, 1.0, weights.MOMENTUM)
    assert parts.total == pytest.approx(4.0 * 1.0 * 0.5 * (1 + 1 + 1 + 1))
    assert "channel 0.5" in parts.text() and "= 8.0" in parts.text()
    assert weights.weigh(marketing, last_product=7, streak=weights.STREAK_CAP).momentum == 0  # past the streak


def test_a_step_carries_the_weight_of_the_most_important_step_waiting_on_it() -> None:
    propose = step(2, 14, worth=2.8, kind="ship", urgency=weights.promise_urgency(3))
    interior = step(1, 14, worth=2.8, kind="create", age_days=1, waiting=(propose,))
    parts = weights.weigh(interior)
    assert parts.carried_from == 2 and parts.total == parts.carried > parts.own
    assert parts.total == pytest.approx(2.8 * (1 + 2 + 0.5))  # the promise, weighed with the interior's age
    assert "carries" in parts.text()
    alone = weights.weigh(step(1, 14, worth=2.8, kind="create", age_days=1))
    assert alone.carried_from is None and alone.total == alone.own


def test_a_blocked_step_is_no_candidate_and_does_not_age() -> None:
    waiting_on_owner = step(1, 1, worth=9, age_days=30, blocked=True)
    assert weights.weigh(waiting_on_owner).age == 0
    assert weights.rank([waiting_on_owner, step(2, 2, worth=1)])[0][0].id == 2
    assert weights.choose([waiting_on_owner]).decided == "none"


# --- the order each cycle takes ---


def test_a_pin_comes_first_then_a_promise_due_within_a_day_then_the_weight() -> None:
    # 0.36.0: no ventures' turn between them any more: the Explore step is weighed like any step (test_fixes_0360.py)
    heavy = step(1, 1, worth=9, urgency=3)
    promised = step(2, 2, worth=1, promise_hours=20)
    pinned = step(3, 3, worth=0.5, pinned=True)
    assert weights.choose([heavy, promised, pinned]).decided == "pin"
    pick = weights.choose([heavy, promised])
    assert (pick.step, pick.decided) == (promised, "promise")  # no margin, whatever weighs more
    assert weights.choose([heavy, step(4, 4, promise_hours=30)]).step == heavy


def test_a_promise_on_a_later_step_makes_the_step_in_front_of_it_take_the_cycle_when_due() -> None:
    propose = step(2, 14, worth=2.8, promise_hours=-5)  # overdue
    interior = step(1, 14, worth=2.8, kind="create", waiting=(propose,))
    assert weights.choose([step(3, 6, worth=9, urgency=5), interior]).step == interior


def test_the_product_worked_on_last_keeps_the_cycle_unless_beaten_by_the_margin_until_its_streak_ends() -> None:
    incumbent = step(1, 1, worth=3.0)
    challenger = step(2, 2, worth=7.0)  # heavier than 3 × (1 + momentum), but not by 25%
    assert weights.choose([incumbent, challenger], last_product=1, streak=1).decided == "margin"
    assert weights.choose([incumbent, challenger], last_product=1, streak=weights.STREAK_CAP).step == challenger
    strong = step(3, 3, worth=8.0)  # 8 > 1.25 × 6
    assert weights.choose([incumbent, strong], last_product=1, streak=1).step == strong


def test_the_streak_counts_cycles_in_a_row_on_one_product_and_skips_cycles_on_none() -> None:
    assert weights.streak_of([3, None, 3, 3, 5, 3]) == (3, 3)
    assert weights.streak_of([None, None]) == (None, 0)
    assert weights.streak_of([]) == (None, 0)


def test_the_pick_keeps_its_parts_and_the_ranking_for_the_log() -> None:
    pick = weights.choose([step(1, 1, worth=2), step(2, 2, worth=3)])
    logged = pick.json()
    assert logged["step"] == 2 and logged["decided"] == "weight" and logged["weight"] == pytest.approx(3.0)
    assert [r["step"] for r in logged["ranked"]] == [2, 1]
    assert set(logged["parts"]) >= {"worth", "kind", "urgency", "age", "momentum", "carried", "total"}


# --- the guards, as properties over random days ---


def _simulate(generator: random.Random, cycles: int) -> tuple[list[int | None], int]:
    """Random static steps of 1-3 cycles on 5 products, worked by the rule; returns each cycle's product and the
    number of steps finished."""
    effort: dict[int, int] = {}
    candidates: dict[int, Step] = {}
    for i in range(1, 16):
        candidates[i] = step(
            i, generator.randint(1, 5), worth=generator.uniform(0.5, 8), urgency=generator.choice([0, 0, 1, 2])
        )
        effort[i] = generator.randint(1, 3)
    history: list[int | None] = []
    done = 0
    for _ in range(cycles):
        last, streak = weights.streak_of(list(reversed(history)))
        pick = weights.choose(list(candidates.values()), last_product=last, streak=streak)
        if pick.step is None:
            history.append(None)
            continue
        history.append(pick.step.product)
        effort[pick.step.id] -= 1
        if effort[pick.step.id] == 0:
            del candidates[pick.step.id]
            done += 1
    return history, done


@pytest.mark.parametrize("seed", range(40))
def test_no_thrash_a_switch_needs_a_finished_step_or_the_end_of_a_streak(seed: int) -> None:
    history, done = _simulate(rng(seed), 40)
    switches = sum(1 for a, b in zip(history, history[1:], strict=False) if a and b and a != b)
    streaks_ended = sum(
        1 for i in range(weights.STREAK_CAP, len(history)) if len(set(history[i - weights.STREAK_CAP : i])) == 1
    )
    assert switches <= done + streaks_ended + 1


@pytest.mark.parametrize("seed", range(40))
def test_no_monopoly_past_the_streak_a_heavier_step_of_another_product_wins_at_once(seed: int) -> None:
    generator = rng(seed)
    mine = step(1, 1, worth=generator.uniform(1, 5))
    theirs = step(2, 2, worth=mine.worth * generator.uniform(1.01, 1.2))
    assert weights.choose([mine, theirs], last_product=1, streak=weights.STREAK_CAP).step == theirs


@pytest.mark.parametrize("seed", range(40))
def test_no_starvation_every_ready_step_gets_its_turn_as_it_ages(seed: int) -> None:
    generator = rng(seed)
    low = generator.uniform(0.5, 1.5)
    high = generator.uniform(4, 9)
    days = 0
    while True:  # a fresh step worth up to 9 arrives every day; the low one waits and ages
        fresh = step(2, 2, worth=high, age_days=0)
        old = step(1, 1, worth=low, age_days=days)
        if weights.choose([fresh, old]).step == old:
            break
        days += 1
        assert days < 400
    assert days <= (high / low - 1) / weights.AGE_PER_DAY + 1


@pytest.mark.parametrize("seed", range(40))
def test_age_keeps_importance_first_steps_that_waited_equally_long_keep_their_order(seed: int) -> None:
    generator = rng(seed)
    days = generator.uniform(0, 60)
    a = step(1, 1, worth=generator.uniform(0.5, 9), age_days=days)
    b = step(2, 2, worth=generator.uniform(0.5, 9), age_days=days)
    heavier = a if a.worth > b.worth else b
    assert weights.choose([a, b]).step == heavier


# --- acceptance: the cycles of 2026-10-07 ---
#
# Products, from the agent's own sub-goals of that day (#26-#28): the KDP Haushaltsbuch (#14) could earn $20 a month
# and is in create (worth 4.64 × 0.6); the Etsy lines #3-#7 shared $60 ($12 each, live with fewer than 30 views:
# worth 3.53); the Bauhaus posters (#8) $20 (live: 4.64). The budget sheet's formula fix (#6) was asked for by a review,
# not found by a quality check, so it carries no urgency; it had waited since 10-04. Pinterest opened at 21:36 UTC.

KDP, BUDGET, CAREER, POSTERS = 14, 6, 3, 8
W_KDP = weights.worth(20, "create")
W_ETSY = weights.worth(12, "launch")
W_POSTERS = weights.worth(20, "launch")


def _october_7(*, promise: bool, kdp_age: float, budget_age: float, pinterest: bool, kdp_proposed: bool = False):
    propose = step(
        142,
        KDP,
        worth=W_KDP,
        kind="ship",
        urgency=weights.promise_urgency(3) if promise else 0.0,
        promise_hours=80 if promise else None,
    )
    interior = step(141, KDP, worth=W_KDP, kind="create", age_days=kdp_age, waiting=(propose,), blocked=kdp_proposed)
    budget = step(61, BUDGET, worth=W_ETSY, kind="fix", age_days=budget_age)
    pins = step(31, CAREER, worth=W_ETSY, kind="market", urgency=weights.RECURRING_DUE, blocked=not pinterest)
    bluesky = step(81, POSTERS, worth=W_POSTERS, kind="market", channel=0.6, urgency=weights.RECURRING_DUE)
    return [interior, budget, pins, bluesky]


def test_october_7_the_budget_fix_at_13_11_then_the_promised_book_from_16_13_then_pins_at_cycle_135() -> None:
    # 13:11 (#126): no promise yet; the bundle (#12) was just finished, so no product keeps the cycle
    first = weights.choose(_october_7(promise=False, kdp_age=1, budget_age=3, pinterest=False), 12, 2)
    assert first.step is not None and first.step.product == BUDGET
    # 16:13 (#127): the book is promised for 10-10; the budget fix was worked at #126 and keeps it by the margin
    # unless the book beats it by 25%: the promise, carried back to the interior with the chain's day of waiting, does
    second = weights.choose(_october_7(promise=True, kdp_age=1, budget_age=0, pinterest=False), BUDGET, 1)
    assert second.step is not None and second.step.product == KDP and second.parts is not None
    assert second.parts.carried_from == 142
    # 16:33 and 16:43 (#128, #129): the book keeps the cycles until it is proposed
    for streak in (1, 2):
        held = weights.choose(_october_7(promise=True, kdp_age=0, budget_age=0.1, pinterest=False), KDP, streak)
        assert held.step is not None and held.step.product == KDP
    # 00:09 (#135, after the blog line #10): the book waits on the owner, Pinterest is open: pins for the career
    # listings (a missed day-7 bar makes their marketing urgent, never a title and tag chore)
    last = weights.choose(_october_7(promise=True, kdp_age=0, budget_age=0.3, pinterest=True, kdp_proposed=True), 10, 1)
    assert last.step is not None and last.step.id == 31
