"""Price multipliers the budget guard learns, and the cost of standard calls.

Two things can make calls cost more than the price table says, and both are
stored so every later estimate includes them:

* US-only inference (a workspace setting in the Anthropic Console) costs 1.1x
  on token prices. Ember never asks for it, but when a response reports it,
  estimates and costs use the multiplier from then on.
* If a call ever costs more than its worst-case estimate, the estimates for
  that model and purpose are scaled up (the safety factor), so the same mistake
  can't repeat. The factor is kept per mode (an overrun of the fake model in a
  dry run says nothing about the real API) and, since 0.12.0, per purpose: one
  research call's overrun raised every estimate of its model, planning
  included, until scheduled wake-ups stopped. A factor comes down by 0.05 after
  every 25 calls in a row that cost no more than the unscaled estimate, and
  the owner can reset them all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from ..config import Settings
from ..db import Database
from .estimate import Plan, worst_case_micros

US_INFERENCE_MULTIPLIER = Decimal("1.1")
MAX_SAFETY_FACTOR = Decimal(4)
SAFETY_DECAY = Decimal("0.05")  # taken off a raised factor ...
SAFETY_DECAY_AFTER = 25  # ... after this many accurate calls in a row
_GEO_KEY = "economy.inference_geo_us"
_SAFETY_PREFIX = "economy.safety."  # until 0.12.0: one factor per mode and model (no longer read)
_FACTOR_PREFIX = "economy.factor."  # 0.12.0: economy.factor.<mode>.<purpose>.<model>
_ACCURATE_PREFIX = "economy.factor_ok."  # the accurate calls in a row since the factor last changed


@dataclass(frozen=True)
class CallProfile:
    """A typical request of one kind: prompt size (tool definitions included), output limit and prompt caching (the
    worst case prices a cached prompt at the cache write rate); no server tools."""

    input_tokens: int
    max_tokens: int
    cache_ttls: tuple[str, ...] = ()


# The largest requests the agent builds (measured in tests/test_agent_requests.py, which keeps them in step):
# the planning call that opens a wake cycle (a venture cycle's, with its rules and the VENTURES section: 0.10.0, and
# the ROADMAP: 0.11.0; the owner's LIBRARY, the memory headings, the last cycles' digests, the OBLIGATIONS and the
# lessons the owner pinned: 0.12.0), and the last will. The agent never sends a bigger one.
PLANNER_OPENING = CallProfile(input_tokens=22_100, max_tokens=1_200)
LAST_WILL = CallProfile(input_tokens=6_400, max_tokens=1_000)
# The first work step with the largest brief, and the reflection after it (with the room the loop keeps for one
# step's growth), measured the same way: a wake cycle is only worth starting if both fit after its plan. Measured
# with the most tools (an ordinary cycle's with a mailbox's, a shop's and the library's too: a venture cycle carries
# fewer, 0.12.0) and the library's learnings (0.12.0: and the Etsy renewals, mark_opt_out, and the milestones'
# metrics, budgets, waits and milestone_plan; the OBLIGATIONS and obligation_done; the reflection with the most tool
# calls its cycle didn't do; the rules without what Ember's code enforces or another text already says; draft; the
# lessons the owner pinned).
WORK = CallProfile(input_tokens=31_500, max_tokens=2_000, cache_ttls=("5m",))
REFLECT = CallProfile(input_tokens=43_200, max_tokens=2_000, cache_ttls=("5m",))
# The daily review (0.7.1), measured the same way: the constitution, the knowledge, the review rules (with the
# venture tree's: 0.10.0, and the roadmap's: 0.11.0, with verdicts on milestones: 0.12.0) and a full scorecard.
REVIEW_CALL = CallProfile(input_tokens=14_900, max_tokens=2_200)
# The biggest first call of a workshop run (0.7.0), measured the same way: its rules, a task at its length limit and
# the most files handed over. It also has the code execution tool, priced with every code run and container time.
WORKSHOP_RUN = CallProfile(input_tokens=5_100, max_tokens=8_000, cache_ttls=("5m",))

# Models that always think (Anthropic's adaptive thinking can't be switched off for them, e.g. Claude Opus 5.5):
# their requests get adaptive thinking and this much more room for output, so the thinking can't crowd out the
# answer. Every other model is asked not to think.
ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable", "claude-mythos")
THINKING_ROOM = 4_000


def always_thinks(model: str) -> bool:
    return model.startswith(ALWAYS_THINKING)


def with_room(profile: CallProfile, model: str) -> CallProfile:
    """``profile`` as ``model`` sends it: with room for thinking if the model always thinks."""
    return replace(profile, max_tokens=profile.max_tokens + THINKING_ROOM) if always_thinks(model) else profile


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


def _safety_key(model: str, mode: str, purpose: str) -> str:
    return f"{_FACTOR_PREFIX}{mode}.{purpose}.{model}"


def _accurate_key(model: str, mode: str, purpose: str) -> str:
    return f"{_ACCURATE_PREFIX}{mode}.{purpose}.{model}"


def safety_factor(db: Database, model: str, mode: str = "live", purpose: str = "work") -> Decimal:
    """How much the worst-case estimates of ``model``'s ``purpose`` calls are scaled up (1: not at all)."""
    value = _decimal(db.get_meta(_safety_key(model, mode, purpose)), Decimal(1))
    return min(MAX_SAFETY_FACTOR, max(Decimal(1), value))


def raise_safety_factor(
    db: Database, model: str, actual: int, estimate: int, mode: str = "live", purpose: str = "work"
) -> Decimal:
    """After a call cost more than estimated, scale that model's estimates for that purpose up (10% margin)."""
    current = safety_factor(db, model, mode, purpose)
    needed = (Decimal(actual) / Decimal(max(estimate, 1)) * current * Decimal("1.1")).quantize(Decimal("0.01"))
    factor = min(MAX_SAFETY_FACTOR, max(current, needed))
    db.set_meta(_safety_key(model, mode, purpose), str(factor))
    db.set_meta(_accurate_key(model, mode, purpose), "0")
    return factor


def note_accurate_call(db: Database, model: str, mode: str = "live", purpose: str = "work") -> Decimal | None:
    """A call that cost no more than its unscaled estimate (0.12.0): after SAFETY_DECAY_AFTER of them in a row, a
    raised factor comes down by SAFETY_DECAY, never below 1. Returns the lowered factor, or None."""
    current = safety_factor(db, model, mode, purpose)
    if current <= 1:
        return None
    key = _accurate_key(model, mode, purpose)
    count = int(_decimal(db.get_meta(key), Decimal(0))) + 1
    if count < SAFETY_DECAY_AFTER:
        db.set_meta(key, str(count))
        return None
    lowered = max(Decimal(1), current - SAFETY_DECAY)
    db.set_meta(_safety_key(model, mode, purpose), str(lowered))
    db.set_meta(key, "0")
    return lowered


def raised_safety_factors(db: Database, mode: str) -> list[tuple[str, str, Decimal]]:
    """The factors above 1 in ``mode``: (purpose, model, factor), the highest first (0.12.0, for the dashboard)."""
    prefix = f"{_FACTOR_PREFIX}{mode}."
    with db.connection() as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    found = []
    for row in rows:
        purpose, _, model = row["key"][len(prefix) :].partition(".")
        factor = min(MAX_SAFETY_FACTOR, _decimal(row["value"], Decimal(1)))
        if model and factor > 1:
            found.append((purpose, model, factor))
    return sorted(found, key=lambda f: (-f[2], f[0], f[1]))


def reset_safety_factors(db: Database, mode: str) -> int:
    """The owner's reset (0.12.0): every estimate in ``mode`` unscaled again, the factors from before 0.12.0 included.
    Returns how many raised factors there were."""
    raised = len(raised_safety_factors(db, mode))
    with db.transaction() as conn:
        for prefix in (_FACTOR_PREFIX, _ACCURATE_PREFIX, _SAFETY_PREFIX):
            start = f"{prefix}{mode}."
            conn.execute("DELETE FROM meta WHERE substr(key, 1, ?) = ?", (len(start), start))
    return raised


def profile_cost(
    settings: Settings,
    db: Database,
    model: str,
    profile: CallProfile,
    mode: str = "live",
    *,
    purpose: str = "work",
    code_execution: bool = False,
) -> int | None:
    """Worst-case micros of one ``profile`` call on ``model`` for ``purpose``; None if the model has no price."""
    price = settings.price_for(model)
    if price is None:
        return None
    plan = Plan(
        model=model,
        input_tokens=profile.input_tokens,
        max_output_tokens=profile.max_tokens,
        cache_ttls=profile.cache_ttls,
        code_execution=code_execution,
    )
    estimate = worst_case_micros(
        plan, price, settings.web_search_usd_per_1000, geo_multiplier(db), settings.code_execution_usd_per_hour
    )
    factor = safety_factor(db, model, mode, purpose)
    return int((Decimal(estimate) * factor).to_integral_value(rounding=ROUND_CEILING))


def opening_cost(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """What the planning call that opens a wake cycle can cost at most."""
    model = settings.planner_model
    return profile_cost(settings, db, model, with_room(PLANNER_OPENING, model), mode, purpose="plan")


def working_cycle_cost(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """What a wake cycle needs to do any work: the worst case of its plan, one work step and the reflection."""
    worker = settings.worker_model
    costs = [
        opening_cost(settings, db, mode),
        profile_cost(settings, db, worker, with_room(WORK, worker), mode, purpose="work"),
        profile_cost(settings, db, worker, with_room(REFLECT, worker), mode, purpose="reflect"),
    ]
    return None if None in costs else sum(c for c in costs if c is not None)


def workshop_run_cost(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """What the first call of a workshop run can cost at most, on the workshop's model (the worker's if none is set)."""
    model = settings.workshop_model or settings.worker_model
    run = with_room(WORKSHOP_RUN, model)
    return profile_cost(settings, db, model, run, mode, purpose="workshop", code_execution=True)


def last_will_reserve(settings: Settings, db: Database, mode: str = "live") -> int | None:
    """Money held back so the agent can always write its last will."""
    worker = settings.worker_model
    return profile_cost(settings, db, worker, with_room(LAST_WILL, worker), mode, purpose="last_will")
