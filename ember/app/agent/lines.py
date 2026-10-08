"""One thing a cycle (0.28.0): what a wake cycle is, and what its work steps are told of the product line it works on.

An ordinary cycle worked on several product lines, an unbacked venture and marketing at once: its rules said "keep
2-3 experiments in flight ... when a project waits, work on another", its plan named a project, a venture and the
roadmap's milestone due first apart, and its tools took any project. Live, a cycle on venture #12 listed #12's licence
bundle in the cover-letter line, files were filed under the wrong project, and one line's cost counted for another.

Now every wake cycle is about one thing:

* an ordinary cycle works on one product line: build it, fix it, research it, scale it;
* a marketing cycle brings buyers to one line's listings: pins, Bluesky posts, a blog post, the link page, a Reddit
  post, titles and tags;
* a venture cycle decides one venture (desk.py, unchanged);
* a reactive cycle reacts to its event, on one line at most.

The tools refuse another line's work for the rest of the cycle (tools._line). 0.28.0 to 0.34.0: Ember's code ranked
the lines for each ordinary and marketing plan (READY, its pick kept in desk_picks), and the owner's marketing share
of each day's spending made marketing cycles. 0.35.0: the plan tree takes each cycle's step (plan.py), and its step
decides what the cycle is and its line; what is left here is what the work steps' FOCUS says of the line
(``focus_text``) and this week's questions (YOUR STEP shows them).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from ..integrations import etsy_publisher
from . import bets, metrics, reach, review, weekly
from .store import AgentScope

# The kinds of wake cycle (cycles.venture and cycles.marketing record the two that took a share of the spending until
# 0.35.0; marketing a step of the plan tree's since).
ORDINARY, MARKETING, VENTURE, REACTIVE = "ordinary", "marketing", "venture", "reactive"
ITEM_CHARS = 200  # a question, as YOUR STEP shows it
MAX_LISTINGS = 6  # a marketing cycle's live listings FOCUS lists
HANDOFF_FOCUS_CHARS = 300  # 0.30.0: of a line's last handoff, as FOCUS quotes it
REVIEW_WHY_CHARS = 240  # 0.33.0: of today's review's why on the line, as FOCUS quotes it


def _one_line(value: Any, limit: int) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def questions(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[str]:
    """What this week's look asked the week to answer (slack.py's question items before 0.28.0)."""
    look = weekly.latest(conn, scope, today)
    if look is None:
        return []
    try:
        asked = json.loads(look["answer"] or "{}").get("questions") or []
    except (ValueError, AttributeError):
        return []
    return [str(q) for q in asked if str(q).strip()]


def focus_text(
    conn: sqlite3.Connection, scope: AgentScope, line: int, marketing: bool, today: date | None = None
) -> str:
    """The work steps' FOCUS on the cycle's line: Ember's code keeps the tools on it, its funnel and the reach done
    for it, and in a marketing cycle its live listings (a pin, a post and an edit name one). 0.30.0: in an ordinary
    cycle, the next step the last cycle on the line left (the cycles between were often about other things), and on a
    live line without an open bet, to bet on what the cycle's change brings (a bet settled is a case to learn from).
    0.33.0: what today's review said of the line (``today``): the work steps saw no review, and live, the cycle whose
    review said "no title or tag edits" on its line spent its steps on them."""
    found = [
        f"Your line this cycle: project #{line}. Ember's code keeps your tools on it: what another line needs waits for"
        " a cycle of its own."
    ]
    judged = review_said(conn, scope, today, line) if today is not None else ""
    if judged:
        found.append(judged)
    said = handoffs(conn, scope).get(line) if not marketing else None
    if said is not None:
        quoted = json.dumps(_one_line(said[1], HANDOFF_FOCUS_CHARS), ensure_ascii=False)
        found.append(f"Your last cycle on it (#{said[0]}) left as next: {quoted}")
    funnel = reach.funnels(conn, scope).get(line)
    if funnel is not None:
        found.append(funnel.text())
        if not marketing and funnel.listings and line not in bets.open_lines(conn, scope):
            found.append(  # 0.32.0: a change meant to bring buyers (live, a fix of a file was bet on, refused twice)
                "No open bet on it: say what you expect this cycle's change to bring (project_update bet, e.g. '+15"
                " views in 7 days: why'), so Ember's code checks it and your review learns from it. A change that"
                " brings no views, favorites or orders (a fix) needs none."
            )
    if marketing:
        for r in etsy_publisher.live_rows(metrics.listings(conn, scope, line, None), {})[:MAX_LISTINGS]:
            whose = " (made by Printify)" if r["printify"] else ""
            found.append(
                f"- listing #{r['listing_id']}{whose} {_one_line(r['title'], 60)}: {r['views'] or 0} views,"
                f" {r['favorites'] or 0} favorites"
            )
    return "\n".join(found)


def handoffs(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, tuple[int, str]]:
    """0.30.0: the next step (its journal's next) the newest cycle on each line left, with that cycle's number: the
    plan's YOUR LAST CYCLE shows the last cycle's, often of another line, a venture or marketing."""
    where, params = scope.where("j")
    found: dict[int, tuple[int, str]] = {}
    for r in conn.execute(
        f"SELECT y.project_id, j.cycle_id, j.handoff FROM journal j JOIN cycles y ON y.id = j.cycle_id WHERE {where}"
        " AND j.author = 'agent' AND j.handoff <> '' AND y.project_id IS NOT NULL AND y.venture = 0"
        " ORDER BY j.id DESC LIMIT 500",
        params,
    ):
        found.setdefault(int(r[0]), (int(r[1]), str(r[2])))
    return found


def review_said(conn: sqlite3.Connection, scope: AgentScope, today: date, line: int) -> str:
    """0.33.0: today's review's verdict on a line, its bottleneck and why, for the work steps' FOCUS ("" without
    one)."""
    row = review.of_day(conn, scope, today)
    try:
        items = json.loads(row["verdicts"] or "[]") if row is not None else []
    except ValueError:
        return ""
    for v in items if isinstance(items, list) else []:
        if isinstance(v, dict) and v.get("project_id") == line and v.get("verdict") in review.VERDICTS:
            neck = f" ({v['bottleneck']})" if v.get("bottleneck") else ""
            why = _one_line(str(v.get("why") or ""), REVIEW_WHY_CHARS)
            return f"Today's review of it: {v['verdict']}{neck}" + (f": {why}" if why else "")
    return ""
