"""0.42.2 (analysis-0.40.1, 5.6): the step a cycle takes, split out of plan.py.

``steer`` takes each cycle's step before the plan (``choose``: weights.choose over the candidates, with the streak of
the cycles before, ``history``), and so what the cycle is; ``record`` keeps its pick. ``busy`` says whether the plan
had a step a cycle can advance, which cuts Ember's sleep (slack.py; loop.py calls it through plan, where tests patch
it). ``settings`` lists the numbers the steps are weighed and chosen with, for the report.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..economy.clock import from_iso
from . import obligations, templates, weights
from .plan_model import (
    CLICKS_PER_PIN,
    FRESH_PINS,
    FRESH_POSTS,
    HISTORY,
    OBLIGATION_HOURS,
    PROMISE_TRIES,
    RESULT_DAYS,
    VIEWS_PER_POST,
    Facts,
    _midnight,
    _spec,
    _update,
    node,
    nodes,
)
from .plan_ready import Candidate, _limited, _made_in, candidates, limits, mark
from .plan_ventures import VENTURE_TEMPLATES, venture_of
from .store import AgentScope


def busy(conn: sqlite3.Connection, scope: AgentScope, steered: Steer, now: str, cycle_id: int | None = None) -> bool:
    """Whether a cycle's plan had work worth the owner's shortest sleep (slack.sleep): a step ready when it began that a
    cycle can advance. The cycle's own step (``cycle_id``: the cycle) counts only if something moved for it during the
    cycle (otherwise it waits now); another only if something moved since a cycle last took it, or none did (ready
    again the next day, a step that moved nothing gets its try without cutting a sleep). Live on 2026-10-09, a step no
    cycle could advance cut every sleep to 30 minutes until the daily cap was spent. 0.37.9: none still open that waits
    on a limit of the owner's as the cycle ends (``limits``): its own request or message may have used it up, and the
    tool would refuse the next cycle's (a step waiting on one when the cycle began was no candidate). One the cycle
    closed waits on nothing: the steps after it may be ready now. 0.39.6: only work (``_works``): not a chore, a
    report, nor a step of a channel at its floor, unless something makes it urgent. 0.40.2: nor a step of the
    ventures (a brainstorm, a triage), unless it is urgent."""
    tried = {c.step.id for c in steered.found if c.tried}
    own = steered.step.id if steered.step is not None else None
    facts = Facts(conn, scope, now)
    found = limits(conn, scope, steered.day_start or _midnight(from_iso(now).date()), now)
    for step, parts in steered.pick.ranked:
        row = node(conn, scope, step.id)
        if not _works(parts, venture=row is not None and row["template"] in VENTURE_TEMPLATES):
            continue
        if row is not None and row["status"] == "open" and _limited(facts, row, found) is not None:
            continue
        if step.id == own:
            if steered.mark is None or row is None or mark(facts, row) != steered.mark:
                return True
            if cycle_id is not None and _made_in(conn, cycle_id, row):
                return True
        elif step.id not in tried:
            return True
    return False


def _works(parts: weights.Parts, venture: bool = False) -> bool:
    """0.39.6 (analysis-0.39.5, 4.2.1): whether a ready step is work worth cutting the agent's sleep for (``busy``):
    urgent (a promise or decision near its day, a defect, a missed views bar, a launch's first marketing, a step due
    today), or of a kind that makes or ships (not a chore or a report) on a channel above its floor. Live on 2026-10-15
    every sleep the agent chose (mostly 4 to 6 hours) was cut to 30 minutes: 16 cycles from 00:07 to 07:55 spent $5.59
    of the $7 cap, five of them on the critic's chores for listings 1 to 3 people had seen and one on a views
    baseline, and the owner's decision at 07:48 waited for 20:00. 0.40.2 (analysis-0.40.1, 3.4): a step of the
    ventures (``venture``: a brainstorm, a triage, an appraisal) only when urgent (the owner's wish, a park within
    days). Live on 2026-10-21 a brainstorm and a triage cut every sleep to 30 minutes: 6 ideas, all 6 parked, and the
    next brainstorm ready at once."""
    if parts.urgency > 0:
        return True
    if venture:
        return False
    # 0.39.8 (analysis-0.39.6, 3.4): a channel clearly above its floor (weights.CHANNEL_WORKS): Bluesky at 0.21 cut
    # 7 of 12 sleeps on 10-16 with a listing view for every five posts
    return parts.kind >= weights.KIND["create"] and parts.channel >= weights.CHANNEL_WORKS


def history(conn: sqlite3.Connection, scope: AgentScope, before: int | None = None) -> list[int | None]:
    """The product lines of the latest cycles, newest first (for the streak)."""
    rows = conn.execute(
        "SELECT project_id FROM cycles WHERE session = ? AND simulated = ? AND (? IS NULL OR id < ?)"
        " ORDER BY id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, before, before, HISTORY),
    ).fetchall()
    return [r["project_id"] for r in rows]


def choose(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
    before: int | None = None,
    *,
    day_start: str | None = None,
) -> tuple[weights.Pick, list[Candidate]]:
    found = candidates(conn, scope, now, today, channels, exploring, before, day_start=day_start)
    last, streak = weights.streak_of(history(conn, scope, before))
    return weights.choose([c.step for c in found], last, streak), found


def settings() -> dict[str, Any]:
    """0.35.2: the numbers a cycle's step is chosen with, for the diagnostics: weights.py's, and this module's that
    feed them (a channel's factor, the cycles read for the streak, how often a promise or a decision comes first).
    0.39.1: and the critic's, which decide whether its suggestions weigh as a chore."""
    from . import quality  # here, not at the top: quality imports prompts, which imports tools, then this

    return {
        "weights.py": weights.settings(),
        "plan.py": {
            "FRESH_PINS": FRESH_PINS,
            "FRESH_POSTS": FRESH_POSTS,
            "RESULT_DAYS": RESULT_DAYS,  # 0.35.3
            "CLICKS_PER_PIN": CLICKS_PER_PIN,
            "VIEWS_PER_POST": VIEWS_PER_POST,
            "HISTORY": HISTORY,
            "OBLIGATION_HOURS": OBLIGATION_HOURS,
            "PROMISE_TRIES": PROMISE_TRIES,
        },
        "quality.py": {"MIN_PASS": quality.MIN_PASS, "STUCK_CHECKS": quality.STUCK_CHECKS},  # 0.39.1
    }


@dataclass(frozen=True)
class Steer:
    """0.35.0: what the tree decided for a cycle: its step (None when nothing is ready), what the cycle is ('ordinary',
    'marketing' or 'venture', 0.37.0: for a step of the ventures: lines.py's kinds), every candidate it was chosen
    from, and (0.37.0) the venture a venture step is about (None: a brainstorm, or another kind of cycle). 0.37.6: what
    the step's check read when the cycle took it (``mark``) and the owner's day (``day``), kept with the pick. 0.37.9:
    when that day began (``day_start``), from which ``busy`` counts the day's messages."""

    pick: weights.Pick
    found: tuple[Candidate, ...]
    kind: str
    venture: int | None = None
    mark: str | None = None
    day: str | None = None
    day_start: str | None = None

    @property
    def step(self) -> weights.Step | None:
        return self.pick.step

    @property
    def line(self) -> int | None:
        return self.pick.step.product if self.pick.step is not None else None


def steer(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    *,
    exploring: bool = False,
    cycle_id: int | None = None,
    day_start: str | None = None,
) -> Steer:
    """The step a cycle takes (weights.choose: a pin, else the heaviest step; 0.37.0: a promise or decision of the
    owner's weighed like any) and so what the cycle is: a venture cycle for a step of the ventures (0.37.0: aimed at
    its venture; 0.36.0: the Explore step, in place of the ventures' turn; ``exploring``: whether this cycle may
    explore), a marketing cycle for a marketing step, else an ordinary one (also when no step is ready: it may start
    a new product, or end). 0.37.9: ``day_start``, when the owner's day began (their clock's: ``limits``)."""
    day_start = day_start or _midnight(today)
    pick, found = choose(conn, scope, now, today, channels, exploring=exploring, before=cycle_id, day_start=day_start)
    row = node(conn, scope, pick.step.id) if pick.step is not None else None
    taken = {
        "mark": mark(Facts(conn, scope, now), row) if row is not None else None,
        "day": today.isoformat(),
        "day_start": day_start,
    }
    if row is not None and row["template"] in VENTURE_TEMPLATES:
        return Steer(pick, tuple(found), "venture", venture_of(conn, scope, int(row["id"])), **taken)
    if pick.step is not None and (pick.step.kind == "market" or _markets(row)):
        return Steer(pick, tuple(found), "marketing", **taken)
    return Steer(pick, tuple(found), "ordinary", **taken)


def on_channel(conn: sqlite3.Connection, scope: AgentScope, step: weights.Step | None) -> bool:
    """0.35.0: whether a step is one of a channel's own product (its setup, in an ordinary cycle that keeps the
    channel's section and tools)."""
    if step is None or step.product is None:
        return False
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (step.product,))
    return bool(found) and templates.by_key(found[0]["template"]).name == "channel"


def _markets(row: Mapping[str, Any] | None) -> bool:
    """Whether a step's check needs the marketing tools (only a marketing cycle carries them): a pin, a post or a blog
    post, or a request a marketing cycle makes (obligations.MARKETING_EXECUTORS: a website product's post)."""
    if row is None:
        return False
    if row["check_kind"] in ("pin", "post", "blog"):
        return True
    if row["kind"] == "promise" and row["channel"]:  # 0.35.1: a promise of pins, a post or a blog post
        return True
    return row["check_kind"] == "request" and _spec(row).get("executor") in obligations.MARKETING_EXECUTORS


def record(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, steered: Steer) -> None:
    """A cycle's pick, with its weight's parts and the ranking it came from, so a week can be re-scored offline; 0.37.6:
    and what its step's check read then, on the owner's day (the brake: ``candidates``)."""
    logged = steered.pick.json()
    conn.execute(
        "INSERT INTO plan_picks (mode, session, cycle_id, created_at, kind, line, node_id, product, decided, weight,"
        " parts, ranked, mark, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            now,
            steered.kind,
            steered.line,
            logged["step"],
            logged["product"],
            steered.pick.decided,
            logged["weight"],
            json.dumps(logged["parts"]) if logged["parts"] else None,
            json.dumps(logged["ranked"], ensure_ascii=False),
            steered.mark if steered.step is not None else None,
            steered.day,
        ),
    )
    step = steered.step
    if step is not None and step.pinned and steered.kind == "venture":
        _update(conn, step.id, now, pinned=0)  # 0.36.0: the owner's Explore next is one venture cycle (it never closes)
