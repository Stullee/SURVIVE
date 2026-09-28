"""Price multipliers the budget guard learns, and the cost of standard calls.

Two things can make calls cost more than the price table says, and both are
stored so every later estimate includes them:

* US-only inference (a workspace setting in the Anthropic Console) costs 1.1x
  on token prices. Ember never asks for it, but when a response reports it,
  estimates and costs use the multiplier from then on.
* If a call ever costs more than its worst-case estimate, the estimate for that
  model is scaled up (the safety factor), so the same mistake can't repeat. The
  factor is kept per mode: an overrun of the fake model in a dry run says
  nothing about the real API.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from ..config import Settings
from ..db import Database
from .estimate import Plan, worst_case_micros

US_INFERENCE_MULTIPLIER = Decimal("1.1")
MAX_SAFETY_FACTOR = Decimal(4)
_GEO_KEY = "economy.inference_geo_us"
_SAFETY_PREFIX = "economy.safety."


@dataclass(frozen=True)
class CallProfile:
    """A typical request of one kind: prompt size (tool definitions included), output limit and prompt caching (the
    worst case prices a cached prompt at the cache write rate); no server tools."""

    input_tokens: int
    max_tokens: int
    cache_ttls: tuple[str, ...] = ()


# The largest requests the agent builds (measured in tests/test_agent_requests.py, which keeps them in step):
# the planning call that opens a wake cycle, and the last will. The agent never sends a bigger one.
PLANNER_OPENING = CallProfile(input_tokens=10_200, max_tokens=1_200)
LAST_WILL = CallProfile(input_tokens=6_400, max_tokens=1_000)
# The first work step with the largest brief, and the reflection after it (with the room the loop keeps for one
# step's growth), measured the same way: a wake cycle is only worth starting if both fit after its plan.
WORK = CallProfile(input_tokens=11_700, max_tokens=2_000, cache_ttls=("5m",))
REFLECT = CallProfile(input_tokens=22_200, max_tokens=2_000, cache_ttls=("5m",))


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


def _safety_key(model: str, mode: str) -> str:
    return f"{_SAFETY_PREFIX}{mode}.{model}"


def safety_factor(db: Database, model: str, mode: str = "live") -> Decimal:
    value = _decimal(db.get_meta(_safety_key(model, mode)), Decimal(1))
    return min(MAX_SAFETY_FACTOR, max(Decimal(1), value))


def raise_safety_factor(db: Database, model: str, actual: int, estimate: int, mode: str = "live") -> Decimal:
    """After a call cost more than estimated, scale that model's estimates up (10% margin)."""
    current = safety_factor(db, model, mode)
    needed = (Decimal(actual) / Decimal(max(estimate, 1)) * current * Decimal("1.1")).quantize(Decimal("0.01"))
    factor = min(MAX_SAFETY_FACTOR, max(current, needed))
    db.set_meta(_safety_key(model, mode), str(factor))
    return factor


def profile_cost(settings: Settings, db: Database, model: str, profile: CallProfile, mode: str = "live") -> int | None:
    """Worst-case micros of one ``profile`` call on ``model``; None if the model has no price."""
    price = settings.price_for(model)
    if price is None:
        return None
    plan = Plan(
        model=model,
        input_tokens=profile.input_tokens,
        max_output_tokens=profile.max_tokens,
        cache_ttls=profile.cache_ttls,
    )
    estimate = worst_case_micros(plan, price, settings.web_search_usd_per_1000, geo_multiplier(db))
    return int((Decimal(estimate) * safety_factor(db, model, mode)).to_integral_value(rounding=ROUND_CEILING))


def opening_cost(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """What the planning call that opens a wake cycle can cost at most."""
    return profile_cost(settings, db, settings.planner_model, PLANNER_OPENING, mode)


def working_cycle_cost(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """What a wake cycle needs to do any work: the worst case of its plan, one work step and the reflection."""
    costs = [
        opening_cost(settings, db, mode),
        profile_cost(settings, db, settings.worker_model, WORK, mode),
        profile_cost(settings, db, settings.worker_model, REFLECT, mode),
    ]
    return None if None in costs else sum(c for c in costs if c is not None)


def last_will_reserve(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """Money held back so the agent can always write its last will."""
    return profile_cost(settings, db, settings.worker_model, LAST_WILL, mode)
