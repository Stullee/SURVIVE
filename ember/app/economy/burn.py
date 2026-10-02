"""Burn modes (0.12.0): how fast Ember may spend, set by Ember's code from the net runway (how long the balance lasts at
the last week's API spending less its net revenue), not by a line in the prompt ("the daily cap is there to be spent"
steered a whole live week).

* explore: more than 30 days of net runway, or it earns at least what it spends: as the owner's options allow;
* focus: 15 to 30 days: the tests already running go on (a venture backed or live), no brainstorms, and new ideas
  only those the owner brings (0.15.0: venture_create stays offered for them);
* maintenance: under 15 days: one scheduled cycle a day of at most $0.40 (0.15.0: every call in it counted, the
  daily review and the library's study too), with no workshop runs and no venture cycles;
* dormant: the last will is written and the runway is critical: no model calls until money comes in (only the owner's
  Wake now runs a cycle). The Etsy sync goes on, so a sale is still read and recorded.

A mode moves down at once and up only once the net runway is 20% past the threshold, so it doesn't flicker around one;
every change is in the System log. The runway counts gross API charges (FIX NOW 30), so a refund can't flip the mode.
0.15.0: at today's burn the net runway shrinks by a day a day, so STATUS and the dashboard say when the mode moves
down next (``projected``); it moves up only when money comes in.

0.16.2: until then it moved up whenever the net runway was past the margin, and a lower mode spends less, so the week's
spending fell and the runway grew past it within days: on the owner's ledger maintenance went back to focus and full
spending two days after it began, and flipped 11 more times before critical. A mode below explore now moves up only
once money came in since it began (a grant, revenue or an adjustment that adds, as the critical state ends), judged at
the API spending of the week before it moved down when that was more (``Since``, ``judged``), so a small sale doesn't
buy back a week of full spending either.

0.18.0: the owner's ``spending_stance`` decides how far a shrinking runway takes the mode. Narrowing what the agent may
do as its runway shrank was backwards: when what it does isn't working, it needs new ideas more, not fewer.

* invest (the default): explore whatever the runway, until the last will (dormant); the owner's caps are the only
  limits. Under MAINTENANCE_DAYS of net runway STATUS tells the agent to go for the fastest path to a first euro
  (``FIGHT``) and the System log warns the owner once (``WARNED_KEY``), so they decide: a grant or another stance;
* steady: explore, and focus under EXPLORE_DAYS; never maintenance;
* conserve: the modes above, as they were from 0.12.0 to 0.17.0.

A mode kept below what the stance allows (the owner changed it, or the upgrade to 0.18.0) moves up to it at once:
the owner's choice counts like money coming in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import events
from ..db import Database
from .life import RUNWAY_CAP_DAYS, LifeStatus

EXPLORE, FOCUS, MAINTENANCE, DORMANT = "explore", "focus", "maintenance", "dormant"
INVEST, STEADY, CONSERVE = "invest", "steady", "conserve"  # 0.18.0: the owner's spending_stance
FLOOR = {INVEST: EXPLORE, STEADY: FOCUS, CONSERVE: MAINTENANCE}  # the lowest mode a stance sets (dormant aside)
MODES = (DORMANT, MAINTENANCE, FOCUS, EXPLORE)  # the lowest first
EXPLORE_DAYS = 30.0  # of net runway, and more: explore
MAINTENANCE_DAYS = 15.0  # and less: maintenance
MARGIN = 1.2  # a mode moves up only this far past its threshold
MAINTENANCE_CYCLE_USD = 0.40  # a maintenance cycle's cap
MAINTENANCE_SLEEP_MINUTES = 24 * 60  # one scheduled cycle a day
PROJECTED_DAYS = 30  # 0.15.0: a change further off than this isn't projected
KEY = "burn_mode.{mode}.{life}"
WARNED_KEY = "burn_mode_warned.{mode}.{life}"  # 0.18.0: invest's warning to the owner under MAINTENANCE_DAYS
FIGHT = (
    "under {days:.0f} days of net runway: go for the fastest honest path to a first euro, and stop what has evidence"
    " against it"
)
SINCE_KEY = "burn_mode_since.{mode}.{life}"  # 0.16.2: where the mode below explore began (Since), "" in explore
MEANING = {
    EXPLORE: "as your owner's options allow",
    FOCUS: "finish the tests already running (a venture backed or live); no brainstorms, new ideas only your owner's",
    # 0.15.0: the day counts from the last cycle of any kind (service.Agent._maintenance_day)
    MAINTENANCE: (
        f"one cycle a day of at most ${MAINTENANCE_CYCLE_USD:.2f}, every call counted, no workshop runs or venture"
        " cycles (your owner's or an event's wake-up starts a new day): earn or cut costs"
    ),
    DORMANT: "no model calls until money comes in (a sale or your owner's grant)",
}


@dataclass(frozen=True)
class Since:
    """0.16.2: where a mode below explore began (kept by ``current``): the newest ledger row that had brought money in
    by then (``LifeStatus.money_in_id``), and the API spending a day of the week before it moved down, in micros (the
    most since the mode was last explore: after a short move up, the week before the next move down holds days of the
    lower mode's spending too)."""

    money_in: int
    spend: int


@dataclass(frozen=True)
class Burn:
    mode: str
    net_days: float | None
    held: str = ""  # 0.16.2: why the mode stays below what the net runway alone would make it ("" if it doesn't)
    stance: str = INVEST  # 0.18.0: the owner's spending_stance

    @property
    def venture_cycles(self) -> bool:
        """Whether venture cycles run (in focus, only while a venture is backed or live)."""
        return self.mode in (EXPLORE, FOCUS)

    @property
    def brainstorms(self) -> bool:
        return self.mode == EXPLORE

    @property
    def workshop(self) -> bool:
        """0.15.0: whether workshop runs are offered: not in maintenance (one costs about what a whole cycle may)."""
        return self.mode != MAINTENANCE

    def cycle_cap(self, cap_micros: int) -> int:
        return min(cap_micros, int(MAINTENANCE_CYCLE_USD * 1_000_000)) if self.mode == MAINTENANCE else cap_micros

    @property
    def fight(self) -> str:
        """0.18.0: invest's note under MAINTENANCE_DAYS of net runway, or ""."""
        short = self.net_days is not None and self.net_days < MAINTENANCE_DAYS
        return FIGHT.format(days=MAINTENANCE_DAYS) if self.stance == INVEST and self.mode == EXPLORE and short else ""

    def text(self) -> str:
        runway = "earning at least what it spends" if self.net_days is None else f"{self.net_days:.1f} days"
        held = f"; {self.held}" if self.held else ""
        fight = f"; {self.fight}" if self.fight else ""
        return f"{self.mode} (net runway: {runway}{held}): {MEANING[self.mode]}{fight}"


def projected(burn: Burn, now: datetime) -> tuple[str, datetime] | None:
    """0.15.0: the next mode at today's burn and about when it comes (``now`` plus the net runway's days past the
    threshold below), or None: none within PROJECTED_DAYS, or the net runway doesn't shrink (it earns what it
    spends). A mode moves up only when money comes in, and dormant waits for the last will, so only explore and focus
    have one."""
    if burn.net_days is None or burn.mode not in (EXPLORE, FOCUS):
        return None
    lower, floor = (FOCUS, EXPLORE_DAYS) if burn.mode == EXPLORE else (MAINTENANCE, MAINTENANCE_DAYS)
    if MODES.index(lower) < MODES.index(FLOOR.get(burn.stance, EXPLORE)):
        return None  # 0.18.0: the owner's stance never sets it
    days = max(0.0, burn.net_days - floor)
    return (lower, now + timedelta(days=days)) if days <= PROJECTED_DAYS else None


def projected_text(burn: Burn, now: datetime) -> str:
    """The projection as STATUS and the dashboard say it ("maintenance from about 10-03 at today's burn"), or ""."""
    found = projected(burn, now)
    return f"{found[0]} from about {found[1]:%m-%d} at today's burn" if found else ""


def _floor(status: LifeStatus) -> str:
    """0.18.0: the lowest mode the owner's stance sets (an unknown stance counts as invest, the default)."""
    return FLOOR.get(status.stance, EXPLORE)


def _raw(status: LifeStatus) -> str:
    net = status.runway.net_days
    if status.last_will_at is not None and status.critical:
        return DORMANT
    mode = EXPLORE if net is None or net > EXPLORE_DAYS else FOCUS if net > MAINTENANCE_DAYS else MAINTENANCE
    floor = _floor(status)
    return floor if MODES.index(mode) < MODES.index(floor) else mode


def _spend(status: LifeStatus) -> int:
    """The API spending a day of the runway's week, in micros (0 without any)."""
    runway = status.runway
    return int(runway.window_spend / runway.active_days) if runway.active_days > 0 else 0


def judged(status: LifeStatus, since: Since | None) -> float | None:
    """0.16.2: the net runway a move up from the mode that began at ``since`` is judged by: at the API spending of the
    week before it moved down when that was more than now's (a lower mode spends less, and that alone made the runway
    look long), less the revenue and expenses of the runway's week; None while that earns at least what it spends."""
    runway = status.runway
    if since is None or since.spend <= _spend(status):
        return runway.net_days
    rate = since.spend - (runway.window_net_in / runway.active_days if runway.active_days > 0 else 0)
    return min(max(0, status.balance) / rate, RUNWAY_CAP_DAYS) if rate > 0 else None


def settle(previous: str | None, status: LifeStatus, since: Since | None = None) -> str:
    """The mode now, given the mode before (``previous``): down at once, up only MARGIN past a threshold, and (0.16.2)
    only once money came in since the mode before began (``since``), judged at the spending from before it moved down
    (``judged``)."""
    return _settle(previous, status, since)[0]


def _settle(previous: str | None, status: LifeStatus, since: Since | None) -> tuple[str, str]:
    """(``settle``'s mode, why it stays below what the net runway alone would make it, or "")."""
    target = _raw(status)
    floor = _floor(status)
    if previous in MODES and previous != DORMANT and MODES.index(previous) < MODES.index(floor):
        previous = floor  # 0.18.0: the owner's stance lifts a mode kept below it at once
    if previous not in MODES or DORMANT in (target, previous) or MODES.index(target) <= MODES.index(previous):
        return target, ""
    up = str(previous)
    if since is None or status.money_in_id <= since.money_in:  # (a mode kept before 0.16.2 begins at its next check)
        return up, "up again only once money comes in"
    net = judged(status, since)
    if net is None:
        return target, ""
    for mode, floor in ((FOCUS, MAINTENANCE_DAYS), (EXPLORE, EXPLORE_DAYS)):
        if MODES.index(up) < MODES.index(mode) <= MODES.index(target) and net > floor * MARGIN:
            up = mode
    if up != target and since.spend > _spend(status):
        return up, f"{net:.1f} days at the spending from before it moved down"
    return up, ""


def _since(db: Database, status: LifeStatus) -> Since | None:
    value = db.get_meta(SINCE_KEY.format(mode=status.mode, life=status.life_id)) or ""
    try:
        money_in, spend = (int(part) for part in value.split())
    except ValueError:
        return None
    return Since(money_in, spend)


def _burn(previous: str | None, status: LifeStatus, since: Since | None) -> Burn:
    mode, held = _settle(previous, status, since)
    return Burn(mode, status.runway.net_days, held, status.stance if status.stance in FLOOR else INVEST)


def peek(db: Database, status: LifeStatus) -> Burn:
    """The mode now, without keeping it (the dashboard)."""
    previous = db.get_meta(KEY.format(mode=status.mode, life=status.life_id))
    return _burn(previous, status, _since(db, status))


def current(db: Database, status: LifeStatus) -> Burn:
    """The mode now, kept; a change is in the System log. 0.16.2: so is where a mode below explore began (``Since``):
    the money in by then, and the most API spending a day since the mode was last explore."""
    key = KEY.format(mode=status.mode, life=status.life_id)
    previous = db.get_meta(key)
    since = _since(db, status)
    burn = _burn(previous, status, since)
    since_key = SINCE_KEY.format(mode=status.mode, life=status.life_id)
    if burn.mode == EXPLORE:
        if since is not None:
            db.set_meta(since_key, "")
    elif burn.mode != previous or since is None:
        spend = max(_spend(status), since.spend if since is not None else 0)
        db.set_meta(since_key, f"{status.money_in_id} {spend}")
    if burn.mode != previous:
        db.set_meta(key, burn.mode)
        if previous is not None:
            lower = MODES.index(burn.mode) < MODES.index(previous) if previous in MODES else False
            events.record(db, "warning" if lower else "info", "economy", f"Burn mode: {burn.text()}"[:300])
    _warn(db, status, burn)
    return burn


def _warn(db: Database, status: LifeStatus, burn: Burn) -> None:
    """0.18.0: under invest, the owner hears once when the net runway falls under MAINTENANCE_DAYS (again after it was
    MARGIN past it): the code no longer cuts the spending, so the owner decides."""
    key = WARNED_KEY.format(mode=status.mode, life=status.life_id)
    warned = db.get_meta(key) == "1"
    if burn.fight and not warned:
        db.set_meta(key, "1")
        events.record(
            db,
            "warning",
            "economy",
            f"Net runway {burn.net_days:.1f} days: your spending stance is invest, so Ember keeps exploring at your"
            " caps. Add a grant, or choose steady or conserve in the options to spend less.",
        )
    elif warned and (burn.net_days is None or burn.net_days > MAINTENANCE_DAYS * MARGIN):
        db.set_meta(key, "")
