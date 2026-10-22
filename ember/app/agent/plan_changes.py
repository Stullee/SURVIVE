"""0.42.2 (analysis-0.40.1, 5.6): the changes Ember and its owner make to the tree, split out of plan.py.

Ember's (0.35.0: plan_step's ``ACTIONS``): steps added, split, replaced, done, skipped, declined or waiting, and a
product line held or resumed. The owner's, from the Plan tab: a pin, a worth, a wait lifted, a rework, a product
ended, held, resumed or kept, a freeze of new products (0.36.0), and what they said since (``owner_words``).
``line_held`` and ``review_held`` say what holds a line (the owner, or a review's stop), which no tool works on.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from . import obligations, templates, weights
from .plan_keep import REVIEW_HOLD, _close_product, _reworkable
from .plan_model import (
    _close,
    _cut,
    _ids,
    _insert,
    _make_room,
    _midnight,
    _quoted,
    _replaced,
    _spec,
    _stages,
    _steps,
    _update,
    change,
    check_words,
    current_stage,
    node,
    nodes,
)
from .plan_ready import _executors, _recurring_market, _rejected
from .plan_ventures import VENTURE_TEMPLATES
from .store import AgentScope


def line_held(conn: sqlite3.Connection, scope: AgentScope, line: int) -> str | None:
    """0.39.3 (analysis-0.39.1, 4.4): the owner's hold on product line ``line`` ("its steps wait until you resume
    it"): why, while it lasts. Ember's code keeps it as it keeps their park: no step of the line is taken, its promises
    and the owner's decisions on it included (``candidates``), no cycle is aimed at it (loop.py), and no tool takes it
    as a cycle's line (tools._line). Ember's own hold isn't the owner's: what is owed on a line she holds goes on."""
    found = nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
    if not found or found[0]["hold_by"] != "owner" or not found[0]["hold_reason"]:
        return None
    return str(found[0]["hold_reason"])


def review_held(conn: sqlite3.Connection, scope: AgentScope, line: int) -> str | None:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "Your hold, on files too"): the hold a review's stop put on product line
    ``line`` (``_review_holds``): why, while it lasts. The tools keep it as they keep the owner's (tools._held) until
    the owner keeps or drops the line, or plan_step resume lifts it; a hold Ember put on a line herself they don't."""
    found = nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
    if not found or found[0]["hold_by"] != "agent" or not str(found[0]["hold_reason"] or "").startswith(REVIEW_HOLD):
        return None
    return str(found[0]["hold_reason"])


# --- the owner's word ---


class PlanError(ValueError):
    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


def pin(conn: sqlite3.Connection, scope: AgentScope, node_id: int, pinned: bool, who: str | None, now: str) -> None:
    row = node(conn, scope, node_id)
    if row is None:
        raise PlanError("id", "no such step", 404)
    if row["level"] != "step" or row["kind"] in ("promise", templates.OWNER_KIND):
        raise PlanError("id", "only a step Ember takes can be pinned")
    if row["status"] != "open":
        raise PlanError("id", "this step is closed", 409)
    _update(conn, node_id, now, pinned=1 if pinned else 0)
    _word(conn, scope, node_id, "pin" if pinned else "unpin", None, who, now)


def set_worth(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, worth: float | None, who: str | None, now: str
) -> None:
    row = node(conn, scope, node_id)
    if row is None:
        raise PlanError("id", "no such product", 404)
    if row["level"] not in ("product", "venture") and not _is_ventures(row):  # 0.36.0: the ventures'; 0.37.0: each's
        raise PlanError("id", "a worth is a product's, a venture's, or all your ventures'")
    if row["status"] != "open":
        raise PlanError("id", "this product is closed", 409)
    if worth is not None and not weights.OWNER_WORTH_MIN <= worth <= weights.OWNER_WORTH_MAX:
        raise PlanError("worth", f"give a worth from {weights.OWNER_WORTH_MIN:g} to {weights.OWNER_WORTH_MAX:g}")
    _update(conn, node_id, now, owner_worth=worth)
    _word(conn, scope, node_id, "worth" if worth is not None else "clear_worth", worth, who, now)


def _word(
    conn: sqlite3.Connection,
    scope: AgentScope,
    node_id: int,
    action: str,
    value: float | None,
    who: str | None,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO plan_words (mode, session, node_id, action, value, signed, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, node_id, action, value, (who or None) and who[:60], now),
    )


# --- Ember's changes (0.35.0) ---

ACTIONS = ("add", "split", "replace", "done", "wait", "hold", "resume", "skip", "decline")  # 0.39.8: skip; 0.42.1
STEP_KINDS = ("create", "ship", "fix", "market", "chore", "report")  # the kinds of step Ember adds
WAIT_ON = ("owner", "upgrade", "step", "date")
MAX_NEW_STEPS = 4  # steps one change adds
TITLE_CHARS = 120
WAITS_A_DAY = 2  # steps Ember may say wait, a day (the owner sees each one)
WAIT_DAYS = 14  # a wait on a day, at most this far ahead
DECLINE_WORDS = 6  # 0.42.1: a decline tells the owner why in at least as many words (``decline_step``)
FIXED_SOURCES = ("owner", "promise", "code")  # what the owner, a promise or Ember's code put there stays


def _open_product(conn: sqlite3.Connection, scope: AgentScope, line: int) -> sqlite3.Row:
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    if not found or found[0]["status"] != "open":
        raise PlanError("project_id", f"product line #{line} isn't an open product of your plan")
    return found[0]


def owner_promise(conn: sqlite3.Connection, scope: AgentScope, step_id: int | None) -> bool:
    """0.40.2 (analysis-0.40.1, 3.4): whether ``step_id`` keeps a promise of no product line (the Owner project's): a
    cycle on it has no line, and plan_step wait was refused ("this cycle works on no product line yet"). Live, the
    promise to propose the Kassenbuch by 10-25, which KDP's weekly limit held until then, could only be tried and
    refused, a cycle a day."""
    row = node(conn, scope, step_id) if step_id is not None else None
    return row is not None and row["level"] == "step" and row["kind"] == "promise" and row["project_id"] is None


def _line_step(conn: sqlite3.Connection, scope: AgentScope, step_id: int | None, line: int | None) -> sqlite3.Row:
    """An open step of product line ``line``; 0.39.1: with no line (a venture cycle's wait), a step of the Ventures;
    0.40.2: or a promise of no product line (``owner_promise``)."""
    if step_id is None:
        raise PlanError("step_id", "name the step (step_id)")
    row = node(conn, scope, step_id)
    if row is None or row["level"] != "step":
        raise PlanError("step_id", f"there is no step #{step_id}", 404)
    if line is None:
        if row["template"] not in VENTURE_TEMPLATES and not owner_promise(conn, scope, step_id):
            raise PlanError("step_id", f"step #{step_id} isn't a step of your ventures: a venture cycle changes those")
    elif row["project_id"] != line:
        raise PlanError(
            "step_id", f"step #{step_id} is product line #{row['project_id']}'s: this cycle works on line #{line}"
        )
    if row["status"] != "open":
        raise PlanError("step_id", f"step #{step_id} is {row['status']}, which is final", 409)
    return row


def _changeable(row: Mapping[str, Any], what: str) -> None:
    """Whether Ember may split or replace a step: not what the owner, a promise or Ember's code put there (an owner's
    step, a promise, a decision, a recurring duty), nor a step the owner pinned."""
    if row["obligation_id"] is not None or row["kind"] in ("promise", templates.OWNER_KIND):
        raise PlanError(
            "step_id", f"step #{row['id']} is your owner's (a promise, a decision or theirs to take): no {what}"
        )
    if row["source"] in FIXED_SOURCES:
        raise PlanError("step_id", f"step #{row['id']} was put there by your owner or Ember's code: no {what}")
    if row["pinned"]:
        raise PlanError("step_id", f"your owner pinned step #{row['id']}: take it as it is")


def _new_steps(
    conn: sqlite3.Connection,
    scope: AgentScope,
    stage: Mapping[str, Any],
    seq: int,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> list[int]:
    made = []
    for offset, step in enumerate(steps):
        kind = step["kind"] if step["kind"] in STEP_KINDS else "create"
        made.append(
            _insert(
                conn,
                scope,
                now,
                parent_id=stage["id"],
                level="step",
                project_id=stage["project_id"],
                stage=stage["stage"],
                kind=kind,
                title=_cut(step["title"], TITLE_CHARS),
                check_kind="agent",
                seq=seq + offset,
                source="agent",
            )
        )
        change(conn, scope, made[-1], cycle_id, "agent", "add", why, now)
    return made


def _stage_of(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> sqlite3.Row:
    stage = node(conn, scope, row["parent_id"]) if row["parent_id"] else None
    if stage is None or stage["level"] != "stage" or stage["status"] != "open":
        raise PlanError("step_id", f"step #{row['id']}'s stage is closed: add to an open one")
    return stage


def add_steps(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
    *,
    stage: str | None = None,
    before: int | None = None,
) -> str:
    """New steps of Ember's: before a step, or at the end of a stage (the current one unless she names one). She
    says when they are done (check 'agent')."""
    product = _open_product(conn, scope, line)
    if before is not None:
        anchor = _line_step(conn, scope, before, line)
        parent = _stage_of(conn, scope, anchor)
        seq = int(anchor["seq"])
        _make_room(conn, parent["id"], seq, len(steps), now)
    else:
        found = (
            next(
                (s for s in _stages(conn, scope, product["id"]) if s["stage"] == stage and not _replaced(s, product)),
                None,
            )
            if stage
            else current_stage(conn, scope, product["id"])
        )
        if found is None or found["status"] != "open":
            raise PlanError("stage", f"product line #{line} has no open {stage or 'current'} stage")
        parent = found
        kept = [int(s["seq"]) for s in _steps(conn, scope, parent["id"]) if s["obligation_id"] is None]
        seq = max(kept, default=-1) + 1
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    return f"Added {_ids(made)} to the {parent['stage']} stage of line #{line}."


def split_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """A step as smaller ones in its place. One Ember says is done (check 'agent') gives way to its parts; one Ember's
    code checks stays after them, and closes when its check passes."""
    row = _line_step(conn, scope, step_id, line)
    _changeable(row, "split")
    if len(steps) < 2:
        raise PlanError("steps", "a split needs at least 2 smaller steps (one new step: add or replace)")
    parent = _stage_of(conn, scope, row)
    seq = int(row["seq"])
    _make_room(conn, parent["id"], seq, len(steps), now)
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    change(conn, scope, row["id"], cycle_id, "agent", "split", f"into {_ids(made)}: {why}", now)
    if row["check_kind"] == "agent":
        _close(conn, row["id"], now, "dropped", f"split into {_ids(made)}: {why}", by="agent")
        return f"Step #{row['id']} is split into {_ids(made)}."
    return (
        f"Step #{row['id']} is split: {_ids(made)} come first, and #{row['id']} stays after them (Ember's code "
        "closes it when its check passes)."
    )


def replace_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """Other steps in a step's place. A step Ember's code checks can be replaced only where its stage has a check of
    its own (else its check is part of what makes the stage done: Ember can't loosen that)."""
    row = _line_step(conn, scope, step_id, line)
    _changeable(row, "replacing")
    parent = _stage_of(conn, scope, row)
    if row["check_kind"] not in (None, "agent") and not parent["check_kind"]:
        raise PlanError(
            "step_id",
            f"step #{row['id']}'s check is part of what makes its stage done: split it (plan_step split) instead",
        )
    seq = int(row["seq"])
    _make_room(conn, parent["id"], seq, len(steps), now)
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    _close(conn, row["id"], now, "dropped", f"replaced by {_ids(made)}: {why}", by="agent")
    change(conn, scope, row["id"], cycle_id, "agent", "replace", f"by {_ids(made)}: {why}", now)
    return f"Step #{row['id']} is replaced by {_ids(made)}."


def step_done(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """A step Ember says is done: only one without a check Ember's code reads."""
    row = _line_step(conn, scope, step_id, line)
    if row["check_kind"] != "agent":
        words = check_words(row["check_kind"], _spec(row)) or "its check passes"
        raise PlanError("step_id", f"Ember's code checks step #{row['id']}: it closes once {words}")
    _close(conn, row["id"], now, "done", why, by="agent")
    change(conn, scope, row["id"], cycle_id, "agent", "done", why, now)
    return f"Step #{row['id']} is done."


def skip_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """0.39.8 (analysis-0.39.6, 3.4): a recurring pin, post or blog post of Ember's code's that Ember skips for its
    period, with why (the owner sees it): it closes dropped, and the channel's next step for the line comes when the
    period ends. Outside the WAITS_A_DAY waits. Live, the code's weekly steps couldn't be replaced and the waits were
    used up, so cycles wrote a blog post the agent's own strategy had stopped, or spent themselves putting one off."""
    row = _line_step(conn, scope, step_id, line)
    if not _recurring_market(row) or row["source"] != "code":
        raise PlanError(
            "step_id",
            f"skip is for a recurring pin, post or blog post of Ember's code's; step #{row['id']} isn't one (wait, "
            "replace or say done instead)",
        )
    if row["pinned"]:
        raise PlanError("step_id", f"your owner pinned step #{row['id']}: they want it done")
    _close(conn, row["id"], now, "dropped", f"skipped for this period: {why}", by="agent")
    change(conn, scope, row["id"], cycle_id, "agent", "drop", f"skipped for this period: {why}"[:300], now)
    ends = f" (it ends {row['due']})" if row["due"] else ""
    return f"Step #{row['id']} is skipped for this period{ends}: its channel's next step for this line comes after it."


def decline_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "A rework Ember can decline"): a step proposing again a request the owner
    rejected (``_rework``: urgent until it is proposed again, 0.40.1) that Ember won't propose again, as their
    rejection means "not this": it closes dropped with why, which the owner sees (the step and its reason on their
    Plan tab, Changes today, the System log), and no step proposes that request again (``_again``) until a newer one
    of its kind is decided; the owner can have it reworked anyway (``owner_rework``). Until now Ember could only make
    it wait (WAIT_DAYS at most, one of her WAITS_A_DAY) or hold the whole line. Outside the waits of the day."""
    row = _line_step(conn, scope, step_id, line)
    rejected = _rejected(conn, scope, row, line)
    if rejected is None:
        raise PlanError(
            "step_id",
            f"decline is for a step proposing again a request your owner rejected; step #{row['id']} isn't one (wait, "
            "replace or say done instead)",
        )
    if row["pinned"]:
        raise PlanError("step_id", f"your owner pinned step #{row['id']}: they want it reworked")
    if len(why.split()) < DECLINE_WORDS:
        raise PlanError(
            "why",
            f"a decline tells your owner why their rejection means not to propose request #{rejected} again: say it "
            f"in a sentence of {DECLINE_WORDS} words or more",
        )
    _close(conn, row["id"], now, "dropped", f"declined request #{rejected}: {why}", by="agent")
    said = f"declined: won't propose request #{rejected} again: {why}"  # a drop, as a skip is (plan_changes' actions)
    change(conn, scope, row["id"], cycle_id, "agent", "drop", said, now)
    return (
        f"Step #{row['id']} is closed: you won't propose request #{rejected} again. Your owner sees why on their Plan"
        " tab and can have it reworked anyway; a later request of its kind they reject gets a step again."
    )


def wait_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int | None,
    step_id: int | None,
    on: str | None,
    ref: int | None,
    until: str | None,
    why: str,
    cycle_id: int | None,
    now: str,
    today: date,
    *,
    day_start: str | None = None,
) -> str:
    """A step that waits on a block Ember's code can check: a request to the owner still waiting, an upgrade request
    not released or declined, another open step, or a day at most WAIT_DAYS ahead (WAITS_A_DAY a day). Its age stops
    meanwhile, and the code takes it up again once the block is gone (``_lift_waits``). 0.39.1: with no ``line``, a
    venture cycle's step of its ventures (a triage, a brainstorm): live, an idea "paused until 10-20" came back the next
    day, as nothing could make it wait. 0.40.1 (analysis-0.40.0, 3.4): a promise's step on a date, PROMISE_NEAR_DAYS
    before the promise is due at the latest (_promise_waits); a decision's and the owner's own steps stay theirs.

    0.40.3 (analysis-0.40.1 codebase, PL5): not a step the owner pinned (hold and skip refused one; a wait voided the
    pin silently), and not on a step that waits on it (``_waits_on``): only its closing lifts such a wait, so the two
    waited for each other for good ("Propose the listing" on "You approve it", 30 days in the audit). The owner can
    lift a wait (``owner_unwait``). PL2: the day's waits count from when the owner's day began (``day_start``, their
    clock's), not from the UTC date's start: waits said at 22:30 were counted against the next day's first."""
    row = _line_step(conn, scope, step_id, line)
    latest = _promise_waits(conn, row, on, today)
    if (row["obligation_id"] is not None and latest is None) or row["kind"] == templates.OWNER_KIND:
        raise PlanError("step_id", f"step #{row['id']} is your owner's: what blocks it goes in your message to them")
    if row["pinned"]:
        raise PlanError("step_id", f"your owner pinned step #{row['id']}: they want it done, not put off")
    where, params = scope.where()
    waited = conn.execute(
        f"SELECT COUNT(*) FROM plan_changes WHERE {where} AND actor = 'agent' AND action = 'wait' AND created_at >= ?",
        (*params, day_start or _midnight(today)),
    ).fetchone()[0]
    if waited >= WAITS_A_DAY:
        raise PlanError("action", f"{WAITS_A_DAY} steps were said to wait today already: do what you can of this one")
    blocked = ""
    if on == "owner":
        found = conn.execute(f"SELECT status FROM approvals WHERE id = ? AND {where}", (ref, *params)).fetchone()
        if found is None or found["status"] != "pending":
            raise PlanError("ref", f"request #{ref} isn't waiting for your owner")
        blocked = f"request #{ref}, until your owner decides it"
    elif on == "upgrade":
        found = conn.execute(f"SELECT status FROM upgrades WHERE id = ? AND {where}", (ref, *params)).fetchone()
        if found is None or found["status"] not in ("new", "accepted"):
            raise PlanError("ref", f"upgrade request #{ref} isn't open")
        blocked = f"upgrade request #{ref}, until your owner releases or declines it"
    elif on == "step":
        other = node(conn, scope, ref) if ref is not None else None
        if other is not None and other["id"] == row["id"]:  # 0.39.1: live, the step's own number went in ref
            raise PlanError("ref", f"step #{ref} can't wait on itself: ref names the other step it waits on")
        if other is None or other["level"] != "step" or other["status"] != "open":
            raise PlanError("ref", f"#{ref} isn't another open step")
        loop = _waits_on(conn, scope, row, other)
        if loop:
            raise PlanError("ref", f"{loop}, so the wait would never lift: take #{row['id']} first, or wait on a day")
        blocked = f"step #{ref}, until it is closed"
    elif on == "date":
        try:
            day = date.fromisoformat(until or "")
        except ValueError:
            raise PlanError("until", "give the day it waits for as YYYY-MM-DD") from None
        if not today < day <= today + timedelta(days=WAIT_DAYS):
            raise PlanError("until", f"a step waits for a day after today, at most {WAIT_DAYS} days ahead")
        if latest is not None and day > latest:
            raise PlanError("until", f"step #{row['id']} keeps a promise: it waits until {latest} at the latest")
        blocked = f"{day.isoformat()}"
    else:
        raise PlanError("on", f"say what it waits on: {', '.join(WAIT_ON)}")
    _update(
        conn,
        row["id"],
        now,
        waiting=on,
        wait_ref=ref if on != "date" else None,
        wait_until=until if on == "date" else None,
        wait_why=_cut(why, 200),
        ready_since=None,
    )
    change(conn, scope, row["id"], cycle_id, "agent", "wait", f"on {blocked}: {why}", now)
    return f"Step #{row['id']} waits on {blocked} ({waited + 1} of {WAITS_A_DAY} waits today)."


def _waits_on(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any], other: Mapping[str, Any]) -> str:
    """0.40.3 (analysis-0.40.1 codebase, PL5): how step ``other`` waits on step ``row``, in words, "" when it doesn't:
    it is ``row`` at the end of a chain of waits on steps (each closes only once the one it waits on does); or it, or a
    step along that chain, is the owner's step for the request ``row`` makes ("You approve it" waits for "Propose the
    listing"), or a step of a later stage of the same product (it waits until ``row``'s stage is done; maintain's go
    side by side with launch's)."""
    makes = set(_executors(row["check_kind"], _spec(row)))
    stage = str(row["stage"] or "")
    seen: set[int] = set()
    step: Mapping[str, Any] | None = other
    while step is not None and step["id"] not in seen:
        seen.add(int(step["id"]))
        who = f"step #{other['id']}" + (f" waits on step #{step['id']}, which" if step["id"] != other["id"] else "")
        if step["id"] == row["id"]:
            return f"step #{other['id']} waits on step #{row['id']}"
        if step["kind"] == templates.OWNER_KIND and makes & set(_executors(step["check_kind"], _spec(step))):
            return f"{who} is your owner's and waits for the request step #{row['id']} makes"
        later = str(step["stage"] or "")
        if (
            step["project_id"] == row["project_id"]
            and step["obligation_id"] is None
            and stage in templates.STAGES
            and later in templates.STAGES
            and later != "maintain"
            and templates.STAGES.index(later) > templates.STAGES.index(stage)
        ):
            return f"{who} comes in the {later} stage, which waits until the {stage} stage is done"
        if step["waiting"] != "step" or step["wait_ref"] is None:
            break
        step = node(conn, scope, int(step["wait_ref"]))
    return ""


def _promise_waits(conn: sqlite3.Connection, row: Mapping[str, Any], on: str | None, today: date) -> date | None:
    """0.40.1 (analysis-0.40.0, 3.4): the last day a step keeping a promise may wait for, PROMISE_NEAR_DAYS before the
    promise is due, when it waits on a date; None for any other step. A promise's step couldn't wait at all: live,
    three cycles took "Report milestone #18's outcome by 10-31", which can't be kept before the milestone is judged on
    10-24, each was refused ("what blocks it goes in your message to them") and two of them wrote to the owner."""
    if row["obligation_id"] is None or row["kind"] != "promise":
        return None
    owed = conn.execute("SELECT due FROM obligations WHERE id = ?", (row["obligation_id"],)).fetchone()
    if owed is None:
        return None
    due = date.fromisoformat(str(owed["due"]))
    latest = due - timedelta(days=weights.PROMISE_NEAR_DAYS)
    if on != "date" or latest <= today:
        when = f"from {latest.isoformat()} on" if latest > today else "now"
        raise PlanError(
            "on" if on != "date" else "until",
            f"step #{row['id']} keeps your promise due {due.isoformat()}: it waits only on a date, until"
            f" {latest.isoformat()} at the latest; {when} keep it, or tell your owner why you can't",
        )
    return latest


def hold(conn: sqlite3.Connection, scope: AgentScope, line: int, why: str, cycle_id: int | None, now: str) -> str:
    """Ember halts a product to work elsewhere (never closes or drops it: the owner does): not while it has a promise
    open or a step the owner pinned. The owner's decisions on it still come first."""
    product = _open_product(conn, scope, line)
    if product["hold_reason"]:
        raise PlanError("action", f"product line #{line} is on hold already")
    pinned = nodes(conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND pinned = 1", (line,))
    promised = nodes(conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND kind = 'promise'", (line,))
    if pinned or promised:
        raise PlanError(
            "action", f"product line #{line} has {'a pinned step' if pinned else 'a promise'} open: no hold"
        )
    _update(conn, product["id"], now, hold_reason=_cut(why, 200), hold_by="agent")
    change(conn, scope, product["id"], cycle_id, "agent", "hold", why, now)
    return (
        f"Product line #{line} is on hold: Ember's code takes none of its steps until it is resumed (plan_step "
        "resume, or your owner). End this cycle's work on it."
    )


def resume(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    why: str,
    cycle_id: int | None,
    now: str,
    actor: str = "agent",
) -> str:
    product = _open_product(conn, scope, line)
    if not product["hold_reason"]:
        raise PlanError("project_id", f"product line #{line} isn't on hold")
    if actor == "agent" and product["hold_by"] == "owner":  # 0.36.0: the owner's hold is theirs to lift
        raise PlanError("project_id", f"your owner holds product line #{line}: only they resume it")
    _update(conn, product["id"], now, hold_reason=None, hold_by=None)
    change(conn, scope, product["id"], cycle_id, actor, "resume", why, now)
    return f"Product line #{line} is resumed: Ember's code weighs its steps again."


def owner_unwait(conn: sqlite3.Connection, scope: AgentScope, node_id: int, who: str | None, now: str) -> int | None:
    """0.40.3 (analysis-0.40.1 codebase, PL5): the owner lifts a wait Ember said for a step (plan_step wait): it is
    weighed again at once. Only Ember's code lifted one (``_lift_waits``, once its block was gone), so a wait said by
    mistake held its step until then, and neither the owner nor Ember could undo it; their pin keeps Ember from saying
    it again. Returns the step's line (None: a step of no product line)."""
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "step":
        raise PlanError("id", "no such step", 404)
    if row["status"] != "open":
        raise PlanError("id", "this step is closed", 409)
    if not said_wait(row):
        raise PlanError("id", "Ember said no wait for this step", 409)
    _update(conn, row["id"], now, waiting=None, wait_ref=None, wait_until=None, wait_why=None)
    change(conn, scope, row["id"], None, "owner", "unwait", f"{(who or 'your owner')[:60]} lifted the wait", now)
    return int(row["project_id"]) if row["project_id"] is not None else None


def owner_rework(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, who: str | None, now: str
) -> tuple[int, int]:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "A rework Ember can decline"): the owner has a rework Ember declined
    (``decline_step``) made anyway, while its request is still the newest of its kind (``_reworkable``): a step
    proposing it again takes the declined one's place, pinned (the next cycle takes it first, and Ember can't decline
    it). A closed step is final, so it is a new one. Returns its line and the new step."""
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "step":
        raise PlanError("id", "no such step", 404)
    if _reworkable(conn, scope, row) is None:
        raise PlanError("id", "Ember declined no rework here that is still to make", 409)
    step = _insert(
        conn,
        scope,
        now,
        parent_id=row["parent_id"],
        level="step",
        project_id=row["project_id"],
        template=row["template"],
        stage=row["stage"],
        kind=row["kind"],
        title=row["title"],
        check_kind=row["check_kind"],
        check_spec=json.dumps({**_spec(row), "since": now}),
        seq=row["seq"],
        source="owner",
        pinned=1,
    )
    why = f"{(who or 'your owner')[:60]} has the rework you declined (step #{row['id']}) made anyway"
    change(conn, scope, step, None, "owner", "add", why, now)
    return int(row["project_id"]), step


def said_wait(row: Mapping[str, Any]) -> bool:
    """0.40.3 (PL5): whether a step waits on a block Ember said (plan_step wait: its number or its day; a template's
    wait on an upgrade nobody asked for has neither), which the owner may lift (``owner_unwait``)."""
    return bool(row["waiting"]) and (row["wait_ref"] is not None or row["wait_until"] is not None)


# --- the owner's word on a product (0.35.0) ---


def _owner_product(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> sqlite3.Row:
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "product":
        raise PlanError("id", "no such product", 404)
    if row["status"] != "open":
        raise PlanError("id", f"this product is {row['status']}", 409)
    return row


def end_product(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, done: bool, why: str, who: str | None, now: str
) -> int:
    """The owner closes a product: done (its line succeeded) or dropped (abandoned), with everything under it. Only
    the owner does (Ember holds a product at most). Returns its line."""
    product = _owner_product(conn, scope, node_id)
    line = int(product["project_id"])
    status = "succeeded" if done else "abandoned"
    words = " ".join(why.split())[:300] or ("you marked it done" if done else "you dropped it")
    conn.execute(
        "UPDATE projects SET status = ?, updated_at = ?, notes = substr(notes || ?, -2000) WHERE id = ?"
        " AND status IN ('idea', 'active', 'waiting')",
        (status, now, f"\n[your owner] {words}", line),
    )
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (line,)).fetchone()
    _close_product(conn, scope, product, project, now, by="owner")
    change(
        conn, scope, node_id, None, "owner", "close" if done else "drop", f"{(who or 'your owner')[:60]}: {words}", now
    )
    return line


def _is_ventures(row: Mapping[str, Any]) -> bool:
    return row["level"] == "project" and row["platform"] == "ventures"


def _owner_held(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> sqlite3.Row:
    """0.36.0: what the owner holds or resumes: a product, or the Ventures project (its Explore step)."""
    row = node(conn, scope, node_id)
    if row is not None and _is_ventures(row):
        return row
    return _owner_product(conn, scope, node_id)


def owner_resume(conn: sqlite3.Connection, scope: AgentScope, node_id: int, who: str | None, now: str) -> int | None:
    """The owner lifts a hold on a product (0.36.0: theirs or Ember's), or on the ventures. Returns its line (None:
    the ventures)."""
    held = _owner_held(conn, scope, node_id)
    why = f"{(who or 'your owner')[:60]} lifted it"
    if _is_ventures(held):
        if not held["hold_reason"]:
            raise PlanError("id", "your ventures aren't on hold")
        _update(conn, held["id"], now, hold_reason=None, hold_by=None)
        change(conn, scope, held["id"], None, "owner", "resume", why, now)
        return None
    resume(conn, scope, int(held["project_id"]), why, None, now, actor="owner")
    return int(held["project_id"])


def owner_hold(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, why: str, who: str | None, now: str
) -> tuple[int | None, list[str]]:
    """0.36.0: the owner holds a product (its steps wait until they resume it; Ember can't), theirs in place of a hold
    of Ember's, or new things on the Ventures ("nothing new": no venture cycle and no new product until they resume
    them). Returns its line (None: the ventures) and (0.39.3) what is owed on the line that waits with it, named: the
    promises Ember made the owner about it and the owner's decisions on its requests (``owed_on``), which until then
    went on under their hold (analysis-0.39.1, 4.4)."""
    held = _owner_held(conn, scope, node_id)
    if held["hold_by"] == "owner":
        raise PlanError("id", "you hold it already", 409)
    words = " ".join(why.split())[:200] or "your owner holds it"
    _update(conn, held["id"], now, hold_reason=words, hold_by="owner")
    change(conn, scope, held["id"], None, "owner", "hold", f"{(who or 'your owner')[:60]}: {words}", now)
    if _is_ventures(held):
        return None, []
    line = int(held["project_id"])
    return line, owed_on(conn, scope, line)


def owed_on(conn: sqlite3.Connection, scope: AgentScope, line: int) -> list[str]:
    """0.39.3: the open promises to the owner and the owner's decisions of product line ``line`` (obligations.owed),
    in words for the owner: what their hold of the line holds besides its steps. A decision is recorded at Ember's
    next wake: one made just now isn't named yet, and waits with the rest."""
    where, params = scope.where()
    said: list[str] = []
    for row in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind IN ('promise', 'decision') AND status = 'open' ORDER BY id",
        params,
    ).fetchall():
        if obligations.owed(conn, scope, row).line != line:
            continue
        if row["kind"] == "promise":
            due = f" (due {row['due']})" if row["due"] else ""
            said.append(f"promise #{row['id']} {_quoted(row['what'])}{due}")
        elif row["approval_id"] is not None:
            said.append(f"your decision on request #{row['approval_id']}")
    return said


# --- the owner's freezes (0.36.0) ---

FREEZE_DAYS = 60  # a freeze's last day is this many days ahead at most


def freeze(
    conn: sqlite3.Connection, scope: AgentScope, until: str | None, who: str | None, now: str, today: date
) -> str | None:
    """The owner freezes the titles and tags of the live listings until ``until`` (its last day), or lifts the freeze
    (None): live, "no title or tag edits before 10-20" was a sentence the plan and the critic never read. Returns the
    day it holds until, None once lifted."""
    if until is not None:
        try:
            day = date.fromisoformat(until)
        except ValueError:
            raise PlanError("until", "give the freeze's last day as YYYY-MM-DD") from None
        if not today <= day <= today + timedelta(days=FREEZE_DAYS):
            raise PlanError("until", f"give a last day from today to {FREEZE_DAYS} days ahead")
    where, params = scope.where()
    conn.execute(
        f"UPDATE plan_freezes SET lifted_at = ? WHERE {where} AND what = 'titles_tags' AND lifted_at IS NULL",
        (now, *params),
    )
    if until is not None:
        conn.execute(
            "INSERT INTO plan_freezes (mode, session, what, until, signed, created_at)"
            " VALUES (?, ?, 'titles_tags', ?, ?, ?)",
            (scope.mode, scope.session, until, who, now),
        )
    return until


def frozen(conn: sqlite3.Connection, scope: AgentScope, today: date) -> str | None:
    """The last day of the owner's freeze of the live listings' titles and tags while it holds, else None."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT until FROM plan_freezes WHERE {where} AND what = 'titles_tags' AND lifted_at IS NULL AND until >= ?"
        " ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    return str(row["until"]) if row else None


def owner_keep(conn: sqlite3.Connection, scope: AgentScope, node_id: int, why: str, who: str | None, now: str) -> int:
    """The owner keeps a product at its decide-by date: their step closes, the product goes on. Returns its line."""
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "step" or row["template"] != "decide/owner":
        raise PlanError("id", "no such decision of yours", 404)
    if row["status"] != "open":
        raise PlanError("id", "you decided it already", 409)
    words = " ".join(why.split())[:200] or "keep it"
    _close(conn, row["id"], now, "done", f"Your owner kept it: {words}", by="owner")
    change(conn, scope, row["id"], None, "owner", "close", f"{(who or 'your owner')[:60]} kept it: {words}", now)
    line = int(row["project_id"])
    for product in nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,)):
        # 0.40.2: the hold a review's stop put on the line (_review_holds) ends with the owner's keep
        if product["hold_by"] == "agent" and str(product["hold_reason"] or "").startswith(REVIEW_HOLD):
            _update(conn, product["id"], now, hold_reason=None, hold_by=None)
            change(conn, scope, product["id"], None, "owner", "resume", f"{(who or 'your owner')[:60]} kept it", now)
    return line


CLOSED_WORDS = {  # 0.40.3
    "close": "closed",
    "drop": "dropped",
    "resume": "resumed",
    "unwait": "lifted the wait of",
    "add": "added",  # 0.42.1: a rework you declined, made anyway (owner_rework)
}


def owner_words(conn: sqlite3.Connection, scope: AgentScope, since: str | None) -> list[str]:
    """What the owner did to the plan since ``since`` (the last cycle's end): a step pinned, a worth set, a product
    closed, dropped or resumed, a decide-by date answered. YOUR PLAN says it right after the goal."""
    if since is None:
        return []
    where, params = scope.where("w")
    said = [
        {
            "pin": "pinned step",
            "unpin": "unpinned step",
            "worth": "set the worth of",
            "clear_worth": "cleared the worth of",
        }[r["action"]]
        + f" #{r['node_id']} {_quoted(r['title'])}"
        + (f" to {r['value']:g}" if r["action"] == "worth" else "")
        for r in conn.execute(
            f"SELECT w.*, n.title FROM plan_words w JOIN plan_nodes n ON n.id = w.node_id WHERE {where}"
            " AND w.created_at > ? ORDER BY w.id",
            (*params, since),
        )
    ]
    where, params = scope.where("c")
    said += [
        f"{CLOSED_WORDS.get(r['action'], r['action'])} #{r['node_id']} {_quoted(r['title'])}: {_cut(r['why'], 80)}"
        for r in conn.execute(
            f"SELECT c.*, n.title FROM plan_changes c JOIN plan_nodes n ON n.id = c.node_id WHERE {where}"
            " AND c.actor = 'owner' AND c.created_at > ? ORDER BY c.id",
            (*params, since),
        )
    ]
    return said
