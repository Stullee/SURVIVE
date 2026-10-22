"""0.42.2 (analysis-0.40.1, 5.6): the owner's Plan tab, split out of plan.py.

``view`` is the tree as the Plan tab shows it (views.py): each product with its stages and steps and why a step waits,
the channels and how well they work, the upgrade requests, the ventures (``ventures_view``) and the cycles' picks;
``stamp`` changes whenever any of it does. ``_channels_view`` reads ``_channel_factors`` through plan, where tests
patch it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso, to_iso
from . import desk, policy, reach, roadmap, templates, ventures, weights
from .plan_changes import frozen, said_wait
from .plan_keep import _Day, _reworkable
from .plan_model import (
    CHANNELS,
    PICKS_SHOWN,
    STALE_DAYS,
    Facts,
    _days,
    _midnight,
    _open_steps,
    _replaced,
    _stages,
    _steps,
    current_stage,
    nodes,
)
from .plan_ready import Candidate
from .plan_steer import choose
from .plan_texts import Place, _places, grouped
from .plan_ventures import BRAINSTORM_TEMPLATE, DECISION_OF, venture_worth, ventures_node
from .store import AgentScope

# --- the owner's Plan tab ---


def view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
    *,
    day_start: str | None = None,
) -> dict[str, Any]:
    """The tree for the owner's Plan tab: every project, product, stage and step with its state and weight, the step
    the tree would take now and the next ones, what waits on the owner, the latest picks, and (0.35.0) what changed
    today, what each channel does and the upgrades the steps wait on. 0.36.0: and the ventures (0.37.0: each venture's
    step; ``exploring``: whether the burn mode runs venture cycles), each step of a venture naming it. 0.37.9:
    ``day_start``, when the owner's day began (their clock's: ``limits``)."""
    pick, found = choose(conn, scope, now, today, channels, exploring, day_start=day_start)
    by_id = {c.step.id: c for c in found}
    parts = weights.parts_by_id(pick.ranked)
    titles = {r["id"]: str(r["title"]) for r in nodes(conn, scope, "level = 'step' AND status = 'open'")}
    facts = Facts(conn, scope, now)
    projects: list[dict[str, Any]] = []
    for top, rows in grouped(conn, scope):
        products = [_product_view(conn, scope, q, by_id, parts, titles, facts, now) for q in rows]
        projects.append({"id": top["id"], "platform": top["platform"], "title": top["title"], "products": products})
    ranked = [(s, p) for s, p in pick.ranked]
    places = _places(conn, scope)
    waiting_owner = [_step_brief(c, None, titles, places) for c in found if c.waiting == "owner"]
    goal = roadmap.root(conn, scope)
    return {
        "today": today.isoformat(),
        "goal": {"title": goal["title"], "due": goal["due"]} if goal is not None else None,
        "projects": projects,
        "now": _pick_view(pick, titles, places),
        "next": [
            _step_brief(by_id[s.id], p, titles, places) for s, p in ranked if pick.step is None or s.id != pick.step.id
        ][:3],
        "waiting": waiting_owner,
        "picks": _picks(conn, scope, places),
        "changes": _changes_today(conn, scope, day_start or _midnight(today)),
        "channels": _channels_view(conn, scope, by_id, parts, now),
        "upgrades": _upgrades_view(conn, scope, by_id),
        "ventures": ventures_view(conn, scope, found, parts, today),  # 0.36.0
        "freeze": frozen(conn, scope, today),  # 0.36.0: the last day titles and tags stay as they are, or None
        "stamp": stamp(conn, scope),
    }


def _changes_today(conn: sqlite3.Connection, scope: AgentScope, day_start: str) -> list[dict[str, Any]]:
    """0.35.0: the changes to the tree today that weren't a check passing: Ember's (a step added, split, replaced,
    done or waiting, a product held or resumed), Ember's code's (an owner's decision as a step, a wait lifted, a
    decide-by date) and the owner's, the newest first. 0.40.3 (analysis-0.40.1 codebase, PL2): today from when the
    owner's day began (``day_start``), not the UTC date: from their midnight to 01:00 or 02:00 it showed yesterday's."""
    where, params = scope.where("c")
    return [
        {
            "at": r["created_at"],
            "actor": r["actor"],
            "action": r["action"],
            "id": r["node_id"],
            "title": r["title"],
            "line": r["project_id"],
            "why": r["why"],
        }
        for r in conn.execute(
            f"SELECT c.*, n.title, n.project_id FROM plan_changes c JOIN plan_nodes n ON n.id = c.node_id WHERE {where}"
            " AND c.created_at >= ? ORDER BY c.id DESC LIMIT 40",
            (*params, day_start),
        )
    ]


def _channels_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    by_id: Mapping[int, Candidate],
    parts: Mapping[int, weights.Parts],
    now: str,
) -> list[dict[str, Any]]:
    """0.35.0: each channel as a mirror of the products' marketing: what it does next and what it did lately, with
    how well it works (clicks per pin, 0.39.6: listing views after a post or blog post: its factor in the weights)."""
    from .plan import _channel_factors  # 0.42.2: read through plan, where tests patch it

    factors = _channel_factors(conn, scope, now)
    since = to_iso(from_iso(now) - timedelta(days=14))
    found = []
    for channel in CHANNELS:
        steps = []
        for row in nodes(
            conn, scope, "level = 'step' AND channel = ? AND (status = 'open' OR closed_at >= ?)", (channel, since)
        ):
            candidate = by_id.get(row["id"])
            weight = parts.get(row["id"])
            steps.append(
                {
                    "id": row["id"],
                    "line": row["project_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "due": row["due"],
                    "closed_at": row["closed_at"],
                    "waiting": candidate.waiting if candidate else None,
                    "refused": candidate.refused if candidate else None,  # 0.42.0
                    "weight": round(weight.total, 2) if weight else None,
                }
            )
        found.append({"channel": channel, "factor": factors.get(channel, 1.0), "steps": steps})
    return found


def _upgrades_view(conn: sqlite3.Connection, scope: AgentScope, by_id: Mapping[int, Candidate]) -> list[dict[str, Any]]:
    """0.35.0: the upgrades open, each with the steps that wait on it and what they would weigh once it is built, so
    the owner sees what is worth building next; steps that wait on an ability nobody asked for yet come last."""
    where, params = scope.where()
    found = []
    for u in conn.execute(
        f"SELECT id, title, status FROM upgrades WHERE {where} AND status IN ('new', 'accepted') ORDER BY id",
        params,
    ):
        steps = nodes(
            conn, scope, "level = 'step' AND status = 'open' AND waiting = 'upgrade' AND wait_ref = ?", (u["id"],)
        )
        found.append({"id": u["id"], "title": u["title"], "status": u["status"], "steps": _waiting_steps(steps, by_id)})
    unasked = nodes(conn, scope, "level = 'step' AND status = 'open' AND waiting = 'upgrade' AND wait_ref IS NULL")
    if unasked:
        found.append({"id": None, "title": None, "status": None, "steps": _waiting_steps(unasked, by_id)})
    return found


def _waiting_steps(rows: list[Any], by_id: Mapping[int, Candidate]) -> list[dict[str, Any]]:
    shown = []
    for row in rows:
        candidate = by_id.get(row["id"])
        weight = weights.weigh(candidate.step).total if candidate is not None else None
        shown.append(
            {"id": row["id"], "line": row["project_id"], "title": row["title"], "weight": weight and round(weight, 2)}
        )
    return sorted(shown, key=lambda s: -(s["weight"] or 0))


def _product_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    by_id: Mapping[int, Candidate],
    parts: Mapping[int, weights.Parts],
    titles: Mapping[int, str],
    facts: Facts,
    now: str,
) -> dict[str, Any]:
    pid = int(product["project_id"])
    project = conn.execute("SELECT title, status FROM projects WHERE id = ?", (pid,)).fetchone()
    current = current_stage(conn, scope, product["id"]) if product["status"] == "open" else None
    funnel = facts.funnel(pid) if product["status"] == "open" else None
    stage_name = str(current["stage"]) if current is not None else "maintain"
    computed = weights.worth(
        float(product["could_earn"] or 0),
        stage_name,
        funnel.views if funnel else 0,
        funnel.favorites if funnel else 0,
        funnel.orders if funnel else 0,
    )
    stages = []
    for stage in _stages(conn, scope, product["id"]):
        if _replaced(stage, product):
            continue
        steps = []
        for step in _steps(conn, scope, stage["id"]):
            candidate = by_id.get(step["id"])
            weight = parts.get(step["id"])
            steps.append(
                {
                    "id": step["id"],
                    "title": step["title"],
                    "kind": step["kind"],
                    "channel": step["channel"],
                    "status": step["status"],
                    "waiting": candidate.waiting if candidate else None,
                    "refused": candidate.refused if candidate else None,  # 0.42.0: its tool's words ('blocked')
                    "due": step["due"],
                    "source": step["source"],
                    "template": step["template"],  # 0.35.0: decide/owner is the owner's keep-or-drop
                    "pinned": bool(step["pinned"]),
                    "unwait": step["status"] == "open" and said_wait(step),  # 0.40.3: the owner may lift it
                    # 0.42.1: a rework Ember declined (decline_step), which the owner may have made anyway
                    "reworkable": _reworkable(conn, scope, step) is not None,
                    "check": templates.CHECKS.get(step["check_kind"] or "", ""),
                    "result": step["result"],
                    "weight": round(weight.total, 2) if weight else None,
                    "why": weight.text(titles.get(weight.carried_from or 0)) if weight else None,
                    "age_days": round(_days(step["ready_since"], now), 1) if step["ready_since"] else None,
                    "stale": step["status"] == "open"
                    and bool(step["ready_since"])
                    and _days(step["ready_since"], now) >= STALE_DAYS,
                }
            )
        stages.append(
            {
                "id": stage["id"],
                "stage": stage["stage"],
                "title": stage["title"],
                "status": stage["status"],
                "current": current is not None and stage["id"] == current["id"],
                "steps": steps,
                "done": sum(1 for s in steps if s["status"] == "done"),
                "total": sum(1 for s in steps if s["status"] != "dropped"),
            }
        )
    own = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    unlocks = (
        [
            {"rule": g["rule"], "level": g["level"], "per_day": g["per_day"], "budget": g["budget"]}
            for g in policy.unlocked(conn, scope, int(own["id"]))
        ]
        if own is not None
        else []
    )
    return {
        "id": product["id"],
        "line": pid,
        "title": project["title"] if project is not None else product["title"],
        "hold": product["hold_reason"],  # 0.35.0: Ember's hold, with her reason
        "hold_by": product["hold_by"],  # 0.36.0: 'owner' for the owner's own (Ember can't lift it)
        "live_since": product["live_since"],
        "decide_by": json.loads(product["decide_by"]) if product["decide_by"] else {},
        "pushed_until": product["pushed_until"],
        "milestone": int(own["id"]) if own is not None else None,  # its own milestone (the Autonomy box's)
        "unlocks": unlocks,
        "autonomy": _autonomy(conn, scope, int(own["id"]) if own is not None else None, now),
        "template": templates.by_key(product["template"]).title,
        "status": product["status"],
        "stage": stage_name if current is not None else None,
        "audience": product["audience"],
        "could_earn": product["could_earn"],
        "worth": product["owner_worth"] if product["owner_worth"] is not None else round(computed, 2),
        "worth_code": round(computed, 2),
        "owner_worth": product["owner_worth"],
        "chance": weights.chance(stage_name, *(_numbers(funnel))),
        "numbers": {
            "views": funnel.views if funnel else 0,
            "favorites": funnel.favorites if funnel else 0,
            "orders": funnel.orders if funnel else 0,
            "pins": funnel.pins if funnel else 0,
            "posts": funnel.bluesky if funnel else 0,
            "blog": funnel.posts if funnel else 0,
        },
        "stages": stages,
    }


def _autonomy(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int | None, now: str) -> list[dict[str, Any]]:
    """0.35.0: a product's Autonomy box: each rule's unlock on its own milestone, or (before one is set, and so its
    milestone made) every rule manual, as a milestone of its line would show it."""
    if milestone_id is not None:
        return policy.view(conn, scope, _Day(now), milestone_id)
    return [
        {
            "rule": rule.name,
            "label": rule.label,
            "action_class": rule.action_class,
            "fits": rule.action_class != "email.reply",  # a milestone of a line: its listings, no email replies
            "levels": list(policy.levels(rule.name)),
            "level": "manual",
            "per_day": policy.PER_DAY,
            "budget": policy.BUDGET,
            "used": 0,
            "used_today": 0,
            "by": None,
            "why": None,
            "since": None,
        }
        for rule in policy.RULES.values()
    ]


def _numbers(funnel: reach.Funnel | None) -> tuple[int, int, int]:
    return (funnel.views, funnel.favorites, funnel.orders) if funnel else (0, 0, 0)


def _venture_named(places: Mapping[int, Place], step_id: int | None) -> dict[str, Any]:
    """0.37.0: a venture's step names its venture (its title says only the decision)."""
    place = places.get(step_id) if step_id is not None else None
    if place is None or place.kind != "venture":
        return {"venture": None, "venture_title": None}
    return {"venture": place.venture, "venture_title": place.title}


def _step_brief(
    candidate: Candidate, parts: weights.Parts | None, titles: Mapping[int, str], places: Mapping[int, Place]
) -> dict[str, Any]:
    return {
        "id": candidate.step.id,
        "line": candidate.step.product,
        "title": candidate.step.title,
        "stage": candidate.stage,
        "waiting": candidate.waiting,
        "refused": candidate.refused,  # 0.42.0
        "weight": round(parts.total, 2) if parts else None,
        "why": parts.text(titles.get(parts.carried_from or 0)) if parts else None,
        **_venture_named(places, candidate.step.id),
    }


def _pick_view(pick: weights.Pick, titles: Mapping[int, str], places: Mapping[int, Place]) -> dict[str, Any] | None:
    if pick.step is None:
        return {
            "decided": pick.decided,
            "id": None,
            "title": None,
            "line": None,
            "weight": None,
            "why": None,
            **_venture_named(places, None),
        }
    return {
        "decided": pick.decided,
        "id": pick.step.id,
        "title": pick.step.title,
        "line": pick.step.product,
        "weight": round(pick.parts.total, 2) if pick.parts else None,
        "why": pick.parts.text(titles.get(pick.parts.carried_from or 0)) if pick.parts else None,
        **_venture_named(places, pick.step.id),
    }


def _picks(conn: sqlite3.Connection, scope: AgentScope, places: Mapping[int, Place]) -> list[dict[str, Any]]:
    where, params = scope.where("k")
    rows = conn.execute(
        "SELECT k.*, n.title AS step_title, p.title AS line_title, q.title AS product_title, c.project_id AS worked"
        " FROM plan_picks k LEFT JOIN plan_nodes n ON n.id = k.node_id LEFT JOIN projects p ON p.id = k.line"
        f" LEFT JOIN projects q ON q.id = k.product LEFT JOIN cycles c ON c.id = k.cycle_id WHERE {where}"
        " ORDER BY k.id DESC LIMIT ?",
        (*params, PICKS_SHOWN),
    ).fetchall()
    return [
        {
            "cycle": r["cycle_id"],
            "at": r["created_at"],
            "kind": r["kind"],
            "line": r["line"],
            "line_title": r["line_title"],
            "worked": r["worked"],
            "step": r["node_id"],
            "step_title": r["step_title"],
            "product": r["product"],
            "product_title": r["product_title"],
            "decided": r["decided"],
            "weight": r["weight"],
            "agrees": r["product"] is not None and r["product"] == (r["worked"] or r["line"]),
            **_venture_named(places, r["node_id"]),
        }
        for r in rows
    ]


def stamp(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """Changes whenever the tree, its words or its picks do (the tab reloads then); 0.37.8: and the publishing queue
    its Channels show (a pin or post approved, decided otherwise or made)."""
    where, params = scope.where()
    tree = conn.execute(f"SELECT COUNT(*), MAX(updated_at) FROM plan_nodes WHERE {where}", params).fetchone()
    picks = conn.execute(f"SELECT MAX(id) FROM plan_picks WHERE {where}", params).fetchone()
    words = conn.execute(f"SELECT MAX(id) FROM plan_words WHERE {where}", params).fetchone()
    freezes = conn.execute(f"SELECT MAX(id), MAX(lifted_at) FROM plan_freezes WHERE {where}", params).fetchone()
    queue = conn.execute(
        f"SELECT COUNT(*), MAX(version), MAX(decided_at), MAX(closed_at) FROM approvals WHERE {where}"
        " AND executor IN ('pinterest_pin', 'bluesky_post')",
        params,
    ).fetchone()
    return (
        f"{tree[0]}:{tree[1] or ''}:{picks[0] or 0}:{words[0] or 0}:{freezes[0] or 0}:{freezes[1] or ''}"
        f":{queue[0]}:{queue[1] or 0}:{queue[2] or ''}:{queue[3] or ''}"
    )


def channels_from(settings: Any) -> dict[str, bool]:
    """Which channels Ember can market in now, from the owner's options. 0.37.8: the blog by its own switch alone, as
    the cycles take it (loop.blog_on): since 0.34.0 it also needed the owner's website (site_enabled), another option,
    so with the website off the Plan tab and the keeper took the blog for off while the cycles wrote its posts (live,
    the tab's next steps were not the cycles')."""
    return {
        "pinterest": bool(getattr(settings, "pinterest_enabled", False)),
        "bluesky": bool(getattr(settings, "bluesky_enabled", False)),
        "blog": bool(getattr(settings, "blog_enabled", False)),
    }


def ventures_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    found: list[Candidate],
    parts: Mapping[int, weights.Parts],
    today: date,
) -> dict[str, Any] | None:
    """The Ventures as the owner's Plan tab shows them: their worth (the owner's, else each venture's own) and their
    hold, each venture being explored with its step (its weight now, or what it waits on) and what it needs decided,
    and the brainstorm when one is due. None before they are laid out."""
    top = ventures_node(conn, scope)
    if top is None:
        return None
    by_id = {c.step.id: c for c in found}
    rows = {int(v["id"]): v for v in ventures.all_ventures(conn, scope)}
    room = sum(1 for v in rows.values() if v["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE

    def shown(step: Mapping[str, Any]) -> dict[str, Any]:
        candidate, weight = by_id.get(int(step["id"])), parts.get(int(step["id"]))
        return {
            "id": step["id"],
            "title": step["title"],
            "decision": DECISION_OF.get(str(step["template"]), "brainstorm"),
            "pinned": bool(step["pinned"]),
            "waiting": candidate.waiting if candidate is not None else None,
            "weight": round(weight.total, 2) if weight else None,
            "why": weight.text() if weight else None,
        }

    items = []
    for here in nodes(conn, scope, "level = 'venture' AND status = 'open'"):
        v = rows.get(int(here["venture_id"]))
        steps = _open_steps(conn, scope, int(here["id"]))
        item = desk.item(conn, v, today=today, room=room) if v is not None else None
        items.append(
            {
                "node": here["id"],
                "venture": here["venture_id"],
                "title": v["title"] if v is not None else here["title"],
                "stage": v["stage"] if v is not None else None,
                "owner_worth": here["owner_worth"],
                "worth": here["owner_worth"]
                if here["owner_worth"] is not None
                else top["owner_worth"]
                if top["owner_worth"] is not None
                else venture_worth(conn, v),
                "step": shown(steps[0]) if steps else None,
                "needs": _needs(item, v),
            }
        )
    brainstorm = nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,))
    return {
        "id": top["id"],
        "worth": top["owner_worth"],  # the owner's for every venture, or None: each venture's own
        "owner_worth": top["owner_worth"],
        "hold": top["hold_reason"],
        "hold_by": top["hold_by"],
        "ventures": items,
        "brainstorm": (
            {**shown(brainstorm[0]), "needs": desk.brainstorm_due(list(rows.values()))} if brainstorm else None
        ),
    }


def _needs(item: desk.Item | None, v: Mapping[str, Any] | None) -> str | None:
    """What a venture needs now (desk.item), without its title: the Plan tab's row names it."""
    if item is None or v is None:
        return None
    title = " ".join(str(v["title"] or "").split())[:50]  # as desk.item begins its text
    return item.text.replace(f"{title} · ", "", 1)
