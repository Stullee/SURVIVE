"""Burn modes (0.12.0): how fast Ember may spend, set by Ember's code from the net runway (how long the balance lasts at
the last week's API spending less its net revenue), not by a line in the prompt ("the daily cap is there to be spent"
steered a whole live week).

* explore: more than 30 days of net runway, or it earns at least what it spends: as the owner's options allow;
* focus: 15 to 30 days: the tests already running go on (a venture backed or live), no brainstorms, no new ideas;
* maintenance: under 15 days: one scheduled cycle a day of at most $0.40, and no venture cycles;
* dormant: the last will is written and the runway is critical: no model calls until money comes in (only the owner's
  Wake now runs a cycle). The Etsy sync goes on, so a sale is still read and recorded.

A mode moves down at once and up only once the net runway is 20% past the threshold, so it doesn't flicker around one;
every change is in the System log. The runway counts gross API charges (FIX NOW 30), so a refund can't flip the mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import events
from ..db import Database
from .life import LifeStatus

EXPLORE, FOCUS, MAINTENANCE, DORMANT = "explore", "focus", "maintenance", "dormant"
MODES = (DORMANT, MAINTENANCE, FOCUS, EXPLORE)  # the lowest first
EXPLORE_DAYS = 30.0  # of net runway, and more: explore
MAINTENANCE_DAYS = 15.0  # and less: maintenance
MARGIN = 1.2  # a mode moves up only this far past its threshold
MAINTENANCE_CYCLE_USD = 0.40  # a maintenance cycle's cap
MAINTENANCE_SLEEP_MINUTES = 24 * 60  # one scheduled cycle a day
KEY = "burn_mode.{mode}.{life}"
MEANING = {
    EXPLORE: "as your owner's options allow",
    FOCUS: "finish the tests already running (a venture backed or live); no brainstorms, no new ideas",
    # 0.14.0: the day counts from the last cycle of any kind (service.Agent._maintenance_day)
    MAINTENANCE: (
        f"one cycle a day of at most ${MAINTENANCE_CYCLE_USD:.2f} (your owner's or an event's wake-up starts a new"
        " day), no venture cycles: earn or cut costs"
    ),
    DORMANT: "no model calls until money comes in (a sale or your owner's grant)",
}


@dataclass(frozen=True)
class Burn:
    mode: str
    net_days: float | None

    @property
    def venture_cycles(self) -> bool:
        """Whether venture cycles run (in focus, only while a venture is backed or live)."""
        return self.mode in (EXPLORE, FOCUS)

    @property
    def brainstorms(self) -> bool:
        return self.mode == EXPLORE

    def cycle_cap(self, cap_micros: int) -> int:
        return min(cap_micros, int(MAINTENANCE_CYCLE_USD * 1_000_000)) if self.mode == MAINTENANCE else cap_micros

    def text(self) -> str:
        runway = "earning at least what it spends" if self.net_days is None else f"{self.net_days:.1f} days"
        return f"{self.mode} (net runway: {runway}): {MEANING[self.mode]}"


def _raw(status: LifeStatus) -> str:
    net = status.runway.net_days
    if status.last_will_at is not None and status.critical:
        return DORMANT
    if net is None or net > EXPLORE_DAYS:
        return EXPLORE
    return FOCUS if net > MAINTENANCE_DAYS else MAINTENANCE


def settle(previous: str | None, status: LifeStatus) -> str:
    """The mode now, given the mode before (``previous``): down at once, up only MARGIN past a threshold."""
    target = _raw(status)
    if previous not in MODES or DORMANT in (target, previous) or MODES.index(target) <= MODES.index(previous):
        return target
    net = status.runway.net_days
    if net is None:
        return target
    up = str(previous)
    for mode, floor in ((FOCUS, MAINTENANCE_DAYS), (EXPLORE, EXPLORE_DAYS)):
        if MODES.index(up) < MODES.index(mode) <= MODES.index(target) and net > floor * MARGIN:
            up = mode
    return up


def peek(db: Database, status: LifeStatus) -> Burn:
    """The mode now, without keeping it (the dashboard)."""
    previous = db.get_meta(KEY.format(mode=status.mode, life=status.life_id))
    return Burn(settle(previous, status), status.runway.net_days)


def current(db: Database, status: LifeStatus) -> Burn:
    """The mode now, kept; a change is in the System log."""
    key = KEY.format(mode=status.mode, life=status.life_id)
    previous = db.get_meta(key)
    burn = Burn(settle(previous, status), status.runway.net_days)
    if burn.mode != previous:
        db.set_meta(key, burn.mode)
        if previous is not None:
            lower = MODES.index(burn.mode) < MODES.index(previous) if previous in MODES else False
            events.record(db, "warning" if lower else "info", "economy", f"Burn mode: {burn.text()}"[:300])
    return burn
