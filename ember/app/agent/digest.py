"""A digest of every wake cycle, built by Ember's code from its records when the cycle ends (0.12.0).

The journal is the model's last reply: it could be lost (a cut-off or unaffordable reflection) or claim work that
never happened (live: cycle #33 recorded two file writes as written after both had been skipped at the reply's token
limit). The digest is built from the records instead, for every cycle, failed, cut-off and unreflected ones too:
what the cycle was aimed at, its goal, what its tools did and what they didn't (refused, failed, skipped or cut off),
how its work ended, whether it reflected, and what it cost. It is stored once (``cycle_digests``) and read:

* by the next plans: the last two digests, in YOUR LAST CYCLE;
* by the work brief: the newest digest of a cycle aimed at its focus milestone and venture, in FOCUS;
* by the reflection: what its own cycle did not do (``undone``), so undone work can't be reported as done;
* by the owner: on the cycle in the Activity tab.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..economy.costs import micros_to_usd
from .store import AgentScope

MAX_CHARS = 2_000  # a stored digest (the table's limit)
LISTED = 8  # the tool calls a digest lists by name, of each kind
TOOL_CHARS = 40
TARGET_CHARS = 50
REASON_CHARS = 70
UNDONE_SHOWN = 6  # what the reflection is shown of its cycle's undone calls
BRIEF_CHARS = 400  # a digest as the work brief's FOCUS shows it
# How a cycle's work ended (loop's end reasons), in the digest's words; any other reason is shown as it is.
ENDED = {
    "": "the plan was done",
    "done": "the agent ended it",
    "step limit reached": "the step limit",
    "the conversation got too long": "the conversation's size limit",
}
# What names a tool call's target, in this order: a file, a record's number, a title or question.
_TARGET_KEYS = (
    "path",
    "file",
    "milestone_id",
    "venture_id",
    "project_id",
    "listing_id",
    "request_id",
    "approval_id",
    "title",
    "question",
    "name",
)
NOT_DONE = ("error", "skipped", "interrupted", "started")
BOOKKEEPING = frozenset({"write_journal", "set_sleep"})  # not listed as done: the reflection line says it
STEP_CHARS = 60  # a plan step, as the digest of a cycle that ended before its plan was done lists it (0.14.0)


def build(conn: sqlite3.Connection, cycle_id: int, status: str, note: str | None) -> tuple[str, int]:
    """The digest of cycle ``cycle_id`` ending ``status`` (``note``: why), and how many of its tool calls were not
    done."""
    cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    head = f"Cycle #{cycle_id} {status}" + (f" ({_flat(note, 160)})" if note else "")
    cost = conn.execute("SELECT COALESCE(SUM(cost_micros), 0) FROM llm_calls WHERE cycle_id = ?", (cycle_id,))
    head += f" · ${micros_to_usd(int(cost.fetchone()[0])):.4f}"
    aims = []
    if cycle is not None:
        for column, word in (("venture_id", "venture"), ("milestone_id", "milestone"), ("project_id", "project")):
            if cycle[column]:
                aims.append(f"{word} #{cycle[column]}")
    if aims:
        head += f" · for {', '.join(aims)}"
    if cycle is not None and cycle["venture"]:
        head += " · a venture cycle"
    lines = [head]
    goal = _goal(cycle)
    lines.append(f"Goal: {json.dumps(goal, ensure_ascii=False)}" if goal else "No plan was made.")
    calls = conn.execute(
        "SELECT tool, status, summary, input, phase FROM tool_calls WHERE cycle_id = ? AND parent_id IS NULL"
        " AND phase IN ('act', 'reflect') ORDER BY id",
        (cycle_id,),
    ).fetchall()
    undone = [c for c in calls if c["status"] in NOT_DONE and not _early_journal(c)]
    done = [c for c in calls if c["status"] == "ok" and c["tool"] not in BOOKKEEPING]
    if undone:
        lines.append(f"Not done ({len(undone)}): {_listed([undone_line(c) for c in undone])}")
    if done:
        lines.append(f"Done ({len(done)}): {_listed([_done(c) for c in done])}")
    if not undone and not done and goal:
        lines.append("No tool was used for the work.")
    if cycle is not None and goal:
        lines += ended(cycle, status)
    lines.append(_reflection(conn, cycle_id))
    return "\n".join(lines)[:MAX_CHARS], len(undone)


def write(conn: sqlite3.Connection, cycle_id: int, status: str, note: str | None, now: str) -> None:
    """Store the digest of a cycle that is ending (once: a digest never changes)."""
    text, undone = build(conn, cycle_id, status, note)
    conn.execute(
        "INSERT OR IGNORE INTO cycle_digests (cycle_id, created_at, text, undone) VALUES (?, ?, ?, ?)",
        (cycle_id, now, text, undone),
    )


def undone(conn: sqlite3.Connection, cycle_id: int) -> list[str]:
    """The work steps' tool calls of a cycle that were not done (refused, failed, skipped or cut off), for its
    reflection."""
    calls = conn.execute(
        "SELECT tool, status, summary, input FROM tool_calls WHERE cycle_id = ? AND parent_id IS NULL"
        f" AND phase = 'act' AND status IN ({', '.join(repr(s) for s in NOT_DONE)}) ORDER BY id",
        (cycle_id,),
    ).fetchall()
    calls = [c for c in calls if c["tool"] != "write_journal"]  # 0.14.0: tried during the work (_early_journal)
    shown = [undone_line(c) for c in calls[:UNDONE_SHOWN]]
    if len(calls) > UNDONE_SHOWN:
        shown.append(f"and {len(calls) - UNDONE_SHOWN} more")
    return shown


def latest(conn: sqlite3.Connection, scope: AgentScope, limit: int = 2) -> list[str]:
    """The digests of the scope's last ``limit`` cycles, newest first."""
    rows = conn.execute(
        "SELECT d.text FROM cycle_digests d JOIN cycles y ON y.id = d.cycle_id WHERE y.session = ? AND y.simulated = ?"
        " ORDER BY d.cycle_id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, limit),
    ).fetchall()
    return [str(r["text"]) for r in rows]


def newest_for(conn: sqlite3.Connection, scope: AgentScope, column: str, value: int, before: int) -> str:
    """The newest digest of a cycle before cycle ``before`` aimed at a milestone or venture (``column``:
    milestone_id or venture_id), as the work brief shows it; "" when there is none."""
    if column not in ("milestone_id", "venture_id"):
        raise ValueError(column)
    row = conn.execute(
        "SELECT d.text FROM cycle_digests d JOIN cycles y ON y.id = d.cycle_id WHERE y.session = ? AND y.simulated = ?"
        f" AND y.{column} = ? AND d.cycle_id < ? ORDER BY d.cycle_id DESC LIMIT 1",
        (scope.session, 1 if scope.simulated else 0, value, before),
    ).fetchone()
    if row is None:
        return ""
    text = " / ".join(str(row["text"]).split("\n"))
    return text if len(text) <= BRIEF_CHARS else text[: BRIEF_CHARS - 1] + "…"


def newest_by(conn: sqlite3.Connection, scope: AgentScope, column: str) -> dict[int, str]:
    """The newest digest of the cycles aimed at each milestone or venture (``column``: milestone_id or venture_id),
    for the dashboard's cards."""
    if column not in ("milestone_id", "venture_id"):
        raise ValueError(column)
    params = (scope.session, 1 if scope.simulated else 0)
    rows = conn.execute(
        f"SELECT y.{column} AS aim, d.text FROM cycle_digests d JOIN cycles y ON y.id = d.cycle_id WHERE d.cycle_id IN"
        f" (SELECT MAX(d2.cycle_id) FROM cycle_digests d2 JOIN cycles y2 ON y2.id = d2.cycle_id WHERE y2.session = ?"
        f" AND y2.simulated = ? AND y2.{column} IS NOT NULL GROUP BY y2.{column})",
        params,
    ).fetchall()
    return {int(r["aim"]): str(r["text"]) for r in rows}


def _goal(cycle: sqlite3.Row | None) -> str:
    try:
        plan = json.loads(cycle["plan"]) if cycle is not None and cycle["plan"] else {}
    except ValueError:
        return ""
    goal = plan.get("goal") if isinstance(plan, dict) else None
    return _flat(goal, 200) if isinstance(goal, str) else ""


def ended(cycle: sqlite3.Row, status: str) -> list[str]:
    """How the cycle's work ended: its end reason (loop's). 0.14.0: a cycle without one that didn't complete ended
    before its work did (the budget guard stopped it, an error, a restart): that, and the plan's steps. It said "the
    plan was done". The code journal says it too."""
    reason = cycle["act_end_reason"] or ""
    if reason or status in ("completed", "idle"):
        return [f"Work ended: {ENDED.get(reason) or _flat(reason, 120)}."]
    where = (
        f" at work step {cycle['step']} of {cycle['max_steps']}" if cycle["phase"] == "act" and cycle["step"] else ""
    )
    lines = [f"Work ended: {status}{where}, before its plan was done."]
    steps = _steps(cycle)
    if steps:
        quoted = (f"{n}. {json.dumps(_flat(step, STEP_CHARS), ensure_ascii=False)}" for n, step in enumerate(steps, 1))
        lines.append(f"Its plan's steps (not all done): {'; '.join(quoted)}")
    return lines


def _steps(cycle: sqlite3.Row) -> list[str]:
    try:
        plan = json.loads(cycle["plan"]) if cycle["plan"] else {}
    except ValueError:
        return []
    steps = plan.get("steps") if isinstance(plan, dict) else None
    return [s for s in steps if isinstance(s, str)] if isinstance(steps, list) else []


def _early_journal(call: sqlite3.Row) -> bool:
    """0.14.0: a journal tried during the work. It is refused (the reflection's) and ends the work: the reflection
    writes it, so it isn't work left undone."""
    return bool(call["tool"] == "write_journal" and call["phase"] == "act")


def _reflection(conn: sqlite3.Connection, cycle_id: int) -> str:
    reflected = conn.execute(
        "SELECT 1 FROM llm_calls WHERE cycle_id = ? AND purpose = 'reflect' AND status IN ('ok', 'interrupted')",
        (cycle_id,),
    ).fetchone()
    author = conn.execute("SELECT author FROM journal WHERE cycle_id = ?", (cycle_id,)).fetchone()
    journal = "none yet" if author is None else "by Ember's code" if author["author"] == "system" else "by the agent"
    return f"Reflection: {'yes' if reflected else 'none'}; journal {journal}."


def undone_line(call: sqlite3.Row) -> str:
    target = _target(call["input"])
    status = "interrupted" if call["status"] == "started" else call["status"]
    why = _flat(call["summary"], REASON_CHARS)
    detail = f"{status}: {why}" if why and why != status else status
    return f"{_flat(call['tool'], TOOL_CHARS)}{' ' + target if target else ''} ({detail})"


def _done(call: sqlite3.Row) -> str:
    summary = _flat(call["summary"], REASON_CHARS)
    tool = _flat(call["tool"], TOOL_CHARS)
    return f"{tool} ({summary})" if summary else tool


def _target(raw: Any) -> str:
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in _TARGET_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"#{value}"
        if isinstance(value, str) and value.strip():
            text = _flat(value, TARGET_CHARS)
            return json.dumps(text, ensure_ascii=False) if key in ("title", "question") else text
    return ""


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:LISTED])
    return shown + (f"; and {len(items) - LISTED} more" if len(items) > LISTED else "")


def _flat(text: Any, chars: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= chars else flat[: chars - 1] + "…"
