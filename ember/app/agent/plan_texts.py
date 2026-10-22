"""0.42.2 (analysis-0.40.1, 5.6): what the plan sees of the tree, split out of plan.py.

YOUR STEP (``step_text``; a venture's, ``_venture_step_text``): the step the cycle took, what done means, why the other
steps wait (``WAITS``) and the alternatives; YOUR PLAN (``plan_text``): the goal and a line for each product; and the
focus of a cycle's brief (``focus_text``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import desk, policy, store, templates, ventures, weights
from .plan_changes import frozen, owner_words
from .plan_model import (
    ALTERNATIVES,
    Facts,
    _cut,
    _quoted,
    _spec,
    _stages,
    _steps,
    check_words,
    current_stage_name,
    node,
    nodes,
)
from .plan_ready import _owners, _rejected
from .plan_steer import Steer
from .plan_ventures import DECISION_OF, _ventures_line, new_things_held
from .store import AgentScope

# --- what the plan sees of it ---

TAKEN = {  # 0.37.0: a promise or an owner's decision is weighed like any step (until 0.36.0 it came first)
    "pin": "your owner pinned it",
    "weight": "the heaviest step that is ready",
    "margin": f"you are on its product, and no other step is {round((weights.MARGIN - 1) * 100)}% heavier",
}
WAITS = {
    "owner": "your owner",
    "approved": "an approved request waiting for its slot or being carried out",  # 0.37.6; 0.37.8: the queue
    "channel": "a channel that isn't set up",
    "upgrade": "an upgrade",
    "date": "a date",
    "step": "another step",
    "hold": "a hold",
    "tried": "a change since a cycle took it today",  # 0.37.6: the brake
    "views": f"its listings' views ({weights.CRITIC_VIEWS} in all): a cover few have seen can't be judged",  # 0.39.6
    # 0.39.8 (analysis-0.39.6, 3.3): a line's weekly pin, post or blog post links its own listings, and it has none
    "listing": "a listing of its own (one of your listings counted for another line moves with project_update's "
    "listing_id)",
    "floor": "its channel to bring more than next to nothing (it is at its floor), or your owner's pin",  # 0.39.8
    # 0.37.9: the owner's limits (``limits``)
    "queue": "room in the publishing queue (your owner decides a request, or one goes out)",
    "sales": "room for a sell request (your owner decides a listing, a change or a product that waits for them)",
    "messages": "tomorrow, or your owner's next message (today's messages that answer none of theirs are sent)",
    # 0.40.3 (analysis-0.40.1 codebase, PL3): message_owner refuses every message while that many are unread
    "unread": f"your owner to read your messages (your last {store.MAX_UNREAD_MESSAGES} are unread: none goes through)",
    # 0.42.0 (analysis-0.40.1 codebase, 6.2.1): another rule its tool refuses by (preconditions.py), in its words
    "blocked": "what its tool would refuse now",
    # 0.42.1 (6.2 "A paid test in the plan"): a change of a listing in the owner's paid test (preconditions.PAID)
    "paid": "the end of your owner's paid test of its listings",
}
WORDED = ("blocked", "paid")  # 0.42.1: the waits YOUR STEP says with the tool's words (Candidate.refused)
QUESTIONS = "This week's questions (keep them in mind; not steps to take):"
NEW_PRODUCT = "Start a new product (project_create): Ember's code lays it out with its stages and steps."
TRIED = (  # 0.37.6: no step ready, as the brake holds steps a cycle took today without anything moving
    "Nothing new starts while steps of yours wait for a change or tomorrow (a cycle took them today and nothing they"
    " are checked by moved): deal with what is owed, then end the cycle."
)


def step_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    steered: Steer,
    *,
    explore: bool,
    questions: list[str] | None = None,
    cash_eur: float = 0.0,
    net_days: float | None = None,
    forecasts: str = "",
    today: date | None = None,
) -> str:
    """YOUR STEP, as an ordinary or marketing plan sees it: the step Ember's code took, what done means and why it was
    taken, what comes after it in its stage, and the next heaviest steps; with no step ready, what waits and whether a
    new product may start (the explore burn mode). Its most important lines come first: the context cuts the end.
    0.37.0: a venture plan's too (in place of READY): its venture's decision and what it needs now (desk.item, with
    the cash a venture may need, ``cash_eur``, the net runway, ``net_days``, and the day, ``today``), and the record of
    the agent's forecasts (``forecasts``)."""
    if steered.kind == "venture" and steered.step is not None:
        return _venture_step_text(conn, scope, steered, cash_eur, net_days, forecasts, today)
    lines: list[str] = []
    step = steered.step
    if step is not None:
        row = node(conn, scope, step.id)
        product = nodes(conn, scope, "level = 'product' AND project_id = ?", (step.product,)) if step.product else []
        where = ""
        if product:
            kind = templates.by_key(product[0]["template"]).title
            where = f" of product line #{step.product} {_quoted(product[0]['title'])} ({kind})"
        stage = f", stage {row['stage']}" if row is not None and row["stage"] else ""
        lines.append(f"Step #{step.id}{where}{stage}: {_cut(step.title, 160)}")
        if row is not None and row["kind"] == "promise":  # 0.35.1: the promise itself, with where its product stands
            lines.append("Done when: you kept it and closed it with obligation_done.")
            stages = _stages(conn, scope, int(product[0]["id"])) if product else []
            ahead = [
                s
                for part in stages
                if part["status"] == "open"
                for s in _steps(conn, scope, part["id"])
                if s["status"] == "open" and s["kind"] != "promise"
            ]
            if ahead:
                lines.append(
                    "Its product's open steps: "
                    + " · ".join(f"#{s['id']} {s['title']} ({s['stage']})" for s in ahead[:4])
                )
        elif row is not None and row["check_kind"]:
            done = (
                "you say it is done (plan_step done)"
                if row["check_kind"] == "agent"
                else f"{check_words(row['check_kind'], _spec(row))} (Ember's code checks it)"
            )
            lines.append(f"Done when: {done}.")
        rejected = _rejected(conn, scope, row, step.product) if row is not None else None
        if rejected is not None:  # 0.42.1 (analysis-0.40.1 codebase, 6.2 "A rework Ember can decline")
            lines.append(
                f"Your owner rejected request #{rejected}: propose it again as they asked. If their rejection means"
                " not this, close the step with plan_step decline, why in a sentence (your owner sees it)."
            )
        if row is not None and row["check_kind"] == "critic" and step.product is not None:
            from . import quality  # here, not at the top: quality imports prompts, which imports tools, then this

            lines += [f"- {_cut(f, 260)}" for f in quality.fixes(conn, scope, step.product)[:3]]
        parts = steered.pick.parts
        if parts is not None:
            carried = node(conn, scope, parts.carried_from) if parts.carried_from else None
            lines.append(
                f"Why: {TAKEN.get(steered.pick.decided, steered.pick.decided)}. Weight "
                + parts.text(str(carried["title"]) if carried is not None else None)
            )
        if row is not None and row["parent_id"] is not None:
            after = [
                s
                for s in _steps(conn, scope, row["parent_id"])
                if s["status"] == "open" and s["id"] != step.id and s["seq"] >= row["seq"] and s["kind"] != "promise"
            ]
            if after:
                lines.append("Next in this stage: " + " · ".join(f"#{s['id']} {s['title']}" for s in after[:3]))
    else:
        waiting: dict[str, int] = {}
        for c in steered.found:
            if c.waiting is not None and c.stage != "venture":  # YOUR PLAN's Ventures line says what they wait on
                # 0.42.0: a tool's other refusal in its words (one group each); 0.42.1: a paid test's too
                why = f"{WAITS[c.waiting]} ({c.refused})" if c.waiting in WORDED and c.refused else c.waiting
                waiting[why] = waiting.get(why, 0) + 1
        said = ", ".join(f"{n} on {WAITS.get(why, why)}" for why, n in sorted(waiting.items(), key=lambda w: -w[1]))
        lines.append(f"No step of your plan is ready{f' ({said})' if said else ''}.")
        held = new_things_held(conn, scope) if explore else None
        if held:  # 0.36.0: the owner's "nothing new", which project_create keeps too
            lines.append(
                f"Nothing new starts: your owner holds new things ({_cut(held, 80)}): deal with what is owed, then end"
                " the cycle."
            )
        elif explore and any(c.waiting == "tried" for c in steered.found):
            lines.append(TRIED)  # 0.37.6: a step that waits for tomorrow is still the plan's work, not room for new
        else:
            lines.append(
                NEW_PRODUCT
                if explore
                else "Nothing new starts in this burn mode: deal with what is owed, then end the cycle."
            )
    others = [(s, p) for s, p in steered.pick.ranked if step is None or s.id != step.id][:ALTERNATIVES]
    if others:
        places = _places(conn, scope)
        lines.append(
            "Next heaviest: "
            + " · ".join(f"#{s.id} {_cut(s.title, 60)} ({_line_of(s, places)}, {p.total:.1f})" for s, p in others)
        )
    if questions:
        lines.append(QUESTIONS)
        lines += [f"- {_cut(q, 200)}" for q in questions]
    return "\n".join(lines)


VENTURE_DONE = {  # 0.37.0: when a venture's step is done (Ember's code reads the venture's stage and records)
    "triage": "you researched it (stage researching) or parked it with why (venture_update).",
    "appraise": (
        "you saved what your research found (evidence, venture_update learned) and rescored it; once its research is"
        " done, its business case (venture_case, stage proposed) or parked with why. 0.37.6: a cycle that saves no"
        " evidence, case or stage leaves the step until tomorrow (a note or a rescore alone moves nothing)."
    ),
    "answer": "you answered the critic with evidence or new numbers (venture_case), or parked it with why.",
    "brainstorm": "your brainstorm (the brainstorm tool) added its ideas to your tree.",
}


def _venture_step_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    steered: Steer,
    cash_eur: float,
    net_days: float | None,
    forecasts: str,
    today: date | None,
) -> str:
    """YOUR STEP for a venture cycle (0.37.0): the venture's decision Ember's code took, what it needs now, its next
    question, what done means, why it was taken, the record of the agent's forecasts and the next heaviest steps."""
    step = steered.step
    assert step is not None
    row = node(conn, scope, step.id)
    decision = DECISION_OF.get(str(row["template"]), "brainstorm") if row is not None else "brainstorm"
    v = ventures.get(conn, scope, steered.venture) if steered.venture is not None else None
    lines: list[str] = []
    if v is not None:
        lines.append(f"Step #{step.id} of venture #{v['id']} {_quoted(v['title'])} ({v['stage']}): {step.title}")
        everyone = ventures.all_ventures(conn, scope)
        room = sum(1 for x in everyone if x["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE
        item = (
            desk.item(conn, v, today=today, cash_eur=cash_eur, net_days=net_days, room=room)
            if today is not None
            else None
        )
        if item is not None:
            lines.append(f"Now: {item.text}")
        if decision == "appraise" and v["next_question"]:
            lines.append(f"Its next question: {_cut(' '.join(str(v['next_question']).split()), 300)}")
        others = _triages_waiting(conn, scope, int(step.id)) if decision == "triage" else []
        if others:  # 0.39.1 (analysis-0.38.0, 3.3)
            lines.append(
                "Other ideas wait for their triage: "
                + " · ".join(f"#{o['id']} {_quoted(_cut(str(o['title']), 60))}" for o in others[:TRIAGES_SHOWN])
                + (f" and {len(others) - TRIAGES_SHOWN} more" if len(others) > TRIAGES_SHOWN else "")
                + ": triage them in this cycle too (venture_update each: park the ones you would park, with why;"
                " research the one that deserves it most). A cycle for each costs more than its decision."
            )
    else:
        lines.append(f"Step #{step.id} of your ventures: {step.title}")
        due = desk.brainstorm_due(ventures.all_ventures(conn, scope))
        if due:
            lines.append(f"Now: {due}.")
    lines.append(f"Done when: {VENTURE_DONE.get(decision, VENTURE_DONE['brainstorm'])}")
    parts = steered.pick.parts
    if parts is not None:
        lines.append(f"Why: {TAKEN.get(steered.pick.decided, steered.pick.decided)}. Weight {parts.text(None)}")
    if forecasts:
        lines.append(f"Your forecasts, settled by Ember's code: {forecasts}.")
    others = [(s, p) for s, p in steered.pick.ranked if s.id != step.id][:ALTERNATIVES]
    if others:
        places = _places(conn, scope)
        lines.append(
            "Next heaviest: "
            + " · ".join(f"#{s.id} {_cut(s.title, 60)} ({_line_of(s, places)}, {p.total:.1f})" for s, p in others)
        )
    return "\n".join(lines)


TRIAGES_SHOWN = 8  # 0.39.1: the other ideas a triage's YOUR STEP names


def _triages_waiting(conn: sqlite3.Connection, scope: AgentScope, step_id: int) -> list[sqlite3.Row]:
    """0.39.1: the other ideas whose triage is open and not put off (plan_step wait), oldest first. Live, six cycles
    in a row each parked one idea of a brainstorm, $1.03, 30 minutes apart: one cycle could have parked all six."""
    found = []
    for s in nodes(conn, scope, "level = 'step' AND status = 'open' AND template = 'triage@1' AND waiting IS NULL"):
        parent = node(conn, scope, int(s["parent_id"])) if s["id"] != step_id and s["parent_id"] else None
        v = ventures.get(conn, scope, int(parent["venture_id"])) if parent and parent["venture_id"] else None
        if v is not None and v["stage"] == "idea":
            found.append(v)
    return found


def _line_of(step: weights.Step, places: Mapping[int, Place]) -> str:
    """Where a ranked step belongs: its product line, or (0.37.0) its venture, the ventures (a brainstorm) or the
    owner (a promise or decision of no product)."""
    if step.product is not None:
        return f"line #{step.product}"
    place = places.get(step.id)
    if place is not None and place.kind == "venture":
        return f"venture #{place.venture}"
    return "for your owner" if place is not None and place.kind == "owner" else "your ventures"


@dataclass(frozen=True)
class Place:
    """0.37.0: where a step of no product line belongs: a venture's (``venture``, its node's ``title``), the ventures'
    as a whole (a brainstorm) or the owner's (a promise or decision of no product)."""

    kind: str  # 'venture', 'ventures' or 'owner'
    venture: int | None = None
    title: str = ""


def _places(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, Place]:
    """Each step of no product line, open or closed, with where it belongs (Place)."""
    where, params = scope.where("s")
    found: dict[int, Place] = {}
    for r in conn.execute(
        "SELECT s.id, p.level, p.platform, p.venture_id, p.title FROM plan_nodes s JOIN plan_nodes p"
        f" ON p.id = s.parent_id WHERE {where} AND s.level = 'step' AND s.project_id IS NULL",
        params,
    ):
        if r["level"] == "venture":
            found[int(r["id"])] = Place("venture", int(r["venture_id"]), str(r["title"]))
        elif r["platform"] in ("ventures", "owner"):
            found[int(r["id"])] = Place(str(r["platform"]))
    return found


def plan_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    goal: str = "",
    milestones: list[str] | None = None,
    since: str | None = None,
    closed: list[str] | None = None,
) -> str:
    """The planner's YOUR PLAN (0.35.0, in place of the roadmap): the owner's goal and how far it got (``goal``:
    roadmap.root_line), what the owner did to the plan since the last cycle (``since``: its end), then each project's
    open products with their stage and numbers, what waits on the owner, and the milestones still open
    (``milestones``: a venture's first test, the owner's own) and those Ember's code closed since (``closed``). A
    product's steps are YOUR STEP's to show, and the owner's Plan tab shows the whole tree: this stays short however
    big the tree grows."""
    lines = [f"Today: {today:%A} {today.isoformat()}."]
    if goal:
        lines.append(goal)
    words = owner_words(conn, scope, since)
    if words:
        lines.append("Your owner since your last cycle: " + "; ".join(words) + ".")
    until = frozen(conn, scope, today)
    if until:  # 0.36.0: the owner's freeze, which Ember's code keeps (propose_etsy_edit, propose_printify_edit refuse)
        lines.append(f"Your owner froze the titles and tags of your listings until {until}: change neither.")
    head = len(lines)
    facts = Facts(conn, scope, now)
    for top, products in grouped(conn, scope, "status = 'open'"):
        lines.append(f"{top['title']}: " + " · ".join(_product_line(conn, scope, facts, p) for p in products))
    owners = [  # 0.37.6: those with something for them to decide (live, "You approve it" with no request waiting)
        f"#{s['id']} {_cut(s['title'], 50)} (line #{s['project_id']})"
        for s in nodes(conn, scope, "level = 'step' AND status = 'open' AND kind = ?", (templates.OWNER_KIND,))
        if (s["project_id"] is None or _product_open(conn, scope, int(s["project_id"])))
        and _owners(facts, s, s["project_id"]) == "owner"
    ]
    if owners:
        lines.append("Waiting on your owner: " + " · ".join(owners[:6]))
    if milestones:
        lines.append("Milestones still open:")
        lines += milestones
    if closed:
        lines.append("Since your last cycle, Ember's code closed from its records: " + "; ".join(closed) + ".")
    if len(lines) == head:
        lines.append("Your plan has no products yet: your first one starts it.")
    explore = _ventures_line(conn, scope)  # 0.36.0: the ventures, a step of the plan
    if explore:
        lines.append(explore)
    return "\n".join(lines)


def grouped(conn: sqlite3.Connection, scope: AgentScope, condition: str = "1") -> list[tuple[Any, list[Any]]]:
    """Each project with its products (``condition``: which), each under its type's project: a product re-typed
    from generic (``_retemplate``) stays under its first one in the records (a node's place is fixed)."""
    tops = nodes(conn, scope, "level = 'project'")
    homes = {str(t["platform"]): int(t["id"]) for t in tops}
    found: dict[int, list[Any]] = {int(t["id"]): [] for t in tops}
    for product in nodes(conn, scope, f"level = 'product' AND ({condition})"):
        found[homes.get(templates.by_key(product["template"]).platform, int(product["parent_id"]))].append(product)
    return [(t, found[int(t["id"])]) for t in tops if found[int(t["id"])]]


def _product_open(conn: sqlite3.Connection, scope: AgentScope, line: int) -> bool:
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    return bool(found) and found[0]["status"] == "open"


def _product_line(conn: sqlite3.Connection, scope: AgentScope, facts: Facts, product: Mapping[str, Any]) -> str:
    """One product in PLAN: its line, title, stage with its steps done, its numbers once live, a promise due, a hold."""
    line = int(product["project_id"])
    stage = current_stage_name(conn, scope, line) or "-"
    funnel = facts.funnel(line)
    numbers = (
        f" · {funnel.views} views, {funnel.favorites} favorites, {funnel.orders} orders"
        if funnel is not None and funnel.listings
        else ""
    )
    promised = [
        f"promise #{s['obligation_id']} due {s['due']}"
        for s in nodes(
            conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND kind = 'promise'", (line,)
        )
    ]
    promise = f" · {', '.join(promised)}" if promised else ""
    held = f": {_cut(product['hold_reason'], 60)}" if product["hold_reason"] else ""
    own = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    unlocked = policy.unlocked_text(policy.unlocked(conn, scope, int(own["id"])), True) if own is not None else ""
    unlocks = f" · {unlocked}" if unlocked else ""  # 0.16.3 (analysis bug 5): what your owner unlocked for it
    return f"#{line} {_cut(product['title'], 40)} ({stage}{held}{numbers}{promise}{unlocks})"


def focus_text(conn: sqlite3.Connection, scope: AgentScope, steered: Steer) -> str:
    """The brief's FOCUS on the cycle's step: what it is and what done means (the plan saw the rest)."""
    step = steered.step
    if step is None:
        return ""
    row = node(conn, scope, step.id)
    said = f"Your step this cycle: #{step.id} {_cut(step.title, 160)}"
    if row is not None and row["check_kind"]:
        done = (
            "say so with plan_step done"
            if row["check_kind"] == "agent"
            else f"{check_words(row['check_kind'], _spec(row))}, which Ember's code checks"
        )
        said += f"\nDone when: {done}."
    return said
