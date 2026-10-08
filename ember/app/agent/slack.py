"""Waiting time (0.18.0, vision/learning.md part 7): useful work while projects wait.

Live, the agent slept 12 hours while its products waited for buyers and its requests for the owner, and 8 of 12 cycles
were started by the owner's Wake now. Work already done is paid for; waiting on it earns nothing. An ordinary cycle's
plan got READY (the list a venture cycle's plan gets from the decision desk) with what Ember's code found worth doing
meanwhile: bringing buyers to a line nobody had seen, the quality critic's fixes, a missing demand note, this week's
questions. 0.28.0: READY is the product lines' list a plan takes its one line from (lines.py), and a marketing cycle
brings the buyers; what is left here is the sleep.

While READY lists a concrete job (lines.Item.job: what a line owes, its milestone due, the critic's fixes, a missing
demand note, buyers to bring to a line nobody saw; not a line only waiting for the owner, a new line, or this week's
questions), the sleep a cycle chooses is cut to SLEEP_MINUTES (but not below the owner's shortest sleep), unless the
burn mode is maintenance or dormant: a cycle that could do something useful doesn't sleep half a day.

0.21.0 (analysis 0.20.1, FIX NOW 5): only a cycle that worked has its sleep cut, only for work (this week's questions
are always in READY for 7 days: every cycle's sleep was cut, "Nothing until 10-07." too, up to 8 paid plans a day), and
never below the owner's default interval (wake_interval_minutes), so raising it slows Ember down again.

0.35.0: the plan tree takes each cycle's step (plan.py): an ordinary or marketing cycle whose plan had steps ready is
busy, whatever else waits on the owner.
"""

from __future__ import annotations

SLEEP_MINUTES = 180
WHY = "your plan has steps ready"  # why Ember's code cut the sleep, as the System log and the dashboard say


def sleep(minutes: int | None, busy: bool, shortest: int, burn_mode: str) -> int | None:
    """The sleep a cycle that worked keeps: cut to SLEEP_MINUTES, or ``shortest`` if longer, while ``busy`` (its plan
    had steps ready; not in maintenance or dormant)."""
    if minutes is None or not busy or burn_mode in ("maintenance", "dormant"):
        return minutes
    return min(minutes, max(SLEEP_MINUTES, shortest))
