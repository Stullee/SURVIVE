"""0.42.2 (analysis-0.40.1, 5.6): what is ready, and its weight, split out of plan.py.

``candidates`` reads every open step as weights.py weighs it (a ``Candidate``): its kind, worth, urgency and age
(``_clocks``), a promise's or an owner's decision (``_owner_candidates``), a venture's step (``_venture_candidates``)
and how well its channel works (``_channel_factors``). The brake (0.37.6: ``mark``, ``_stale``) holds a step a cycle
took with nothing moving, and one whose own requests are on their way; the owner's limits and the tools'
preconditions (0.37.9, 0.42.0: ``limits``, ``_limited``) hold a step its tool would refuse, with the tool's words.
``candidates`` reads ``_channel_factors``, ``_stale`` and ``_ventured_last`` through plan, where tests patch them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso, local_date, to_iso
from ..integrations import publish_queue
from . import desk, obligations, preconditions, reach, store, templates, ventures, weights
from .plan_model import (
    CHANNEL_EXECUTORS,
    CHANNELS,
    CHECK_EXECUTORS,
    CLICKS_PER_PIN,
    FINISHED,
    FRESH_PINS,
    FRESH_POSTS,
    IN_FLIGHT,
    MADE_BY,
    OBLIGATION_HOURS,
    PROMISE_TRIES,
    REPORT,
    RESULT_DAYS,
    SEQUENTIAL,
    VIEWS_PER_POST,
    Facts,
    _days,
    _midnight,
    _open_steps,
    _spec,
    _stages,
    _steps,
    _update,
    current_stage,
    node,
    nodes,
)
from .plan_ventures import VENTURE_STEPS, VENTURE_TEMPLATES, _last_taken, venture_of, venture_worth, ventures_node
from .store import AgentScope, canonical, sha256

# --- what is ready, and its weight ---


@dataclass(frozen=True)
class Candidate:
    """A step as the scorer sees it, with what the owner's tab shows of it."""

    step: weights.Step
    stage: str
    # why it can't be taken now ('owner', 'channel', 'upgrade', 'step', 'hold'; 0.37.6 'approved', 'tried'; 0.37.9 a
    # limit of the owner's, ``limits``: 'queue', 'messages'; 0.38.1 'sales'; 0.39.6 'views'; 0.40.3 'unread'; 0.42.0
    # 'blocked', another precondition of its tool's), None: ready
    waiting: str | None
    # 0.37.6: a cycle took it, and nothing its check reads moved since (it waits 'tried' while that was today; ready
    # again the next day for a try, it doesn't cut a cycle's sleep: busy)
    tried: bool = False
    # 0.42.0: the words its tool would refuse it with, while it waits on one of the tools' preconditions (REFUSED)
    refused: str | None = None


def _waiting(row: Mapping[str, Any], channels: Mapping[str, bool]) -> str | None:
    if row["kind"] == templates.OWNER_KIND:
        return "owner"
    if row["waiting"]:
        return str(row["waiting"])
    if row["channel"] and not channels.get(row["channel"], False):
        return "channel"
    return None


def _channel_factors(conn: sqlite3.Connection, scope: AgentScope, now: str) -> dict[str, float]:
    """How well each channel works, per item (an untested channel: 1.0): clicks per live pin, of those live RESULT_DAYS
    at least (0.35.3); 0.39.6: for Bluesky and the blog, the listing views that followed a post (``_views_after``).
    Live on 10-15, Bluesky was worth its reactions (2.3 a post, from people following Ember's story, who don't buy:
    case #25) and the blog 1.0, never measured: its monthly posts ranked second after the daily review had stopped
    its test for near-zero traffic, while Pinterest, measured at 0 clicks, sat at the floor."""
    where, params = scope.where()
    factors = {c: 1.0 for c in CHANNELS}
    settled = to_iso(from_iso(now) - timedelta(days=RESULT_DAYS))
    pins = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(clicks), 0) AS clicks FROM pinterest_pins WHERE {where}"
        " AND status = 'active' AND finished_at <= ?",
        (*params, settled),
    ).fetchone()
    if pins["n"] >= FRESH_PINS:
        factors["pinterest"] = round(
            min(weights.CHANNEL_MAX, max(weights.CHANNEL_MIN, pins["clicks"] / pins["n"] / CLICKS_PER_PIN)), 2
        )
    posts = [  # 0.40.3 (analysis-0.40.1 codebase, PL2): the owner's days, as the observations' and the blog's are
        (local_date(str(r["finished_at"])), f"{r['link'] or ''} {r['second_link'] or ''}")
        for r in conn.execute(
            f"SELECT finished_at, link, second_link FROM bluesky_posts WHERE {where} AND status = 'active'"
            " AND sent = 1 AND finished_at <= ?",
            (*params, settled),
        )
    ]
    blog = [
        (str(r["day"]), str(r["payload"] or ""))
        for r in conn.execute(
            f"SELECT b.day, a.payload FROM blog_posts b LEFT JOIN approvals a ON a.id = b.approval_id"
            f" WHERE {scope.where('b')[0]} AND b.day <= ?",
            (*scope.where("b")[1], local_date(settled)),
        )
    ]
    for channel, items in (("bluesky", posts), ("blog", blog)):
        if len(items) >= FRESH_POSTS:
            gained = sum(_views_after(conn, scope, day, text) for day, text in items)
            factors[channel] = round(
                min(weights.CHANNEL_MAX, max(weights.CHANNEL_MIN, gained / len(items) / VIEWS_PER_POST)), 2
            )
    return factors


def _views_after(conn: sqlite3.Connection, scope: AgentScope, day: str, text: str) -> int:
    """0.39.6: the views the listings ``text`` links gained from ``day`` to RESULT_DAYS later, by the daily views
    Ember's code observes (from its first day observed after ``day`` when none was before; 0 for a text that links no
    listing: what it brought the shop can't be seen)."""
    where, params = scope.where()
    until = (date.fromisoformat(day) + timedelta(days=RESULT_DAYS)).isoformat()

    def seen(listing: int, condition: str, bound: str, order: str) -> int | None:
        row = conn.execute(
            f"SELECT value FROM observations WHERE {where} AND subject = 'listing' AND subject_id = ?"
            f" AND metric = 'views' AND {condition} ORDER BY day {order} LIMIT 1",
            (*params, listing, bound),
        ).fetchone()
        return None if row is None else int(row["value"])

    gained = 0
    for listing in sorted(reach.listing_ids(text)):
        before = seen(listing, "day <= ?", day, "DESC")
        if before is None:
            before = seen(listing, "day >= ?", day, "ASC")
        after = seen(listing, "day <= ?", until, "DESC")
        if before is not None and after is not None:
            gained += max(0, after - before)
    return gained


def _missed(conn: sqlite3.Connection, scope: AgentScope, today: date) -> set[int]:
    """The products whose marketing is urgent: a views bar of their decide-by dates missed lately (pushed_until;
    0.33.0: a miss owes a push for buyers, never title and tag edits)."""
    return {
        int(r["project_id"])
        for r in nodes(conn, scope, "level = 'product' AND status = 'open' AND pushed_until >= ?", (today.isoformat(),))
    }


def _worked(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """When each product line was last worked on: its newest cycle's start."""
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT project_id, MAX(started_at) FROM cycles WHERE session = ? AND simulated = ?"
            " AND project_id IS NOT NULL GROUP BY project_id",
            (scope.session, 1 if scope.simulated else 0),
        )
    }


def candidates(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
    before: int | None = None,
    *,
    day_start: str | None = None,
) -> list[Candidate]:
    """Every open step of the open products, ready or waiting, weighed by weights.py's parts (the scorer leaves out
    the waiting ones). An owner's decision (``_decisions``) is a step of its own in whatever stage it stands, and
    (0.35.1) so is a promise; it waits while a request of its product made since it was promised waits on the owner.
    0.37.0: both are weighed like any step, worth at least weights.PROMISE_WORTH and urgent as their day nears, until
    cycles took them PROMISE_TRIES times (a decision once) in OBLIGATION_HOURS (until 0.36.0 they came first that
    often; 0.37.1: urgent only from weights.PROMISE_NEAR_DAYS before their day); those of no product are the Owner
    project's (``_owner_candidates``). The owner's word comes before Ember's hold: only their park or kill, a closed
    line or (0.39.3) their own hold stops these two. 0.36.0: and the ventures' steps (0.37.0: each venture's;
    ``exploring``: whether this cycle may explore; ``before``: the cycle it is for, None the next), which take turns
    with a new product while no product step is ready (a new install's first cycles: live until 0.35.3 the day's first
    cycle was an ordinary one, which started a product).

    0.39.3 (analysis-0.39.1, 4.4): the owner's hold ("its steps wait until you resume it") holds its promises and their
    decisions too, as their park does. Only a stopped venture held them: a product the owner held after rejecting its
    listing ("I look at it next week") got the next cycle for the rejection, on its day, and one a day for a promise.

    0.37.6, the brake, one rule for every step: one a cycle took without what its check reads moving (``mark``) waits
    until that moves or the owner's next day ('tried'); a step of Ember's whose own requests are on their way, and all
    its check still needs, waits on them ('owner' while one waits for the owner's decision, 'approved' while they are
    carried out), without one of Ember's waits; an owner's step with nothing for them to decide waits on the step of
    Ember's before it ('step': in the analysis of 0.37.0, "You approve it" waited on the owner while no request
    did).

    0.37.9: and a step whose work a limit of the owner's would refuse waits on it (``_limited``): a pin, a post or a
    blog post while the publishing queue takes no more ('queue'), a report to the owner once the day's messages that
    answer none of theirs are sent ('messages'; ``day_start``: when the owner's day began, their clock's); 0.38.1 a
    listing, a change of one, a product or a book proposed while the sell requests that may wait for the owner wait
    ('sales'). 0.42.0: each the tool's own precondition, and so is any other of its tool's ('blocked': KDP's week, a
    first listing's demand note), with the tool's words (Candidate.refused)."""
    from .plan import _channel_factors, _stale, _ventured_last  # 0.42.2: read through plan, where tests patch them

    facts = Facts(conn, scope, now)
    capped = limits(conn, scope, day_start or _midnight(today), now)
    factors = _channel_factors(conn, scope, now)
    missed = _missed(conn, scope, today)
    worked = _worked(conn, scope)
    lately = _taken_counts(conn, scope, to_iso(from_iso(now) - timedelta(hours=OBLIGATION_HOURS)))
    asked = [(r["line"], r["created_at"]) for r in facts.requests() if r["status"] == "pending"]
    promised_at = _promised_at(conn, scope)
    picks = _last_picks(conn, scope)
    day = today.isoformat()
    taken_today = _taken_counts(conn, scope, day_start or _midnight(today))  # 0.39.8: a rework's second try
    found: list[Candidate] = []
    for product in nodes(conn, scope, "level = 'product' AND status = 'open'"):
        pid = int(product["project_id"])
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        stopped = project is None or ventures.project_stopped(conn, scope, pid) is not None
        held = bool(product["hold_reason"])
        owners_hold = held and product["hold_by"] == "owner"  # 0.39.3: it holds what is owed too
        current = current_stage(conn, scope, product["id"])
        if current is None:
            continue
        funnel = facts.funnel(pid)
        worth = weights.worth(
            float(product["could_earn"] or 0),
            str(current["stage"]),
            funnel.views if funnel else 0,
            funnel.favorites if funnel else 0,
            funnel.orders if funnel else 0,
            product["owner_worth"],
        )
        verdict = facts.verdict(pid)
        live = facts.live(pid) > 0  # its maintain steps (the critic's fix, a decide-by's) are ready once it is live
        rows = [
            (stage, step)
            for stage in _stages(conn, scope, product["id"])
            for step in _steps(conn, scope, stage["id"])
            if step["status"] == "open" and (stage["status"] == "open" or step["obligation_id"] is not None)
        ]
        owed = [(stage, step) for stage, step in rows if step["obligation_id"] is not None]
        work = [(stage, step) for stage, step in rows if step["obligation_id"] is None]
        steps: dict[int, weights.Step] = {}
        reasons: dict[int, str | None] = {}
        stale: dict[int, str | None] = {}
        rework: dict[int, bool] = {}  # 0.39.8: proposing again what the owner rejected lately (_rework)
        first_taken = False
        for stage, step in work:
            sequential = stage["stage"] in SEQUENTIAL
            later = stage["id"] != current["id"] and not (
                stage["stage"] == "maintain" and (current["stage"] == "launch" or live)
            )
            stale[step["id"]] = _stale(facts, step, picks)
            rework[step["id"]] = _rework(facts, step, pid)
            reason: str | None
            if stopped or held:
                reason = "hold"
            elif later or (sequential and first_taken):
                reason = "step"
            elif step["kind"] == templates.OWNER_KIND:
                reason = _owners(facts, step, pid)  # 0.37.6: theirs while something waits for them
            else:
                reason = _waiting(step, channels) or _requested(facts, step, pid) or _limited(facts, step, capped)
                if reason is None and _recurring_market(step) and not live:
                    reason = "listing"  # 0.39.8: its check counts what links the line's own listings, and it has none
                if reason is None and _at_floor(step, factors):
                    reason = "floor"  # 0.39.8: its channel brings next to nothing; the owner's pin brings it back
                # 0.39.8 (analysis-0.39.6, 3.2): what the owner rejected and waits for gets a second cycle a day: live,
                # the cycle that took the KDP book at 01:09 ran out of room, and the brake held it for the whole day
                second = rework[step["id"]] and taken_today.get(int(step["id"]), 0) < weights.REWORK_TRIES
                if reason is None and stale[step["id"]] == day and not second:
                    reason = "tried"
            if sequential and stage["id"] == current["id"] and reason is None:
                first_taken = True
            urgency = 0.0
            if step["kind"] == "market" and stage["stage"] == "launch" and live:  # 0.35.3: its first buyers
                urgency = weights.REACH
            if step["kind"] == "market" and pid in missed:
                urgency = max(urgency, weights.MISSED_BAR)
            quiet = False  # 0.39.1: the critic's suggestions while few see the line, or after checks without a pass
            if step["check_kind"] == "critic" and verdict == "improve":  # 0.35.3: a defect, or suggestions
                defect = facts.defect(pid)
                unseen = (funnel.views if funnel else 0) < weights.CRITIC_VIEWS
                quiet = not defect and (unseen or facts.stuck(pid))
                urgency = max(urgency, weights.DEFECT if defect else 0.0 if quiet else weights.IMPROVE)
                if quiet and unseen and reason is None and not step["pinned"]:  # the owner's pin: they want it now
                    # 0.39.6 (analysis-0.39.5, 4.2.7): it waits for views. As a chore it was still taken whenever
                    # nothing heavier was ready: live on 10-15 five cycles ($1.95) made new covers and files for
                    # listings 1 to 3 people had seen, each one more request for the owner, and the agent's own
                    # case said a cover can't be judged at 0-4 views. A defect still comes first.
                    reason = "views"
            if step["due"] and step["source"] == "code" and date.fromisoformat(step["due"]) <= today:
                urgency = max(urgency, weights.RECURRING_DUE)
            if rework[step["id"]]:  # 0.39.8: the owner rejected it lately and waits for it again
                urgency = max(urgency, weights.REWORK)
            since = max(filter(None, (step["ready_since"], worked.get(pid))), default=None)
            steps[step["id"]] = weights.Step(
                step["id"],
                pid,
                str(step["title"]),
                "chore" if quiet else _kind(step),
                worth,
                channel=factors.get(step["channel"], 1.0) if step["channel"] else 1.0,
                urgency=urgency,
                age_days=round(_days(since, now), 3) if reason is None else 0.0,
                pinned=bool(step["pinned"]),
                blocked=reason is not None,
            )
            reasons[step["id"]] = reason
        ordered = [steps[step["id"]] for _, step in work]
        for index, (stage, step) in enumerate(work):
            behind = ordered[index + 1 :] if stage["stage"] in SEQUENTIAL else []
            waiting = tuple(s for s in behind if s.product == pid)
            found.append(
                Candidate(
                    weights.Step(**{**_fields(steps[step["id"]]), "waiting": waiting}),
                    str(stage["stage"]),
                    reasons[step["id"]],
                    stale[step["id"]] is not None,
                    _words(facts, step, capped, reasons[step["id"]]),
                )
            )
        for stage, step in owed:
            promise = step["kind"] == "promise"
            reason = "hold" if stopped or owners_hold else _waiting(step, channels)
            made = promised_at.get(int(step["obligation_id"]))
            if reason is None and promise and made and any(line == pid and at > made for line, at in asked):
                reason = "owner"  # a request for it waits on the owner's decision
            reason = reason or _limited(facts, step, capped)  # 0.37.9
            tried = _stale(facts, step, picks)
            if reason is None and tried == day:
                reason = "tried"
            since = max(filter(None, (step["ready_since"], worked.get(pid))), default=None)
            found.append(
                Candidate(
                    weights.Step(
                        step["id"],
                        pid,
                        str(step["title"]),
                        _kind(step),
                        max(worth, weights.PROMISE_WORTH),  # 0.37.0: the owner's, whatever its product is worth
                        urgency=_owed_urgency(step, today, lately),
                        age_days=round(_days(since, now), 3) if reason is None else 0.0,
                        blocked=reason is not None,
                    ),
                    str(stage["stage"]),
                    reason,
                    tried is not None,
                    _words(facts, step, capped, reason),
                )
            )
    found += _owner_candidates(facts, today, channels, lately, picks, capped)  # 0.37.0: what is owed of no product
    # 0.37.6: a step that waits for tomorrow since a cycle tried it is work of Ember's still: no new product's turn
    turn = exploring and not any(c.waiting in (None, "tried") for c in found) and _ventured_last(conn, scope, before)
    # 0.37.0: and the ventures' steps
    return found + _venture_candidates(conn, scope, now, today, exploring, turn, picks)


def _taken_counts(conn: sqlite3.Connection, scope: AgentScope, since: str) -> dict[int, int]:
    """How often cycles took each step since ``since`` (0.35.0: an owner's decision is taken first once in
    OBLIGATION_HOURS; 0.35.1: a promise PROMISE_TRIES times)."""
    where, params = scope.where()
    return {
        int(r[0]): int(r[1])
        for r in conn.execute(
            f"SELECT node_id, COUNT(*) FROM plan_picks WHERE {where} AND node_id IS NOT NULL AND created_at >= ?"
            " GROUP BY node_id",
            (*params, since),
        )
    }


def _promised_at(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """0.35.1: when each open promise was made."""
    where, params = scope.where()
    return {
        int(r["id"]): str(r["created_at"])
        for r in conn.execute(
            f"SELECT id, created_at FROM obligations WHERE {where} AND kind = 'promise' AND status = 'open'", params
        )
    }


def _venture_candidates(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    exploring: bool,
    turn: bool = False,
    picks: Mapping[int, Any] | None = None,
) -> list[Candidate]:
    """The ventures' steps as the scorer sees them: each worth the owner's (the venture's, else all the ventures'),
    else what its case expects (venture_worth); urgent when the owner wished for it (weights.ASKED) or Ember's code
    parks it soon (weights.PARK_SOON); aging since it was ready or a cycle last took it. They wait on the owner (their
    decision on a case), while the owner holds new things ('hold'), while the cycle runs no venture work ('mode': a
    burn mode without venture cycles, or what the owner waits for first: ``exploring`` false), when a new product has
    its turn ('turn': no product step is ready and the cycle before explored; not the step the owner pinned), an
    idea while ventures.MAX_ACTIVE are researched or proposed ('room'), and once its venture was backed, parked or
    killed until the next cycle's keeper closes it ('moved'). 0.37.6: and once a cycle took it without its venture's
    records moving (``mark``: its stage, evidence, cases, the critic's answers; a brainstorm's new ideas), until
    they move or the next day ('tried')."""
    from .plan import _stale  # 0.42.2: read through plan, where tests patch it

    top = ventures_node(conn, scope)
    if top is None:
        return []
    facts = Facts(conn, scope, now)
    picks = _last_picks(conn, scope) if picks is None else picks
    rows = {int(v["id"]): v for v in ventures.all_ventures(conn, scope)}
    room = sum(1 for v in rows.values() if v["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE
    taken = _last_taken(conn, scope)
    owned = (*VENTURE_TEMPLATES, VENTURE_STEPS["decide"][0])
    marks = ", ".join("?" for _ in owned)
    found: list[Candidate] = []
    for step in nodes(conn, scope, f"level = 'step' AND status = 'open' AND template IN ({marks})", owned):
        parent = node(conn, scope, int(step["parent_id"]))
        v = rows.get(int(parent["venture_id"])) if parent is not None and parent["venture_id"] is not None else None
        item = desk.item(conn, v, today=today, room=room) if v is not None else None
        mine = parent is not None and parent["venture_id"] is not None  # a venture's step (not the brainstorm)
        reason: str | None
        if mine and (v is None or v["stage"] not in ventures.EXPLORING):
            reason = "moved"  # backed, parked or killed since the tree was kept: the next cycle's keeper closes it
        elif step["kind"] == templates.OWNER_KIND:
            reason = "owner"
        elif top["hold_reason"]:
            reason = "hold"
        elif not exploring:
            reason = "mode"
        elif turn and not step["pinned"]:  # the owner's Explore next is their word: it doesn't wait for the turn
            reason = "turn"
        elif step["waiting"]:
            reason = str(step["waiting"])
        elif mine and item is None:
            reason = "room"
        else:
            reason = None
        stale = _stale(facts, step, picks)
        if reason is None and stale == today.isoformat():
            reason = "tried"
        if parent is not None and parent["owner_worth"] is not None:
            worth = float(parent["owner_worth"])
        elif top["owner_worth"] is not None:
            worth = float(top["owner_worth"])
        else:
            worth = venture_worth(conn, v)
        tier = item.tier if item is not None else ""
        urgency = weights.ASKED if tier == "wish" else weights.PARK_SOON if tier == "urgent" else 0.0
        since = max(filter(None, (step["ready_since"], taken.get(int(step["id"])))), default=None)
        found.append(
            Candidate(
                weights.Step(
                    step["id"],
                    None,
                    str(step["title"]),
                    _kind(step),
                    worth,
                    urgency=urgency,
                    age_days=round(_days(since, now), 3) if reason is None else 0.0,
                    pinned=bool(step["pinned"]),
                    blocked=reason is not None,
                ),
                "venture",
                reason,
                stale is not None,
            )
        )
    return found


def _owner_candidates(
    facts: Facts,
    today: date,
    channels: Mapping[str, bool],
    lately: Mapping[int, int],
    picks: Mapping[int, Any],
    capped: OwnerLimits,
) -> list[Candidate]:
    """0.37.0: the Owner project's steps as the scorer sees them: worth weights.PROMISE_WORTH, urgent as their day
    nears (_owed_urgency). 0.37.6: one waits once a cycle took it without its obligation closing or, for a promise of a
    channel's work, the channel's requests made since it was promised moving ('tried', ``mark``), until they do or the
    next day. 0.37.9: and while a limit of the owner's would refuse its work (``_limited``: ``capped``)."""
    from .plan import _stale  # 0.42.2: read through plan, where tests patch it

    conn, scope, now = facts.conn, facts.scope, facts.now
    top = nodes(conn, scope, "level = 'project' AND platform = 'owner'")
    if not top:
        return []
    found = []
    for step in _open_steps(conn, scope, int(top[0]["id"])):
        reason = _waiting(step, channels) or _limited(facts, step, capped)
        stale = _stale(facts, step, picks)
        if reason is None and stale == today.isoformat():
            reason = "tried"
        found.append(
            Candidate(
                weights.Step(
                    step["id"],
                    None,
                    str(step["title"]),
                    _kind(step),
                    weights.PROMISE_WORTH,
                    urgency=_owed_urgency(step, today, lately),
                    age_days=round(_days(step["ready_since"], now), 3) if reason is None else 0.0,
                    pinned=bool(step["pinned"]),
                    blocked=reason is not None,
                ),
                "owner",
                reason,
                stale is not None,
                _words(facts, step, capped, reason),
            )
        )
    return found


def _rework(facts: Facts, step: Mapping[str, Any], line: int) -> bool:
    """0.39.8 (analysis-0.39.6, 3.2): whether a step proposing a request again (_again) follows the owner's rejection of
    the last one: they asked for a change and wait for it (live, "finally looking good. last small change is to extend
    the lines for the notes"). 0.40.1 (analysis-0.40.0, 3.3): until the request is proposed again (the owner's hold or
    drop of the line ends its steps), not for 3 days. An expired, withdrawn or failed request isn't. 0.42.1: which one
    (``_rejected``); Ember may decline it (``decline_step``)."""
    return _rejected(facts.conn, facts.scope, step, line) is not None


def _rejected(conn: sqlite3.Connection, scope: AgentScope, step: Mapping[str, Any], line: int | None) -> int | None:
    """0.42.1: the request a step proposing it again reworks (``_rework``): the newest of its kind on the line, when the
    owner rejected it; None for none."""
    template = str(step["template"] or "")
    if not template.startswith("propose/") or step["kind"] != "ship" or line is None:
        return None
    where, params = scope.where("a")
    last = conn.execute(
        "SELECT a.id, a.status, a.decided_at FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
        f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = ? ORDER BY a.id DESC LIMIT 1",
        (*params, line, template.removeprefix("propose/")),
    ).fetchone()
    return int(last["id"]) if last is not None and last["status"] == "rejected" and last["decided_at"] else None


def _recurring_market(step: Mapping[str, Any]) -> bool:
    """0.39.8: whether a step is a product's recurring marketing (a weekly pin or post, a monthly blog post)."""
    return str(step["template"] or "").startswith("recurring/") and step["kind"] == "market"


def _at_floor(step: Mapping[str, Any], factors: Mapping[str, float]) -> bool:
    """0.39.8 (analysis-0.39.6, 3.4): whether a recurring marketing step's channel is worth less than
    weights.CHANNEL_WORKS (twice its floor) and the owner didn't pin it: it waits ('floor'). Live, the code's weekly
    Bluesky post of each line and the blog's monthly posts took 9 of 12 cycles on 10-16, while the agent's own strategy
    allowed one Bluesky post a week and no blog work, and couldn't drop them (no replacing, 2 waits a day). 0.40.3
    (analysis-0.40.1 codebase, PL1): a launch's marketing too ("Post it twice on Bluesky", "A German blog post"): it
    was ready, took the launch's urgency, became YOUR STEP and cut the sleep, on a channel that brings nothing."""
    launch = step["kind"] == "market" and step["stage"] == "launch" and step["obligation_id"] is None
    if not (_recurring_market(step) or launch) or step["pinned"] or not step["channel"]:
        return False
    return factors.get(str(step["channel"]), 1.0) < weights.CHANNEL_WORKS


# --- the brake (0.37.6) ---


def _last_picks(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, sqlite3.Row]:
    """Each step's latest pick by a cycle that ran its plan (FINISHED: a cycle that failed, was refused or still runs
    didn't get to work on it), with what its check read then (``mark``) and the owner's day it was taken on."""
    where, params = scope.where("k")
    found: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT k.node_id, k.cycle_id, k.mark, k.day FROM plan_picks k JOIN cycles c ON c.id = k.cycle_id"
        f" WHERE {where} AND k.node_id IS NOT NULL AND c.status IN {FINISHED} ORDER BY k.id",
        params,
    ):
        found[int(r["node_id"])] = r
    return found


def _stale(facts: Facts, row: Mapping[str, Any], picks: Mapping[int, Any]) -> str | None:
    """The owner's day a cycle last took a step on, while nothing moved for it since (``mark``; a promise's or a step's
    Ember says is done: and that cycle made nothing for its product, ``_made_in``): it waits 'tried' while that day is
    today. None when no cycle took it (since 0.37.6) or something moved."""
    pick = picks.get(int(row["id"]))
    if pick is None or not pick["mark"] or mark(facts, row) != pick["mark"]:
        return None
    return None if _made_in(facts.conn, int(pick["cycle_id"]), row) else str(pick["day"] or "")


COUNT_NOTHING = (None, "agent", "obligation")  # checks that count nothing of their own before they pass


def mark(facts: Facts, row: Mapping[str, Any]) -> str:
    """What a step's check reads now, as a short digest, kept with each pick (plan_picks.mark): something moved for the
    step once this changed. The numbers its check counts and its own requests with their states (a request made,
    decided, expired or carried out); for a promise, a decision or a step Ember says is done, which count nothing of
    their own, their obligation and what became of their product's requests (the owner's decisions: one made since
    doesn't move them; what the cycle that took a promise or Ember's step made for its product does, ``_made_in``), and
    for one of no product what became of the requests of its channel made since it was promised; for a venture's step
    its venture's records (stage, evidence, cases, the critic's answers; not a note or a rescore), for a brainstorm
    the ideas; and the owner's word on the step (a pin since)."""
    conn = facts.conn
    line = row["project_id"]
    kind, spec = row["check_kind"], _spec(row)
    read: Any
    if row["template"] in VENTURE_TEMPLATES:
        read = _venture_read(conn, facts.scope, row)
    elif kind in COUNT_NOTHING:
        owed = None
        if kind == "obligation":
            owed = conn.execute("SELECT status, created_at FROM obligations WHERE id = ?", (spec.get("id"),)).fetchone()
        own: Any = None
        if line is not None:
            own = _own(facts, int(line), None)
        elif owed is not None and row["channel"] in CHANNEL_EXECUTORS:
            own = _own(facts, None, (CHANNEL_EXECUTORS[str(row["channel"])],), since=str(owed["created_at"]))
        decided = [r for r in own or () if r[1] != "pending"]
        read = [owed["status"] if owed is not None else None, decided]
    else:
        read = _check_read(facts, int(line), kind, spec) if line is not None else None
    words = conn.execute("SELECT COUNT(*) FROM plan_words WHERE node_id = ?", (row["id"],)).fetchone()[0]
    return sha256(canonical([words, read]))[:16]


def _made_in(conn: sqlite3.Connection, cycle_id: int, row: Mapping[str, Any]) -> bool:
    """Whether the cycle that took a promise of a product or a step Ember says is done (checks that count nothing of
    their own before they pass: work toward them can take several cycles) made something for its product that its
    checks read: a file, a picture or a KDP package, a demand note, a request to the owner. Work on the product in
    other cycles doesn't move them (those take the product's own steps), and an owner's decision moves only once it is
    closed or something became of its product's requests (in a simulated week of dry run, decisions the fake model
    never closed were taken again after each request a cycle made)."""
    if row["check_kind"] not in (None, "agent") and row["kind"] != "promise":
        return False
    if row["project_id"] is None or row["template"] in VENTURE_TEMPLATES:
        return False
    marks = ", ".join("?" for _ in MADE_BY)
    made = conn.execute(
        f"SELECT 1 FROM tool_calls WHERE cycle_id = ? AND status = 'ok' AND tool IN ({marks}) LIMIT 1",
        (cycle_id, *MADE_BY),
    ).fetchone()
    noted = conn.execute(
        "SELECT 1 FROM demand_notes WHERE cycle_id = ? AND project_id = ? LIMIT 1", (cycle_id, row["project_id"])
    ).fetchone()
    asked = conn.execute("SELECT 1 FROM approvals WHERE cycle_id = ? LIMIT 1", (cycle_id,)).fetchone()
    return made is not None or noted is not None or asked is not None


def _check_read(facts: Facts, line: int, kind: str, spec: Mapping[str, Any]) -> Any:
    """What a check counts for a product, with its own requests (``mark``)."""
    if kind == "any":
        return [_check_read(facts, line, str(s.get("check")), s) for s in spec.get("of", ())]
    own = _own(facts, line, _executors(kind, spec))
    if kind in ("pin", "post", "blog", "live"):
        return [_have(facts, line, kind), own]
    if kind == "critic":
        return [_newest_check(facts, line), own]
    if kind == "demand":
        return _demand_notes(facts, line)
    if kind in ("built", "kdp_check"):
        tools = ("propose_kdp_book",) if kind == "kdp_check" else tuple(str(t) for t in spec.get("tools", ()))
        return _made(facts, line, tools)
    return own


def _venture_read(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> Any:
    """A venture step's venture's records (``mark``): its stage, its evidence (the numbers research found, graded by
    Ember's code), business cases and the critic's answers; not a note or a rescore alone (in a simulated week of dry
    run, the fake model's repeated research saved a note and new scores every cycle: 183 of 220 cycles). A brainstorm's,
    the ideas."""
    venture = venture_of(conn, scope, int(row["id"]))
    if venture is None:
        where, params = scope.where()
        return list(conn.execute(f"SELECT COUNT(*), MAX(id) FROM ventures WHERE {where}", params).fetchone())
    v = conn.execute("SELECT stage FROM ventures WHERE id = ?", (venture,)).fetchone()
    return [
        v["stage"] if v is not None else None,
        *(
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE venture_id = ?", (venture,)).fetchone()[0]
            for table in ("evidence", "venture_cases", "venture_critiques")
        ),
    ]


def _own(
    facts: Facts, line: int | None, executors: tuple[str, ...] | None, since: str | None = None
) -> list[list[Any]]:
    """A product line's requests (None: every line's) by these executors (None: any), made since ``since``: [id,
    status], oldest first."""
    return [
        [r["id"], r["status"]]
        for r in facts.requests()
        if (line is None or r["line"] == line)
        and (executors is None or r["executor"] in executors)
        and (since is None or r["created_at"] >= since)
    ]


def _made(facts: Facts, line: int, tools: tuple[str, ...]) -> int:
    """The calls of these tools that worked in a product's cycles (a check-line file counts: it moved)."""
    if not tools:
        return 0
    marks = ", ".join("?" for _ in tools)
    return int(
        facts.conn.execute(
            "SELECT COUNT(*) FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.project_id = ?"
            f" AND c.session = ? AND c.simulated = ? AND t.tool IN ({marks}) AND t.status = 'ok'",
            (line, facts.scope.session, 1 if facts.scope.simulated else 0, *tools),
        ).fetchone()[0]
    )


def _demand_notes(facts: Facts, line: int) -> list[Any]:
    where, params = facts.scope.where()
    return list(
        facts.conn.execute(
            f"SELECT COUNT(*), MAX(id) FROM demand_notes WHERE {where} AND project_id = ?", (*params, line)
        ).fetchone()
    )


def _newest_check(facts: Facts, line: int) -> int | None:
    where, params = facts.scope.where()
    return facts.conn.execute(
        f"SELECT MAX(id) FROM quality_checks WHERE {where} AND project_id = ?", (*params, line)
    ).fetchone()[0]


def _have(facts: Facts, line: int, kind: str) -> int:
    """How many of what a counted check needs a product has: its live listings, or the pins, Bluesky posts and blog
    posts live that link one of them."""
    if kind == "live":
        return facts.live(line)
    funnel = facts.funnel(line)
    return 0 if funnel is None else {"pin": funnel.pins, "post": funnel.bluesky, "blog": funnel.posts}[kind]


def _executors(kind: str | None, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The executors of the requests a check counts (CHECK_EXECUTORS; a request check names its own)."""
    if kind == "any":
        return tuple(dict.fromkeys(e for s in spec.get("of", ()) for e in _executors(s.get("check"), s)))
    if kind == "request":
        return (str(spec["executor"]),) if spec.get("executor") else ()
    return CHECK_EXECUTORS.get(kind or "", ())


def _requested(facts: Facts, row: Mapping[str, Any], line: int) -> str | None:
    """What a step of Ember's waits on while its own requests are on their way and they are all its check still needs
    (two pins: live or asked for): 'owner' while one waits for the owner's decision, 'approved' while the approved
    ones are carried out (by Ember's code, or the owner's own hand). None while it needs more than is asked, and for a
    check a request made passes (Ember's code closes it)."""
    return _flying(facts, line, row["check_kind"], _spec(row))


def _flying(facts: Facts, line: int, kind: str | None, spec: Mapping[str, Any]) -> str | None:
    if kind == "any":
        return next((w for s in spec.get("of", ()) if (w := _flying(facts, line, s.get("check"), s))), None)
    if kind == "request" and spec.get("status") != "done":
        return None
    executors = _executors(kind, spec)
    mine = [
        r for r in facts.requests() if r["line"] == line and r["executor"] in executors and r["status"] in IN_FLIGHT
    ]
    if not mine:
        return None
    if kind in ("pin", "post", "blog", "live") and _have(facts, line, kind) + len(mine) < int(spec.get("count", 1)):
        return None  # more to ask for
    return "owner" if any(r["status"] == "pending" for r in mine) else "approved"


def _owners(facts: Facts, row: Mapping[str, Any], line: int | None) -> str:
    """What an owner's step waits on: the owner ('owner'), while one of the requests its check reads is on its way or
    it reads none (keep or drop a product, back a venture); else the step of Ember's before it ('step'): "You approve
    it, and it goes live" waits for a listing proposed again (live, it waited on the owner while no request did)."""
    executors = _executors(row["check_kind"], _spec(row))
    if not executors or line is None:
        return "owner"
    flying = any(
        r["line"] == line and r["executor"] in executors and r["status"] in IN_FLIGHT for r in facts.requests()
    )
    return "owner" if flying else "step"


# --- the owner's limits (0.37.9), and the tools' other preconditions (0.42.0) ---

# 0.37.9: two limits of the owner's that only the tools enforced (analysis-0.37.8, 6.3 and 6.4): the publish requests
# that may wait for them (store.PENDING_CAPS, and since 0.37.8 a channel's pins or posts waiting for them or their slot,
# publish_queue.WAITING), and the messages a day that answer none of theirs (store.MESSAGES_PER_DAY). Live, of the 30
# cycles #192 to #221, 9 took a pin, a post or a blog post while the queue was full ($2.57) and 7 a report to the owner
# after the day's messages were sent ($1.55; five in a row, #193 to #197): each step looked ready, cut the sleep to the
# owner's shortest and paid for a plan whose request the tool then refused. Now such a step waits on the limit
# ('queue', 'messages', ``candidates``) and cuts no sleep (``busy``). 0.42.0 (analysis-0.40.1 codebase, 6.2.1): the
# plan copied each rule (0.38.1 the sell requests, 0.40.3 the unread messages) and knew neither KDP's week nor a first
# listing's demand note: it asks the tools' own preconditions now (preconditions.py), one rule each, for the tool a
# step's work needs (``_calls``), and the step waits with the tool's words ('blocked' for a rule that is no limit of
# the owner's).

# 0.42.0: the tool that makes each kind of request a step's check counts, by its executor (PUBLISHES: by its channel)
CALLED = {
    "pinterest_pin": "propose_pin",
    "bluesky_post": "propose_bluesky_post",
    "site_post": "propose_blog_post",
    "site_links": "propose_link_page",
    "reddit_link": "propose_reddit_post",
    "etsy_listing": "propose_etsy_listing",
    "etsy_edit": "propose_etsy_edit",
    "printify_product": "propose_printify_product",
    "printify_edit": "propose_printify_edit",
    "kdp_package": "propose_kdp_book",
    "email": "propose_email",
}
PUBLISHES = {channel: CALLED[executor] for channel, executor in CHANNEL_EXECUTORS.items()}
EDITS = (CALLED["etsy_edit"], CALLED["printify_edit"])  # 0.42.1: a change of one of the line's listings (``_calls``)
# 0.42.6 (analysis-0.42.3, 3.1): a promise whose words propose a KDP book is propose_kdp_book's work (``_calls``)
KDP_PROMISE = re.compile(r"\bkdp\b", re.IGNORECASE)
PROPOSES = re.compile(r"\b(?:propos|submit)", re.IGNORECASE)
_CHECKED_BOOK = re.compile(r"KDP would take this (ebook|paperback) ")  # a propose_kdp_book check's result
# the waits the tools' preconditions set: each carries the tool's words (Candidate.refused)
REFUSED = frozenset(rule.wait for rules in preconditions.TOOLS.values() for rule in rules)


@dataclass(frozen=True)
class OwnerLimits:
    """The owner's limits a step's work can run into, as they stand: the publish requests waiting for the owner (a pin,
    a Bluesky post, a blog post and the link page are all publish requests), each queue channel's pins or posts
    waiting for the owner or for their slot, the messages of the owner's day that answered none of theirs, and
    whether a message of theirs waits for an answer (one that answers them is no message of that kind: it can carry
    a report). 0.38.1: and the sell requests waiting for the owner (a listing, a change of one, a Printify product, a
    KDP book). 0.40.3 (analysis-0.40.1 codebase, PL3): and Ember's messages the owner hasn't read yet. 0.42.0
    (analysis-0.40.1 codebase, 6.2.1): read by the tools' own preconditions (``facts``: preconditions.Facts without a
    call's), not by counts compared with copies of their limits; and the tools' other rules a step can run into (KDP's
    week, a first listing's demand note), each asked once a keep or a pick (``refusal``)."""

    facts: preconditions.Facts
    asked: bool = False  # a message of the owner's waits for an answer
    seen: dict[tuple[Any, ...], tuple[preconditions.Rule, str] | None] = field(
        default_factory=dict, compare=False, repr=False
    )

    def refusal(self, tool: str, **call: Any) -> tuple[preconditions.Rule, str] | None:
        """What ``tool`` would refuse now by a rule the plan can know, with the rule (preconditions.refusal), for a call
        about ``call`` (preconditions.Facts' fields)."""
        key = (tool, *sorted(call.items()))
        if key not in self.seen:
            self.seen[key] = preconditions.refusal(tool, replace(self.facts, **call))
        return self.seen[key]

    def full(self, channel: str | None) -> bool:
        """Whether a request to publish on ``channel`` (a queue channel, else the blog or the link page) would be
        refused now: as many publish requests as may wait for the owner wait, or as many of the channel's pins or
        posts as may wait for the owner or their slot."""
        return self.refusal(PUBLISHES.get(channel or "", "propose_link_page"), channel=channel) is not None

    @property
    def sales_full(self) -> bool:
        """0.38.1: whether a sell request (a listing, a change of one, a Printify product, a KDP book) would be refused
        now: as many as may wait for the owner wait."""
        return preconditions.SELL.check(self.facts) is not None

    def text(self) -> str:
        """The limits in words, for the diagnostics."""
        conn, scope = self.facts.conn, self.facts.scope
        queued = ", ".join(
            f"{publish_queue.THINGS[c][1]} {len(publish_queue.waiting(conn, scope, c))} of {publish_queue.WAITING}"
            for c in publish_queue.CHANNELS
        )
        return (
            f"publish requests waiting for the owner {store.pending_of_type(conn, scope, 'publish')} of"
            f" {store.PENDING_CAPS['publish']}; waiting for the owner or their slot: {queued}; sell requests waiting"
            f" for the owner {store.pending_of_type(conn, scope, 'sell')} of {store.PENDING_CAPS['sell']}; messages"
            f" today that answer none of the owner's {store.unasked_since(conn, scope, self.facts.day_start)} of"
            f" {store.MESSAGES_PER_DAY}"
            + ("; a message of the owner's waits for an answer" if self.asked else "")
            + (
                f"; all {store.MAX_UNREAD_MESSAGES} of Ember's last messages are unread"
                if preconditions.UNREAD.check(self.facts)
                else ""
            )
        )


def limits(conn: sqlite3.Connection, scope: AgentScope, day_start: str, now: str | None = None) -> OwnerLimits:
    """The owner's limits (OwnerLimits) now; ``day_start``: when the owner's day began (their clock's day_start), from
    which message_owner counts the day's messages. 0.42.0: ``now``, for KDP's week and a demand note's age (the day's
    start without it: the counts alone, as the diagnostics show them)."""
    facts = preconditions.Facts(conn, scope, from_iso(now or day_start), day_start)
    return OwnerLimits(facts, obligations.messages_waiting(conn, scope) > 0)


def _limited(facts: Facts, row: Mapping[str, Any], found: OwnerLimits) -> str | None:
    """Which of the tools' preconditions a step waits on (preconditions.Rule.wait), None for none: 'queue' when its
    work is a request to publish (a pin, a post, a blog post, a promise of them) and its channel takes no more requests
    (0.39.6: or each of its line's live listings has one waiting for its slot); 0.38.1 'sales' when its work is a sell
    request and as many as may wait for the owner wait; 'messages' when its work is a report to the owner and today's
    messages that answer none of theirs are sent, unless a report of it would go through all the same
    (``_let_through``); 0.40.3 'unread' when its work is a report and the owner hasn't read as many of Ember's
    messages as may be unread (no message goes through). 0.42.0 (analysis-0.40.1 codebase, 6.2.1): the rule the tool
    itself refuses by (``_refused``), and 'blocked' for one that is no limit of the owner's: a book in a week KDP
    holds full, a first listing without a demand note of the last 14 days. Ready again once the owner decides a
    request or one goes out, the next day (KDP's next week), when the owner writes, or (0.40.3) reads, or once a note
    is written."""
    refused = _refused(facts, row, found)
    return refused[0].wait if refused is not None else None


def _words(facts: Facts, row: Mapping[str, Any], found: OwnerLimits, reason: str | None) -> str | None:
    """0.42.0: the words of the refusal a step waits on (Candidate.refused), None while it waits on none."""
    refused = _refused(facts, row, found) if reason in REFUSED else None
    return refused[1] if refused is not None else None


def _refused(facts: Facts, row: Mapping[str, Any], found: OwnerLimits) -> tuple[preconditions.Rule, str] | None:
    """0.42.0: what the tool a step's work needs would refuse now by a rule the plan can know (preconditions.TOOLS),
    with the rule; None while one way to do it goes through (``_calls``)."""
    ways = [found.refusal(tool, **call) for tool, call in _calls(facts, row, found)]
    return ways[0] if ways and all(ways) else None


def _calls(facts: Facts, row: Mapping[str, Any], found: OwnerLimits) -> list[tuple[str, dict[str, Any]]]:
    """0.42.0 (analysis-0.40.1 codebase, 6.2.1): the tool calls that would do a step's work, each with what it is about
    (preconditions.Facts): message_owner for a report (``_reports``: one the day's limit lets through while a message
    of the owner's waits for the answer that can carry it, or ``_let_through``); for a promise of pins, a post or a
    blog post its channel's tool; else the tool of each request its check counts (CALLED; none for a step that is the
    owner's, makes files or writes a note): a pin or a post of any of its line's live listings, a book of its line's
    format (``_book_format``), a listing of its line; 0.42.1, a change of any of its line's live listings (the
    critic's fix too: preconditions.PAID leaves one in the owner's paid test as it is)."""
    if row["kind"] == templates.OWNER_KIND:
        return []
    if _reports(row):
        return [("message_owner", {"answers": found.asked or _let_through(facts, row)})]
    if row["kind"] == "promise":
        tools = [PUBLISHES[row["channel"]]] if row["channel"] in PUBLISHES else []
        if not tools and KDP_PROMISE.search(str(row["title"])) and PROPOSES.search(str(row["title"])):
            tools = ["propose_kdp_book"]  # 0.42.6: it waits while KDP's week holds its book's format
    else:
        tools = [CALLED[e] for e in _executors(row["check_kind"], _spec(row)) if e in CALLED]
    line = int(row["project_id"]) if row["project_id"] is not None else None
    if line is None and tools == ["propose_kdp_book"]:
        line = _promised(facts, row)
    calls: list[tuple[str, dict[str, Any]]] = []
    for tool in tools:
        channel = next((c for c, t in PUBLISHES.items() if t == tool), None)
        if channel in publish_queue.CHANNELS:
            funnel = facts.funnel(line) if line is not None else None
            links = [(f"listing {n}",) for n in funnel.listings] if funnel is not None and funnel.listings else [()]
            calls += [(tool, {"channel": channel, "links": these}) for these in links]
        elif tool == "propose_kdp_book":
            calls.append((tool, {"line": line, "form": _book_format(facts, line) if line is not None else None}))
        elif tool in EDITS:
            funnel = facts.funnel(line) if line is not None else None
            listings = funnel.listings if funnel is not None and funnel.listings else [None]
            calls += [(tool, {"line": line, "listing": n}) for n in listings]
        else:
            calls.append((tool, {"line": line}))
    return calls


def _promised(facts: Facts, row: Mapping[str, Any]) -> int | None:
    """0.42.6: the line a promise's step of no line is about: its obligation's, else the one its words name
    (obligations.promised_line). Live, promise #67's step stood under "Your owner" while its obligation named line
    #14, the KDP line whose book format KDP's week counts."""
    if row["obligation_id"] is not None:
        found = facts.conn.execute(
            "SELECT project_id FROM obligations WHERE id = ?", (row["obligation_id"],)
        ).fetchone()
        if found is not None and found["project_id"] is not None:
            return int(found["project_id"])
    return obligations.promised_line(facts.conn, facts.scope, str(row["title"]))


def _book_format(facts: Facts, line: int) -> str | None:
    """0.42.0: the format of a KDP line's book, as Ember's records say it (KDP counts its week by format): its newest
    passing package check's ("KDP would take this paperback"), else its newest request's; None: not known."""
    checked = facts.conn.execute(
        "SELECT t.result FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.project_id = ? AND c.session = ?"
        " AND c.simulated = ? AND t.tool = 'propose_kdp_book' AND t.status = 'ok'"
        " AND json_extract(t.input, '$.check') = 1 ORDER BY t.id DESC LIMIT 1",
        (line, facts.scope.session, 1 if facts.scope.simulated else 0),
    ).fetchone()
    found = _CHECKED_BOOK.search(str(checked["result"] or "")) if checked is not None else None
    if found:
        return found[1]
    where, params = facts.scope.where("a")
    asked = facts.conn.execute(
        "SELECT a.action FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
        f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = 'kdp_package'"
        " ORDER BY a.id DESC LIMIT 1",
        (*params, line),
    ).fetchone()
    form = json.loads(asked["action"] or "{}").get("format") if asked is not None else None
    return str(form) if form else None


def _reports(row: Mapping[str, Any]) -> bool:
    """Whether a step's work is a report to the owner, told in a message: a promise to report (its words, REPORT, as
    promise_channel reads them) or a step of Ember's of the kind report."""
    if row["kind"] == "promise":
        return bool(REPORT.search(str(row["title"])))
    return row["kind"] == "report"


def _let_through(facts: Facts, row: Mapping[str, Any]) -> bool:
    """Whether a message reporting on a promise goes through the day's limit: one naming it, while it is open and Ember
    hasn't written to the owner since the message that made it (0.42.0: preconditions.untold, as message_owner reads
    it)."""
    if row["kind"] != "promise" or row["obligation_id"] is None:
        return False
    return preconditions.untold(facts.conn, facts.scope, int(row["obligation_id"]))


def _fields(step: weights.Step) -> dict[str, Any]:
    return {name: getattr(step, name) for name in weights.Step.__dataclass_fields__}


def _kind(row: Mapping[str, Any]) -> str:
    """A step's kind as weights.KIND weighs it: one it doesn't know (a promise) as shipping, and (0.37.9) a promise to
    report as a report (``_reports``), told in a message: it weighed as much as a promise to ship (analysis-0.37.8, 6.3
    2: 7 of the first 18 promises were reports, each worth 3, more than any Etsy product's 2)."""
    if _reports(row):
        return "report"
    kind = str(row["kind"])
    return kind if kind in weights.KIND else "ship"


def _promise_urgency(row: Mapping[str, Any], today: date) -> float:
    if not row["due"]:
        return weights.PROMISE_FLOOR
    left = (date.fromisoformat(row["due"]) - today).days
    return weights.promise_urgency(max(left, 0) + 0.5, slips=1 if left < 0 else 0)


def _owed_urgency(row: Mapping[str, Any], today: date, lately: Mapping[int, int]) -> float:
    """0.37.0: a promise's or an owner's decision's urgency, as its day nears (weights.promise_urgency), until cycles
    took it PROMISE_TRIES times (a decision once) in OBLIGATION_HOURS without closing it: then none until those hours
    have passed, its worth alone (0.35.0 to 0.36.0 that many times it came first; 0.37.0 kept the floor, which still
    outweighed every product step, so a promise Ember couldn't keep yet took every cycle)."""
    tries = lately.get(int(row["id"]), 0)
    fresh = tries < PROMISE_TRIES if row["kind"] == "promise" else tries == 0
    return _promise_urgency(row, today) if fresh else 0.0


PASSING = ("mode", "turn")  # what a venture's step waits on for one cycle only (0.36.0: the Explore step's): it ages


def _clocks(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    day_start: str | None = None,
) -> None:
    """A step's age runs from when it became ready; a step that waits starts again from nought once it is ready."""
    for candidate in candidates(conn, scope, now, today, channels, exploring=True, day_start=day_start):
        row = node(conn, scope, candidate.step.id)
        if row is None or candidate.waiting in PASSING:
            continue
        if candidate.waiting is None and row["ready_since"] is None:
            _update(conn, row["id"], now, ready_since=now)
        elif candidate.waiting is not None and row["ready_since"] is not None:
            _update(conn, row["id"], now, ready_since=None)
