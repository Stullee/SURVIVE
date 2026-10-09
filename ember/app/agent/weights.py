"""The plan tree's weights (0.34.0): which step of the tree comes next, computed by Ember's code alone.

The diagnostics of 2026-10-07 showed twelve cycles on eight things: READY's ranking, the obligations, the spending
shares and the daily review each decided part of what Ember worked on, and the code settled their disagreements
differently every hour. Release 2 puts one tree under the owner's goal (projects, products, stages, small steps) and
picks every cycle's step from it with one rule. In 0.34.0 the tree only runs in the shadow: each cycle records the
step it would have picked next to what READY picked, so a week of real cycles can tune these numbers before the tree
steers (plan.py keeps the tree; this module only weighs and chooses, without a database).

A step's weight = worth × kind × channel × (1 + urgency + age + momentum), and a product's worth = what it could
earn × its chance; the owner's own worth replaces it. Each cycle takes the first of: a step the owner pinned, a promise
or an owner's decision due within 24 hours (no margin), (0.35.1) any other promise to the owner, the soonest due
first, else the highest weight, where a step of another product must be 25% better than the best of the product
worked on last (for up to 3 cycles in a row). A step carries the weight of
the most important step waiting on it. Age has no cap: it only lifts an older step over newer ones (two steps that
waited equally long keep their order by worth), and it doesn't run while a step is blocked or its product is on hold.

0.35.1: live, a KDP book promised to the owner for 10-10 waited behind the critic's fixes and two venture cycles: a
promise only carried weight to the steps in front of it, and came first only on its last day. The owner: "a promise
should alter the plan", "I want it asap". A promise is now a step of its own, taken before the heaviest step and the
ventures' turn from the moment it is made, until it is kept.

0.35.3: live on 2026-10-09 the critic's suggestions for six live products (scores 4 to 6, each counted as a defect)
outweighed their first pins, which the owner had put first for the week, and Pinterest's factor fell to its floor on
four pins hours old. A defect the critic found (a low score) still comes first; a missed views bar puts the product's
marketing above every other product's; a live product's launch marketing comes before the critic's suggestions.

0.36.0: the venture share retires. It made a cycle a venture cycle whenever ventures had had less than the owner's
share of the day's spending: live, 10 of 24 cycles, which Ember, told "nothing new", left undone (cheap idle cycles
made the share want more of them). Exploring is one more step of the tree now (plan.py's Explore step: worth
EXPLORE_WORTH unless the owner sets one, a wish of the owner's ASKED, a venture about to be parked PARK_SOON), weighed
like any other and held by the owner's word.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

# What a product could earn, as worth: damped, so optimistic numbers count less ($5 -> 2, $20 -> 4.6, $60 -> 7.4)
WORTH_MIN, WORTH_MAX = 1.0, 10.0
OWNER_WORTH_MIN, OWNER_WORTH_MAX = 0.5, 10.0
# Chance: first how far the product has got, then (once its listings had EVIDENCE_VIEWS views) how it sells
STAGE_CHANCE = {"research": 0.4, "create": 0.6, "release": 0.8, "launch": 1.0, "maintain": 1.0}
EVIDENCE_VIEWS = 30
EVIDENCE_MIN, EVIDENCE_MAX = 0.3, 3.0
KIND = {"ship": 1.0, "launch": 1.0, "fix": 1.0, "market": 1.0, "create": 0.8, "chore": 0.5, "report": 0.3}
CHANNEL_MIN, CHANNEL_MAX = 0.2, 1.5
URGENCY_CAP = 12.0
PROMISE_FLOOR, PROMISE_SCALE, PROMISE_SLIP = 2.0, 4.5, 2.0
DATE_SCALE = 3.0  # the owner's own date: 3 / days left
# 0.35.3: the critic's verdicts and a product's first buyers, a ladder: a defect, a missed views bar, a live product's
# launch marketing, the critic's suggestions (each improve verdict counted as a defect until 0.35.2)
DEFECT = 5.0  # the critic found a defect (a score of DEFECT_SCORE or less): fixed before more buyers see it
DEFECT_SCORE = 3
MISSED_BAR = 4.0  # a product missed a views bar lately: its marketing comes first (a push for buyers, 0.33.0)
REACH = 3.0  # a live product's launch marketing: its first buyers before the critic's suggestions
IMPROVE = 1.0  # the critic's suggestions (an improve verdict above DEFECT_SCORE)
ASKED = 2.0  # the owner asked for it
RECURRING_DUE = 1.0  # a recurring step on its due day
# 0.36.0: the Explore step (plan.py): the ventures' worth unless the owner sets one (that of a product that could earn
# $5 a month), and a venture Ember's code parks within a week by its stage's rule
EXPLORE_WORTH = 2.0
PARK_SOON = 1.0
OWN_DATE_CAP = 1.5  # a date Ember set herself
AGE_PER_DAY = 0.5
MOMENTUM = 1.0
STREAK_CAP = 3  # cycles in a row on one product with momentum and the margin (prompts.py's streak)
MARGIN = 1.25
PROMISE_HOURS = 24.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def prior(usd: float) -> float:
    """A product's worth from what it could earn a month at the goal date ($): 2·log2(1 + usd/5), from 1 to 10."""
    return round(_clamp(2 * math.log2(1 + max(usd, 0.0) / 5), WORTH_MIN, WORTH_MAX), 2)


def evidence(views: int, favorites: int, orders: int) -> float:
    """How the product sells, once it has been seen (unseen isn't unwanted: 1 until EVIDENCE_VIEWS views)."""
    if views < EVIDENCE_VIEWS:
        return 1.0
    return round(_clamp((orders + 0.25 * favorites + 1) / (0.02 * views + 1), EVIDENCE_MIN, EVIDENCE_MAX), 2)


def chance(stage: str, views: int = 0, favorites: int = 0, orders: int = 0) -> float:
    return round(STAGE_CHANCE.get(stage, 1.0) * evidence(views, favorites, orders), 3)


def worth(
    usd: float, stage: str, views: int = 0, favorites: int = 0, orders: int = 0, owner: float | None = None
) -> float:
    """A product's worth: what it could earn × its chance, or the owner's own worth, which replaces it."""
    if owner is not None:
        return round(_clamp(owner, OWNER_WORTH_MIN, OWNER_WORTH_MAX), 2)
    return round(prior(usd) * chance(stage, views, favorites, orders), 3)


def promise_urgency(days_left: float, slips: int = 0) -> float:
    """A promise to the owner: at least 2, more as its date nears, +2 for each time it slipped."""
    return min(URGENCY_CAP, max(PROMISE_FLOOR, PROMISE_SCALE / max(days_left, 0.25)) + PROMISE_SLIP * max(slips, 0))


def date_urgency(days_left: float) -> float:
    """A date the owner set (a hard date): 3 ÷ days left."""
    return min(URGENCY_CAP, DATE_SCALE / max(days_left, 0.25))


def own_date_urgency(days_left: float) -> float:
    """A date Ember set herself counts, but never more than OWN_DATE_CAP."""
    return min(OWN_DATE_CAP, DATE_SCALE / max(days_left, 0.25))


@dataclass(frozen=True)
class Step:
    """A candidate step, as plan.py reads it from the tree: every number already computed by Ember's code."""

    id: int
    product: int | None  # the product's line (projects.id); None for a step of a project as a whole
    title: str
    kind: str  # a key of KIND
    worth: float  # its product's worth
    channel: float = 1.0  # a marketing step: how well its channel works (clicks or view gains per item)
    urgency: float = 0.0  # the largest urgency that applies (promise_urgency, date_urgency, DEFECT, ...)
    age_days: float = 0.0  # days ready and untouched (plan.py counts none while it is blocked or on hold)
    pinned: bool = False  # the owner pinned it
    promise_hours: float | None = None  # hours until a promise it serves is due (negative: overdue)
    promise: bool = False  # 0.35.1: a promise to the owner: taken before the heaviest step
    blocked: bool = False  # waits on the owner, a channel, a date or an approval: not a candidate
    waiting: tuple[Step, ...] = field(default=(), compare=False)  # open steps that wait on this one


@dataclass(frozen=True)
class Parts:
    worth: float
    kind: float
    channel: float
    urgency: float
    age: float
    momentum: float
    own: float  # the step's own weight
    carried: float  # the weight of the most important step waiting on it (0: none)
    carried_from: int | None
    total: float

    def json(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def text(self, carried_title: str | None = None) -> str:
        """The weight in words, for the owner's Plan tab and the diagnostics (``carried_title``: the title of the step
        whose weight it carries, else its number)."""
        said = (
            f"worth {self.worth:.2g} × kind {self.kind:g}"
            + (f" × channel {self.channel:g}" if self.channel != 1.0 else "")
            + f" × (1 + urgency {self.urgency:.2g} + age {self.age:.2g} + momentum {self.momentum:g}) = {self.own:.1f}"
        )
        if self.carried_from is not None and self.carried > self.own:
            source = f'"{carried_title}"' if carried_title else f"step #{self.carried_from}"
            said += f"; carries {self.carried:.1f} from {source}, which waits on it"
        return said


def _momentum(step: Step, last_product: int | None, streak: int) -> float:
    incumbent = step.product is not None and step.product == last_product and streak < STREAK_CAP
    return MOMENTUM if incumbent else 0.0


def _weight(step: Step, age: float, momentum: float) -> float:
    kind = KIND.get(step.kind, 1.0)
    channel = _clamp(step.channel, CHANNEL_MIN, CHANNEL_MAX)
    return step.worth * kind * channel * (1 + _clamp(step.urgency, 0.0, URGENCY_CAP) + age + momentum)


def weigh(step: Step, last_product: int | None = None, streak: int = 0) -> Parts:
    """A step's weight and its parts. A step carries the weight of the most important open step waiting on it, if
    that is higher than its own: each is weighed as if it stood in this step's place, with its age and momentum (a
    step that waits can't age itself, but the chain has waited as long as the step in front of it)."""
    age = round(AGE_PER_DAY * max(step.age_days, 0.0), 3) if not step.blocked else 0.0
    momentum = _momentum(step, last_product, streak)
    own = _weight(step, age, momentum)
    carried, source = 0.0, None
    for waiting in step.waiting:
        weight = _weight(waiting, age, momentum)
        if weight > carried:
            carried, source = weight, waiting.id
    kind = KIND.get(step.kind, 1.0)
    channel = _clamp(step.channel, CHANNEL_MIN, CHANNEL_MAX)
    urgency = _clamp(step.urgency, 0.0, URGENCY_CAP)
    return Parts(step.worth, kind, channel, urgency, age, momentum, own, carried, source, max(own, carried))


def promise_hours(step: Step) -> float | None:
    """Hours until the soonest promise the step serves is due: its own, or one of a step waiting on it."""
    hours = [h for h in (step.promise_hours, *(w.promise_hours for w in step.waiting)) if h is not None]
    return min(hours) if hours else None


@dataclass(frozen=True)
class Pick:
    step: Step | None
    decided: str  # 'pin', 'promise', 'weight', 'margin' (the product worked on last kept it), 'none' (and 'venture',
    # the ventures' turn, in the records until 0.35.3)
    parts: Parts | None
    ranked: tuple[tuple[Step, Parts], ...]  # every candidate, best first

    def json(self, top: int = 6) -> dict[str, Any]:
        return {
            "step": self.step.id if self.step else None,
            "product": self.step.product if self.step else None,
            "decided": self.decided,
            "weight": round(self.parts.total, 3) if self.parts else None,
            "parts": self.parts.json() if self.parts else None,
            "ranked": [
                {"step": s.id, "product": s.product, "title": s.title, "weight": round(p.total, 3)}
                for s, p in self.ranked[:top]
            ],
        }


def rank(steps: Iterable[Step], last_product: int | None = None, streak: int = 0) -> list[tuple[Step, Parts]]:
    """The candidates (steps not blocked), heaviest first; ties go to the older step, then the lower id."""
    weighed = [(s, weigh(s, last_product, streak)) for s in steps if not s.blocked]
    return sorted(weighed, key=lambda sp: (-round(sp[1].total, 9), -sp[0].age_days, sp[0].id))


def choose(steps: Sequence[Step], last_product: int | None = None, streak: int = 0) -> Pick:
    """The step a cycle takes, in this order: a step the owner pinned; a promise or an owner's decision due within 24
    hours (or overdue), with no margin; (0.35.1) any other promise to the owner, the soonest due first; else the
    heaviest step, unless the product worked on last (fewer than STREAK_CAP cycles in a row) has a step the winner
    doesn't beat by MARGIN. 0.36.0: no ventures' turn any more (the Explore step is weighed like any other)."""
    ranked = tuple(rank(steps, last_product, streak))
    for step, parts in ranked:
        if step.pinned:
            return Pick(step, "pin", parts, ranked)
    due = [(s, p, h) for s, p in ranked if (h := promise_hours(s)) is not None and h <= PROMISE_HOURS]
    if due:
        step, parts, _ = min(due, key=lambda sph: (sph[2], -sph[1].total, sph[0].id))
        return Pick(step, "promise", parts, ranked)
    promised = [(s, p) for s, p in ranked if s.promise]
    if promised:
        step, parts = min(promised, key=lambda sp: (_hours(sp[0]), -sp[1].total, sp[0].id))
        return Pick(step, "promise", parts, ranked)
    if not ranked:
        return Pick(None, "none", None, ranked)
    best, parts = ranked[0]
    if last_product is not None and streak < STREAK_CAP and best.product != last_product:
        kept = next(((s, p) for s, p in ranked if s.product == last_product), None)
        if kept is not None and parts.total < MARGIN * kept[1].total:
            return Pick(kept[0], "margin", kept[1], ranked)
    return Pick(best, "weight", parts, ranked)


def _hours(step: Step) -> float:
    return step.promise_hours if step.promise_hours is not None else math.inf


def streak_of(products: Sequence[int | None]) -> tuple[int | None, int]:
    """The product of the last cycle and how many cycles in a row it had, from the cycles' products newest first
    (None: a cycle on no product, e.g. a venture cycle, which ends no streak and counts in none)."""
    last, count = None, 0
    for product in products:
        if product is None:
            continue
        if last is None:
            last = product
        if product != last:
            break
        count += 1
    return last, count


def parts_by_id(ranked: Iterable[tuple[Step, Parts]]) -> Mapping[int, Parts]:
    return {s.id: p for s, p in ranked}


def settings() -> dict[str, Any]:
    """Every number above, by its name, a table as a copy (0.35.2: the diagnostics list them, so the picks a report
    shows can be re-scored without Ember's code)."""
    return {
        name: dict(value) if isinstance(value, dict) else value for name, value in globals().items() if name.isupper()
    }
