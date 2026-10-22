"""0.42.2 (analysis-0.40.1, 5.6): the tools of Ember's projects, plan, ventures and milestones, split out of tools.py.

project_create, project_list and project_update (a product line, and the listings that count for it), plan_step
(Ember's change to the plan tree: plan.py), venture_create and venture_update, evidence and venture_case (a venture's
case and its numbers) and milestone_update (``update_milestone``: the daily review's verdicts too), with the lookups
they share: an open venture, an open milestone, the day one is due.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .. import events
from ..economy.clock import to_iso
from . import (
    bets,
    econ,
    evidence,
    knockouts,
    learning,
    metrics,
    netguard,
    plan,
    policy,
    predictions,
    roadmap,
    stages,
    store,
    ventures,
)
from .memory import HEADING_REFUSAL, heading_line
from .sandbox import QuotaError, SandboxError
from .store import OPEN_STATUSES, AgentScope
from .tools_checks import validate
from .tools_context import Outcome, ToolContext, ToolError, _cut, _numbers, _unstop
from .tools_limits import _PLAIN_NUMBER, CASE_LIMITS, DONE_WORDS
from .tools_lines import _held, _line, _line_venture, _open_project, _stopped
from .tools_specs import PROJECT_STATUSES, SPECS


def _no_heading(args: dict[str, Any], *names: str) -> None:
    """0.12.0: a project's texts, in every plan, never hold a line that begins like a heading of the context."""
    for name in names:
        if heading_line(args.get(name) or ""):
            raise ToolError(HEADING_REFUSAL.format(name=name))


def _project_create(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    _no_heading(args, "title", "hypothesis")
    line = ctx.state.focus_project_id
    if ctx.state.one_line and line is not None:  # 0.28.0: one line a cycle
        raise ToolError(
            f"this cycle works on product line #{line}: a new product line starts in a cycle of its own (Ember's code "
            "gives you one when no step of your plan is ready)"
        )
    venture_id = args.get("venture_id")
    held = plan.new_things_held(conn, ctx.scope)
    # 0.36.0: the owner's "nothing new", kept by Ember's code; 0.39.3 (analysis-0.39.1, 4.4): whatever venture the new
    # line names. One that named a venture went through, and an ordinary cycle can make an idea (venture_create).
    if held:
        raise ToolError(f"your owner holds new things ({held}): no new product until they resume them")
    open_ = store.open_projects(conn, ctx.scope)  # 0.19.1: as many as the agent needs (it was at most 8)
    if any(p["title"].strip().lower() == args["title"].strip().lower() for p in open_):
        raise ToolError("an open project already has this title")
    if venture_id is not None:
        _working_venture(conn, ctx.scope, venture_id)
    project_id = store.create_project(
        conn,
        ctx.scope,
        cycle_id=ctx.cycle_id,
        title=args["title"].strip(),
        hypothesis=args["hypothesis"].strip(),
        next_step="",  # 0.35.0: its steps are the plan tree's (plan.py lays it out)
        status=args["status"],
        now=ctx.now(),
        venture_id=venture_id,
    )
    if ctx.state.one_line:
        ctx.state.touched = project_id  # 0.28.0: the cycle's line once the call succeeds (_lock), aimed at it too
    elif ctx.state.focus_project_id is None:
        ctx.state.focus_project_id = project_id
    cycle = conn.execute("SELECT project_id FROM cycles WHERE id = ?", (ctx.cycle_id,)).fetchone()
    if cycle is not None and cycle["project_id"] is None:
        # A cycle's cost counts toward its project: without a focus from the plan, that is the one it started.
        store.update_cycle(conn, ctx.cycle_id, project_id=project_id)
    alike = _alike(conn, ctx, f"{args['title']} {args.get('hypothesis') or ''}")
    return Outcome(
        True,
        f"Created project #{project_id}: Ember's code lays it out in your plan with its stages and steps.{alike}",
        f"created #{project_id} {args['title'][:60]}",
        project_id,
    )


def _alike(conn: Any, ctx: ToolContext, text: str) -> str:
    """0.18.0: the cases most like a new project or venture, for the answer that creates it ("" without any)."""
    found = learning.similar(conn, ctx.scope, text)
    return (" You tried something like this before: " + " | ".join(found)) if found else ""


def _project_list(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.19.1 (Ember's upgrade request): its open projects with their numbers, so it can close the stale ones itself
    (the plan shows the ones updated last in full; a work step's brief only its focus)."""
    projects = store.open_projects(conn, ctx.scope)
    if not projects:
        return Outcome(True, "No open projects.", "0 open projects")
    # 0.23.1: the ones waiting while the owner parks their venture last (store.open_projects)
    lines = [f"{len(projects)} open projects, the one changed last first, the ones your owner's park stopped last:"]
    for p in projects:
        venture = f" · venture #{p['venture_id']}" if p["venture_id"] else ""
        title = _cut(" ".join(str(p["title"]).split()), 80)
        changed = str(p["updated_at"])[:10]
        stage = plan.current_stage_name(conn, ctx.scope, int(p["id"]))  # 0.35.0: its steps are the plan's
        lines.append(f"#{p['id']} [{p['status']}] {title}{venture} · changed {changed} · plan: {stage or '-'}")
    return Outcome(True, "\n".join(lines), f"{len(projects)} open projects")


def _project_update(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    _no_heading(args, "hypothesis", "note")
    row = store.project(conn, ctx.scope, args["project_id"])
    if row is None:
        raise ToolError(f"there is no project #{args['project_id']}")
    if row["status"] not in OPEN_STATUSES:
        raise ToolError(f"project #{row['id']} is {row['status']}, which is final")
    changes: dict[str, Any] = {}
    status = args.get("status")
    if status is not None and status not in PROJECT_STATUSES:  # 0.35.0: past the schema too (a call run from a text)
        raise ToolError(f"only your owner closes or drops a project: project #{row['id']} stays {row['status']}")
    # 0.33.0: keeping another line's record (a note, its hypothesis, waiting) is no work on it: live, a Pinterest
    # project's next step said "waiting on owner" for a day after its owner set it up, and the daily review read it as
    # "never set up". Reopening it, a bet on it or moving it to a venture is its own cycle's work, and a cycle without a
    # line yet takes the one it touches first. 0.35.0: only the owner closes a project (its next step is the plan's).
    work = status in ("idea", "active") or bool(args.get("bet")) or args.get("venture_id") is not None
    work = work or args.get("listing_id") is not None  # 0.39.8: a listing moved to it is the line's work
    # 0.39.3: the record of a line the owner holds is kept like another line's: no cycle takes the line for it
    # (0.42.1: or a review's stop holds, _held)
    keeps = not work and _held(conn, ctx.scope, int(row["id"]), "update") is not None
    if work or (ctx.state.focus_project_id is None and not keeps):
        _line(ctx, conn, int(row["id"]), "update")
    if status in ("idea", "active") and status != row["status"]:
        held = ventures.owner_stopped(conn, ctx.scope, row["venture_id"])
        if held is not None:
            # 0.22.0 (analysis 0.20.1, FIX NOW 12): the owner's park or kill stops the venture's project work
            what = "killed" if held["stage"] == "killed" else "parked"
            raise ToolError(
                f"your owner {what} venture #{held['id']}: its projects wait"
                + (" until they take it up again" if what == "parked" else "; close this one")
            )
    if status and status != row["status"]:
        changes["status"] = status
    if args.get("hypothesis"):
        changes["hypothesis"] = args["hypothesis"].strip()
    if args.get("note"):
        stamp = f"[#c{ctx.cycle_id}] {args['note'].strip()}"
        notes = (row["notes"] + "\n" + stamp).strip()
        changes["notes"] = notes[-2000:]
    venture_id = args.get("venture_id")
    if venture_id is not None and venture_id != row["venture_id"]:
        held = ventures.owner_stopped(conn, ctx.scope, row["venture_id"])
        if held is not None:
            # 0.23.1: moved out, then set active, a project of the venture worked on as before the owner's park
            what = "killed" if held["stage"] == "killed" else "parked"
            raise ToolError(
                f"your owner {what} venture #{held['id']}: its projects stay with it"
                + (" and wait until they take it up again" if what == "parked" else "; close this one")
                + ". For another venture, open a project of its own"
            )
        _working_venture(conn, ctx.scope, venture_id)
        tied = _tied(conn, ctx.scope, row["id"])
        if tied:  # 0.22.0 (analysis 0.20.1, FIX NOW 11): what Ember's code counted for it stays the venture's
            where = f"venture #{row['venture_id']}" if row["venture_id"] is not None else "its channel's venture"
            raise ToolError(
                f"project #{row['id']} has {tied}: what Ember's code counts, grades and settles for it belongs to "
                f"{where}, so it stays there. For another venture, open a project of its own"
            )
        for vid in (row["venture_id"], venture_id):
            held = policy.venture_unlock(conn, ctx.scope, vid)
            if held is not None:  # 0.21.0: it covers the venture's projects, so none moves in or out
                raise ToolError(
                    f"your owner's unlock for milestone #{held} covers the projects of venture #{vid}: no project "
                    "moves into or out of it while that stands"
                )
        changes["venture_id"] = venture_id
    moved = ""
    if args.get("listing_id") is not None:  # 0.39.8: before the bet, which then counts the listing's views
        moved = _move_listing(ctx, conn, row, int(args["listing_id"]), str(args.get("note") or ""))
    placed = ""
    if args.get("bet"):  # 0.18.0: settled by Ember's code (bets.py)
        try:
            refused = _stopped(conn, ctx.scope, row["id"])  # 0.23.3: no bet on the work the owner's park stopped
            if refused:
                raise bets.BetError(refused)
            placed = bets.place(conn, ctx.scope, row["id"], args["bet"], ctx.cycle_id, ctx.clock.today(), ctx.now())
        except bets.BetError as exc:
            # 0.24.0: the rest of the update is made, and the answer says the bet wasn't. 0.19.2 refused it all ("so
            # send the update again"), and live the agent sent the bet again without its note, which was lost.
            if not changes:
                raise ToolError(f"bet: {exc}") from None
            placed = f"Your bet was not placed: {exc}."
    if not changes and not placed and not moved:
        raise ToolError("nothing to change")
    if changes:
        store.update_project(conn, row["id"], ctx.now(), **changes)
    transition = f"{row['status']} → {changes['status']}" if "status" in changes else "updated"
    if "venture_id" in changes:
        transition += f", part of venture #{venture_id}"
    text = f"Project #{row['id']}: {transition}." + "".join(f" {t}" for t in (moved, placed) if t)
    text += _left_behind(ctx, conn, row)
    return Outcome(True, text, f"#{row['id']} {transition}", row["id"])


def _move_listing(ctx: ToolContext, conn: Any, row: Any, listing_id: int, why: str) -> str:
    """0.39.8 (analysis-0.39.6, 3.3): one of Ember's listings counted for the line ``row`` from now on: its views, its
    pins and posts and the steps that count them, its orders' revenue (ventures.listing_line reads listing_lines).
    A listing belongs to the line of the request that made it, and nothing changed that: live, the coach licence
    bundle was made by a request of line #4, so line #12 (whose only listing it is) had none, and each Bluesky post
    for it in a line-#12 cycle was refused. Not out of or into a line the owner holds or whose venture they stopped."""
    found = ventures.listing_line(conn, ctx.scope, listing_id)
    if found is None:
        raise ToolError(f"listing #{listing_id} isn't one of yours (etsy_listing shows them)")
    channel, line = found
    if line == row["id"]:
        raise ToolError(f"listing #{listing_id} counts for line #{row['id']} already")
    if not why.strip():
        raise ToolError(f"say in note why listing #{listing_id} is line #{row['id']}'s")
    for project_id in (line, int(row["id"])):
        if project_id is None:
            continue
        held = plan.line_held(conn, ctx.scope, project_id)
        if held is not None:
            raise ToolError(f"your owner holds line #{project_id} ({_cut(held, 80)}): its listings stay as they are")
        stopped = ventures.project_stopped(conn, ctx.scope, project_id, channel)
        if stopped is not None:
            raise ToolError(
                f"your owner stopped venture #{stopped['id']}: line #{project_id}'s listings stay as they are"
            )
    conn.execute(
        "INSERT INTO listing_lines (mode, session, listing_id, project_id, moved_from, cycle_id, created_at, why)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ctx.scope.mode, ctx.scope.session, listing_id, row["id"], line, ctx.cycle_id, ctx.now(), why.strip()[:300]),
    )
    was = f"line #{line}" if line is not None else "no line"
    events.record(
        ctx.db,
        "info",
        "agent",
        f"Ember moved listing #{listing_id} from {was} to line #{row['id']}: {_cut(why.strip(), 200)}",
        {"listing_id": listing_id, "project_id": row["id"], "moved_from": line},
    )
    return (
        f"Listing #{listing_id} counts for line #{row['id']} from now on (it counted for {was}): its views, and the "
        "pins and posts that link it."
    )


def _left_behind(ctx: ToolContext, conn: Any, before: Any) -> str:
    """0.19.2: what a project leaving a backed venture (moved to another, or closed) leaves behind: the venture's first
    test with no open project (live, a website test's project became a Bluesky channel's, and READY asked again for a
    project to set up the website's test)."""
    old = before["venture_id"]
    if old is None:
        return ""
    now = store.project(conn, ctx.scope, before["id"])
    if now is not None and now["venture_id"] == old and now["status"] in OPEN_STATUSES:
        return ""
    venture = ventures.get(conn, ctx.scope, old)
    if venture is None or venture["stage"] not in ("building", "live"):
        return ""
    where, params = ctx.scope.where()
    marks = ", ".join("?" for _ in OPEN_STATUSES)
    others = conn.execute(
        f"SELECT COUNT(*) FROM projects WHERE {where} AND venture_id = ? AND status IN ({marks})",
        (*params, old, *OPEN_STATUSES),
    ).fetchone()[0]
    if others:
        return ""
    return f" Venture #{old} ({venture['title']}) has no open project now: its test needs one."


def _plan_step(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.35.0: Ember's change to her plan (plan.py), on the cycle's product line (resume names the line on hold: a
    held line never has a cycle of its own); 0.39.1: a venture cycle's, a step of its ventures waiting. Each change is
    kept with its reason, which the owner sees."""
    _no_heading(args, "why")
    steps = args.get("steps") or []
    for step in steps:
        if heading_line(step["title"]):
            raise ToolError(HEADING_REFUSAL.format(name="steps"))
    action, why = args["action"], " ".join(args["why"].split())
    now = ctx.now()
    # 0.39.1 (analysis-0.38.0, 3.6): a step to say done, wait, split or replace is YOUR STEP unless one is named. Live,
    # four calls were refused for naming none (or the step in ref), each meaning the cycle's own.
    step_id = args.get("step_id")
    if step_id is None and action in ("split", "replace", "done", "wait", "skip", "decline"):
        step_id = ctx.state.step_id
    if action == "resume":
        line = args.get("project_id")
        if line is None:
            raise ToolError("resume names the product line on hold (project_id)")
    else:
        # 0.39.1: a venture cycle makes a step of its ventures wait (a day, say; its variant has no other action):
        # there was no way to put one off
        line = None if ctx.venture else ctx.state.focus_project_id
        # 0.40.2 (analysis-0.40.1, 3.4): a promise of no product line can wait on a date, in the cycle that has it
        loose = action == "wait" and plan.owner_promise(conn, ctx.scope, step_id)
        if line is None and not ctx.venture and not loose:
            raise ToolError("this cycle works on no product line yet: plan_step changes the plan of your step's line")
        if action in ("add", "split", "replace") and not steps:
            raise ToolError(f"{action} needs the new steps (steps)")
    try:
        if action == "add":
            text = plan.add_steps(
                conn,
                ctx.scope,
                line,
                steps,
                why,
                ctx.cycle_id,
                now,
                stage=args.get("stage"),
                before=args.get("step_id"),
            )
        elif action == "split":
            text = plan.split_step(conn, ctx.scope, line, step_id, steps, why, ctx.cycle_id, now)
        elif action == "replace":
            text = plan.replace_step(conn, ctx.scope, line, step_id, steps, why, ctx.cycle_id, now)
        elif action == "done":
            # 0.39.2 (analysis-0.39.1, 3.4): live, a step was closed with the reason "placeholder", its pins unsent;
            # the owner reads each reason on their Plan tab
            if len(why.split()) < DONE_WORDS:
                raise ToolError("a done says in why what was done, in a few words: one word says nothing")
            text = plan.step_done(conn, ctx.scope, line, step_id, why, ctx.cycle_id, now)
        elif action == "wait":
            text = plan.wait_step(
                conn,
                ctx.scope,
                line,
                step_id,
                args.get("on"),
                args.get("ref"),
                args.get("until"),
                why,
                ctx.cycle_id,
                now,
                ctx.clock.today(),
                day_start=to_iso(ctx.clock.day_start(ctx.clock.today())),  # 0.40.3: the owner's day's waits
            )
        elif action == "hold":
            text = plan.hold(conn, ctx.scope, line, why, ctx.cycle_id, now)
        elif action == "skip":  # 0.39.8: a recurring pin, post or blog post, for its period
            text = plan.skip_step(conn, ctx.scope, line, step_id, why, ctx.cycle_id, now)
        elif action == "decline":  # 0.42.1: a rework of a request the owner rejected, with why they see
            text = plan.decline_step(conn, ctx.scope, line, step_id, why, ctx.cycle_id, now)
            events.record(
                ctx.db, "info", "agent", f"Ember declined the rework of plan step #{step_id} (line #{line}): {why}"
            )
        else:
            text = plan.resume(conn, ctx.scope, line, why, ctx.cycle_id, now)
    except plan.PlanError as exc:
        raise ToolError(str(exc)) from None
    return Outcome(True, text, f"{action}: {_cut(text, 80)}", line)


def project_net(conn: Any, scope: AgentScope, project_id: int) -> tuple[int, int]:
    """(what a project earned, what its API calls cost) in micros (0.12.0). Earned: the revenue the owner recorded for
    it less its expenses, corrections included (Etsy's fees are expenses); cost: every call of the cycles that worked
    on it. The live scope counts real money only."""
    simulated = "" if scope.simulated else " AND simulated = 0"
    earned = conn.execute(
        "SELECT COALESCE(SUM(CASE type WHEN 'revenue' THEN amount_micros ELSE -amount_micros END), 0) FROM ledger"
        f" WHERE type IN ('revenue', 'expense') AND project_id = ?{simulated}",
        (project_id,),
    ).fetchone()[0]
    cost = conn.execute(
        "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
        " WHERE y.project_id = ? AND c.status IN ('ok', 'interrupted') AND y.simulated = ?",
        (project_id, 1 if scope.simulated else 0),
    ).fetchone()[0]
    return int(earned), int(cost)


def _working_venture(conn: Any, scope: AgentScope, venture_id: int, what: str = "no project goes into it") -> Any:
    """0.22.0 (analysis 0.20.1, FIX NOW 12): a venture a project may join: open (not killed) and not parked. A project
    could be opened under a venture the owner parked or killed, and work went on. 0.23.2: a milestone's link too
    (``what``)."""
    row = _open_venture(conn, scope, venture_id)
    if row["stage"] == "parked":
        who, then = {
            "owner": ("your owner", "it is theirs to take up again"),
            "code": ("Ember's code", "only your owner takes it up again"),
        }.get(row["parked_by"] or "", ("you", "take it up again first (venture_update)"))
        raise ToolError(f"{who} parked venture #{venture_id}: {what} while it is parked; {then}")
    return row


def _linkable(conn: Any, scope: AgentScope, name: str, value: int) -> bool:
    """0.23.2: whether a milestone may be linked to this venture (``name`` venture_id) or project (project_id)."""
    try:
        (_milestone_venture if name == "venture_id" else _open_project)(conn, scope, value)
    except ToolError:
        return False
    return True


def _milestone_venture(conn: Any, scope: AgentScope, venture_id: int) -> Any:
    """0.23.2: a venture a milestone may be linked to: open and not parked (a park dropped its milestones, and a new
    one carried its work on, its unlocks included)."""
    return _working_venture(conn, scope, venture_id, "no milestone is linked to it")


def _tied(conn: Any, scope: AgentScope, project_id: int) -> str:
    """0.22.0 (analysis 0.20.1, FIX NOW 11): what ties a project to its venture ("" when nothing does): its listings,
    the revenue recorded for it, or a bet. Moving such a project closed another venture's "listings_live >= 1" as met,
    settled its forecast as a hit, and let an empty venture escape its park."""
    if metrics.listings(conn, scope, project_id, None):
        return "listings"
    if conn.execute("SELECT 1 FROM ledger WHERE project_id = ? AND type = 'revenue' LIMIT 1", (project_id,)).fetchone():
        return "revenue"
    if conn.execute("SELECT 1 FROM bets WHERE project_id = ? LIMIT 1", (project_id,)).fetchone():
        return "a bet"
    return ""


def _researched_venture(conn: Any, scope: AgentScope, venture_id: int, what: str) -> Any:
    """0.23.3: a venture research, evidence or a business case may be for: open, and not parked by the owner (its
    research budget was spent after the owner's park)."""
    row = _open_venture(conn, scope, venture_id)
    if row["parked_by"] == "owner":
        raise ToolError(f"your owner parked venture #{venture_id}: no {what} for it until they take it up again")
    return row


def _open_venture(conn: Any, scope: AgentScope, venture_id: int) -> Any:
    row = ventures.get(conn, scope, venture_id)
    if row is None:
        raise ToolError(f"there is no venture #{venture_id}")
    if row["stage"] == "killed":
        raise ToolError(f"your owner killed venture #{venture_id}")
    return row


# --- evidence (0.12.0) ---


def _evidence(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    low = _value(args["low"], "low")
    high = low if args.get("high") is None else _value(args["high"], "high")
    if high < low:
        raise ToolError("high must be at least low")
    url = args["url"].strip()
    if not url.startswith(("https://", "http://")) or any(c.isspace() for c in url):
        raise ToolError("url must be a web address from your research results (https://...)")
    texts = {name: " ".join(args[name].split()) for name in ("claim", "metric", "unit", "region")}
    venture_id = args.get("venture_id", ctx.state.focus_venture_id)  # offered in venture cycles only
    if venture_id is None:
        raise ToolError("name the venture it is evidence for (venture_id): this cycle has no focus venture")
    _researched_venture(conn, ctx.scope, venture_id, "evidence")
    number, grade = evidence.add(
        conn, ctx.scope, venture_id, ctx.cycle_id, **texts, low=low, high=high, url=url, now=ctx.now()
    )
    why = {
        "independent": "a page from your research results",
        "marketing": "a vendor's or an affiliate's page: it sells what it describes, so weigh it lightly",
        "unchecked": "not a page from your research results: your word only until research finds it",
    }[grade]
    return Outcome(
        True,
        f"Saved evidence #{number} for venture #{venture_id}. Its source is {grade}: {why}.",
        f"evidence #{number} {grade}",
    )


def _value(text: str, name: str) -> float:
    """A plain number: 1200, 1,200, 4.5 or -3 (not 4,5 or 1.200,50: which comma is the decimal one is a guess)."""
    plain = text.strip()
    if not _PLAIN_NUMBER.fullmatch(plain):
        raise ToolError(f"{name} must be a plain number like 1200 or 4.5")
    return float(plain.replace(",", ""))


def _venture_case(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.13.0: a venture's numbers; Ember's code computes the economics (econ.py) and keeps both."""
    venture_id = args.get("venture_id", ctx.state.focus_venture_id)
    if venture_id is None:
        raise ToolError("name the venture (venture_id): this cycle has no focus venture")
    _researched_venture(conn, ctx.scope, venture_id, "business case")
    amounts = {}
    for name, most in CASE_LIMITS.items():
        amounts[name] = _value(args[name], name)
        if not 0 <= amounts[name] <= most:
            raise ToolError(f"{name} must be between 0 and {most:,}")
    if amounts["price_eur"] <= 0:
        raise ToolError("price_eur must be above 0")
    sales = (args["sales_low"], args["sales_mid"], args["sales_high"])
    if not sales[0] <= sales[1] <= sales[2]:
        raise ToolError("sales must rise: sales_low (P10) <= sales_mid (P50) <= sales_high (P90)")
    case = econ.Case(
        channel=args["channel"],
        price_eur=amounts["price_eur"],
        unit_cost_eur=amounts["unit_cost_eur"],
        monthly_costs_eur=amounts["monthly_costs_eur"],
        sales=sales,
        setup_eur=amounts["setup_eur"],
        owner_hours=amounts["owner_hours"],
        first_sale_days=args["first_sale_days"],
        api_usd=amounts["api_usd"],
    )
    needs = [n.strip() for n in (args.get("needs") or "").split(",") if n.strip()]
    unknown = [n for n in needs if n not in knockouts.NEEDS]
    if unknown:
        raise ToolError(f"needs takes {' and '.join(knockouts.NEEDS)}, not {unknown[0]!r}")
    result = econ.compute(case, ctx.usd_per_eur)
    number = ventures.add_case(conn, venture_id, ctx.cycle_id, case, result, ctx.now(), ",".join(sorted(set(needs))))
    rate = (
        ""
        if ctx.usd_per_eur > 0
        else f" (at an assumed USD {econ.DEFAULT_USD_PER_EUR:.2f} per EUR: your owner set no exchange rate)"
    )
    # 0.24.0: the knock-outs these numbers meet, at once (live, a case's research went on for two more calls, and its
    # proposal was refused for a first sale too late for the runway, which the case had shown from the start)
    row = ventures.get(conn, ctx.scope, venture_id)
    knocked = (
        knockouts.active(
            knockouts.check(
                conn, row, cash_eur=ctx.venture_cash_eur, net_days=ctx.net_runway_days, whole_runway=ctx.whole_runway
            )
        )
        if row is not None and row["stage"] in ventures.EXPLORING
        else []
    )
    out = (
        " Knocked out by Ember's code (it isn't proposed while one stands): "
        + "; ".join(f"{k.label} ({k.why})" for k in knocked)
        + "."
        if knocked
        else ""
    )
    return Outcome(
        True,
        f"Saved the numbers of venture #{venture_id} as case #{number}{rate}. {result.text(case)}{out}",
        f"case #{number} for venture #{venture_id}: EUR {result.ev_eur:.0f} a month expected",
    )


# --- ventures (0.10.0) ---


def _venture_create(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    title = " ".join(args["title"].split())
    if not title:
        raise ToolError("the title is empty")
    held = plan.new_things_held(conn, ctx.scope)
    if held:  # 0.39.3 (analysis-0.39.1, 4.4): the owner's "nothing new" holds new ideas too
        raise ToolError(
            f"your owner holds new things ({held}): no new venture until they resume them. Tell them the idea in your "
            "answer if it can't wait: they can add it themselves"
        )
    if ventures.count(conn, ctx.scope) >= ventures.MAX_VENTURES:
        raise ToolError(f"your tree holds {ventures.MAX_VENTURES} ventures, as many as it can")
    stage = args["stage"]
    if stage in ventures.EXPLORED and ventures.count(conn, ctx.scope, ventures.EXPLORED) >= ventures.MAX_ACTIVE:
        raise ToolError(f"{ventures.MAX_ACTIVE} ventures are being researched already: add it as an idea, or park one")
    same = ventures.by_title(conn, ctx.scope, title)
    if same is not None:
        raise ToolError(f"venture #{same['id']} ({same['stage']}) already has this title: update it instead")
    parent_id = args.get("parent_id")
    if parent_id is not None and ventures.get(conn, ctx.scope, parent_id) is None:
        raise ToolError(f"there is no venture #{parent_id} to branch from")
    venture_id = ventures.create(
        conn,
        ctx.scope,
        title=title,
        pitch=args["pitch"].strip(),
        stage=stage,
        now=ctx.now(),
        cycle_id=ctx.cycle_id,
        next_question=(args.get("next_question") or "").strip(),
        parent_id=parent_id,
    )
    file = ventures.file_of(venture_id, title)
    branch = f", a branch of #{parent_id}" if parent_id is not None else ""
    return Outcome(
        True,
        f"Venture #{venture_id} is in your tree ({stage}{branch}). Score it and save what you learn with "
        f"venture_update: your findings go to {file}." + _alike(conn, ctx, f"{title} {args['pitch']}"),
        f"venture #{venture_id} {title[:60]}",
    )


# 0.33.0: what any cycle may keep of a venture another line's cycle found out about (venture_update's fields)
KEPT_BY_ANY_CYCLE = frozenset({"venture_id", "learned", "note", "next_question", "stage"})


def _kept_by_any_cycle(args: dict[str, Any], row: Any) -> bool:
    """0.33.0: an update of a venture that is no venture cycle's work: what was learned, a note, its next question, or
    parking an idea not backed yet (live, the cycle after the venture cycle that showed venture #23 couldn't work was
    refused its park, and the venture stayed proposed)."""
    given = {name for name, value in args.items() if value not in (None, "")}
    stage = args.get("stage")
    parks = stage == "parked" and row["stage"] in ventures.EXPLORING
    return given <= KEPT_BY_ANY_CYCLE and (not stage or parks)


def _venture_update(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    row = _open_venture(conn, ctx.scope, args["venture_id"])
    if not _kept_by_any_cycle(args, row):  # 0.33.0
        _line_venture(ctx, conn, int(row["id"]), "update")  # 0.28.0
    vid, current = row["id"], row["stage"]
    changes: dict[str, Any] = {}
    for name in ("pitch", "next_question", *ventures.CASE_FIELDS):
        value = (args.get(name) or "").strip()
        if value and value != row[name]:
            changes[name] = value
    # 0.32.0: what is refused (scores without research, a stage the venture can't take yet) is said, and the rest of
    # the update is made: live, a refused proposal threw its whole case away, and the agent sent it again and again
    # (ten refusals in two venture cycles) until the conversation got too long (project_update's bet: 0.24.0).
    refused: list[str] = []
    scores = {name: args[name] for name in ventures.SCORE_FIELDS if args.get(name) is not None}
    if scores and ventures.researched(row) < ventures.RESEARCH_TO_SCORE:  # 0.12.0: labelled research without any
        refused.append(
            f"scores come from research: research venture #{vid} first (research with venture_id {vid}; it counts "
            "once it finds something), then score it"
        )
        scores = {}
    if scores:
        changes.update(scores)
        changes["scores_by"] = "research"
    stage = args.get("stage")
    if stage and stage != current:
        try:
            changes.update(_new_stage(ctx, conn, row, stage, changes, args))
        except ToolError as exc:
            refused.append(str(exc))
    if args.get("note"):
        changes["notes"] = ventures.add_note(row["notes"], ctx.cycle_id, args["note"])
    learned = (args.get("learned") or "").strip()
    if not changes and not learned:
        raise ToolError("; ".join(refused) if refused else "nothing to change")
    if changes:
        ventures.update(conn, vid, ctx.now(), **changes)
    dropped = []
    if changes.get("stage") == "parked":  # 0.12.0: its milestones go with it (your owner's stay theirs)
        dropped = stages.drop_milestones(conn, ctx.scope, vid, ctx.now(), f"Venture #{vid} was parked.", "agent")
    saved = _save_learned(ctx, row, learned) if learned else ""
    transition = f"{current} → {changes['stage']}" if "stage" in changes else "updated"
    if dropped:
        transition += f" (its milestones {_numbers(dropped)} dropped with it)"
    after = " Your owner sees its business case on the Plan tab." if changes.get("stage") == "proposed" else ""
    if scores:
        after += f" Now {ventures.scores_text({**dict(row), **changes})}."
    after += "".join(f" Not done (the rest is saved): {why}." for why in refused)
    part = " (not all of it)" if refused else ""
    return Outcome(True, f"Venture #{vid}: {transition}.{saved}{after}", f"venture #{vid} {transition}{part}")


def _new_stage(
    ctx: ToolContext, conn: Any, row: Any, stage: str, changes: dict[str, Any], args: dict[str, Any]
) -> dict[str, Any]:
    """The changes that move a venture to ``stage`` (``changes``: the rest of its update), or ToolError with why it
    can't move there yet."""
    vid, current = row["id"], row["stage"]
    busy = ventures.count(conn, ctx.scope, ventures.EXPLORED)  # 0.19.3: backed ones take no room
    if stage in ventures.EXPLORED and current not in ventures.EXPLORED and busy >= ventures.MAX_ACTIVE:
        raise ToolError(f"{ventures.MAX_ACTIVE} ventures are being researched already: park or propose one first")
    if stage == "live" and current != "building":
        raise ToolError("a venture goes live once your owner backed it (building) and it launched")
    if stage == "live" and not _tested(conn, ctx.scope, row):  # 0.12.0: the stage's rule, kept by the database too
        test = f"milestone #{row['test_milestone_id']}" if row["test_milestone_id"] else "a milestone"
        raise ToolError(
            f"venture #{vid} goes live once its first test ({test} on your roadmap) is met, as Ember's code checks "
            "it or your owner confirms it"
        )
    if current in ("building", "live") and stage != "parked" and stage != "live":
        raise ToolError(f"venture #{vid} is {current}: your owner backed it; park it with a note if it should stop")
    if current == "parked" and row["parked_by"] in ("owner", "code"):  # 0.12.0: the owner's (and code's) park
        who = (
            f"your owner parked venture #{vid}: only they take it up again"
            if row["parked_by"] == "owner"
            else f"Ember's code parked venture #{vid} by its stage's rule: only your owner takes it up again"
        )
        raise ToolError(
            f"{who} (Research next, on the Plan tab). If you found something that changes the picture, tell "
            "them with message_owner"
        )
    if stage == "parked" and not (args.get("note") or "").strip():
        raise ToolError("say why in note when you park a venture")
    moved: dict[str, Any] = {"parked_by": "agent" if stage == "parked" else None}
    if stage == "proposed":
        # 0.24.0: an idea researched enough is proposed at once (the room researching takes is checked above):
        # live, its whole case was refused for "the researching stage first", and sent again for it
        values = {**dict(row), **changes}
        gaps = ventures.proposal_gaps(values, "researching" if current == "idea" else current)
        knocked = knockouts.active(
            knockouts.check(
                conn, values, cash_eur=ctx.venture_cash_eur, net_days=ctx.net_runway_days, whole_runway=ctx.whole_runway
            )
        )
        out = "; ".join(f"{k.label} ({k.why})" for k in knocked)
        if gaps:  # 0.32.0: with the knock-outs that stand too (live, the agent met one only once its gaps were filled)
            raise ToolError(
                f"venture #{vid} can't be proposed yet: a business case needs {'; '.join(gaps)}"
                + (f". Ember's code knocks it out too: {out}" if knocked else "")
            )
        if knocked:  # 0.13.0
            raise ToolError(
                f"venture #{vid} is knocked out by Ember's code: {out}. Fix what can be fixed (a new venture_case, "
                "evidence), park it with the numbers, or ask your owner to lift a knock-out on the Plan tab"
            )
        moved["proposed_at"] = ctx.now()
    moved["stage"] = stage
    return moved


def _tested(conn: Any, scope: AgentScope, row: Any) -> bool:
    """Whether a backed venture's first test is met (0.12.0), or the owner dropped it. 0.15.0: met as Ember's code or
    the owner closed it, never on the agent's word alone."""
    test = roadmap.get(conn, scope, row["test_milestone_id"]) if row["test_milestone_id"] else None
    return test is not None and (
        (test["status"] == "done" and test["closed_by"] in ("code", "owner"))
        or (test["status"] == "dropped" and test["closed_by"] == "owner")
    )


def _save_learned(ctx: ToolContext, row: Any, learned: str) -> str:
    """0.12.0: a venture's findings, appended to its knowledge file; a full file continues in its next part. A write
    that fails anyway is reported, and the rest of the update stays saved (a full file failed the whole update, scores
    and stage too, and counted against the agent)."""
    entry = f"\n### {ctx.clock.today().isoformat()}, cycle #{ctx.cycle_id}\n{learned}\n"
    parts = ventures.knowledge_parts(ctx.workspace, row["id"], row["title"])
    path = parts[-1] if parts else ventures.file_of(row["id"], row["title"])
    head = "" if parts else ventures.knowledge_head(row)
    try:
        try:
            size = ctx.workspace.write(path, head + entry, append=True)
        except QuotaError:
            used = ctx.workspace.size_of(path, "text") or 0
            if not parts or used + len(entry.encode("utf-8")) <= ctx.workspace.limits.max_file_bytes:
                raise  # the workspace is full, not the file
            if len(parts) >= ventures.KNOWLEDGE_PARTS:
                raise SandboxError(f"all {ventures.KNOWLEDGE_PARTS} parts of the knowledge file are full") from None
            path = ventures.file_of(row["id"], row["title"], len(parts) + 1)
            size = ctx.workspace.write(path, f"{ventures.knowledge_head(row)}(Continued from {parts[-1]}.)\n{entry}")
    except SandboxError as exc:
        return f" What you learned was NOT saved ({exc}); the rest of the update was: save it again once there is room."
    continued = f", continued from {parts[-1]}" if parts and path != parts[-1] else ""
    return f" What you learned is in {path} ({size:,} B{continued})."


# --- the roadmap (0.11.0) ---


def _due_date(text: str, today: date) -> date:
    """A milestone's date: written YYYY-MM-DD, from today to a year ahead."""
    day = roadmap.parse_day(text)
    if day is None:
        raise ToolError(f"due must be a date written YYYY-MM-DD, e.g. {(today + timedelta(days=7)).isoformat()}")
    if day < today:
        raise ToolError(f"due must be today ({today.isoformat()}) or later")
    last = today + timedelta(days=roadmap.AHEAD_DAYS)
    if day > last:
        raise ToolError(f"due can be at most a year ahead ({last.isoformat()})")
    return day


def _open_milestone(conn: Any, scope: AgentScope, milestone_id: int) -> Any:
    row = roadmap.get(conn, scope, milestone_id)
    if row is None:
        raise ToolError(f"there is no milestone #{milestone_id}")
    if row["status"] != "open":
        raise ToolError(f"milestone #{milestone_id} is {row['status']}, which is final")
    return row


def _parent(conn: Any, scope: AgentScope, parent_id: int, due: date, milestone_id: int | None = None) -> Any:
    """The open milestone a milestone due on ``due`` may lead to (never itself or one that leads to it)."""
    parent = _open_milestone(conn, scope, parent_id)
    if milestone_id is not None and roadmap.leads_to(conn, milestone_id, parent_id):
        raise ToolError(f"milestone #{parent_id} leads to #{milestone_id} already")
    # 0.29.0: the money goal stands in for the owner's: what leads to it may be due later (it leads to the next one)
    if due.isoformat() > parent["due"] and parent["kind"] != "money_goal":
        raise ToolError(
            f"milestone #{parent_id} is due {parent['due']}: a milestone leading to it is due by then at the latest"
        )
    return parent


# What a "done" names as its evidence (0.12.0): a number, a reference (#123) or a link or file. "Done." closed one.
EVIDENCE = re.compile(r"\d|https?://|[\w-]+/[\w./-]+\.\w+")


def update_milestone(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.12.0: a milestone_update Ember's code makes for the daily review's verdict, with the tool's rules; refused
    like the tool (the Outcome says why), counted toward no limit and recorded by the review."""
    try:
        checked = validate(SPECS["milestone_update"], args)
        with ctx.db.transaction() as conn, netguard.sealed():
            return _milestone_update(ctx, checked, conn)
    except ToolError as exc:
        return Outcome(False, _unstop(str(exc)), f"refused: {exc}"[:300])


def _wait(args: dict[str, Any], row: Any, today: date, closing: bool) -> dict[str, Any]:
    """0.12.0: the wait a milestone_update sets or ends (its columns; {} if none changes). Closing ends a wait."""
    wait_for = " ".join((args.get("wait_for") or "").split())
    check = args.get("check_at")
    if closing:
        if wait_for or check:
            raise ToolError("close a milestone or let it wait, not both")
        return {"wait_for": None, "check_at": None} if row["wait_for"] else {}
    if wait_for.lower() in ("nothing", "none", "-"):
        if not row["wait_for"]:
            raise ToolError(f"milestone #{row['id']} doesn't wait")
        return {"wait_for": None, "check_at": None}
    if not wait_for and not check:
        return {}
    if not wait_for or not check:
        raise ToolError("a wait needs both: what it waits for (wait_for) and when to check again (check_at)")
    day = roadmap.parse_day(check)
    last = today + timedelta(days=roadmap.WAIT_DAYS)
    if day is None or not today < day <= last:
        raise ToolError(
            f"check_at is a day after today and at most {roadmap.WAIT_DAYS} days ahead ({last.isoformat()})"
        )
    return {"wait_for": wait_for, "check_at": day.isoformat()}


def _milestone_update(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    row = _open_milestone(conn, ctx.scope, args["milestone_id"])
    mid = row["id"]
    today = ctx.clock.today()
    status = args.get("status")
    result = " ".join((args.get("result") or "").split())
    note = (args.get("note") or "").strip()
    theirs = row["created_by"] == "owner"  # 0.12.0: the owner's milestone is theirs to move and drop
    drop = "ask your owner to drop it" if theirs else "drop it (why)"
    changes: dict[str, Any] = {}
    fields = ("status", "due", "wait_for", "check_at", "parent_id", "venture_id", "project_id")
    if roadmap.is_goal(row) and any(args.get(name) not in (None, "") for name in fields):  # 0.29.0
        raise ToolError(
            f"milestone #{mid} is your owner's goal: they set it and change it, and Ember's code checks it from the "
            "books. Work on what leads to it; a note is all you add to it"
        )
    if status and args.get("due"):
        raise ToolError("close a milestone or move its date, not both")
    if result and not status:
        raise ToolError("result is for closing a milestone: set status too")
    if status == "done" and row["metric"]:  # 0.12.0: Ember's code closes it from its records
        raise ToolError(
            f"{metrics.status_text(row)}. Ember's code closes milestone #{mid} done once its metric is met: work "
            "toward it; if it is out of reach, move its date (why) or drop it (why)"
        )
    if status == "done" and row["kind"] == "first_test":  # 0.15.0: a venture goes live on it
        raise ToolError(
            f"milestone #{mid} is a first test, met as Ember's code checks it or your owner confirms it: tell your "
            "owner what shows it is met (message_owner), and your owner confirms it on the Plan tab"
        )
    if row["created_by"] == "code":  # 0.12.0: the money goal and its decision points
        if args.get("due") and args["due"] != row["due"]:
            raise ToolError(f"Ember's code set the date of milestone #{mid}: it doesn't move")
        if args.get("parent_id") is not None and args["parent_id"] != row["parent_id"]:
            raise ToolError(f"Ember's code set milestone #{mid}: it stays where it is")
        if status == "dropped":
            raise ToolError(f"Ember's code set milestone #{mid}: only your owner drops it")
        if status and row["kind"] == "money_goal":
            raise ToolError(
                "Ember's code closes the money goal from the books (revenue less expenses against your API spending "
                "over the last 30 days): work toward it"
            )
    due = roadmap.parse_day(row["due"]) or today
    moved_to: date | None = None
    if args.get("due"):
        wanted = _due_date(args["due"], today)
        if wanted.isoformat() != row["due"]:
            if not note:
                raise ToolError("say in note why the date moves")
            if int(row["moves"]) >= roadmap.MAX_MOVES:
                rest = (
                    f"Close it now: done if its measure is met (with the evidence), missed if not (why, and what "
                    f"now); or {drop}"
                    if due < today
                    else f"Reach it by {row['due']}, or {drop}; if it isn't reached, close it missed once that day "
                    "has passed"
                )
                raise ToolError(
                    f"milestone #{mid} has moved {row['moves']} times (first due {row['first_due']}): a date moves "
                    f"{roadmap.MAX_MOVES} times at most. {rest}"
                )
            later = [k for k in roadmap.children(conn, mid) if k["status"] == "open" and k["due"] > wanted.isoformat()]
            if later:
                raise ToolError(
                    f"milestone #{later[0]['id']} leads to it and is due {later[0]['due']}: move that first"
                )
            moved_to = wanted
    parent_id = args.get("parent_id")
    if parent_id is not None and parent_id != row["parent_id"]:
        _parent(conn, ctx.scope, parent_id, moved_to or due, mid)
        changes["parent_id"] = parent_id
    elif row["parent_id"] is not None and moved_to is not None:
        parent = roadmap.get(conn, ctx.scope, row["parent_id"])
        if (
            parent is not None
            and parent["status"] == "open"
            and parent["kind"] != "money_goal"  # 0.29.0: as in _parent
            and moved_to.isoformat() > parent["due"]
        ):
            raise ToolError(
                f"it leads to milestone #{parent['id']}, due {parent['due']}: move that first, or link it elsewhere"
            )
    if moved_to is not None and theirs:
        if row["proposed_due"] == moved_to.isoformat():
            raise ToolError(f"you proposed {moved_to.isoformat()} already: your owner decides on the Plan tab")
        changes.update(
            proposed_due=moved_to.isoformat(),
            proposed_note=" ".join(note.split())[: roadmap.NOTE_CHARS],
            proposed_at=ctx.now(),
            proposed_cycle_id=ctx.cycle_id,
        )
    elif moved_to is not None:
        due = moved_to
        changes.update(due=moved_to.isoformat(), moves=int(row["moves"]) + 1)
    for name, check in (("venture_id", _milestone_venture), ("project_id", _open_project)):
        value = args.get(name)
        if value is not None and value != row[name]:
            if row["created_by"] == "code" or row["metric"]:  # 0.15.0: links decide what Ember's code counts
                who = "set" if row["created_by"] == "code" else "checks"
                raise ToolError(
                    f"Ember's code {who} milestone #{mid}: what Ember's code counts for it is fixed, so it stays "
                    "linked as it is"
                )
            held = policy.unlocked(conn, ctx.scope, mid)
            if held:  # 0.21.0: an unlock covers what its milestone is linked to
                raise ToolError(
                    f"your owner unlocked {policy.unlocked_text(held, short=True)} for milestone #{mid}, for what it "
                    "is linked to: it stays linked as it is while that stands"
                )
            check(conn, ctx.scope, value)
            changes[name] = value
    if status:
        if not result:
            raise ToolError(
                {
                    "done": "say in result what shows its measure is met",
                    "missed": "say in result why it was missed, and what now",
                    "dropped": "say in result why it no longer matters",
                }[status]
            )
        if status == "done" and not EVIDENCE.search(result):
            raise ToolError(
                "say in result what shows its measure is met: a number (3 listings live, 12 views) or a reference "
                "(#123, a link or a workspace file); a done you close is shown as self-reported"
            )
        if status == "missed" and due >= today:
            move = "propose a new date" if theirs else "move its date"
            ways = drop if int(row["moves"]) >= roadmap.MAX_MOVES else f"{move} (due, with why in note), or {drop}"
            raise ToolError(
                f"milestone #{mid} is due {row['due']} ({roadmap.when(due, today)}): it is missed only once that day "
                f"has passed. Until then, reach it, or {ways}"
            )
        if status == "dropped" and theirs:
            raise ToolError(
                "your owner put this milestone on your roadmap: only they can drop it. Ask them (message_owner), "
                "or propose a new date (due, with why in note)"
            )
        if status == "dropped":
            owners = [k for k in roadmap.open_steps(conn, mid) if k["created_by"] == "owner"]
            if owners:
                raise ToolError(
                    f"your owner's milestone #{owners[0]['id']} leads to it: dropping it would drop theirs too. Link "
                    "theirs to another milestone first (parent_id), or ask your owner"
                )
        changes.update(
            status=status,
            result=result[: roadmap.LIMITS["result"]],
            closed_at=ctx.now(),
            closed_cycle_id=ctx.cycle_id,
            closed_by="agent",  # 0.12.0: the agent's word, shown as self-reported (Ember's code closes with "code")
            **roadmap.NO_PROPOSAL,
        )
    waits = _wait(args, row, today, bool(status))
    if waits:
        changes.update(waits)
        if waits["wait_for"]:
            note = f"{note} (waits for {waits['wait_for']} until {waits['check_at']})".strip()
    if note:
        changes["notes"] = roadmap.add_note(row["notes"], ctx.cycle_id, note)
    if not changes:
        raise ToolError("nothing to change")
    roadmap.update(conn, mid, ctx.now(), **changes)
    after = ""
    if status == "dropped":
        what = status
        dropped = roadmap.drop_steps(conn, mid, ctx.now(), f"Dropped with #{mid}: {result}", "agent", ctx.cycle_id)
        if dropped:
            after = f" Dropped with it, as they led to it: {_numbers(dropped)}."
        called = predictions.of_milestones(conn, [mid]).get(mid)
        if called is not None and called["status"] == "open":
            after += f" Your odds on it ({float(called['probability']):.0%}) count as a miss."  # 0.15.0
    elif status:
        what = status
        waiting = [k["id"] for k in roadmap.children(conn, mid) if k["status"] == "open"]
        if waiting:
            after = (
                f" Milestones leading to it are still open ({_numbers(waiting)}): close them, or link them to another."
            )
    elif "proposed_due" in changes:
        what = (
            f"you proposed moving it to {changes['proposed_due']}. It is your owner's milestone, so the date moves "
            f"only if they accept; until then it stays due {row['due']}"
        )
    elif "due" in changes:
        moves = changes["moves"]
        what = f"moved to {changes['due']} ({roadmap.when(due, today)}; moved {moves} time{'s' if moves != 1 else ''})"
    elif waits.get("wait_for"):
        what = f"waits for {waits['wait_for']} until {waits['check_at']} (not flagged overdue until then)"
    elif waits:
        what = "no longer waits"
    else:
        what = "updated"
    return Outcome(True, f"Milestone #{mid}: {what}.{after}", f"milestone #{mid} {what}"[:300])
