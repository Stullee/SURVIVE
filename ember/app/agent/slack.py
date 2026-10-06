"""Waiting time (0.18.0, vision/learning.md part 7): useful work while projects wait.

Live, the agent slept 12 hours while its products waited for buyers and its requests for the owner, and 8 of 12 cycles
were started by the owner's Wake now. Work already done is paid for; waiting on it earns nothing. Now an ordinary
cycle's plan gets READY (the list a venture cycle's plan gets from the decision desk) with what Ember's code finds
worth doing meanwhile, the most pressing first:

* reach: a product line its buyers haven't seen (reach.py: not seen, less than reach.ENOUGH done to bring them);
* improve: what the quality critic's last check of each listing said to fix (quality.py, 0.24.0: the listing named);
* research: a product line live without a demand note;
* question: what this week's look asked the week to answer (weekly.py).

While READY lists something, the sleep a cycle chooses is cut to SLEEP_MINUTES (but not below the owner's shortest
sleep), unless the burn mode is maintenance or dormant: a cycle that could do something useful doesn't sleep half a
day.

0.21.0 (analysis 0.20.1, FIX NOW 5): only a cycle that worked has its sleep cut, only for work (this week's questions
are always in READY for 7 days: every cycle's sleep was cut, "Nothing until 10-07." too, up to 8 paid plans a day), and
never below the owner's default interval (wake_interval_minutes), so raising it slows Ember down again.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from . import quality, reach, weekly
from .store import AgentScope

SLEEP_MINUTES = 180
QUESTION = "question"  # an item's key: what this week's look asked (not work that cuts a sleep)
MAX_ITEMS = 6
HEADING = "Useful while your projects wait, ranked by Ember's code (work on one when nothing more pressing is due):"


@dataclass(frozen=True)
class Item:
    key: str  # "reach #3"
    text: str


def items(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[Item]:
    where, params = scope.where()
    found: list[Item] = []
    funnels = reach.funnels(conn, scope)
    for project_id, f in sorted(funnels.items()):
        if f.listings and f.stage == "not_seen" and f.reach < reach.ENOUGH:
            found.append(
                Item(
                    f"reach #{project_id}",
                    f"bring buyers to project #{project_id}'s listings ({f.views} views, {f.reach} of {reach.ENOUGH}"
                    " reach actions): a blog post that recommends them, pins, better titles and tags",
                )
            )
    for project_id in sorted(funnels):
        # 0.24.0: each listing's newest check, with the listing it judged (a check from before names none)
        checked = quality.newest(conn, scope, project_id) or quality.unplaced(conn, scope, project_id)
        for row in checked:  # one changed at Etsy since its check waits for the next check (quality.due)
            if row["verdict"] == "improve" and row["fixes"] and not quality.changed_since(conn, scope, row):
                judged = quality.label(conn, scope, row["listing_id"])
                found.append(
                    Item(f"improve #{project_id}", f"the quality critic said of {judged}: {row['fixes'][:200]}")
                )
    for project_id, f in sorted(funnels.items()):
        note = conn.execute(
            f"SELECT 1 FROM demand_notes WHERE {where} AND project_id = ? LIMIT 1", (*params, project_id)
        ).fetchone()
        if f.listings and note is None:
            found.append(Item(f"research #{project_id}", "no demand note: what buyers search, buy and complain about"))
    look = weekly.latest(conn, scope, today)
    if look is not None:
        try:
            asked = json.loads(look["answer"] or "{}").get("questions") or []
        except ValueError:
            asked = []
        found += [Item(f"{QUESTION} {i}", str(q)[:200]) for i, q in enumerate(asked, 1)]
    return found[:MAX_ITEMS]


def text(found: list[Item]) -> str:
    if not found:
        return ""
    return "\n".join([HEADING, *(f"- {i.key}: {i.text}" for i in found)])


def sleep(minutes: int | None, found: list[Item], shortest: int, burn_mode: str) -> int | None:
    """The sleep a cycle that worked keeps: cut to SLEEP_MINUTES, or ``shortest`` if longer, while READY lists work
    (not in maintenance or dormant). 0.21.0: this week's questions aren't work to cut a sleep for."""
    work = [i for i in found if not i.key.startswith(QUESTION)]
    if minutes is None or not work or burn_mode in ("maintenance", "dormant"):
        return minutes
    return min(minutes, max(SLEEP_MINUTES, shortest))
