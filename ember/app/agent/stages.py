"""A venture's stages (0.12.0): the rule of each, kept by Ember's code.

A venture moved through its stages on the agent's word alone: research could go on without ever reaching a business
case, the business case's first test never became a milestone, and a killed venture's milestones stayed open. Now each
stage has a rule (``RULES``): what completes it, and when Ember's code parks the venture instead (its kill rule).

* researching: a business case from research (proposed). Parked when there is none RESEARCH_DAYS after its research
  in the stage began (its first research call), while nothing is built for it (no open project). A venture no one
  researches waits: the seeded Etsy leg sits in researching until its first listing.
* proposed: the owner's decision (back, park or kill).
* building: when the owner backs a venture, its first test becomes a milestone Ember's code sets (``first_test``, due in
  FIRST_TEST_DAYS, its date fixed), and (0.19.3) Ember's code opens its project (``open_project``): a backed venture is
  project work, in ordinary cycles, and venture cycles find and decide new ones. The venture goes live once that is met
  (the database refuses it before), and is parked when it is missed (closed missed, or still open FIRST_TEST_GRACE_DAYS
  after its date). 0.15.0: a venture a channel of Ember's code serves (CHANNEL_TESTS) gets its first test only once that
  channel is set up: its clock doesn't run while the owner hasn't connected it. 0.16.3 (analysis bug 1): a first test
  with a metric has the same grace as one in words (metrics.grade no longer closes it missed at its date); a channel's
  first test that is still unmet when its grace ends, while no product of the channel was ever made (CHANNEL_PRODUCTS),
  never ran: it starts once more instead (once a venture, ``run_again``); and the owner hears it a week before a first
  test's date (``warn``), as the agent does (OBLIGATIONS, ``owed``).
* idea (0.13.0, triage): an idea of the agent's is researched (researching) or parked within TRIAGE_DAYS of coming up;
  Ember's code parks it then. The owner's ideas wait for them.
* live (0.13.0, scale): a live venture that earns more than it costs (its P&L: revenue less expenses and the API calls
  that worked for it) gets a decision point Ember's code sets, to scale it (``scale``, due in SCALE_DAYS); one that has
  sold nothing (no revenue recorded, no Etsy order of its listings) LIVE_DAYS after it went live is parked.
For a venture already in the stage when a rule came, the rule counts from then (``rules_from``).

Ember's code parks reversibly (``parked_by`` 'code'): only the owner takes such a venture up again, and only the owner
backs or kills one (migration 0030). A venture parked or killed takes its open milestones with it
(``drop_milestones``): all of them when the owner parked or killed it, else all but the owner's; 0.15.0: and the bars
of its projects' listing tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso
from ..integrations import pinterest_publisher, printify_publisher
from . import knockouts, metrics, policy, roadmap, store, ventures
from .store import AgentScope

RESEARCH_DAYS = ventures.RESEARCH_DAYS
FIRST_TEST_DAYS = ventures.FIRST_TEST_DAYS
TRIAGE_DAYS = ventures.TRIAGE_DAYS  # 0.13.0
LIVE_DAYS = ventures.LIVE_DAYS
SCALE_DAYS = ventures.SCALE_DAYS
FIRST_TEST_GRACE_DAYS = ventures.FIRST_TEST_GRACE_DAYS
# 0.16.3 (analysis bug 1): the owner hears it once (the System log) when a backed venture's first test is due within
# this many days and unmet, a week before its date and two before the venture would be parked; the agent owes it too.
WARN_DAYS = 7
WARNED_KEY = "agent.{mode}.first_test_warned.{milestone}"
OPEN_PROJECTS = ("idea", "active", "waiting")
NO_TEST = (
    "Its first test is met: a small launch whose result shows whether it can earn (its business case names none yet)"
)
# 0.13.0 (Phase E2): a venture a channel of Ember's code serves has that channel's first test, a metric Ember's code
# checks (missed: the venture is parked, as any first test): its pins must bring buyers to the shop.
CHANNEL_TESTS = {
    "pinterest": ("pin_clicks", 10, "Its pins bring 10 clicks to the shop's listings (Pinterest's numbers)"),
    "printify": ("pod_orders", 1, "A buyer orders one of its products (Printify's records)"),  # Phase E4
}
# 0.16.3 (analysis bug 1): what a channel's venture makes, and whether Ember's code ever made one. Unmet when its grace
# ends while none was ever made, a channel's first test never ran (Ember's own checks refused the poster twice): it
# starts once more instead of parking the venture, once a venture (NEVER_RAN names such a test in its result).
CHANNEL_PRODUCTS: dict[str, tuple[str, Callable[[sqlite3.Connection, AgentScope], bool]]] = {
    "pinterest": ("pin", pinterest_publisher.made_any),
    "printify": ("Printify product", printify_publisher.made_any),
}
NEVER_RAN = "so it never ran"


def _day(stamp: str) -> date:
    return from_iso(stamp).date()


def _q(text: Any) -> str:
    return json.dumps(" ".join(str(text or "").split()), ensure_ascii=False)


def first_test(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], today: date, now: str) -> int:
    """The milestone of a backed venture's first test: set by Ember's code (its date never moves, only the owner drops
    it), due in FIRST_TEST_DAYS, leading to the money goal when that is due later, measured by the business case's
    first test. Returns its number."""
    due = today + timedelta(days=FIRST_TEST_DAYS)
    goal = roadmap.money_goal(conn, scope)
    parent = goal["id"] if goal is not None and goal["due"] >= due.isoformat() else None
    text = " ".join(str(venture["first_test"] or "").split())
    measure = f"Its first test is met: {text}" if text else NO_TEST
    metric, target = None, None
    channel = venture["channel"] if "channel" in venture.keys() else None  # noqa: SIM118 - a Row, not a dict
    if channel in CHANNEL_TESTS:
        metric, target, measure = CHANNEL_TESTS[channel]
    limit = roadmap.LIMITS["measure"]
    milestone_id = roadmap.create(
        conn,
        scope,
        title=f"First test: {' '.join(str(venture['title']).split())}"[: roadmap.LIMITS["title"]],
        measure=measure if len(measure) <= limit else measure[: limit - 1].rstrip() + "…",
        due=due.isoformat(),
        now=now,
        parent_id=parent,
        venture_id=int(venture["id"]),
        created_by="code",
        kind="first_test",
        metric=metric,
        target=target,
    )
    conn.execute(
        "UPDATE ventures SET test_milestone_id = ?, updated_at = ? WHERE id = ?", (milestone_id, now, venture["id"])
    )
    return milestone_id


def open_project(
    conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], test_id: int, cycle_id: int, now: str
) -> str:
    """0.19.3: the project of a backed venture that never had one, opened by Ember's code (the owner: "once we do it,
    it goes into an active project"). Live, two venture cycles tried to set one up for the licence packs and couldn't,
    and the website's project became the Bluesky channel's, so READY asked again and again. Returns what happened, or
    "" when the venture had a project (open or closed: one the agent closed isn't opened again)."""
    had = conn.execute("SELECT 1 FROM projects WHERE venture_id = ? LIMIT 1", (venture["id"],)).fetchone()
    if had is not None:
        return ""
    title = " ".join(str(venture["title"]).split())[:80]
    hypothesis = " ".join(str(venture["first_test"] or venture["pitch"] or title).split())
    project = store.create_project(
        conn,
        scope,
        cycle_id=cycle_id,
        title=title,
        hypothesis=hypothesis if len(hypothesis) <= 400 else hypothesis[:399].rstrip() + "…",
        next_step=f"Run its first test (milestone #{test_id}): what to make or set up, where buyers see it, and a bet.",
        status="active",
        now=now,
        venture_id=int(venture["id"]),
    )
    return (
        f"Ember's code opened project #{project} for venture #{venture['id']}, which your owner backed: its first test "
        f"(milestone #{test_id}) is the project's work, in ordinary cycles"
    )


def drop_milestones(
    conn: sqlite3.Connection, scope: AgentScope, venture_id: int, now: str, result: str, by: str
) -> list[int]:
    """A venture parked or killed takes its open milestones with it, and the open steps leading to them: all of them
    when the owner parked or killed it (``by`` 'owner'), else all but the owner's (which stay theirs to drop). 0.15.0:
    when the owner or Ember's code parks or kills it, also the bars Ember's code set for its projects (their listing
    tests end with it). The agent's own park leaves them open: it can't end a listing test. 0.23.2: the owner's park or
    kill takes every milestone of its projects too (it stops their work: stop_projects), the agent's own included.
    Returns their numbers."""
    where, params = scope.where()
    bars, owner = by in ("owner", "code"), by == "owner"
    linked = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'open' AND (venture_id = ? OR (project_id IN"
        " (SELECT id FROM projects WHERE venture_id = ?) AND (? OR (? AND created_by = 'code')))) ORDER BY id",
        (*params, venture_id, venture_id, owner, bars),
    ).fetchall()
    dropped: list[int] = []
    for row in [*linked, *(s for r in linked for s in roadmap.open_steps(conn, r["id"]))]:
        if row["id"] in dropped or (row["created_by"] == "owner" and by != "owner"):
            continue
        closed_by = "code" if by == "code" or (by == "agent" and row["created_by"] == "code") else by
        conn.execute(
            "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = ?, updated_at = ?,"
            " proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL"
            " WHERE id = ? AND status = 'open'",
            (result[: roadmap.LIMITS["result"]], now, closed_by, now, row["id"]),
        )
        dropped.append(int(row["id"]))
    return dropped


PARKED_STEP = "None until your owner takes the venture up again."
_WAS = "It was {status}; its next step: "


def stop_projects(conn: sqlite3.Connection, venture_id: int, stage: str, now: str) -> list[int]:
    """0.22.0 (analysis 0.20.1, FIX NOW 12): the owner's park or kill (``stage``: parked or killed) stops the venture's
    project work: its open projects are abandoned when it is killed, and wait while it is parked (project_update
    doesn't make them active again until the owner takes it up). They stayed active, and the agent worked on them.
    0.23.1: what each was and its next step stay in its notes (the park overwrote the next step for good), for
    resume_projects; a park leaves ``updated_at`` the agent's (the plan shows the projects it updated last in full).
    Returns their numbers."""
    stopped = []
    for row in conn.execute(
        "SELECT id, status, next_step, notes FROM projects WHERE venture_id = ? AND status IN ('idea', 'active',"
        " 'waiting') ORDER BY id",
        (venture_id,),
    ).fetchall():
        status = "abandoned" if stage == "killed" else "waiting"
        said = f"[owner] Your owner {stage} venture #{venture_id}."
        if stage == "parked" and row["next_step"] != PARKED_STEP:
            said += f" {_WAS.format(status=row['status'])}{' '.join(str(row['next_step'] or '-').split())}"
        nxt = "None: closed with its venture." if stage == "killed" else PARKED_STEP
        conn.execute(  # a kill closes it, now (the review and the lessons read what closed when)
            "UPDATE projects SET status = ?, next_step = ?, notes = ?, updated_at = CASE WHEN ? = 'abandoned' THEN ?"
            " ELSE updated_at END WHERE id = ?",
            (status, nxt, f"{row['notes']}\n{said}".strip()[-2000:], status, now, row["id"]),
        )
        stopped.append(int(row["id"]))
    return stopped


def resume_projects(conn: sqlite3.Connection, venture_id: int) -> list[int]:
    """0.23.1: the owner took a venture they parked up again (back or research): its waiting projects get back what
    stop_projects kept in their notes, their status and next step, unless the agent set another next step meanwhile.
    One whose park left nothing (0.22.0's, migration 0077) gets a next step saying so. Returns their numbers."""
    resumed = []
    said = f"[owner] Your owner took venture #{venture_id} up again."
    for row in conn.execute(
        "SELECT id, notes FROM projects WHERE venture_id = ? AND status = 'waiting' AND next_step = ? ORDER BY id",
        (venture_id, PARKED_STEP),
    ).fetchall():
        status, nxt = "waiting", f"None yet: your owner took venture #{venture_id} up again; set one (project_update)."
        mark = f"[owner] Your owner parked venture #{venture_id}. "
        for line in reversed(str(row["notes"]).split("\n")):
            if line.startswith(mark):
                kept = re.match(re.escape(mark) + r"It was (idea|active|waiting); its next step: (.+)$", line)
                if kept is not None:
                    status, nxt = kept[1], kept[2]
                break
        conn.execute(
            "UPDATE projects SET status = ?, next_step = ?, notes = ? WHERE id = ?",
            (status, nxt, f"{row['notes']}\n{said}".strip()[-2000:], row["id"]),
        )
        resumed.append(int(row["id"]))
    return resumed


def hold_requests(conn: sqlite3.Connection, scope: AgentScope, venture_id: int, stage: str, now: str) -> list[int]:
    """0.23.3: what was approved for the work the owner's park or kill (``stage``) of a venture stops, and Ember's code
    hasn't begun (no journal entry, nothing claimed in its executor's table), isn't carried out
    (ventures.request_stopped): a new listing or product, a change, a pin or a post of a stopped line. An unlock's
    approval waits for the owner again (as policy._stop); the owner's own is closed, saying why (a listing approved
    before the park went live after it). The owner's Undo of an action isn't held: it ends work, it doesn't carry it
    on. Returns their numbers."""
    where, params = scope.where("a")
    marks = ", ".join("?" for _ in ventures.HELD_EXECUTORS)
    claimed = " ".join(
        f"AND NOT (a.executor = '{executor}' AND EXISTS (SELECT 1 FROM {table} c WHERE c.approval_id = a.id))"
        for executor, table in ventures.HELD_EXECUTORS.items()
    )
    held = []
    for row in conn.execute(
        f"SELECT a.* FROM approvals a WHERE {where} AND a.status IN ('approved', 'approved_with_changes')"
        f" AND a.executor IN ({marks}) AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = a.id)"
        f" AND NOT EXISTS (SELECT 1 FROM action_undos u WHERE u.approval_id = a.id) {claimed} ORDER BY a.id",
        (*params, *ventures.HELD_EXECUTORS),
    ).fetchall():
        venture = ventures.request_stopped(conn, scope, row)
        if venture is None or venture["id"] != venture_id:
            continue
        said = f"your owner {stage} venture #{venture_id} before Ember's code carried it out"
        twin = conn.execute(
            "SELECT id FROM approvals WHERE mode = ? AND session = ? AND payload_sha256 = ? AND status = 'pending'",
            (row["mode"], row["session"], row["payload_sha256"]),
        ).fetchone()
        if row["decided_by"] == policy.POLICY_BY and row["status"] == "approved" and twin is None:
            conn.execute(
                "UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL, decision_comment = ?,"
                " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status = 'approved'",
                (f"Approved by your unlock, but {said}: it waits for you.", row["id"]),
            )
        else:
            conn.execute(
                "UPDATE approvals SET status = 'failed', closed_at = ?, closed_by = ?, result_note = ?,"
                " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status IN ('approved',"
                " 'approved_with_changes')",
                (now, policy.REVOKED_BY, f"Not carried out: {said}.", row["id"]),
            )
        held.append(int(row["id"]))
    return held


def park(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], now: str, why: str) -> str:
    """Ember's code parks a venture by its stage's rule (``why``), reversibly: only the owner takes it up again. Its
    open milestones go with it, the owner's aside. Returns what happened, for the events."""
    vid = int(venture["id"])
    note = ventures.add_note(venture["notes"], None, f"Parked by Ember's code: {why}.")
    conn.execute(
        "UPDATE ventures SET stage = 'parked', parked_by = 'code', notes = ?, updated_at = ? WHERE id = ?",
        (note, now, vid),
    )
    dropped = drop_milestones(conn, scope, vid, now, f"Venture #{vid} was parked by Ember's code: {why}.", "code")
    also = f"; dropped with it: {', '.join(f'#{i}' for i in dropped)}" if dropped else ""
    return f"Ember's code parked venture #{vid} ({venture['title']}): {why}{also}"


def backing_problem(
    conn: sqlite3.Connection, venture: Mapping[str, Any], *, cash_eur: float, net_days: float | None
) -> str:
    """0.15.0: why a proposal can't be backed as it stands ("" when it can): it has no numbers (proposed before they
    were needed) or a knock-out stands. Such a proposal goes back to researching (``reopen``)."""
    if not ventures.latest_case(conn, int(venture["id"])):
        if venture["stage"] == "proposed":
            return "it has no numbers (venture_case), as it was proposed before a business case needed them"
        return "it has no numbers (venture_case) yet"
    standing = knockouts.active(knockouts.check(conn, venture, cash_eur=cash_eur, net_days=net_days))
    if standing:
        return "it is knocked out (" + "; ".join(f"{k.label}: {k.why}" for k in standing) + ")"
    return ""


def reopen(conn: sqlite3.Connection, venture: Mapping[str, Any], now: str, why: str, said: str | None = None) -> None:
    """0.15.0: Ember's code sends a proposal back to researching (``why``, kept in its notes): a venture proposed
    before the gates, or one knocked out since, isn't backed as it stands. What the owner ``said`` with their Back is
    kept there too."""
    note = f"Back to researching by Ember's code: {why}."
    if said:
        note += f" Your owner said with their Back: {said}"
    note = ventures.add_note(venture["notes"], None, note)
    conn.execute(
        "UPDATE ventures SET stage = 'researching', notes = ?, updated_at = ? WHERE id = ? AND stage = 'proposed'",
        (note, now, venture["id"]),
    )


def sold(conn: sqlite3.Connection, scope: AgentScope, venture_id: int, paid: ventures.Money) -> bool:
    """0.13.0: whether a venture has sold anything: revenue recorded for it, or an Etsy order of its listings."""
    if paid.earned > 0:
        return True
    ours = {int(r["listing_id"]) for r in metrics.listings(conn, scope, None, venture_id)}
    return bool(ours) and bool(metrics.orders_of(conn, scope, ours, ""))


def scale(
    conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], paid: ventures.Money, today: date, now: str
) -> int:
    """0.13.0: the decision point of a live venture that earns more than it costs: scale what sells. A goal of its own
    in the plan, due in SCALE_DAYS, closed by the agent once that is under way. Returns its number."""
    title = f"Scale it: {' '.join(str(venture['title']).split())}"
    limit = roadmap.LIMITS["title"]
    milestone_id = roadmap.create(
        conn,
        scope,
        title=title if len(title) <= limit else title[: limit - 1].rstrip() + "…",
        measure=(
            f"It earns more than it costs ({ventures.money_text(paid)}): more of what sells (variants, bundles, "
            "another channel) is under way; you close it then"
        )[: roadmap.LIMITS["measure"]],
        due=(today + timedelta(days=SCALE_DAYS)).isoformat(),
        now=now,
        venture_id=int(venture["id"]),
        created_by="code",
        kind="decision",
    )
    conn.execute(
        "UPDATE ventures SET scale_milestone_id = ?, updated_at = ? WHERE id = ?", (milestone_id, now, venture["id"])
    )
    return milestone_id


def waits_for_channel(venture: Mapping[str, Any], ready: Collection[str]) -> bool:
    """0.15.0: whether a venture's first test waits for its channel to be set up (``ready``: the channels that are)."""
    channel = venture["channel"] if "channel" in venture.keys() else None  # noqa: SIM118 - a Row, not a dict
    return channel in CHANNEL_TESTS and channel not in ready


def restart_test(
    conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], test: Mapping[str, Any], now: str
) -> str:
    """0.15.0: a channel venture's open first test, set before the owner had set its channel up (as 0.13.0 did when
    they backed it) or running when they switched it off, is dropped: its date can't move, and its clock shouldn't run
    until then. ``keep`` sets a new one once the channel is ready. Returns what happened, for the events."""
    vid, name = int(venture["id"]), str(venture["channel"]).capitalize()
    why = f"{name} isn't set up (or is switched off): a new first test starts once it is ready."
    conn.execute(
        "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?"
        " WHERE id = ? AND status = 'open'",
        (f"Venture #{vid}'s channel {why}", now, now, test["id"]),
    )
    conn.execute("UPDATE ventures SET test_milestone_id = NULL, updated_at = ? WHERE id = ?", (now, vid))
    return f"Ember's code dropped milestone #{test['id']}, the first test of venture #{vid}: {why}"


def never_ran(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any]) -> str:
    """0.16.3 (analysis bug 1): what a channel's venture never made, so that its first test would start once more if
    still unmet when its grace ends ("" when it made one, isn't a channel's, or its test started once more already)."""
    channel = venture["channel"] if "channel" in venture.keys() else None  # noqa: SIM118 - a Row, not a dict
    if channel not in CHANNEL_PRODUCTS:
        return ""
    product, made = CHANNEL_PRODUCTS[channel]
    if made(conn, scope):
        return ""
    again = conn.execute(
        "SELECT 1 FROM milestones WHERE venture_id = ? AND kind = 'first_test' AND created_by = 'code'"
        " AND status = 'dropped' AND closed_by = 'code' AND result LIKE ? LIMIT 1",
        (venture["id"], f"%{NEVER_RAN}%"),
    ).fetchone()
    return "" if again else product


def run_again(
    conn: sqlite3.Connection,
    scope: AgentScope,
    venture: Mapping[str, Any],
    test: Mapping[str, Any],
    product: str,
    today: date,
    now: str,
) -> str:
    """0.16.3 (analysis bug 1): a channel's first test that never ran (``never_ran``: no ``product`` was ever made by
    its last day) starts once more: a new first test, due in FIRST_TEST_DAYS; the old one is dropped with the reason,
    and the open steps that led to it lead to the new one. Returns what happened, for the events."""
    vid = int(venture["id"])
    made = first_test(conn, scope, venture, today, now)
    why = f"no {product} was ever made by its last day, {NEVER_RAN}"
    conn.execute(
        "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?,"
        " proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL"
        " WHERE id = ? AND status = 'open'",
        (f"Venture #{vid}'s first test: {why}. Ember's code started it once more as #{made}.", now, now, test["id"]),
    )
    conn.execute(  # the agent's and the owner's (one Ember's code set leads only to a money goal, migration 0065)
        "UPDATE milestones SET parent_id = ?, updated_at = ? WHERE parent_id = ? AND status = 'open'"
        " AND created_by <> 'code'",
        (made, now, test["id"]),
    )
    return (
        f"Ember's code started the first test of venture #{vid} once more as milestone #{made} (due in "
        f"{FIRST_TEST_DAYS} days): {why}"
    )


def _lines_tested(conn: sqlite3.Connection, venture_id: int) -> list[int]:
    """The product lines of a venture whose listing tests are open (a bar Ember's code set): its park ends them."""
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT m.project_id FROM milestones m JOIN projects p ON p.id = m.project_id"
            " WHERE p.venture_id = ? AND m.status = 'open' AND m.created_by = 'code' ORDER BY m.project_id",
            (venture_id,),
        )
    ]


def at_stake(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], test: Mapping[str, Any]) -> str:
    """0.16.3 (analysis bug 1): what happens if a backed venture's open first test stays unmet: by when it can still be
    met, the park and what goes down with it, or that it starts once more when no product of its channel is made."""
    vid = int(venture["id"])
    progress = metrics.progress_text(test)
    lines = _lines_tested(conn, vid)
    goes = f", which ends the listing tests of its product lines {', '.join(f'#{p}' for p in lines)}" if lines else ""
    product = never_ran(conn, scope, venture)
    again = f" If no {product} is made by then, it starts the test once more instead." if product else ""
    return (
        f"First test #{test['id']} of venture #{vid} {_q(venture['title'])} is due {test['due']}, not met yet"
        + (f" ({progress})" if progress else "")
        + f". Unmet by {ventures.test_ends(test)}, Ember's code closes it missed and parks venture #{vid}{goes}."
        + again
    )


def _due_soon(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """The backed ventures whose open first test (set by Ember's code) is due within WARN_DAYS or past its date, with
    that test."""
    found = []
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] != "building" or not v["test_milestone_id"]:
            continue
        test = roadmap.get(conn, scope, v["test_milestone_id"])
        due = roadmap.parse_day(test["due"]) if test is not None else None
        if test is None or test["status"] != "open" or due is None or not ventures.is_first_test(test):
            continue
        if (due - today).days <= WARN_DAYS:
            found.append((v, test))
    return found


def warn(conn: sqlite3.Connection, scope: AgentScope, today: date, now: str) -> list[str]:
    """0.16.3 (analysis bug 1): the owner hears once, a week before a backed venture's first test is due unmet (or as
    soon as Ember's code sees one later), what is at stake (``at_stake``). Returns the warnings, for the System log."""
    happened = []
    for v, test in _due_soon(conn, scope, today):
        key = WARNED_KEY.format(mode=scope.mode, milestone=test["id"])
        fresh = conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING", (key, now, now)
        ).rowcount
        if fresh:
            happened.append(at_stake(conn, scope, v, test))
    return happened


def owed(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[str]:
    """0.16.3 (analysis bug 1): what the agent owes for each backed venture whose first test is due within WARN_DAYS
    unmet, for its OBLIGATIONS: what is at stake, then what to do."""
    return [
        f"{at_stake(conn, scope, v, test)} Work toward it first, or tell your owner once what it needs."
        for v, test in _due_soon(conn, scope, today)
    ]


def keep(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    now: str,
    ready: Collection[str] = tuple(CHANNEL_TESTS),
    unset: Collection[str] = (),
    cycle_id: int | None = None,
) -> list[str]:
    """Before every plan: a first test for each backed venture that has none (0.15.0: a channel's venture once its
    channel is ``ready``; while the owner hasn't set the channel up (``unset``), an open first test Ember's code set
    earlier is dropped, and a new one comes once it is), and the stages' rules (research without a business case, a
    missed first test; 0.13.0: an idea no one took up, a live venture that sells nothing or earns more than it costs;
    0.16.3, analysis bug 1: a channel's first test that never ran, as no product of the channel was ever made, starts
    once more instead of being missed, once a venture; 0.19.3: with ``cycle_id``, the project of a backed venture
    that never had one). Returns what happened, for the events."""
    happened = []
    paid = ventures.money(conn, scope)
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] == "idea":  # 0.13.0: triage
            triage = ventures.triage_date(v)
            if triage is not None and today >= triage:
                happened.append(
                    park(conn, scope, v, now, f"no one took the idea up within {TRIAGE_DAYS} days (triage)")
                )
            continue
        if v["stage"] == "live":  # 0.13.0: scale, or park what sells nothing
            money = paid.get(int(v["id"]), ventures.Money())
            start = ventures.rule_clock(v)
            if money.earned > 0 and money.net > 0 and not v["scale_milestone_id"]:
                made = scale(conn, scope, v, money, today, now)
                happened.append(
                    f"Ember's code set milestone #{made}: scale venture #{v['id']} (it earns more than it costs)"
                )
            elif start is not None and (today - start).days >= LIVE_DAYS and not sold(conn, scope, int(v["id"]), money):
                happened.append(park(conn, scope, v, now, f"nothing sold {LIVE_DAYS} days after it went live"))
            continue
        if v["stage"] == "researching":
            if not v["research_from"] or (today - _day(v["research_from"])).days < RESEARCH_DAYS:
                continue
            building = conn.execute(
                f"SELECT 1 FROM projects WHERE venture_id = ? AND status IN {OPEN_PROJECTS} LIMIT 1", (v["id"],)
            ).fetchone()
            if building is None:
                why = f"no business case {RESEARCH_DAYS} days after its research began"
                happened.append(park(conn, scope, v, now, why))
            continue
        if v["stage"] != "building":
            continue
        test = roadmap.get(conn, scope, v["test_milestone_id"]) if v["test_milestone_id"] else None
        if waits_for_channel(v, ready):
            # 0.15.0: no first test runs, nor is missed, while its channel can't be used
            if test is not None and test["status"] == "open" and test["created_by"] == "code" and v["channel"] in unset:
                happened.append(restart_test(conn, scope, v, test, now))
            continue
        if test is None:
            made = first_test(conn, scope, v, today, now)
            happened.append(f"Ember's code set the first test of venture #{v['id']} as milestone #{made}")
            opened = open_project(conn, scope, v, made, cycle_id, now) if cycle_id is not None else ""
            happened += [opened] if opened else []
            continue
        if cycle_id is not None and test["status"] == "open":
            opened = open_project(conn, scope, v, int(test["id"]), cycle_id, now)
            happened += [opened] if opened else []
        late = (today - (roadmap.parse_day(test["due"]) or today)).days
        if test["status"] == "open" and late > FIRST_TEST_GRACE_DAYS:
            product = never_ran(conn, scope, v) if test["created_by"] == "code" else ""
            if product:  # 0.16.3 (analysis bug 1): it measured nothing: once more, instead of parking the venture
                happened.append(run_again(conn, scope, v, test, product, today, now))
                continue
            progress = metrics.progress_text(test)  # 0.16.3 (analysis bug 1): a metric's where it stands
            conn.execute(
                "UPDATE milestones SET status = 'missed', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?"
                " WHERE id = ? AND status = 'open'",
                (
                    f"Still open {late} days after its date"
                    + (f" ({progress})" if progress else "")
                    + ": Ember's code closed it missed.",
                    now,
                    now,
                    test["id"],
                ),
            )
            test = roadmap.get(conn, scope, test["id"])
        if test is not None and test["status"] == "missed":
            happened.append(park(conn, scope, v, now, f"its first test (milestone #{test['id']}) was missed"))
    return happened
