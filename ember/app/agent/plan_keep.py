"""0.42.2 (analysis-0.40.1, 5.6): the plan tree's keeper, split out of plan.py.

``keep`` runs before every plan: it lays out new product lines (plan_layout.py), closes what the checks show done and
the products of closed lines, puts each promise to the owner under its product (``_promises``, ``_owner_owed``),
makes steps of the owner's decisions (``_decisions``), proposes again what a stage still needs of the owner
(``_again``, ``_fresh_demand``), adds a live product's recurring marketing (``_recurring``) and the dates it is
decided on (``_decide_by``), carries out the daily review's stops, holds and changes (``_review_stops``,
``_review_holds``, ``_review_changes``) and takes up the steps whose waits are over (``_lift_waits``). ``move``
closed the records the tree took the place of (0.35.0). ``keep`` reads ``_ventures`` through plan, where tests patch
it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso, local_date
from . import demand, obligations, paid, policy, reach, roadmap, store, templates, ventures
from .plan_layout import _platform, _reaudience, _retemplate, _untype, could_earn, lay_out
from .plan_model import (
    CHANNEL_AUDIENCES,
    CHANNELS,
    DAY7_VIEWS,
    DAY14_FAVORITES,
    DAY14_VIEWS,
    MOVED_KEY,
    PROMISE_CHANNELS,
    PUSH_DAYS,
    RECURRING,
    REPORT,
    SCALE,
    Facts,
    _close,
    _cut,
    _descendants,
    _holds,
    _ids,
    _insert,
    _make_room,
    _open_steps,
    _quoted,
    _replaced,
    _spec,
    _stages,
    _steps,
    _update,
    change,
    current_stage,
    node,
    nodes,
    passes,
)
from .plan_ready import _at_floor, _clocks, _rejected
from .store import CLOSED_STATUSES, OPEN_STATUSES, AgentScope

# --- keeping the tree ---


def keep(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    *,
    day_start: str | None = None,
) -> list[str]:
    """Lays out new product lines, closes what its checks show done, adds promise and recurring steps, closes the
    products of closed lines; returns what happened, for the System log. 0.37.9: ``day_start``, when the owner's day
    began (their clock's): the day's messages count from then (``limits``)."""
    from .plan import _ventures  # 0.42.2: read through plan, where tests patch it

    said: list[str] = []
    facts = Facts(conn, scope, now)
    where, params = scope.where()
    products = {r["project_id"]: r for r in nodes(conn, scope, "level = 'product'")}
    laid: set[int] = set()
    for project in conn.execute(f"SELECT * FROM projects WHERE {where} ORDER BY id", params).fetchall():
        product = products.get(project["id"])
        if product is None and project["status"] in OPEN_STATUSES:
            product_id = lay_out(conn, scope, project, now)
            laid.add(product_id)
            said.append(f"Plan tree: line #{project['id']} laid out as a product (#{product_id}).")
        elif product is not None and product["status"] == "open" and project["status"] in CLOSED_STATUSES:
            owed = _close_product(conn, scope, product, project, now)
            closed = f" What was owed on it closed with it: obligation {_ids(owed)}." if owed else ""  # 0.40.3
            said.append(f"Plan tree: product #{product['id']} closed with line #{project['id']}.{closed}")
    said += move(conn, scope, now)  # once: the milestones the tree takes the place of (migration 0088)
    said += _link_promises(conn, scope)
    decided = _decided(conn, scope)
    for product in nodes(conn, scope, "level = 'product' AND status = 'open'"):
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (product["project_id"],)).fetchone()
        if project is not None:
            retyped = _untype(conn, scope, product, project, now) or _retemplate(conn, scope, product, project, now)
            if retyped:
                said += retyped
                product = node(conn, scope, int(product["id"])) or product
            template = templates.by_key(product["template"])
            earn = could_earn(conn, scope, project, template.could_earn)
            if earn != product["could_earn"]:
                _update(conn, product["id"], now, could_earn=earn)
            if product["id"] not in laid:
                said += _reaudience(conn, scope, product, project, now)  # 0.37.8
                product = node(conn, scope, int(product["id"])) or product
        closed = _close_done(facts, product, now)
        said += closed if product["id"] not in laid else []  # a new product's stages already done: no news
        said += _promises(facts, product, now)
        said += _decisions(facts, product, decided.get(int(product["project_id"]), []), now)
        said += _again(facts, product, now)  # 0.37.6: a request its stage still needs, proposed again
        said += _fresh_demand(facts, product, now)  # 0.40.3: and a new demand note before a first listing's
        said += _recurring(facts, product, now, today, channels)
        said += _decide_by(facts, product, now, today)
    said += _ventures(conn, scope, now)  # 0.37.0: each venture a node, its next decision a step
    said += _owner_owed(conn, scope, now)  # 0.37.0: and what is owed to the owner of no product
    said += _review_stops(conn, scope, now, today)  # 0.39.1: a line the review says stop is the owner's to close
    said += _review_holds(conn, scope, now, today)  # 0.40.2: and it waits on hold until they decide
    said += _review_changes(conn, scope, now, today)  # 0.39.6: and a change it decides is a step of the line's
    said += _lift_waits(conn, scope, now, today)
    _clocks(conn, scope, now, today, channels, day_start)
    return said


def _close_product(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    project: Mapping[str, Any],
    now: str,
    by: str = "code",
) -> list[int]:
    """A product whose line closed, with everything under it and its own milestone (its unlocks end with it). 0.40.3
    (analysis-0.40.1 codebase, PL6): and what was owed on it, its promises and the owner's decisions whose steps close
    here (obligations.close_one, by Ember's code): a closed step is final and an obligation gets one step, so it stayed
    open, and no plan laid it out again. Returns the obligations closed."""
    ended = "done" if project["status"] == "succeeded" else "dropped"
    rows = _descendants(conn, scope, product["id"])
    under = {int(r["id"]) for r in rows}
    # 0.40.3: a promise's or a decision's step stays open under a stage done before it (it closes with its obligation)
    kept = "level = 'step' AND status = 'open' AND obligation_id IS NOT NULL AND project_id = ?"
    rows += [r for r in nodes(conn, scope, kept, (product["project_id"],)) if int(r["id"]) not in under]
    owed: list[int] = []
    why = "line closed by your owner" if by == "owner" else f"its line closed ({project['status']})"
    for row in rows:
        _close(conn, row["id"], now, ended, f"its line closed: {project['status']}")
        if row["obligation_id"] is not None and obligations.close_one(
            conn, int(row["obligation_id"]), why, "code", None, now
        ):
            owed.append(int(row["obligation_id"]))
    _close(conn, product["id"], now, ended, f"its line closed: {project['status']}", by=by)
    _close_milestone(conn, product["id"], now, f"Its product (line #{project['id']}) closed.")
    return owed


def _close_done(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    conn, scope = facts.conn, facts.scope
    said: list[str] = []
    stages = _stages(conn, scope, product["id"])
    for stage in stages:
        if stage["status"] != "open":
            continue
        for step in _steps(conn, scope, stage["id"]):  # a promise's or a decision's: _promises closes it
            if step["status"] == "open" and step["obligation_id"] is None and _holds(facts, step):
                _close(conn, step["id"], now, "done", "Ember's code: its check passed")
    done_upto = -1
    for index, stage in enumerate(stages):
        passed = stage["status"] == "open" and bool(stage["check_kind"]) and _holds(facts, stage)
        if stage["status"] == "done" or passed:
            done_upto = index
    for index, stage in enumerate(stages):
        if stage["status"] != "open":
            continue
        open_steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] == "open"]
        steps_done = (
            not open_steps
            and bool(_steps(conn, scope, stage["id"]))
            and not stage["check_kind"]
            and stage["stage"] != "maintain"  # it recurs: never done
        )
        if index <= done_upto or steps_done:
            moot = "a later stage is done" if index < done_upto else "the stage is done"
            for step in open_steps:
                if step["obligation_id"] is None:  # a promise or a decision closes with its obligation only
                    _close(conn, step["id"], now, "done", f"Ember's code: {moot}")
            _close(conn, stage["id"], now, "done", "Ember's code: its check passed" if index == done_upto else moot)
            said.append(f"Plan tree: product #{product['id']}'s {stage['stage']} stage is done.")
    return said


def _promises(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    conn, scope = facts.conn, facts.scope
    where, params = scope.where()
    said: list[str] = []
    for promise in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind = 'promise' AND project_id = ? AND status = 'open'"
        " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL)",
        (*params, product["project_id"]),
    ).fetchall():
        stage = current_stage(conn, scope, product["id"])
        if stage is None:
            continue
        what = " ".join(str(promise["what"]).split())
        _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=product["project_id"],
            stage=stage["stage"],
            kind="promise",
            channel=promise_channel(what),
            title=f"Keep promise #{promise['id']}: {what}"[:160],
            check_kind="obligation",
            check_spec=json.dumps({"id": promise["id"]}),
            seq=900 + int(promise["id"]) % 100,
            source="promise",
            obligation_id=promise["id"],
            due=promise["due"],
        )
        said.append(f"Plan tree: promise #{promise['id']} is a step of product #{product['id']}.")
    for step in nodes(  # 0.35.0: and a decision's step, in whatever stage (a later stage done leaves it open)
        conn,
        scope,
        "level = 'step' AND obligation_id IS NOT NULL AND status = 'open' AND project_id = ?",
        (product["project_id"],),
    ):
        if _holds(facts, step):
            kept = "the promise was kept" if step["kind"] == "promise" else "the obligation was closed"
            _close(conn, step["id"], now, "done", f"Ember's code: {kept}")
    return said


def _owner_owed(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """0.37.0: a promise to the owner, or their decision, of no product line is a step of the Owner project, weighed
    like any step (until 0.36.0 only an obligation in OBLIGATIONS, its pressing ones coming first by a rule of their
    own): done once its obligation is closed. Laid out after a promise's words could name its product
    (_link_promises), and never moved there later (a node's place is fixed). 0.37.6: a promise of pins, a Bluesky
    post or a blog post has its channel, as one of a product has, so its cycle is a marketing cycle with that channel's
    tools (an ordinary one couldn't keep it, and took every cycle); one laid out before gets it at the next keep."""
    where, params = scope.where()
    said: list[str] = []
    owed = [
        row
        for row in conn.execute(
            f"SELECT * FROM obligations WHERE {where} AND kind IN ('promise', 'decision') AND status = 'open'"
            " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL) ORDER BY id",
            params,
        ).fetchall()
        if obligations.owed(conn, scope, row).line is None
    ]
    if owed:
        top = _platform(conn, scope, "owner", now)
        for row in owed:
            what = " ".join(str(row["what"]).split())
            promise = row["kind"] == "promise"
            _insert(
                conn,
                scope,
                now,
                parent_id=top,
                level="step",
                kind="promise" if promise else "fix",
                channel=promise_channel(what) if promise else None,
                title=(f"Keep promise #{row['id']}: {what}" if promise else f"Obligation #{row['id']}: {what}")[:160],
                check_kind="obligation",
                check_spec=json.dumps({"id": row["id"]}),
                source="promise" if promise else "code",
                obligation_id=row["id"],
                due=row["due"],
            )
            said.append(f"Plan tree: obligation #{row['id']} ({row['kind']}, of no product) is a step of your owner's.")
    top_row = nodes(conn, scope, "level = 'project' AND platform = 'owner'")
    if top_row:
        facts = Facts(conn, scope, now)
        for step in _open_steps(conn, scope, int(top_row[0]["id"])):
            if _holds(facts, step):
                kept = "the promise was kept" if step["kind"] == "promise" else "the obligation was closed"
                _close(conn, step["id"], now, "done", f"Ember's code: {kept}")
            elif step["kind"] == "promise" and step["channel"] is None and promise_channel(str(step["title"])):
                _update(conn, step["id"], now, channel=promise_channel(str(step["title"])))
    return said


def promise_channel(what: str) -> str | None:
    """0.35.1: the channel a promise's words name (PROMISE_CHANNELS), None for none. 0.37.6: none for a report ("Report
    the Bluesky reactions of the week"): a report is told in a message, not made with a channel's tools (live on
    2026-10-09, six of the nine promises open were reports; an Owner promise of a channel's work has its channel
    now)."""
    if REPORT.search(what):
        return None
    return next((channel for channel, words in PROMISE_CHANNELS if words.search(what)), None)


def _link_promises(conn: sqlite3.Connection, scope: AgentScope) -> list[str]:
    """0.35.1: an open promise made without naming its product gets the one its words name (obligations.promised_line:
    a listing's number, KDP), so it becomes a step of that product."""
    where, params = scope.where()
    said: list[str] = []
    for row in conn.execute(
        f"SELECT id, what FROM obligations WHERE {where} AND kind = 'promise' AND status = 'open'"
        " AND project_id IS NULL ORDER BY id",
        params,
    ).fetchall():
        line = obligations.promised_line(conn, scope, str(row["what"]))
        if line is not None and obligations.name_line(conn, int(row["id"]), line):
            said.append(f"Plan tree: promise #{row['id']} names line #{line}: a step of that product now.")
    return said


def _decided(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, list[tuple[Any, bool]]]:
    """0.35.0: the open decisions of the owner's that aren't steps yet, by the product line they are about
    (obligations.owed: a request's line), each with whether it is marketing work (a pin, a post, a blog post)."""
    where, params = scope.where()
    found: dict[int, list[tuple[Any, bool]]] = {}
    for row in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind = 'decision' AND status = 'open'"
        " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL) ORDER BY id",
        params,
    ).fetchall():
        owed = obligations.owed(conn, scope, row)
        if owed.line is not None:
            found.setdefault(owed.line, []).append((row, owed.marketing))
    return found


def _decisions(facts: Facts, product: Mapping[str, Any], decided: list[tuple[Any, bool]], now: str) -> list[str]:
    """0.35.0: an owner's decision on one of the product's requests (rejected, carried out, failed) is a step of its
    current stage, the reaction it asks for: done once its obligation is closed. Live, a rejected listing left its
    product waiting for an approval that never came; now the rejection itself is the next step."""
    conn, scope = facts.conn, facts.scope
    said: list[str] = []
    stage = current_stage(conn, scope, product["id"])
    if stage is None:
        return said
    for row, marketing in decided:
        what = " ".join(str(row["what"]).split())
        step = _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=product["project_id"],
            stage=stage["stage"],
            kind="market" if marketing else "fix",
            title=f"Obligation #{row['id']}: {what}"[:160],
            check_kind="obligation",
            check_spec=json.dumps({"id": row["id"]}),
            seq=800 + int(row["id"]) % 100,
            source="code",
            obligation_id=row["id"],
            due=row["due"],
        )
        change(conn, scope, step, None, "code", "add", f"your owner's decision: obligation #{row['id']}", now)
        said.append(f"Plan tree: obligation #{row['id']} (a decision) is a step of product #{product['id']}.")
    return said


def _again(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    """0.37.6: a request to the owner that a product's stage still needs, proposed again by a step of Ember's code's.
    Once the stage has no step of Ember's open, and none of the kinds of request its check or its steps need is
    pending, approved or carried out for the product, it gets one: "Propose ... again" when an earlier one expired, was
    withdrawn, rejected or failed (the step that proposed it had closed on it, and closed is final: in the analysis of
    0.37.0 the product never had a ready step again, and "You approve it" waited on the owner with nothing waiting
    for them), else the template's step that proposes it (the create stage's check needs the listing proposed, its
    files and photos made). Its check: a request made since then. One at a time."""
    conn, scope = facts.conn, facts.scope
    stage = current_stage(conn, scope, product["id"])
    if stage is None or stage["stage"] == "maintain":
        return []
    steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] != "dropped"]
    if any(s["status"] == "open" and s["obligation_id"] is None and s["kind"] != templates.OWNER_KIND for s in steps):
        return []  # a step of Ember's to take
    line = int(product["project_id"])
    needed = [*_asks(stage["check_kind"], _spec(stage)), *(e for s in steps for e in _asks(s["check_kind"], _spec(s)))]
    for executor in dict.fromkeys(needed):
        if passes(facts, line, "request", {"executor": executor}):
            continue  # one is on its way, or carried out
        where, params = scope.where("a")
        ended = conn.execute(
            "SELECT a.id, a.status FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
            f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = ?"
            " ORDER BY a.id DESC LIMIT 1",
            (*params, line, executor),
        ).fetchone()
        if ended is not None and _declined(conn, scope, line, executor, int(ended["id"])):
            continue  # 0.42.1: Ember won't propose it again (decline_step), until a newer one of its kind is decided
        title = _proposes(product, executor)
        if ended is not None:
            title = f"{title} again: request #{ended['id']} {ended['status']}"
        seq = next((int(s["seq"]) for s in steps if executor in _asks(s["check_kind"], _spec(s))), None)
        if seq is None:
            seq = max((int(s["seq"]) for s in steps if s["obligation_id"] is None), default=-1) + 1
        step = _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=line,
            template=f"propose/{executor}"[:80],
            stage=stage["stage"],
            kind="ship",
            title=_cut(title, 160),
            check_kind="request",
            check_spec=json.dumps({"executor": executor, "since": now}),
            seq=seq,
            source="code",
        )
        why = f"request #{ended['id']} {ended['status']}" if ended is not None else "its stage needs it proposed"
        change(conn, scope, step, None, "code", "add", f"{why}: none waits for your owner", now)
        return [f"Plan tree: line #{line}'s {stage['stage']} stage needs a request to your owner: {_quoted(title)}."]
    return []


FRESH_DEMAND = "demand/fresh"  # 0.40.3: a new demand note before a line's first listing is proposed (_fresh_demand)


def _fresh_demand(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    """0.40.3 (analysis-0.40.1 codebase, PL4): a step of the current stage that proposes the line's first listing
    (its check asks for an Etsy listing's or a Printify product's request: "Propose the listing", or proposing it again,
    ``_again``) gets a step for a new demand note in front of it while the line's newest is older than demand.DAYS
    (demand.needs_note, the rule propose_etsy_listing and propose_printify_product refuse it by): the research stage's
    note closes that stage once, for good (a line with no note yet has the research stage's step for it). In the
    audit, "Propose the listing again" was urgent (0.40.1: until it is proposed) 16 days after the note, and the tool
    refused it at once. In a stage of steps one after another, the proposal waits behind the note and lends it its
    weight."""
    conn, scope = facts.conn, facts.scope
    stage = current_stage(conn, scope, product["id"])
    if stage is None or stage["stage"] == "maintain":
        return []
    steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] == "open" and s["obligation_id"] is None]
    proposing = [
        s
        for s in steps
        if s["kind"] != templates.OWNER_KIND and set(_asks(s["check_kind"], _spec(s))) & set(demand.FIRST_LISTING)
    ]
    line = int(product["project_id"])
    if not proposing or any(s["check_kind"] == "demand" for s in steps) or not demand.needs_note(conn, line, now):
        return []
    where, params = scope.where()
    newest = conn.execute(
        f"SELECT MAX(created_at) FROM demand_notes WHERE {where} AND project_id = ?", (*params, line)
    ).fetchone()[0]
    if newest is None:
        return []  # its research stage's step writes the first one
    of = f"the newest is of {local_date(str(newest))}"
    title = f"Write a new demand note: a first listing needs one from the last {demand.DAYS} days ({of})"
    seq = int(proposing[0]["seq"])
    _make_room(conn, stage["id"], seq, 1, now)
    step = _insert(
        conn,
        scope,
        now,
        parent_id=stage["id"],
        level="step",
        project_id=line,
        template=FRESH_DEMAND,
        stage=stage["stage"],
        kind="create",
        title=_cut(title, 160),
        check_kind="demand",
        check_spec=json.dumps({"days": demand.DAYS}),
        seq=seq,
        source="code",
    )
    why = f"step #{proposing[0]['id']} proposes the line's first listing, which needs a demand note from the last"
    change(conn, scope, step, None, "code", "add", f"{why} {demand.DAYS} days ({of})", now)
    return [f"Plan tree: line #{line} needs a new demand note before its first listing is proposed: step #{step}."]


DECLINED = re.compile(r"declined request #(\d{1,18}): ")  # 0.42.1: how a step Ember declined closed (decline_step)


def _declined(conn: sqlite3.Connection, scope: AgentScope, line: int, executor: str, request_id: int) -> bool:
    """0.42.1: whether Ember declined to propose request ``request_id`` again (``decline_step``: its step's result
    says which; a closed node is final, its check fixed)."""
    steps = nodes(
        conn,
        scope,
        "project_id = ? AND level = 'step' AND status = 'dropped' AND closed_by = 'agent' AND template = ?",
        (line, f"propose/{executor}"),
    )
    return any((found := DECLINED.match(str(s["result"] or ""))) and int(found[1]) == request_id for s in steps)


def _reworkable(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> int | None:
    """0.42.1: the request a rework Ember declined (``decline_step``) was for, while the owner can have it made anyway
    (``owner_rework``): it is still the newest of its kind, the step's stage is open and no step proposes it; else
    None."""
    found = DECLINED.match(str(row["result"] or "")) if row["status"] == "dropped" else None
    stage = node(conn, scope, row["parent_id"]) if found and row["parent_id"] else None
    if found is None or stage is None or stage["status"] != "open":
        return None
    line = int(row["project_id"])
    proposing = "project_id = ? AND level = 'step' AND status = 'open' AND template = ?"
    if _rejected(conn, scope, row, line) != int(found[1]) or nodes(conn, scope, proposing, (line, row["template"])):
        return None
    return int(found[1])


def _asks(kind: str | None, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """0.37.6: the executors whose request a check needs made (a request check, or one of an any)."""
    if kind == "any":
        return tuple(e for s in spec.get("of", ()) for e in _asks(s.get("check"), s))
    return (str(spec["executor"]),) if kind == "request" and spec.get("executor") else ()


def _proposes(product: Mapping[str, Any], executor: str) -> str:
    """The title of the step of a product's template that proposes a request by ``executor`` ("Propose the listing")."""
    template = templates.by_key(product["template"])
    return next(
        (
            step.title
            for stage in template.stages
            for step in stage.steps
            if step.check == "request" and step.spec.get("executor") == executor and step.kind != templates.OWNER_KIND
        ),
        "Propose it to your owner",
    )


# --- a product's own milestone, and the records the tree takes the place of (0.35.0) ---


def product_milestone(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], now: str) -> int:
    """A product's own milestone (Ember's code's, of the kind first_test, linked to its line, due a year on): what the
    owner's Autonomy unlocks for the product stand on (policy.py keys them by milestone), made the first time one is
    needed. The roadmap's readers leave it out (roadmap.SHOWN)."""
    found = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    if found is not None:
        return int(found["id"])
    made = roadmap.create(
        conn,
        scope,
        title=_cut(f"{product['title']}: its place in the plan", 100),
        measure="Ember's code keeps it while the product is in the plan: your unlocks for the product stand on it",
        due=(from_iso(now).date() + timedelta(days=365)).isoformat(),
        now=now,
        project_id=int(product["project_id"]),
        created_by="code",
        kind="first_test",
    )
    conn.execute("UPDATE milestones SET product_node = ? WHERE id = ?", (product["id"], made))
    return made


def _close_milestone(conn: sqlite3.Connection, product_id: int, now: str, why: str) -> None:
    conn.execute(
        "UPDATE milestones SET status = 'dropped', closed_by = 'code', closed_at = ?, updated_at = ?, result = ?"
        " WHERE product_node = ? AND status = 'open'",
        (now, now, why[:600], product_id),
    )


def _upto(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (MOVED_KEY,)).fetchone()
    try:
        return int(row["value"]) if row is not None else 0
    except ValueError:
        return 0


def move(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """Once (migration 0088 named the newest milestone it applies to): the milestones the plan tree takes the place
    of close, the owner's Autonomy unlocks on them carried to their product's milestone first: the listing test's bars
    and scale points, the goal's decision points, and Ember's own milestones (one of a product becomes a step of its
    current stage). A missed bar owes nothing any more. Returns what happened, for the owner's System log."""
    upto = _upto(conn)
    if upto <= 0:
        return []
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'open' AND id <= ? AND product_node IS NULL"
        " AND (created_by = 'agent' OR created_by = 'code' AND (id IN (SELECT milestone_id FROM listing_gates)"
        " OR kind = 'decision' AND venture_id IS NULL AND project_id IS NULL)) ORDER BY id",
        (*params, upto),
    ).fetchall()
    said: list[str] = []
    standing = policy.standing(conn, scope)
    for m in rows:
        products = _products_of(conn, scope, m)
        for g in standing.get(int(m["id"]), []):
            for product in products:
                carried = product_milestone(conn, scope, product, now)
                total, _ = policy.used(conn, int(g["id"]), _Day(now))
                policy.set_grant(
                    conn,
                    scope,
                    carried,
                    str(g["rule"]),
                    str(g["level"]),
                    now,
                    per_day=int(g["per_day"]),
                    budget=max(1, int(g["budget"]) - total),
                    by=str(g["by"]),
                    why=f"0.35.0: carried over from milestone #{m['id']}",
                )
                said.append(
                    f"Plan tree: your unlock for {policy.RULES[str(g['rule'])].short} on milestone #{m['id']} is now"
                    f" on product line #{product['project_id']}."
                )
        step = None
        if m["created_by"] == "agent" and products:
            stage = current_stage(conn, scope, products[0]["id"])
            if stage is not None:
                step = _insert(
                    conn,
                    scope,
                    now,
                    parent_id=stage["id"],
                    level="step",
                    project_id=products[0]["project_id"],
                    stage=stage["stage"],
                    kind="create",
                    title=_cut(f"From your milestone #{m['id']}: {m['title']}", 160),
                    check_kind="agent",
                    seq=700 + int(m["id"]) % 100,
                    source="agent",
                )
                change(conn, scope, step, None, "code", "add", f"your milestone #{m['id']} moved into the plan", now)
        why = (
            f"0.35.0: a step of your plan now (#{step})."
            if step is not None
            else "0.35.0: your plan's steps take the place of your own milestones."
            if m["created_by"] == "agent"
            else "0.35.0: the plan tree's decide-by dates take the place of the listing test's bars and the goal's"
            " decision points."
        )
        conn.execute(
            "UPDATE milestones SET status = 'dropped', closed_by = 'code', closed_at = ?, updated_at = ?, result = ?"
            " WHERE id = ? AND status = 'open'",
            (now, now, why, m["id"]),
        )
        said.append(f"Plan tree: milestone #{m['id']} {_quoted(m['title'])} closed: {why.removeprefix('0.35.0: ')}")
    owed, owed_params = scope.where()
    conn.execute(
        "UPDATE obligations SET status = 'closed', closed_by = 'code', closed_at = ?, result = ?"
        f" WHERE {owed} AND status = 'open' AND kind = 'miss' AND milestone_id IN (SELECT milestone_id FROM"
        " listing_gates)",
        (
            now,
            "0.35.0: the listing test's bars retired; the plan tree's decide-by dates ask what a miss would have",
            *owed_params,
        ),
    )
    return said


@dataclass(frozen=True)
class _Day:
    """policy.used's clock: today's start, from ``now``."""

    now: str

    def today(self) -> date:
        return from_iso(self.now).date()

    def day_start(self, day: date) -> Any:
        return from_iso(f"{day.isoformat()}T00:00:00Z")


def _products_of(conn: sqlite3.Connection, scope: AgentScope, milestone: Mapping[str, Any]) -> list[Any]:
    """The open products a milestone was about: its line's, or (a venture's) each of the venture's lines'."""
    if milestone["project_id"] is not None:
        lines = [int(milestone["project_id"])]
    elif milestone["venture_id"] is not None:
        where, params = scope.where()
        lines = [
            int(r[0])
            for r in conn.execute(
                f"SELECT id FROM projects WHERE {where} AND venture_id = ? ORDER BY id",
                (*params, milestone["venture_id"]),
            )
        ]
    else:
        return []
    found = []
    for line in lines:
        found += nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
    return found


def _decide_by(facts: Facts, product: Mapping[str, Any], now: str, today: date) -> list[str]:
    """A product's decide-by dates once a listing of its line is live (whatever its template: a line laid out before
    its first listing keeps the one it got), each read once on or after its day from the start of its test (the day
    its first listing was seen live; a listing test begun before 0.35.0 keeps its start and what its bars found).
    Their verdicts are kept on the product (decide_by), and a views bar missed makes its marketing urgent
    (pushed_until). What the owner decides comes as a step of theirs; a first order brings Ember a step to scale it.
    None is read while the owner's park or kill of its venture stops the line (they decided on it already). 0.42.1
    (analysis-0.40.1 codebase, 6.2 "A paid test in the plan"): nor while the owner's paid test runs on one of its
    listings (paid.running): its dates are read once the test ends, which STATUS has Ember judge then (live, the
    coach bundle's fell three days into its ads test)."""
    conn, scope = facts.conn, facts.scope
    line = int(product["project_id"])
    if ventures.project_stopped(conn, scope, line) is not None:
        return []
    funnel = facts.funnel(line)
    if funnel is not None and set(funnel.listings) & set(paid.running(conn, scope, today)):
        return []
    found: dict[str, str] = json.loads(product["decide_by"]) if product["decide_by"] else {}
    start = product["live_since"]
    if start is None:
        start, found = _test_start(conn, scope, line, found)
        if start is None:
            if not facts.live(line):
                return []
            start = today.isoformat()
    day = (today - date.fromisoformat(start)).days
    views, favorites, orders = (funnel.views, funnel.favorites, funnel.orders) if funnel else (0, 0, 0)
    traffic = funnel.traffic if funnel else 0
    pushed = product["pushed_until"]
    said: list[str] = []
    numbers = f"{views} views, {favorites} favorites, {orders} orders after {traffic} reach actions"

    def push(until_day: int) -> str:
        return (date.fromisoformat(start) + timedelta(days=until_day)).isoformat()

    if day >= 7 and "day7" not in found:
        found["day7"] = "met" if views >= DAY7_VIEWS else "missed"
        if found["day7"] == "missed":
            pushed = max(pushed or "", push(7 + PUSH_DAYS))
            said.append(
                f"Plan tree: line #{line} has {views} views on day 7 (of {DAY7_VIEWS}): its marketing comes first."
            )
    if day >= 14 and "day14" not in found:
        if views >= DAY14_VIEWS and favorites >= DAY14_FAVORITES:
            found["day14"] = "met"
        elif traffic < reach.ENOUGH:
            found["day14"] = "retry"  # not seen, so not tested: one more try, its marketing urgent until day 28
            pushed = max(pushed or "", push(28))
            said.append(f"Plan tree: line #{line} on day 14: {numbers}; too little reach to judge it: one more try.")
        else:
            found["day14"] = "decide"
            said += _owner_decides(conn, scope, product, now, f"day 14: {numbers}")
    if day >= 28 and found.get("day14") == "retry" and "day28" not in found:
        found["day28"] = "met" if views >= DAY14_VIEWS else "decide"
        if found["day28"] == "decide":
            said += _owner_decides(conn, scope, product, now, f"day 28: {numbers}")
    if day >= 21 and "day21" not in found:
        found["day21"] = "scale" if orders >= 1 else "decide"
        if found["day21"] == "scale":
            said += _scale(conn, scope, product, now)
        else:
            said += _owner_decides(conn, scope, product, now, f"day 21, no order yet: {numbers}")
    changes = {"live_since": start, "decide_by": json.dumps(found, sort_keys=True), "pushed_until": pushed or None}
    if any(product[k] != v for k, v in changes.items()):
        _update(conn, product["id"], now, **changes)
    return said


def _maintain(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any]) -> Any | None:
    stages = _stages(conn, scope, product["id"])
    return next((s for s in stages if s["stage"] == "maintain" and s["status"] == "open"), None)


def _test_start(
    conn: sqlite3.Connection, scope: AgentScope, line: int, found: dict[str, str]
) -> tuple[str | None, dict[str, str]]:
    """A listing test begun before 0.35.0 (listing_gates): its start, and what its closed bars found."""
    where, params = scope.where("g")
    bars = conn.execute(
        "SELECT g.gate, g.started_on, m.status FROM listing_gates g JOIN milestones m ON m.id = g.milestone_id"
        f" WHERE {where} AND g.project_id = ? ORDER BY g.id",
        (*params, line),
    ).fetchall()
    if not bars:
        return None, found
    found = dict(found)
    status = {str(b["gate"]): str(b["status"]) for b in bars}
    if status.get("day7_views") in ("done", "missed"):
        found["day7"] = "met" if status["day7_views"] == "done" else "missed"
    views14, favorites14 = status.get("day14_views"), status.get("day14_favorites")
    if views14 == "missed" or favorites14 == "missed":
        found["day14"] = "decide"
    elif views14 == "done" and favorites14 == "done":
        found["day14"] = "met"
    if status.get("day21_sale") in ("done", "missed"):
        found["day21"] = "scale" if status["day21_sale"] == "done" else "decide"
    return str(min(b["started_on"] for b in bars)), found


def _owner_decides(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    now: str,
    numbers: str,
    reached: str = "reached a decide-by date",
) -> list[str]:
    """A decide-by date that asks the owner: keep the product or drop it, as a step of theirs (one at a time), in the
    product's maintain stage (it never closes, so the step waits there without holding up its launch)."""
    line = int(product["project_id"])
    if nodes(
        conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND template = 'decide/owner'", (line,)
    ):
        return []
    stage = _maintain(conn, scope, product)
    if stage is None:
        return []
    step = _insert(
        conn,
        scope,
        now,
        parent_id=stage["id"],
        level="step",
        project_id=line,
        template="decide/owner",
        stage=stage["stage"],
        kind=templates.OWNER_KIND,
        title=_cut(f"You decide: keep line #{line} or drop it ({numbers})", 160),
        seq=850,
        source="code",
    )
    change(conn, scope, step, None, "code", "add", f"{reached}: {numbers}", now)
    return [f"Plan tree: line #{line} {reached} ({numbers}): your owner decides on it."]


STOP_ASK_DAYS = 7  # 0.39.1: a line the owner decided on (or was asked about) isn't asked about again this soon


def owner_decision(conn: sqlite3.Connection, scope: AgentScope, line: int, since: str) -> str:
    """0.39.1: 'deciding' while a decision of the owner's on line ``line`` (keep it or drop it) waits for them, 'kept'
    when they kept it since ``since``, else ""."""
    asked = "project_id = ? AND level = 'step' AND template = 'decide/owner' AND (status = 'open' OR closed_at >= ?)"
    found = nodes(conn, scope, asked, (line, since))
    if any(s["status"] == "open" for s in found):
        return "deciding"
    return "kept" if any(s["status"] == "done" and s["closed_by"] == "owner" for s in found) else ""


def _review_stops(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date) -> list[str]:
    """0.39.1 (analysis-0.38.0, 3.5): a line today's review says stop becomes the owner's decision, keep it or drop it
    (a step of theirs, as at a decide-by date): since 0.35.0 only the owner closes a line, but the review still told the
    agent to close it with project_update, which can't. Live, three cycles tried, and the next review said again that
    the stop wasn't carried out. Not while the line waits for a decision of theirs, nor within STOP_ASK_DAYS of one."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT day, verdicts FROM reviews WHERE {where} AND day = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    try:
        verdicts = json.loads(row["verdicts"] or "[]") if row is not None else []
    except ValueError:
        return []
    said: list[str] = []
    recent = (today - timedelta(days=STOP_ASK_DAYS)).isoformat()
    for v in verdicts if isinstance(verdicts, list) else []:
        if not isinstance(v, dict) or v.get("verdict") != "stop" or not isinstance(v.get("project_id"), int):
            continue
        line = int(v["project_id"])
        project = store.project(conn, scope, line)
        product = nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
        asked = nodes(
            conn,
            scope,
            "project_id = ? AND level = 'step' AND template = 'decide/owner' AND (status = 'open' OR created_at >= ?)",
            (line, recent),
        )
        if project is None or project["status"] not in OPEN_STATUSES or not product or asked:
            continue
        why = " ".join(str(v.get("why") or "").split())
        stop = f"your review of {str(row['day'])[5:]} says stop: {why}"
        said += _owner_decides(conn, scope, product[0], now, stop, "has a review's stop")
    return said


REVIEW_HOLD = "Your review of"  # 0.40.2: how the hold a review's stop puts on its line begins (_review_holds)


def _review_holds(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date) -> list[str]:
    """0.40.2 (analysis-0.40.1, 3.4): a line today's review says stop is held by Ember's code until the owner keeps it
    (owner_keep lifts the hold) or drops it: its steps wait, and no cycle has to take one of them to hold it. Live on
    2026-10-21 two cycles ($0.37) took the monthly blog post of a line whose review had said stop, only to hold the
    line, and each cut the sleep before it to 30 minutes. The hold is Ember's (hold_by 'agent': the review is hers, and
    plan_step resume lifts it, and it stays lifted for that review), made by her code. Not a line on hold already,
    one the owner kept within STOP_ASK_DAYS, nor one with a step the owner pinned or a promise open (its work keeps the
    promise)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT day, created_at, verdicts FROM reviews WHERE {where} AND day = ? AND status = 'ok'"
        " ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    try:
        verdicts = json.loads(row["verdicts"] or "[]") if row is not None else []
    except ValueError:
        return []
    said: list[str] = []
    recent = (today - timedelta(days=STOP_ASK_DAYS)).isoformat()
    for v in verdicts if isinstance(verdicts, list) else []:
        if not isinstance(v, dict) or v.get("verdict") != "stop" or not isinstance(v.get("project_id"), int):
            continue
        line = int(v["project_id"])
        project = store.project(conn, scope, line)
        product = nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
        if project is None or project["status"] not in OPEN_STATUSES or not product or product[0]["hold_reason"]:
            continue
        if owner_decision(conn, scope, line, recent) == "kept":
            continue
        kept = "project_id = ? AND level = 'step' AND status = 'open' AND (pinned = 1 OR kind = 'promise')"
        if nodes(conn, scope, kept, (line,)):
            continue
        resumed = conn.execute(  # since this review: plan_step resume, or the owner's
            "SELECT 1 FROM plan_changes WHERE node_id = ? AND action = 'resume' AND created_at >= ? LIMIT 1",
            (product[0]["id"], row["created_at"]),
        ).fetchone()
        if resumed is not None:
            continue
        why = " ".join(str(v.get("why") or "").split())
        hold_why = _cut(f"{REVIEW_HOLD} {str(row['day'])[5:]} says stop: {why}", 200)
        _update(conn, product[0]["id"], now, hold_reason=hold_why, hold_by="agent")
        until = f"{hold_why} (until your owner keeps or drops it)"
        change(conn, scope, product[0]["id"], None, "code", "hold", until, now)
        said.append(f"Plan tree: line #{line} is on hold until your owner keeps or drops it: {hold_why}")
    return said


REVIEW_CHANGE = "review/change"  # 0.39.6: the template of a change today's review decided for a line


def _review_changes(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date) -> list[str]:
    """0.39.6 (analysis-0.39.5, 4.2.4): a line today's review says change gets the change as a step of its own, due
    today (weights.RECURRING_DUE), which the agent closes with plan_step done; a later review's change takes the place
    of one still open. Until now a change "waited for its line's cycle", and that cycle took the tree's heaviest step:
    live, the reviews of 10-13, 10-14 and 10-15 each decided on a small Etsy Ads test the owner would turn on, and no
    request for it was made; the cycles made new covers instead. Only for a line whose maintain stage is open (live,
    or launched): before that, its stage's own steps are the change. 0.42.6 (analysis-0.42.3, 3.1): nor for a line
    waiting for the owner's keep-or-drop (an open decide/owner step): live on 10-21 and 10-22 six cycles ($1.34) closed
    "Review: ... Kept passive at zero cost. The owner decides at renewal" on lines #3, #4 and #6, each weighing 4.0
    (due today) above the KDP book's steps, and four of them cut Ember's sleep to 30 minutes."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT day, verdicts FROM reviews WHERE {where} AND day = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    try:
        verdicts = json.loads(row["verdicts"] or "[]") if row is not None else []
    except ValueError:
        return []
    said: list[str] = []
    for v in verdicts if isinstance(verdicts, list) else []:
        if not isinstance(v, dict) or v.get("verdict") != "change" or not isinstance(v.get("project_id"), int):
            continue
        why = " ".join(str(v.get("why") or "").split())
        line = int(v["project_id"])
        project = store.project(conn, scope, line)
        product = nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
        stage = _maintain(conn, scope, product[0]) if product else None
        current = current_stage(conn, scope, product[0]["id"]) if product else None
        launched = bool(product) and (product[0]["live_since"] or (current and current["stage"] == "launch"))
        if project is None or project["status"] not in OPEN_STATUSES or stage is None or not launched or not why:
            continue
        if owner_decision(conn, scope, line, now) == "deciding":
            continue  # 0.42.6: the line waits for the owner's keep-or-drop: the change stays in the review's words
        title = _cut(f"Review {str(row['day'])[5:]}: {why}", 160)
        mine = "project_id = ? AND level = 'step' AND template = ?"
        if nodes(conn, scope, f"{mine} AND title = ?", (line, REVIEW_CHANGE, title)):
            continue  # made today already (and maybe done)
        for old in nodes(conn, scope, f"{mine} AND status = 'open'", (line, REVIEW_CHANGE)):
            _close(conn, int(old["id"]), now, "dropped", f"your review of {str(row['day'])[5:]} decided anew")
        step = _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=line,
            template=REVIEW_CHANGE,
            stage=stage["stage"],
            kind="fix",
            title=title,
            check_kind="agent",
            seq=250,
            source="code",
            due=today.isoformat(),
        )
        change(conn, scope, step, None, "code", "add", f"your review of {row['day']} says change: {why}", now)
        said.append(f"Plan tree: line #{line}'s change from today's review is a step of its own.")
    return said


def _scale(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], now: str) -> list[str]:
    """A first order by day 21: Ember's step to scale the product (5 variants or a bundle), hers to split, in its
    maintain stage."""
    line = int(product["project_id"])
    stage = _maintain(conn, scope, product)
    if stage is None:
        return []
    step = _insert(
        conn,
        scope,
        now,
        parent_id=stage["id"],
        level="step",
        project_id=line,
        template="decide/scale",
        stage=stage["stage"],
        kind="create",
        title=SCALE,
        check_kind="agent",
        seq=300,
        source="code",
    )
    change(conn, scope, step, None, "code", "add", "a first order by day 21", now)
    return [f"Plan tree: line #{line} sold by day 21: a step to scale it."]


CHANNEL_TEMPLATE = "channel"  # 0.39.1: templates.CHANNEL's name


def _channel_duties(
    conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], maintain_id: int, now: str
) -> list[str]:
    """0.39.1 (analysis-0.38.0, 3.4): a channel's line (the Pinterest test, say) gets no weekly pin, post or blog post
    of its own, and the ones it got drop out: their checks count what links the line's own listings, and it has none,
    while the tools refuse a pin or a post for another line's listing. Live, its first Bluesky step cost a cycle the
    minute the channel was set up, refused; each would have taken a cycle a day. Its products' marketing is theirs."""
    dropped = []
    duties = "parent_id = ? AND level = 'step' AND status = 'open' AND template LIKE 'recurring/%'"
    for step in nodes(conn, scope, duties, (maintain_id,)):
        if step["channel"]:
            _close(conn, step["id"], now, "dropped", CHANNEL_DUTY)
            dropped.append(int(step["id"]))
    if not dropped:
        return []
    return [
        f"Plan tree: line #{product['project_id']} is a channel: its steps {', '.join(f'#{n}' for n in dropped)} for"
        " pins and posts of its own dropped (its products' marketing is theirs)."
    ]


CHANNEL_DUTY = (
    "Ember's code: a channel's line has no listing for a pin or a post of its own; its products' marketing is theirs"
)


def _audience_channels(product: Mapping[str, Any]) -> list[str]:
    audience = product["audience"] or "both"
    return [c for c in CHANNELS if audience in CHANNEL_AUDIENCES[c]]


def _launched(
    conn: sqlite3.Connection,
    scope: AgentScope,
    stages: list[Any],
    maintain_id: int,
    channels: Mapping[str, bool],
    factors: Mapping[str, float],
) -> bool:
    """Whether a product's recurring marketing is due: every stage before its maintain stage done (its launch, or a
    template's last before it). 0.39.3 (analysis-0.39.1, 4.5): a launch counts once its first marketing is out, its
    own marketing steps closed: the critic's pass (maintain has a fix of its own whenever the critic says improve),
    what is owed and a step whose channel is off don't hold it up. Live, the critic passed no listing in 13 checks, so
    after their first pins and posts the live products had no marketing step at all, and the day-7 and day-14 pushes
    pushed nothing; and a launch step waiting on a channel that is off kept the weekly steps from the channels that
    are on. Once launched, a product stays launched, as when only its stages said so (they never reopen): a channel
    set up later, or a step added to its launch, doesn't stop its weekly marketing. 0.40.3 (analysis-0.40.1 codebase,
    PL1): nor does a step of a channel at its floor (``_at_floor``: it waits, unless the owner pins it): a new product's
    Bluesky and blog launch steps kept its weekly pin, on the channel that works, from being laid out."""
    duties = "parent_id = ? AND level = 'step' AND template LIKE 'recurring/%' AND template <> 'recurring/critic'"
    if nodes(conn, scope, duties, (maintain_id,)):
        return True  # laid out only once it was launched
    for stage in stages:
        if stage["stage"] == "maintain" or stage["status"] != "open":
            continue
        if stage["stage"] != "launch":
            return False
        for step in _steps(conn, scope, stage["id"]):
            if step["status"] != "open" or step["kind"] != "market" or step["obligation_id"] is not None:
                continue
            if _at_floor(step, factors):
                continue  # 0.40.3: it waits on its channel's floor, as one whose channel is off waits on the channel
            if not step["channel"] or channels.get(step["channel"], False):
                return False  # its first marketing isn't out yet
    return True


def _recurring(
    facts: Facts, product: Mapping[str, Any], now: str, today: date, channels: Mapping[str, bool]
) -> list[str]:
    """A live product's recurring steps: once it is launched (``_launched``: every stage before its maintain stage is
    done, 0.39.3 its launch once its first marketing is out), one open step per channel's duty (a missed one stays
    open and ages); and a fix whenever the critic says improve (it judges live listings only)."""
    conn, scope = facts.conn, facts.scope
    stages = _stages(conn, scope, product["id"])
    maintain = next((s for s in stages if s["stage"] == "maintain" and not _replaced(s, product)), None)
    if maintain is None or maintain["status"] != "open":
        return []
    said: list[str] = []
    title = str(product["title"])
    launched = _launched(conn, scope, stages, int(maintain["id"]), channels, facts.factors())
    kind = templates.by_key(product["template"]).name
    if kind == CHANNEL_TEMPLATE:  # 0.39.1: a channel's line has no listing for a pin or a post of its own
        return _channel_duties(conn, scope, product, int(maintain["id"]), now)
    # 0.39.8 (analysis-0.39.6, 3.3): nor can a line with no listing of its own: what its steps count links its listings.
    # Live, line #12 reached its maintain stage on the agent's words ("Listing #4587912058 already live"), a listing
    # line #4's request had made, and its weekly pin and post could never pass.
    if launched and kind != "kdp_book" and facts.live(product["project_id"]) > 0:  # a book can't be linked yet
        funnel = facts.funnel(product["project_id"])
        for channel in _audience_channels(product):
            if not channels.get(channel, False):
                continue
            check, days, words = RECURRING[channel]
            key = f"recurring/{channel}"
            if nodes(conn, scope, "parent_id = ? AND template = ? AND status = 'open'", (maintain["id"], key)):
                continue
            last = nodes(conn, scope, "parent_id = ? AND template = ? AND status <> 'open'", (maintain["id"], key))
            if last and last[-1]["due"] and date.fromisoformat(last[-1]["due"]) > today:
                continue  # this period's is done: the next comes when it ends
            have = 0 if funnel is None else {"pin": funnel.pins, "post": funnel.bluesky, "blog": funnel.posts}[check]
            _insert(
                conn,
                scope,
                now,
                parent_id=maintain["id"],
                level="step",
                project_id=product["project_id"],
                template=key,
                stage="maintain",
                kind="market",
                channel=channel,
                title=words.format(title=title)[:160],
                check_kind=check,
                check_spec=json.dumps({"count": have + 1}),
                seq=100 + CHANNELS.index(channel),
                source="code",
                due=(today + timedelta(days=days)).isoformat(),
            )
            said.append(f"Plan tree: product #{product['id']}'s {channel} step for this period.")
    if facts.verdict(product["project_id"]) == "improve" and not nodes(
        conn,
        scope,
        "project_id = ? AND level = 'step' AND check_kind = 'critic' AND status = 'open'",
        (product["project_id"],),
    ):
        _insert(
            conn,
            scope,
            now,
            parent_id=maintain["id"],
            level="step",
            project_id=product["project_id"],
            template="recurring/critic",
            stage="maintain",
            kind="fix",
            title="Fix what the critic found",
            check_kind="critic",
            check_spec="{}",
            seq=200,
            source="code",
        )
        said.append(f"Plan tree: product #{product['id']} has a critic's fix to make.")
    return said


def _lift_waits(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date) -> list[str]:
    """A step Ember said waits is taken up again once its block is gone: the request decided, the upgrade request
    released or declined, the other step closed, the day come."""
    where, params = scope.where()
    said: list[str] = []
    for row in nodes(
        conn, scope, "level = 'step' AND status = 'open' AND (wait_ref IS NOT NULL OR wait_until IS NOT NULL)"
    ):
        gone = ""
        if row["waiting"] == "owner":
            found = conn.execute(f"SELECT status FROM approvals WHERE id = ? AND {where}", (row["wait_ref"], *params))
            status = (found.fetchone() or {"status": "gone"})["status"]
            gone = f"request #{row['wait_ref']} is {status}" if status != "pending" else ""
        elif row["waiting"] == "upgrade":
            found = conn.execute(f"SELECT status FROM upgrades WHERE id = ? AND {where}", (row["wait_ref"], *params))
            status = (found.fetchone() or {"status": "gone"})["status"]
            gone = f"upgrade request #{row['wait_ref']} is {status}" if status not in ("new", "accepted") else ""
        elif row["waiting"] == "step":
            other = node(conn, scope, row["wait_ref"])
            gone = f"step #{row['wait_ref']} is closed" if other is None or other["status"] != "open" else ""
        elif row["waiting"] == "date":
            gone = (
                f"{row['wait_until']} has come" if row["wait_until"] and row["wait_until"] <= today.isoformat() else ""
            )
        if gone:
            _update(conn, row["id"], now, waiting=None, wait_ref=None, wait_until=None, wait_why=None)
            change(conn, scope, row["id"], None, "code", "unwait", gone, now)
            said.append(f"Plan tree: step #{row['id']} is ready again ({gone}).")
    return said
