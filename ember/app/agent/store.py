"""The agent's records in SQLite: projects, tool calls, journal, queue items, the owner's standing instructions.

Everything is scoped to a mode and a session (see migration 0003), so a dry run
never mixes with the live agent. Functions that change something take the
caller's connection and must run inside its transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..economy.clock import from_iso, to_iso
from . import never

OPEN_STATUSES = ("idea", "active", "waiting")
CLOSED_STATUSES = ("succeeded", "failed", "abandoned")


@dataclass(frozen=True)
class AgentScope:
    """Whose records: the mode, the dry-run session (0 when live) and the current life."""

    mode: str
    session: int
    life_id: int

    @property
    def simulated(self) -> bool:
        return self.mode == "dry_run"

    def where(self, alias: str = "") -> tuple[str, tuple[Any, ...]]:
        prefix = f"{alias}." if alias else ""
        return f"{prefix}mode = ? AND {prefix}session = ?", (self.mode, self.session)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- cycles ---


def update_cycle(conn: sqlite3.Connection, cycle_id: int, **columns: Any) -> None:
    """Progress of a running cycle (phase, step, current action, plan...). Ignored once it has ended."""
    allowed = {
        "phase",
        "step",
        "max_steps",
        "current_action",
        "plan",
        "project_id",
        "act_end_reason",
        "sleep_minutes",
        "venture",
        "venture_id",
        "milestone_id",
    }
    unknown = set(columns) - allowed
    if unknown:
        raise ValueError(f"unknown cycle columns {sorted(unknown)}")
    if not columns:
        return
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE cycles SET {sets} WHERE id = ? AND status = 'running'", (*columns.values(), cycle_id))


# --- tool calls ---


def next_seq(conn: sqlite3.Connection, llm_call_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tool_calls WHERE llm_call_id = ?", (llm_call_id,)).fetchone()
    return int(row[0]) + 1


def start_tool_call(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    llm_call_id: int,
    phase: str,
    tool: str,
    tool_use_id: str,
    tool_input: Any,
    now: str,
    origin: str = "local",
    parent_id: int | None = None,
    project_id: int | None = None,
) -> int:
    text = canonical(tool_input)
    if len(text) > 20_000:
        text = canonical({"truncated": text[:19_000]})
    cursor = conn.execute(
        "INSERT INTO tool_calls (cycle_id, llm_call_id, parent_id, seq, phase, origin, tool, tool_use_id, project_id,"
        " input, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)",
        (
            cycle_id,
            llm_call_id,
            parent_id,
            next_seq(conn, llm_call_id),
            phase,
            origin,
            tool[:64],
            tool_use_id[:100],
            project_id,
            text,
            now,
        ),
    )
    return int(cursor.lastrowid)


def finish_tool_call(
    conn: sqlite3.Connection, tool_call_id: int, status: str, summary: str, result: str, now: str
) -> None:
    conn.execute(
        "UPDATE tool_calls SET status = ?, summary = ?, result = ?, finished_at = ?"
        " WHERE id = ? AND status = 'started'",
        (status, summary[:300], result[:8000], now, tool_call_id),
    )


def interrupt_open_tool_calls(conn: sqlite3.Connection, now: str, cycle_id: int | None = None) -> int:
    """Tool calls left 'started' (a crash, or a cycle that was stopped)."""
    if cycle_id is None:
        sql = (
            "UPDATE tool_calls SET status = 'interrupted', finished_at = ? WHERE status = 'started'"
            " AND cycle_id IN (SELECT id FROM cycles WHERE status <> 'running')"
        )
        return conn.execute(sql, (now,)).rowcount
    return conn.execute(
        "UPDATE tool_calls SET status = 'interrupted', finished_at = ? WHERE status = 'started' AND cycle_id = ?",
        (now, cycle_id),
    ).rowcount


def count_tool_uses(conn: sqlite3.Connection, cycle_id: int, tool: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE cycle_id = ? AND tool = ? AND status = 'ok'", (cycle_id, tool)
    ).fetchone()
    return int(row[0])


def recent_research(conn: sqlite3.Connection, scope: AgentScope, limit: int = 5) -> list[sqlite3.Row]:
    """The last research calls that worked, newest first: cycle_id, input and result as the tool stored them."""
    return conn.execute(
        "SELECT t.cycle_id, t.input, t.result FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id"
        " WHERE c.session = ? AND c.simulated = ? AND t.tool = 'research' AND t.status = 'ok'"
        " ORDER BY t.id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, limit),
    ).fetchall()


# --- texts ---


def save_call_text(conn: sqlite3.Connection, llm_call_id: int, text: str, stop_details: Any = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO call_texts (llm_call_id, text, stop_details) VALUES (?, ?, ?)",
        (llm_call_id, text[:16_000], canonical(stop_details) if stop_details is not None else None),
    )


# --- projects ---


def project(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM projects WHERE id = ? AND {where}", (project_id, *params)).fetchone()


def open_projects(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM projects WHERE {where} AND status IN {OPEN_STATUSES} ORDER BY updated_at DESC, id DESC",
        params,
    ).fetchall()


def all_projects(conn: sqlite3.Connection, scope: AgentScope, limit: int = 50) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM projects WHERE {where} ORDER BY status IN {CLOSED_STATUSES}, updated_at DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()


def create_project(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    cycle_id: int,
    title: str,
    hypothesis: str,
    next_step: str,
    status: str,
    now: str,
    venture_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
        " status, next_step, venture_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            now,
            now,
            title,
            hypothesis,
            status,
            next_step,
            venture_id,
        ),
    )
    return int(cursor.lastrowid)


def update_project(conn: sqlite3.Connection, project_id: int, now: str, **columns: Any) -> None:
    allowed = {"status", "next_step", "hypothesis", "notes", "venture_id"}
    if set(columns) - allowed:
        raise ValueError("unknown project columns")
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now, project_id))


# --- journal and will ---


def write_journal(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    author: str,
    summary: str,
    entry: str,
    now: str,
    handoff: str = "",
) -> bool:
    """One entry per cycle; returns False if the cycle already has one. ``handoff``: what the next cycle should do
    first (0.12.0), for the next plan."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO journal (mode, session, life_id, cycle_id, created_at, author, summary, entry, handoff)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            now,
            author,
            summary[:240] or "-",
            entry[:2000],
            handoff[:400],
        ),
    )
    return cursor.rowcount == 1


def has_journal(conn: sqlite3.Connection, cycle_id: int) -> bool:
    return conn.execute("SELECT 1 FROM journal WHERE cycle_id = ?", (cycle_id,)).fetchone() is not None


def journal(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM journal WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


def save_last_will(
    conn: sqlite3.Connection, life_id: int, cycle_id: int, llm_call_id: int, text: str, cut_off: bool, now: str
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO last_wills (life_id, cycle_id, llm_call_id, created_at, text, cut_off)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (life_id, cycle_id, llm_call_id, now, text[:6000], 1 if cut_off else 0),
    )
    return cursor.rowcount == 1


def last_will(conn: sqlite3.Connection, life_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM last_wills WHERE life_id = ?", (life_id,)).fetchone()


# --- the owner queues (the owner's side arrives in phase 4) ---


def pending_approval_by_payload(conn: sqlite3.Connection, scope: AgentScope, payload_sha: str) -> int | None:
    where, params = scope.where()
    row = conn.execute(
        f"SELECT id FROM approvals WHERE {where} AND payload_sha256 = ? AND status = 'pending'", (*params, payload_sha)
    ).fetchone()
    return int(row[0]) if row else None


def count_rows(conn: sqlite3.Connection, table: str, scope: AgentScope, condition: str = "1") -> int:
    if table not in {"approvals", "messages", "upgrades", "projects", "milestones"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where} AND {condition}", params).fetchone()[0])


# 0.12.0: how many requests of each type may wait for the owner at once (one cap of 10 for all of them let waiting
# listings block an email reply), and after how many days a request the owner hasn't decided expires.
PENDING_CAPS = {"sell": 6, "contact": 5, "publish": 3, "create_account": 3, "spend_money": 3, "other": 4}
REQUEST_DAYS = {"contact": 7, "publish": 7, "spend_money": 14, "sell": 30, "create_account": 30, "other": 30}


def pending_of_type(conn: sqlite3.Connection, scope: AgentScope, kind: str) -> int:
    where, params = scope.where()
    row = conn.execute(
        f"SELECT COUNT(*) FROM approvals WHERE {where} AND status = 'pending' AND type = ?", (*params, kind)
    )
    return int(row.fetchone()[0])


def expires_at(row: Any) -> str:
    """When a pending request expires (0.12.0): REQUEST_DAYS after it was made."""
    return to_iso(from_iso(str(row["created_at"])) + timedelta(days=REQUEST_DAYS[row["type"]]))


def expire_requests(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[sqlite3.Row]:
    """The pending requests the owner didn't decide within their type's days, expired (0.12.0): news for the agent,
    like a decision, and no longer waiting. Returns them."""
    where, params = scope.where()
    pending = conn.execute(f"SELECT * FROM approvals WHERE {where} AND status = 'pending'", params).fetchall()
    expired = [r for r in pending if expires_at(r) <= now]
    for r in expired:
        conn.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ?, version = version + 1"
            " WHERE id = ? AND status = 'pending'",
            (now, r["id"]),
        )
    return expired


def withdraw_request(
    conn: sqlite3.Connection, scope: AgentScope, approval_id: int, reason: str, cycle_id: int, now: str
) -> None:
    """The agent takes back one of its pending requests (0.12.0), saying why: not news for it (it did it)."""
    conn.execute(
        "UPDATE approvals SET status = 'withdrawn', decided_at = ?, decision_comment = ?, seen_cycle_id = ?,"
        " version = version + 1 WHERE id = ? AND status = 'pending'",
        (now, reason, cycle_id, approval_id),
    )


def insert_approval(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, **fields: Any) -> int:
    """A request for the owner; with ``executor`` and ``action`` (canonical JSON), one Ember's code carries out. It
    names what it works for (0.12.0): its project's venture or the cycle's, and the cycle's focus milestone (0.14.0:
    an unlock goes by what the request acts on instead, policy.carrier)."""
    focus = conn.execute(
        "SELECT y.milestone_id, COALESCE((SELECT venture_id FROM projects WHERE id = ?), y.venture_id, p.venture_id)"
        " FROM cycles y LEFT JOIN projects p ON p.id = y.project_id WHERE y.id = ?",
        (fields.get("project_id"), cycle_id),
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO approvals (mode, session, life_id, cycle_id, project_id, created_at, type, title, description,"
        " payload, payload_sha256, expected_cost, expected_benefit, executor, action, milestone_id, venture_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            fields.get("project_id"),
            now,
            fields["type"],
            fields["title"],
            fields["description"],
            fields["payload"],
            sha256(fields["payload"]),
            fields["expected_cost"],
            fields["expected_benefit"],
            fields.get("executor"),
            fields.get("action"),
            focus[0] if focus else None,
            focus[1] if focus else None,
        ),
    )
    words = never.act_words(fields.get("executor"), fields.get("action"))
    if words is not None:  # 0.14.0: what it says, as NEVER reads it (the database can't normalise text)
        conn.execute("INSERT INTO act_words (approval_id, words) VALUES (?, ?)", (cursor.lastrowid, words))
    return int(cursor.lastrowid)


def insert_message(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int | None, text: str, now: str) -> int:
    cursor = conn.execute(
        "INSERT INTO messages (mode, session, life_id, created_at, sender, cycle_id, text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, scope.life_id, now, "agent" if cycle_id else "owner", cycle_id, text),
    )
    return int(cursor.lastrowid)


def insert_upgrade(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, **fields: Any) -> int:
    cursor = conn.execute(
        "INSERT INTO upgrades (mode, session, life_id, cycle_id, created_at, title, problem, proposed_change,"
        " expected_benefit, priority, script_path, script_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            now,
            fields["title"],
            fields["problem"],
            fields["proposed_change"],
            fields["expected_benefit"],
            fields["priority"],
            fields.get("script_path"),
            fields.get("script_text"),
        ),
    )
    return int(cursor.lastrowid)


# --- the workshop (0.7.0) ---


def insert_workshop_run(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, **fields: Any) -> int:
    cursor = conn.execute(
        "INSERT INTO workshop_runs (mode, session, life_id, cycle_id, created_at, task, script_used, script_path,"
        " inputs, outputs, refused, status, cost_micros, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            now,
            fields["task"],
            fields.get("script_used"),
            fields.get("script_path"),
            canonical(fields.get("inputs") or []),
            canonical(fields.get("outputs") or []),
            canonical(fields.get("refused") or []),
            fields["status"],
            int(fields.get("cost_micros") or 0),
            fields.get("summary") or "",
        ),
    )
    return int(cursor.lastrowid)


def workshop_runs(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM workshop_runs WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def proven_scripts(conn: sqlite3.Connection, scope: AgentScope, limit: int = 2) -> list[tuple[str, str]]:
    """Kept workshop scripts that proved useful and that the agent hasn't yet asked to have built in: (path, why).

    A script proves itself when it was run again (``script`` pointed at it), or when a file it made, or the script
    itself, is named in a request the owner approved. Asking for it (request_upgrade with workshop_script) ends it.
    """
    where, params = scope.where()
    runs = conn.execute(
        f"SELECT script_path, script_used, outputs FROM workshop_runs WHERE {where} AND status = 'ok' ORDER BY id",
        params,
    ).fetchall()
    uses: dict[str, int] = {}
    made: dict[str, set[str]] = {}
    for run in runs:
        outputs = {str(o.get("path")) for o in json.loads(run["outputs"] or "[]") if isinstance(o, dict)}
        if run["script_path"]:
            uses.setdefault(run["script_path"], 1)
            made.setdefault(run["script_path"], set()).update(outputs)
        if run["script_used"] and run["script_used"] in uses:
            uses[run["script_used"]] += 1
            made[run["script_used"]].update(outputs)
    if not uses:
        return []
    asked = {
        row[0]
        for row in conn.execute(f"SELECT script_path FROM upgrades WHERE {where} AND script_path IS NOT NULL", params)
    }
    approved = conn.execute(
        f"SELECT id, description, COALESCE(final_payload, payload) AS text FROM approvals WHERE {where}"
        " AND status IN ('approved', 'approved_with_changes', 'done') ORDER BY id DESC LIMIT 200",
        params,
    ).fetchall()
    found: list[tuple[str, str]] = []
    for path, count in uses.items():
        if path in asked:
            continue
        names = {path, *made.get(path, set())}
        requests = [r["id"] for r in approved if any(n in f"{r['description']}\n{r['text']}" for n in names)]
        why = []
        if count >= 2:
            why.append(f"run {count} times")
        if requests:
            why.append(f"its files are in approved request #{requests[0]}")
        if why:
            found.append((path, "; ".join(why)))
    return found[-limit:]


def workshop_runs_since(conn: sqlite3.Connection, scope: AgentScope, since: str) -> int:
    where, params = scope.where()
    row = conn.execute(f"SELECT COUNT(*) FROM workshop_runs WHERE {where} AND created_at >= ?", (*params, since))
    return int(row.fetchone()[0])


def approvals_for_owner(conn: sqlite3.Connection, scope: AgentScope, closed: int = 30) -> list[sqlite3.Row]:
    """The requests the owner's Approvals tab lists, newest first: every one still waiting or still to do, and the
    newest ``closed`` others (0.12.0: only the newest 30 of all were listed, so older open ones vanished)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND (status IN ('pending', 'approved', 'approved_with_changes')"
        f" OR id IN (SELECT id FROM approvals WHERE {where} AND status NOT IN ('pending', 'approved',"
        " 'approved_with_changes') ORDER BY id DESC LIMIT ?)) ORDER BY id DESC",
        (*params, *params, closed),
    ).fetchall()


def queue(conn: sqlite3.Connection, table: str, scope: AgentScope, limit: int = 30) -> list[sqlite3.Row]:
    if table not in {"approvals", "messages", "upgrades"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


def standing_instructions(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The owner's current standing instructions (the newest row; its text is empty once they were cleared)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM standing_instructions WHERE {where} ORDER BY id DESC LIMIT 1", params
    ).fetchone()


def instructions_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """The standing instructions as the dashboard shows them: None while there are none."""
    if row is None or not row["text"]:
        return None
    return {"text": row["text"], "updated_at": row["created_at"], "entered_by": row["entered_by"]}


def open_messages(conn: sqlite3.Connection, scope: AgentScope, limit: int = 8) -> list[sqlite3.Row]:
    """The owner's messages the agent hasn't answered yet (0.9.1): the new ones first, then the ones it was shown
    without answering them, each oldest first, so a new message gets the room to be read whole. A message whose text
    the owner removed stays only until the agent has seen it."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
        " AND (removed_at IS NULL OR seen_cycle_id IS NULL) ORDER BY seen_cycle_id IS NOT NULL, id LIMIT ?",
        (*params, limit),
    ).fetchall()


def answerable(conn: sqlite3.Connection, scope: AgentScope, ids: list[int]) -> bool:
    """0.12.0: whether any of ``ids`` is an open message of the owner's the agent was shown (one ``mark_answered``
    would mark)."""
    if not ids:
        return False
    where, params = scope.where()
    marks = ", ".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT 1 FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
        f" AND seen_cycle_id IS NOT NULL AND id IN ({marks}) LIMIT 1",
        (*params, *ids),
    ).fetchone()
    return found is not None


def mark_answered(conn: sqlite3.Connection, scope: AgentScope, ids: list[int], answer_id: int) -> list[int]:
    """Record the agent's message ``answer_id`` as the answer to the owner's messages ``ids`` it was shown; returns
    the ones marked (the others aren't open messages of the owner's that the agent has seen)."""
    where, params = scope.where()
    marked = []
    for message_id in ids:
        cursor = conn.execute(
            f"UPDATE messages SET answered_by = ? WHERE id = ? AND {where} AND sender = 'owner'"
            " AND answered_by IS NULL AND seen_cycle_id IS NOT NULL",
            (answer_id, message_id, *params),
        )
        if cursor.rowcount:
            marked.append(message_id)
    return marked


def unseen(conn: sqlite3.Connection, table: str, scope: AgentScope, limit: int = 10) -> list[sqlite3.Row]:
    """Queue items the agent hasn't been shown since they changed (phase 4 adds the owner's decisions)."""
    if table not in {"approvals", "messages", "upgrades"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    condition = "sender = 'owner' AND seen_cycle_id IS NULL" if table == "messages" else "seen_cycle_id IS NULL"
    return conn.execute(
        f"SELECT * FROM {table} WHERE {where} AND {condition} ORDER BY id LIMIT ?", (*params, limit)
    ).fetchall()
