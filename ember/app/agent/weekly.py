"""The weekly look at the whole business (0.18.0, vision/learning.md part 6).

The daily review judged each project on a week of numbers, but nothing looked at the business as a whole: the
strategy stayed as it was written early on (live, it named a venture the agent had parked), all the backed legs were
products, and services were never weighed. Now, once a week after the daily review, a call of its own on the strategy
model reads a view Ember's code builds (``view``): the money, every project with its funnel, reach and bets, the
ventures by stage, where the week's cycles and money went against where results came from, who started the cycles,
the bets' record, the week's cases and the playbook. It answers (``prompts.WEEKLY_SCHEMA``) with its read, the
business's bottleneck, what to stop and start, the mix of business models, a new strategy, up to 3 questions for the
week and the playbook's changes.

Ember's code applies it (``apply``): the strategy replaces the old one unless it names a parked or killed venture
(obligations.stale_strategy) or is too long; the principles go through learning.apply_principles (each must cite
cases); the questions and the bottleneck reach every plan of the week through TODAY'S REVIEW (``planner_text``).
It counts toward the daily cap only (as a review), leaves what the cycle needs to work, and never ends the cycle; a
failed one is kept and tried again the next day.

0.30.0: the week's plan holds. The look rewrote the strategy without seeing the goal it serves (0.29.0's goal and its
sub-goals were in every plan, not in the view), and what it said to start and stop was text no ranking read, so the
next cycles took their lines as before. Now the view shows the goal with how far each sub-goal got (``goal_text``), and
the look chooses the week's focus: up to MAX_FOCUS product lines its ordinary and marketing cycles push first
(``focus``; until 0.34.0 READY ranked them after what is owed and the line in progress). 0.35.0: the plan tree
weighs no focus line: the look still names them (the owner's dashboard shows them), and Release 2c lets it edit the
tree.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from ..economy.clock import to_iso
from ..economy.costs import micros_to_usd
from . import bets, learning, memory, obligations, prompts, reach, roadmap, ventures
from .store import CLOSED_STATUSES, OPEN_STATUSES, AgentScope, open_projects

DAYS = 7
# 0.30.0: the view's room, as weekly_reviews keeps it (was 14,000, while the request had to fit 12,000 tokens: a view
# over about 12,300 characters didn't, and the look was skipped at every cycle with only a line in the log). The loop
# cuts it further until its request fits (VIEW_STEPS).
VIEW_CHARS = 16_000
VIEW_STEPS = (VIEW_CHARS, 12_000, 9_000, 6_000)
LIMITS = {**prompts.WEEKLY_CHARS, "strategy": memory.CAPS["strategy"]}
MAX_LIST = prompts.MAX_WEEKLY_ITEMS  # stop and start items
MAX_QUESTIONS = prompts.MAX_WEEKLY_QUESTIONS
MAX_FOCUS = prompts.MAX_WEEKLY_FOCUS  # 0.30.0: the week's focus lines
GOAL_LINES = 6  # 0.30.0: the sub-goals the view shows under the goal


def due(conn: sqlite3.Connection, scope: AgentScope, today: date) -> bool:
    """Whether the weekly look is due: none came through in the last DAYS days, none failed today, and a daily review
    came through before (it needs a week of reviews' cases to look at, at least one)."""
    where, params = scope.where()
    since = (today - timedelta(days=DAYS - 1)).isoformat()
    if conn.execute(
        f"SELECT 1 FROM weekly_reviews WHERE {where} AND ((status = 'ok' AND day >= ?) OR day = ?) LIMIT 1",
        (*params, since, today.isoformat()),
    ).fetchone():
        return False
    return conn.execute(f"SELECT 1 FROM reviews WHERE {where} AND status = 'ok' LIMIT 1", params).fetchone() is not None


def _usd(micros: int) -> str:
    return f"${micros_to_usd(micros):.2f}"


def _one(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: datetime,
    money: str,
    strategy: str,
    instructions: str,
    goal: str = "",
) -> str:
    """The week as the weekly look reads it, whole (``cut`` bounds it; ``money``: the books' lines Ember's code wrote
    for it; 0.30.0: ``goal``, the goal and its sub-goals, ``goal_text``). 0.30.0: the most important first, as a cut
    takes the end: the owner's standing instructions and the strategy came last, after up to 30 earlier cases and the
    whole playbook, which now grows every day."""
    since = to_iso(now - timedelta(days=DAYS))
    month = to_iso(now - timedelta(days=30))
    where, params = scope.where()
    parts = [
        f"THE WEEK TO {now:%a %Y-%m-%d} (from Ember's records: exact)",
        money,
        goal,
        f"YOUR OWNER'S STANDING INSTRUCTIONS\n{instructions or 'None.'}",
        f"YOUR STRATEGY NOW\n{strategy.strip() or 'None.'}",
    ]
    funnels = reach.funnels(conn, scope)
    open_bets = bets.open_lines(conn, scope)
    lines = ["PROJECTS (open, then those closed in the last 30 days)"]
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    closed = ", ".join("?" for _ in CLOSED_STATUSES)
    for p in conn.execute(
        f"SELECT * FROM projects WHERE {where} AND (status IN ({marks}) OR (status IN ({closed}) AND updated_at >= ?))"
        " ORDER BY status IN ('idea', 'active', 'waiting') DESC, id",
        (*params, *OPEN_STATUSES, *CLOSED_STATUSES, month),
    ).fetchall():
        spent = conn.execute(
            "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
            " WHERE y.project_id = ?",
            (p["id"],),
        ).fetchone()[0]
        venture = f" · venture #{p['venture_id']}" if p["venture_id"] else ""
        lines.append(f"#{p['id']} [{p['status']}] {_one(p['title'], 80)}{venture} · spent {_usd(int(spent))}")
        funnel = funnels.get(int(p["id"]))
        if funnel is not None:
            lines.append(f"   {funnel.text()}")
        lines += [f"   {b}" for b in open_bets.get(int(p["id"]), [])]
    parts.append("\n".join(lines) if len(lines) > 1 else "PROJECTS\nNone.")
    paid = ventures.money(conn, scope)
    lines = ["VENTURES (by stage; the parked and killed ones by name only, never to name in your strategy)"]
    gone = []
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] in ("parked", "killed"):
            gone.append(f"#{v['id']} {_one(v['title'], 50)} ({v['stage']})")
            continue
        m = paid.get(int(v["id"]), ventures.Money())
        lines.append(
            f"#{v['id']} [{v['stage']}] {_one(v['title'], 80)}: {_one(v['pitch'], 160)} · {ventures.money_text(m)}"
        )
    if gone:
        lines.append("Parked or killed: " + "; ".join(gone))
    parts.append("\n".join(lines))
    rows = conn.execute(
        "SELECT trigger, project_id, venture, marketing, COUNT(*) AS n, COALESCE(SUM((SELECT SUM(cost_micros) FROM"
        " llm_calls c WHERE c.cycle_id = y.id)), 0) AS spent FROM cycles y WHERE session = ? AND simulated = ?"
        " AND started_at >= ? GROUP BY trigger, project_id, venture, marketing",
        (scope.session, 1 if scope.simulated else 0, since),
    ).fetchall()
    by_trigger: dict[str, int] = {}
    by_focus: dict[str, tuple[int, int]] = {}
    for r in rows:
        by_trigger[str(r["trigger"])] = by_trigger.get(str(r["trigger"]), 0) + int(r["n"])
        focus = _went_to(r)
        n, spent = by_focus.get(focus, (0, 0))
        by_focus[focus] = (n + int(r["n"]), spent + int(r["spent"]))
    started = ", ".join(f"{n} by {t}" for t, n in sorted(by_trigger.items(), key=lambda x: -x[1])) or "none"
    went = "; ".join(
        f"{focus}: {n} cycles, {_usd(spent)}" for focus, (n, spent) in sorted(by_focus.items(), key=lambda x: -x[1][1])
    )
    parts.append(f"WHERE THE WEEK WENT\nCycles started: {started} ('owner' is your owner waking you).\n{went or ''}")
    record = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM bets WHERE {where} AND placed_at >= ? GROUP BY status", (*params, month)
    ).fetchall()
    parts.append("YOUR BETS (30 days): " + (", ".join(f"{r['n']} {r['status']}" for r in record) or "none placed"))
    week = learning.cases(conn, scope, since)
    parts.append(
        "THIS WEEK'S CASES (cite them by number)\n" + ("\n".join(f"- {learning.case_line(c)}" for c in week) or "None.")
    )
    playbook = learning.principles(conn, scope)
    parts.append(
        "YOUR PLAYBOOK (principles: confirm, merge, dispute or retire them by id)\n"
        + ("\n".join(f"- {learning.principle_line(p)}" for p in playbook) or "Empty.")
    )
    older = [c for c in learning.cases(conn, scope, None, 60) if c not in week]
    if older:
        parts.append("EARLIER CASES\n" + "\n".join(f"- {learning.case_line(c)}" for c in older[:30]))
    return "\n\n".join(p for p in parts if p)


def cut(text: str, chars: int = VIEW_CHARS) -> str:
    """The view in at most ``chars`` characters, its end cut (0.30.0: the loop cuts more, VIEW_STEPS, until its request
    fits the weekly look's budget)."""
    return text if len(text) <= chars else text[: chars - 20].rstrip() + "\n[view cut]"


def parse(text: str) -> dict[str, Any] | None:
    """The weekly look's JSON answer, checked and cut to its limits; None if unusable."""
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if 0 <= start < end else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return None
    found = {key: str(data.get(key) or "").strip()[:limit] for key, limit in LIMITS.items() if key != "question"}
    for key in ("stop", "start"):
        items = data.get(key)
        found[key] = (
            [_one(i, 200) for i in items if isinstance(i, str) and i.strip()][:MAX_LIST]
            if isinstance(items, list)
            else []
        )
    asked = data.get("questions")
    found["questions"] = (
        [_one(q, LIMITS["question"]) for q in asked if isinstance(q, str) and q.strip()][:MAX_QUESTIONS]
        if isinstance(asked, list)
        else []
    )
    chosen = data.get("focus")  # 0.30.0: the week's focus lines (apply keeps the open ones)
    found["focus"] = list(
        dict.fromkeys(i for i in chosen if isinstance(i, int) and not isinstance(i, bool))
        if isinstance(chosen, list)
        else []
    )[:MAX_FOCUS]
    found["principles"] = data.get("principles") if isinstance(data.get("principles"), list) else []
    if not found["assessment"] and not found["strategy"]:
        return None
    return found


def apply(
    conn: sqlite3.Connection, scope: AgentScope, mem: memory.Memory, answer: dict[str, Any], now: str
) -> list[str]:
    """Ember's code carries out the weekly look's answer; returns what happened."""
    happened = []
    strategy = answer.get("strategy", "")
    if strategy:
        stale = obligations.stale_strategy(conn, scope, strategy)
        if stale:
            happened.append("the new strategy wasn't kept: it names a parked or killed venture")
        elif memory.heading_line(strategy):
            happened.append("the new strategy wasn't kept: a line began like a heading of your context")
        elif len(strategy.encode("utf-8")) > memory.CAPS["strategy"]:
            happened.append("the new strategy wasn't kept: it was too long")
        else:
            mem.rewrite(conn, "strategy", strategy.rstrip() + "\n", "weekly", now)
            happened.append("the strategy was rewritten")
    # 0.30.0: the week's focus lines: open ones the owner's park or kill doesn't stop (the answer keeps only those)
    workable = {int(p["id"]) for p in open_projects(conn, scope)}
    chosen = [
        i for i in answer.get("focus") or [] if i in workable and ventures.project_stopped(conn, scope, i) is None
    ]
    left_out = [i for i in answer.get("focus") or [] if i not in chosen]
    answer["focus"] = chosen
    if chosen:
        happened.append("this week's focus: " + ", ".join(f"#{i}" for i in chosen))
    if left_out:
        happened.append("not a focus (no open line of yours): " + ", ".join(f"#{i}" for i in left_out))
    known = {int(c["id"]) for c in learning.cases(conn, scope, None, 10_000)}
    happened += learning.apply_principles(conn, scope, answer.get("principles"), known, now)
    return happened


def focus(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[int]:
    """0.30.0: this week's focus lines, as the newest weekly look chose them, while they are open."""
    look = latest(conn, scope, today)
    if look is None:
        return []
    try:
        chosen = json.loads(look["answer"] or "{}").get("focus") or []
    except (ValueError, AttributeError):
        return []
    workable = {int(p["id"]) for p in open_projects(conn, scope)}
    return [i for i in chosen if isinstance(i, int) and not isinstance(i, bool) and i in workable][:MAX_FOCUS]


def goal_text(conn: sqlite3.Connection, scope: AgentScope, today: date, money: tuple[int, int] | None) -> str:
    """0.30.0: the goal at the root and the sub-goals that lead to it, each with how far it got (``money``: the money
    goal's numbers, roadmap.progress_for), for the weekly look ("" without a goal)."""
    top = roadmap.root(conn, scope)
    if top is None:
        return ""
    progress = roadmap.progress_for(conn, scope, today, money)
    subs = [r for r in roadmap.open_milestones(conn, scope) if r["parent_id"] == top["id"]]
    lines = ["THE GOAL (choose the week's focus toward it)", roadmap.root_line(top, today, progress)]
    lines += [f"- {roadmap.goal_line(r, today, progress=progress)}" for r in subs[:GOAL_LINES]]
    if len(subs) > GOAL_LINES:
        lines.append(f"- and {len(subs) - GOAL_LINES} more sub-goals")
    if not subs:
        lines.append("No sub-goal leads to it yet.")
    return "\n".join(lines)


def save(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    now: str,
    day: date,
    view_text: str,
    answer: dict[str, Any] | None,
    outcome: list[str],
    note: str | None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO weekly_reviews (mode, session, cycle_id, created_at, day, status, view, answer, outcome, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            now,
            day.isoformat(),
            "ok" if answer is not None else "failed",
            view_text[:16_000],
            json.dumps(answer or {}, ensure_ascii=False),
            "; ".join(outcome)[:1000],
            (note or "")[:300] or None,
        ),
    )
    return int(cursor.lastrowid)


def _went_to(row: sqlite3.Row) -> str:
    """What a group of the week's cycles went to: a product line (0.28.0: its marketing apart), ventures, or none."""
    if row["venture"]:
        return "venture cycles"
    if row["project_id"]:
        return f"marketing #{row['project_id']}" if row["marketing"] else f"project #{row['project_id']}"
    return "marketing cycles" if row["marketing"] else "no focus"


def latest(conn: sqlite3.Connection, scope: AgentScope, today: date) -> sqlite3.Row | None:
    """The newest weekly look that came through in the last DAYS days."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM weekly_reviews WHERE {where} AND status = 'ok' AND day > ? ORDER BY id DESC LIMIT 1",
        (*params, (today - timedelta(days=DAYS)).isoformat()),
    ).fetchone()


def planner_text(row: sqlite3.Row | None) -> str:
    """The week's look as TODAY'S REVIEW adds it for every plan of the week ("" without one)."""
    if row is None:
        return ""
    try:
        answer = json.loads(row["answer"] or "{}")
    except ValueError:
        return ""
    lines = [f"This week's look at the whole business ({row['day']}):"]
    if answer.get("bottleneck"):
        lines.append(f"The business's bottleneck: {_one(answer['bottleneck'], 300)}")
    chosen = [i for i in answer.get("focus") or [] if isinstance(i, int) and not isinstance(i, bool)]
    if chosen:  # 0.30.0
        lines.append(
            "This week's focus lines: " + ", ".join(f"#{i}" for i in chosen)
        )
    if answer.get("stop"):
        lines.append("Stop: " + "; ".join(_one(i, 120) for i in answer["stop"]))
    if answer.get("start"):
        lines.append("Start: " + "; ".join(_one(i, 120) for i in answer["start"]))
    lines += [f"Question to answer this week: {_one(q, 200)}" for q in answer.get("questions") or []]
    return "\n".join(lines) if len(lines) > 1 else ""
