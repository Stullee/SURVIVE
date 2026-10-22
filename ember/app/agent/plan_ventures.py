"""0.42.2 (analysis-0.40.1, 5.6): the ventures in the plan tree, split out of plan.py.

The ventures are the plan's sub-goal (0.37.0): a node for each venture being explored, under the Ventures project,
with its next decision as a step (``VENTURE_STEPS``: triage, appraise, answer the critic), and a brainstorm while the
funnel is thin. ``_ventures`` keeps them before every plan, ``venture_worth`` is what a venture's step is worth,
``_ventures_line`` is YOUR PLAN's line on them, and ``new_things_held`` the owner's hold on new things.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from ..economy.clock import from_iso, to_iso
from . import critic, desk, store, templates, ventures, weights
from .plan_layout import _platform
from .plan_model import _close, _cut, _insert, _open_steps, _update, change, node, nodes
from .store import AgentScope

# --- the ventures: the plan's sub-goal, each venture a node with its next decision as a step (0.37.0) ---

# 0.37.0: each venture being explored is a node of the Ventures project, its next decision (desk.py's, READY's until
# then) a step under it, weighed like any step: a cycle that takes one is a venture cycle on that venture. 0.36.0 had
# one Explore step for all of them, and READY said which (migration 0091 closed it). Each decision's step: its
# template, its kind (the owner's decision on a case waits on them) and its title.
VENTURE_STEPS: dict[str, tuple[str, str, str]] = {
    "triage": ("triage@1", "create", "Triage it: research it, or park it with why"),
    "appraise": ("appraise@1", "create", "Research its next question; then its business case, or park it"),
    "answer": ("answer@1", "create", "Answer the critic with evidence or new numbers, or park it"),
    "decide": ("decide@1", templates.OWNER_KIND, "Your owner decides on its business case: back it or park it"),
}
BRAINSTORM_TEMPLATE = "brainstorm@1"
BRAINSTORM_TITLE = "Brainstorm six new ideas for your ventures"
# the steps that make a venture cycle (the owner's decision is theirs)
VENTURE_TEMPLATES = frozenset({"triage@1", "appraise@1", "answer@1", BRAINSTORM_TEMPLATE})
DECISION_OF = {template: decision for decision, (template, _, _) in VENTURE_STEPS.items()}


def ventures_node(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    found = nodes(conn, scope, "level = 'project' AND platform = 'ventures'")
    return found[0] if found else None


def new_things_held(conn: sqlite3.Connection, scope: AgentScope) -> str | None:
    """0.36.0: the owner's hold on the Ventures ("nothing new"): why, while it lasts. Ember's code keeps it: no venture
    cycle, and no new product (project_create refuses one; YOUR STEP offers none). 0.39.3: and no new venture
    (venture_create refuses one), and no new product of a venture either (analysis-0.39.1, 4.4)."""
    top = ventures_node(conn, scope)
    if top is None or not top["hold_reason"] or top["hold_by"] != "owner":
        return None
    return str(top["hold_reason"])


def venture_decision(conn: sqlite3.Connection, v: Mapping[str, Any]) -> str | None:
    """A venture's next decision by its stage: triage an idea, appraise one being researched, answer the critic on a
    proposed one it judged test or park, else the owner's decision on its case; None once it is backed, live, parked
    or killed (a backed one's work is its product line's)."""
    stage = v["stage"]
    if stage == "idea":
        return "triage"
    if stage == "researching":
        return "appraise"
    if stage != "proposed":
        return None
    case_row = ventures.latest_case(conn, int(v["id"])) if v["cases"] else None
    judged = critic.latest(conn, int(v["id"])) if case_row is not None else None
    return "answer" if judged is not None and judged["verdict"] in ("test", "park") else "decide"


def _ventures(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """The Ventures project with a node for each venture being explored and its next decision as a step (the step
    before it closed once its stage moved on), the node closed once the venture is backed or live (done: its work is
    its product line's) or parked or killed (dropped); and a brainstorm step while the funnel is thin
    (desk.brainstorm_due; 0.39.1: not while none of the last brainstorm's ideas went on, desk.brainstorm_rests).
    Kept before every plan."""
    top_id = _platform(conn, scope, "ventures", now)
    first = not nodes(conn, scope, "level = 'venture'")
    open_nodes = {int(n["venture_id"]): n for n in nodes(conn, scope, "level = 'venture' AND status = 'open'")}
    rows = ventures.all_ventures(conn, scope)
    made = 0
    for v in rows:
        vid = int(v["id"])
        decision = venture_decision(conn, v)
        here = open_nodes.get(vid)
        if decision is None:
            if here is not None:
                ended = "done" if v["stage"] in ("building", "live") else "dropped"
                why = _cut(f"venture #{vid} is {v['stage']}", 300)
                for step in _open_steps(conn, scope, int(here["id"])):
                    _close(conn, step["id"], now, ended, why)
                _close(conn, here["id"], now, ended, why)
            continue
        if here is None:
            here_id = _insert(
                conn,
                scope,
                now,
                parent_id=top_id,
                level="venture",
                venture_id=vid,
                title=_cut(" ".join(str(v["title"]).split()), 160),
                source="code",
            )
            made += 1
        else:
            here_id = int(here["id"])
        template, kind, title = VENTURE_STEPS[decision]
        steps = _open_steps(conn, scope, here_id)
        if any(s["template"] == template for s in steps):
            continue
        for step in steps:
            _close(conn, step["id"], now, "done", _cut(f"venture #{vid} is {v['stage']} now", 300))
        _insert(
            conn, scope, now, parent_id=here_id, level="step", kind=kind, template=template, title=title, source="code"
        )
    held = _triage_together(conn, scope, now, rows)  # 0.45.0: the triages a cycle today could have taken along
    due = desk.brainstorm_due(rows)
    rest = desk.brainstorm_rests(rows, now) if due else ""  # 0.39.1: not while none of the last one's ideas went on
    # 0.45.0 (evaluation 2026-10-22, C4): nor while the owner's rulebook or her strategy says "nothing new"
    rest = rest or (desk.brainstorm_stopped(store.rules(conn, scope), _strategy(conn, scope)) if due else "")
    if rest:
        due = ""
    brainstorms = nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,))
    if due and not brainstorms:
        _insert(
            conn,
            scope,
            now,
            parent_id=top_id,
            level="step",
            kind="create",
            template=BRAINSTORM_TEMPLATE,
            title=BRAINSTORM_TITLE,
            source="code",
        )
    elif not due:
        for step in brainstorms:
            if rest:
                _close(conn, step["id"], now, "dropped", _cut(rest, 300))
            else:
                _close(conn, step["id"], now, "done", "enough ideas wait, or have their numbers")
    if first and made:
        return ["Plan tree: each venture you explore is a node of its Ventures now, its next decision a step.", *held]
    return held


def _strategy(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """0.45.0: the agent's strategy as she last wrote it (memory_versions keeps each version)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT content FROM memory_versions WHERE {where} AND file = 'strategy' ORDER BY id DESC LIMIT 1", params
    ).fetchone()
    return str(row["content"]) if row is not None else ""


def _triage_together(conn: sqlite3.Connection, scope: AgentScope, now: str, rows: list[Any]) -> list[str]:
    """0.45.0 (evaluation 2026-10-22, C4): one cycle a day triages the ideas that wait: once a cycle of the owner's
    day took a triage step, whose YOUR STEP named the other ideas waiting for theirs to be taken along (0.39.1), the
    triages still open wait for the next day, when one cycle decides them together. An idea Ember's code parks within
    desk.URGENT_DAYS, the owner's own idea or a step they pinned isn't held. Live on 10-14, six cycles 30 minutes
    apart parked ventures #28 to #33 one each ($1.04), all for one reason."""
    today = ventures.owner_day(now)
    where, params = scope.where("k")
    taken = [
        int(r["node_id"])
        for r in conn.execute(
            f"SELECT k.node_id, k.created_at FROM plan_picks k JOIN plan_nodes n ON n.id = k.node_id WHERE {where}"
            " AND n.template = 'triage@1' AND k.created_at >= ?",
            (*params, to_iso(from_iso(now) - timedelta(days=2))),
        )
        if ventures.owner_day(r["created_at"]) == today
    ]
    if not taken:
        return []
    by_id = {int(v["id"]): v for v in rows}
    tomorrow = (today + timedelta(days=1)).isoformat()
    why = (
        f"a triage cycle today (step #{taken[-1]}) could take this idea along: the next one, tomorrow, decides the"
        " ideas that wait together"
    )
    said = []
    for step in nodes(conn, scope, "level = 'step' AND status = 'open' AND template = 'triage@1' AND waiting IS NULL"):
        parent = node(conn, scope, int(step["parent_id"])) if step["parent_id"] else None
        v = by_id.get(int(parent["venture_id"])) if parent is not None and parent["venture_id"] else None
        parks = ventures.triage_date(v) if v is not None else None
        if parks is None or (parks - today).days <= desk.URGENT_DAYS or step["pinned"]:
            continue
        _update(conn, step["id"], now, waiting="date", wait_until=tomorrow, wait_why=_cut(why, 200), ready_since=None)
        change(conn, scope, int(step["id"]), None, "code", "wait", why, now)
        said.append(f"Plan tree: step #{step['id']} (venture #{v['id']}'s triage) waits for tomorrow: {why}.")
    return said


def _last_taken(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """When a cycle last took each step (0.37.0: a venture's step ages from then, so the others come in turn)."""
    where, params = scope.where()
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            f"SELECT node_id, MAX(created_at) FROM plan_picks WHERE {where} AND node_id IS NOT NULL GROUP BY node_id",
            params,
        )
    }


def _ventured_last(conn: sqlite3.Connection, scope: AgentScope, before: int | None) -> bool:
    """Whether the cycle before ``before`` (None: the latest) was a venture cycle."""
    row = conn.execute(
        "SELECT venture FROM cycles WHERE session = ? AND simulated = ? AND (? IS NULL OR id < ?)"
        " ORDER BY id DESC LIMIT 1",
        (scope.session, 1 if scope.simulated else 0, before, before),
    ).fetchone()
    return bool(row and row["venture"])


def venture_worth(conn: sqlite3.Connection, v: Mapping[str, Any] | None) -> float:
    """What a venture's step is worth unless the owner set one: what its business case expects a month (the critic's
    where it is lower), as a product's worth from what it could earn (weights.prior), at least weights.EXPLORE_WORTH;
    an idea's by its scores (ventures.weight, an unscored one's as middling), half to one and a half of that, so the
    heaviest ideas are researched first (READY's order until 0.36.0)."""
    if v is None:
        return weights.EXPLORE_WORTH
    case_row = ventures.latest_case(conn, int(v["id"])) if v["cases"] else None
    ev = critic.ranking_ev(case_row, critic.latest(conn, int(v["id"])) if case_row is not None else None)
    if ev is not None and ev > 0:
        return max(weights.EXPLORE_WORTH, weights.prior(ev))
    if v["stage"] == "idea":
        scored = ventures.weight(v)
        return round(weights.EXPLORE_WORTH * (0.5 + (50 if scored is None else scored) / 100), 3)
    return weights.EXPLORE_WORTH


def venture_of(conn: sqlite3.Connection, scope: AgentScope, step_id: int | None) -> int | None:
    """The venture a step of the Ventures is about (None: a brainstorm, or no venture's step)."""
    row = node(conn, scope, step_id) if step_id is not None else None
    parent = node(conn, scope, int(row["parent_id"])) if row is not None and row["parent_id"] is not None else None
    return int(parent["venture_id"]) if parent is not None and parent["venture_id"] is not None else None


def _ventures_line(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """YOUR PLAN's line on the ventures: those being explored with their next decision (VENTURES shows the tree), the
    owner's worth and hold, and a brainstorm due."""
    top = ventures_node(conn, scope)
    if top is None:
        return ""
    open_nodes = nodes(conn, scope, "level = 'venture' AND status = 'open'")
    words = []
    for here in open_nodes[:VENTURES_SHOWN]:
        steps = _open_steps(conn, scope, int(here["id"]))
        decision = DECISION_OF.get(str(steps[0]["template"]), "") if steps else ""
        words.append(f"#{here['venture_id']} {_cut(str(here['title']), 40)} ({VENTURE_WORDS.get(decision, '-')})")
    more = len(open_nodes) - VENTURES_SHOWN
    said = "Ventures, each next decision a step of yours: " + (" · ".join(words) if words else "none being explored")
    if more > 0:
        said += f" · {more} more"
    if nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,)):
        said += " · a brainstorm is due"
    if top["owner_worth"] is not None:
        said += f" · your owner's worth for them: {float(top['owner_worth']):g}"
    if top["hold_reason"] and top["hold_by"] == "owner":
        said += (
            f" · your owner holds new things: {_cut(top['hold_reason'], 80)}: no venture cycle and no new product"
            " until they resume them."
        )
    elif top["hold_reason"]:
        said += f" · on hold: {_cut(top['hold_reason'], 80)}: no venture cycle until they're resumed."
    return said


VENTURES_SHOWN = 6  # the ventures YOUR PLAN's line names (VENTURES lists the tree)
VENTURE_WORDS = {"triage": "triage", "appraise": "research", "answer": "answer the critic", "decide": "your owner's"}
