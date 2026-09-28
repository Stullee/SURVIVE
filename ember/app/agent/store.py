"""The agent's records in SQLite: projects, tool calls, journal, queue items.

Everything is scoped to a mode and a session (see migration 0003), so a dry run
never mixes with the live agent. Functions that change something take the
caller's connection and must run inside its transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

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
    allowed = {"phase", "step", "max_steps", "current_action", "plan", "project_id", "act_end_reason", "sleep_minutes"}
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
) -> int:
    cursor = conn.execute(
        "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
        " status, next_step) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, scope.life_id, cycle_id, now, now, title, hypothesis, status, next_step),
    )
    return int(cursor.lastrowid)


def update_project(conn: sqlite3.Connection, project_id: int, now: str, **columns: Any) -> None:
    allowed = {"status", "next_step", "hypothesis", "notes"}
    if set(columns) - allowed:
        raise ValueError("unknown project columns")
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now, project_id))


# --- journal and will ---


def write_journal(
    conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, author: str, summary: str, entry: str, now: str
) -> bool:
    """One entry per cycle; returns False if the cycle already has one."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO journal (mode, session, life_id, cycle_id, created_at, author, summary, entry)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, scope.life_id, cycle_id, now, author, summary[:240] or "-", entry[:2000]),
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
    if table not in {"approvals", "messages", "upgrades", "projects"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where} AND {condition}", params).fetchone()[0])


def insert_approval(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, **fields: Any) -> int:
    """A request for the owner; with ``executor`` and ``action`` (canonical JSON), one Ember's code carries out."""
    cursor = conn.execute(
        "INSERT INTO approvals (mode, session, life_id, cycle_id, project_id, created_at, type, title, description,"
        " payload, payload_sha256, expected_cost, expected_benefit, executor, action)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
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
        " expected_benefit, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    return int(cursor.lastrowid)


def queue(conn: sqlite3.Connection, table: str, scope: AgentScope, limit: int = 30) -> list[sqlite3.Row]:
    if table not in {"approvals", "messages", "upgrades"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


def unseen(conn: sqlite3.Connection, table: str, scope: AgentScope, limit: int = 10) -> list[sqlite3.Row]:
    """Queue items the agent hasn't been shown since they changed (phase 4 adds the owner's decisions)."""
    if table not in {"approvals", "messages", "upgrades"}:
        raise ValueError("unknown table")
    where, params = scope.where()
    condition = "sender = 'owner' AND seen_cycle_id IS NULL" if table == "messages" else "seen_cycle_id IS NULL"
    return conn.execute(
        f"SELECT * FROM {table} WHERE {where} AND {condition} ORDER BY id LIMIT ?", (*params, limit)
    ).fetchall()
