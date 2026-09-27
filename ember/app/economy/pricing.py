"""Price multipliers the budget guard learns, and the cost of standard calls.

Two things can make calls cost more than the price table says, and both are
stored so every later estimate includes them:

* US-only inference (a workspace setting in the Anthropic Console) costs 1.1x
  on token prices. Ember never asks for it, but when a response reports it,
  estimates and costs use the multiplier from then on.
* If a call ever costs more than its worst-case estimate, the estimate for that
  model is scaled up (the safety factor), so the same mistake can't repeat.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..config import Settings
from ..db import Database
from .estimate import Plan, worst_case_micros

US_INFERENCE_MULTIPLIER = Decimal("1.1")
MAX_SAFETY_FACTOR = Decimal(4)
_GEO_KEY = "economy.inference_geo_us"
_SAFETY_PREFIX = "economy.safety."


@dataclass(frozen=True)
class CallProfile:
    """A typical request of one kind: prompt size and output limit, no tools."""

    input_tokens: int
    max_tokens: int


# Placeholders until the agent loop (phase 3) measures its real prompts.
PLANNER_OPENING = CallProfile(input_tokens=6_000, max_tokens=3_000)
LAST_WILL = CallProfile(input_tokens=4_000, max_tokens=1_500)


def _decimal(value: str | None, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(value) if value else default
    except InvalidOperation:
        return default
    return parsed if parsed.is_finite() and parsed > 0 else default


def geo_multiplier(db: Database) -> Decimal:
    return US_INFERENCE_MULTIPLIER if db.get_meta(_GEO_KEY) == "1" else Decimal(1)


def mark_us_inference(db: Database) -> bool:
    """Remember that the account runs US-only inference. Returns True the first time."""
    if db.get_meta(_GEO_KEY) == "1":
        return False
    db.set_meta(_GEO_KEY, "1")
    return True


def safety_factor(db: Database, model: str) -> Decimal:
    return min(MAX_SAFETY_FACTOR, max(Decimal(1), _decimal(db.get_meta(_SAFETY_PREFIX + model), Decimal(1))))


def raise_safety_factor(db: Database, model: str, actual: int, estimate: int) -> Decimal:
    """After a call cost more than estimated, scale that model's estimates up (10% margin)."""
    current = safety_factor(db, model)
    needed = (Decimal(actual) / Decimal(max(estimate, 1)) * current * Decimal("1.1")).quantize(Decimal("0.01"))
    factor = min(MAX_SAFETY_FACTOR, max(current, needed))
    db.set_meta(_SAFETY_PREFIX + model, str(factor))
    return factor


def profile_cost(settings: Settings, db: Database, model: str, profile: CallProfile) -> int | None:
    """Worst-case micros of one ``profile`` call on ``model``; None if the model has no price."""
    price = settings.price_for(model)
    if price is None:
        return None
    plan = Plan(model=model, input_tokens=profile.input_tokens, max_output_tokens=profile.max_tokens)
    estimate = worst_case_micros(plan, price, settings.web_search_usd_per_1000, geo_multiplier(db))
    return int((Decimal(estimate) * safety_factor(db, model)).to_integral_value(rounding="ROUND_CEILING"))


def opening_cost(settings: Settings, db: Database) -> int | None:
    """What the planning call that opens a wake cycle can cost at most."""
    return profile_cost(settings, db, settings.planner_model, PLANNER_OPENING)


def last_will_reserve(settings: Settings, db: Database) -> int | None:
    """Money held back so the agent can always write its last will."""
    return profile_cost(settings, db, settings.worker_model, LAST_WILL)
