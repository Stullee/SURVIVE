"""Dashboard data for the agent's sections: Now, Projects, Activity, Mind, Workspace, the owner queues and the owner's
standing instructions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..economy.clock import to_iso
from ..economy.costs import micros_to_usd
from ..integrations import etsy, etsy_publisher, executor, mailstore, reddit
from . import review, store
from .sandbox import Entry, Jail, Missing, SandboxError, kind_of

if TYPE_CHECKING:
    from .service import Agent

ACTIVITY_CYCLES = 10
REVIEWS_SHOWN = 14  # the daily reviews of the last two weeks


class WorkspaceFileError(ValueError):
    """A workspace file the owner can't open; the message is safe to show."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _usd(micros: int | None) -> float:
    return micros_to_usd(int(micros or 0))


def badges(conn: sqlite3.Connection, scope: store.AgentScope) -> dict[str, int]:
    """What waits for the owner (also in the Home Assistant sensor, so counts only, no text)."""
    return {
        "approvals_pending": store.count_rows(conn, "approvals", scope, "status = 'pending'"),
        # Approved emails are sent by Ember itself, so they aren't the owner's to do.
        "approvals_todo": store.count_rows(
            conn,
            "approvals",
            scope,
            "status IN ('approved', 'approved_with_changes') AND (executor IS NULL OR executor <> 'email')",
        ),
        "inbox_unread": store.count_rows(conn, "messages", scope, "sender = 'agent' AND read_at IS NULL"),
        "upgrades_new": store.count_rows(conn, "upgrades", scope, "status = 'new'"),
    }


def dashboard(agent: Agent) -> dict[str, Any]:
    scope = agent.scope()
    simulated = 1 if agent.mode == "dry_run" else 0
    with agent.db.connection() as conn:
        latest = conn.execute(
            "SELECT * FROM cycles WHERE simulated = ? AND session = ? ORDER BY id DESC LIMIT 1",
            (simulated, scope.session),
        ).fetchone()
        now = _now(agent, conn, latest) if latest else None
        projects = [_project(conn, p) for p in store.all_projects(conn, scope)]
        cycles = conn.execute(
            "SELECT * FROM cycles WHERE simulated = ? AND session = ? ORDER BY id DESC LIMIT ?",
            (simulated, scope.session, ACTIVITY_CYCLES),
        ).fetchall()
        activity = [_activity(conn, c) for c in cycles]
        journal = [
            {
                "cycle_id": j["cycle_id"],
                "created_at": j["created_at"],
                "author": j["author"],
                "summary": j["summary"],
                "entry": j["entry"],
            }
            for j in store.journal(conn, scope, 20)
        ]
        reviews = [_review(conn, r) for r in review.recent(conn, scope, REVIEWS_SHOWN)]
        approvals = [
            {
                **_carried_out(agent, conn, scope, r),
                "id": r["id"],
                "created_at": r["created_at"],
                "type": r["type"],
                "title": r["title"],
                "description": r["description"],
                "payload": r["payload"],
                "expected_cost": r["expected_cost"],
                "expected_benefit": r["expected_benefit"],
                "status": r["status"],
                "project_id": r["project_id"],
                "version": r["version"],
                "decided_at": r["decided_at"],
                "decided_by": r["decided_by"],
                "decision_comment": r["decision_comment"],
                "final_payload": r["final_payload"],
                "closed_at": r["closed_at"],
                "result_note": r["result_note"],
                "result_link": r["result_link"],
                "seen_by_agent": r["seen_cycle_id"] is not None,
                "simulated": r["mode"] == "dry_run",
            }
            for r in store.queue(conn, "approvals", scope)
        ]
        inbox = [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "sender": r["sender"],
                "text": r["text"],
                "read_at": r["read_at"],
                "entered_by": r["entered_by"],
                "removed": r["removed_at"] is not None,
                "seen_by_agent": r["seen_cycle_id"] is not None,
                "answered_by": r["answered_by"],  # the agent's message that answered this one of the owner's
                "simulated": r["mode"] == "dry_run",
            }
            for r in store.queue(conn, "messages", scope)
        ]
        upgrades = [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "title": r["title"],
                "problem": r["problem"],
                "proposed_change": r["proposed_change"],
                "expected_benefit": r["expected_benefit"],
                "priority": r["priority"],
                "status": r["status"],
                "decided_at": r["decided_at"],
                "owner_note": r["owner_note"],
                "released_version": r["released_version"],
                "script_path": r["script_path"],
                "script_bytes": len((r["script_text"] or "").encode("utf-8")),
                "simulated": r["mode"] == "dry_run",
            }
            for r in store.queue(conn, "upgrades", scope)
        ]
        will = store.last_will(conn, scope.life_id) if scope.life_id else None
        counts = badges(conn, scope)
        instructions = store.instructions_json(store.standing_instructions(conn, scope))
    return {
        "badges": counts,
        "now": now,
        "projects": projects,
        "activity": activity,
        "mind": {**agent.memory_files(), "journal": journal, "reviews": reviews},
        "approvals": approvals,
        "inbox": inbox,
        "upgrades": upgrades,
        "instructions": instructions,
        "last_will": {"text": will["text"], "cut_off": bool(will["cut_off"])} if will else None,
    }


def _carried_out(agent: Agent, conn: sqlite3.Connection, scope: store.AgentScope, r: sqlite3.Row) -> dict[str, Any]:
    """How an approval is carried out: by Ember's code (an email), the owner's click (Reddit) or the owner."""
    action = None
    if r["action"]:
        try:
            parsed = json.loads(r["action"])
        except ValueError:
            parsed = None
        action = parsed if isinstance(parsed, dict) else None
    first_contact = False
    reddit_url = None
    if r["executor"] == "email" and action is not None:
        first_contact = not mailstore.has_written(conn, scope, str(action.get("to") or ""))
    if r["executor"] == "reddit_link" and action is not None:
        reddit_url = reddit.prefilled_url(action, r["final_payload"] or None)  # the owner's text, if they changed it
    editable = None
    execution = executor.execution(conn, r, scope, agent.clock, agent.settings.email_daily_limit)
    if r["executor"] == "etsy_listing" and action is not None:
        execution = etsy_publisher.execution(conn, r, scope, agent.clock, agent.settings.etsy_listings_per_day)
        try:
            editable = etsy.editable(etsy.listing_from_action(action))
        except etsy.EtsyError:
            editable = None
    if r["executor"] == "etsy_edit" and action is not None:
        execution = etsy_publisher.edit_execution(conn, r)
        try:
            editable = etsy.edit_editable(etsy.edit_from_action(action))  # None: no words or price change
        except etsy.EtsyError:
            editable = None
    return {
        "executor": r["executor"],
        "action": action,
        "first_contact": first_contact,
        "execution": execution,
        "reddit_url": reddit_url,
        "editable": editable,
        "closed_by": r["closed_by"],
    }


def _now(agent: Agent, conn: sqlite3.Connection, c: sqlite3.Row) -> dict[str, Any]:
    spent, pending = agent.economy.books.cycle_spend(c["id"])
    plan = json.loads(c["plan"]) if c["plan"] else None
    return {
        "cycle_id": c["id"],
        "running": c["status"] == "running",
        "trigger": c["trigger"],
        "status": c["status"],
        "note": c["note"],
        "started_at": c["started_at"],
        "ended_at": c["ended_at"],
        "phase": c["phase"] if c["status"] == "running" else None,
        "step": c["step"],
        "max_steps": c["max_steps"],
        "spent_usd": _usd(spent),
        "pending_usd": _usd(pending),
        "cycle_cap_usd": _usd(c["cap_micros"]),
        "plan": plan.get("goal") if plan else None,
        "plan_detail": {
            "assessment": plan.get("assessment", ""),
            "money_path": plan.get("money_path", ""),
            "steps": plan.get("steps", []),
        }
        if plan
        else None,
        "current_action": c["current_action"],
        "act_end_reason": c["act_end_reason"],
    }


def _project(conn: sqlite3.Connection, p: sqlite3.Row) -> dict[str, Any]:
    spent = conn.execute(
        "SELECT COALESCE(SUM(l.cost_micros), 0), COUNT(DISTINCT y.id) FROM llm_calls l JOIN cycles y"
        " ON y.id = l.cycle_id WHERE y.project_id = ?",
        (p["id"],),
    ).fetchone()
    earned = conn.execute(
        "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'revenue' AND project_id = ?", (p["id"],)
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE project_id = ? AND status = 'pending'", (p["id"],)
    ).fetchone()[0]
    return {
        "id": p["id"],
        "title": p["title"],
        "hypothesis": p["hypothesis"],
        "status": p["status"],
        "next_step": p["next_step"],
        "notes": p["notes"],
        "spent_usd": _usd(spent[0]),
        "earned_usd": _usd(earned),
        "cycles": int(spent[1]),
        "pending_approvals": int(pending),
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
    }


def _review(conn: sqlite3.Connection, r: sqlite3.Row) -> dict[str, Any]:
    verdicts = []
    for v in json.loads(r["verdicts"] or "[]"):
        project = conn.execute("SELECT title, status FROM projects WHERE id = ?", (v.get("project_id"),)).fetchone()
        verdicts.append(
            {
                "project_id": v.get("project_id"),
                "title": project["title"] if project else None,
                "status": project["status"] if project else None,
                "verdict": v.get("verdict"),
                "why": v.get("why"),
            }
        )
    return {
        "id": r["id"],
        "day": r["day"],
        "created_at": r["created_at"],
        "cycle_id": r["cycle_id"],
        "status": r["status"],
        "verdicts": verdicts,
        "working": r["working"],
        "not_working": r["not_working"],
        "owner_feedback": r["owner_feedback"],
        "lesson": r["lesson"],
        "focus": r["focus"],
        "note": r["note"],
        "scorecard": r["scorecard"],
    }


def _activity(conn: sqlite3.Connection, c: sqlite3.Row) -> dict[str, Any]:
    calls = conn.execute("SELECT * FROM llm_calls WHERE cycle_id = ? ORDER BY id", (c["id"],)).fetchall()
    tool_rows = conn.execute("SELECT * FROM tool_calls WHERE cycle_id = ? ORDER BY id", (c["id"],)).fetchall()
    by_call: dict[int, list[sqlite3.Row]] = {}
    for t in tool_rows:
        by_call.setdefault(t["llm_call_id"], []).append(t)
    steps: list[dict[str, Any]] = []
    cost = 0
    for call in calls:
        cost += call["cost_micros"]
        steps.append(_call_step(call))
        steps.extend(_tool_step(t) for t in by_call.get(call["id"], []))
    journal = conn.execute("SELECT summary FROM journal WHERE cycle_id = ?", (c["id"],)).fetchone()
    return {
        "cycle_id": c["id"],
        "trigger": c["trigger"],
        "status": c["status"],
        "note": c["note"],
        "started_at": c["started_at"],
        "ended_at": c["ended_at"],
        "summary": journal["summary"] if journal else None,
        "cost_usd": _usd(cost),
        "calls": len(calls),
        "tools": len(tool_rows),
        "steps": steps,
    }


def _call_step(call: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "llm",
        "id": call["id"],
        "purpose": call["purpose"],
        "model": call["model"],
        "status": call["status"],
        "cost_usd": _usd(call["cost_micros"]),
        "estimate_usd": _usd(call["estimate_micros"]),
        "input_tokens": call["input_tokens"],
        "output_tokens": call["output_tokens"],
        "cache_read_tokens": call["cache_read_tokens"],
        "stop_reason": call["stop_reason"],
        "guard_reason": call["guard_reason"],
        "error": call["error"],
    }


def _tool_step(t: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "tool",
        "id": t["id"],
        "name": t["tool"],
        "origin": t["origin"],
        "status": t["status"],
        "summary": t["summary"],
    }


def cycle_detail(agent: Agent, cycle_id: int) -> dict[str, Any] | None:
    simulated = 1 if agent.mode == "dry_run" else 0
    with agent.db.connection() as conn:
        c = conn.execute("SELECT * FROM cycles WHERE id = ? AND simulated = ?", (cycle_id, simulated)).fetchone()
        if c is None:
            return None
        calls = conn.execute(
            "SELECT l.*, t.text AS text FROM llm_calls l LEFT JOIN call_texts t ON t.llm_call_id = l.id"
            " WHERE l.cycle_id = ? ORDER BY l.id",
            (cycle_id,),
        ).fetchall()
        tool_rows = conn.execute("SELECT * FROM tool_calls WHERE cycle_id = ? ORDER BY id", (cycle_id,)).fetchall()
        summary = _activity(conn, c)
    return {
        "cycle": summary,
        "plan": json.loads(c["plan"]) if c["plan"] else None,
        "calls": [
            {
                "id": r["id"],
                "purpose": r["purpose"],
                "model": r["model"],
                "status": r["status"],
                "estimate_usd": _usd(r["estimate_micros"]),
                "cost_usd": _usd(r["cost_micros"]),
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cache_write_tokens": r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"],
                "cache_read_tokens": r["cache_read_tokens"],
                "stop_reason": r["stop_reason"],
                "guard_reason": r["guard_reason"],
                "error": r["error"],
                "request_id": r["request_id"],
                "text": r["text"],
            }
            for r in calls
        ],
        "tools": [
            {
                "id": t["id"],
                "llm_call_id": t["llm_call_id"],
                "seq": t["seq"],
                "phase": t["phase"],
                "origin": t["origin"],
                "name": t["tool"],
                "status": t["status"],
                "input": t["input"],
                "result": t["result"],
                "summary": t["summary"],
            }
            for t in tool_rows
        ],
    }


# --- workspace ---


PRODUCT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


def workspace(agent: Agent) -> dict[str, Any]:
    """The files in the agent's workspace, walked folder by folder through the jail (links are never listed)."""
    jail = agent.roots()[0]
    tree = jail.walk(jail.limits.max_files)
    files = [e for e in tree.files if _openable(jail, e.path)]
    text_bytes = sum(e.size for e in files if kind_of(e.path) == "text")
    return {
        "mode": agent.mode,
        "files": [
            {
                "path": e.path,
                "size": e.size,
                "kind": kind_of(e.path),
                "modified_at": to_iso(datetime.fromtimestamp(e.modified, UTC)),
            }
            for e in files
        ],
        "file_count": len(files),
        "total_bytes": sum(e.size for e in files),
        "text_bytes": text_bytes,
        "product_bytes": sum(e.size for e in files) - text_bytes,
        "truncated": tree.truncated,
    }


def _openable(jail: Jail, path: str) -> bool:
    """Only files the jail would read: plain names, the text and product extensions."""
    try:
        jail.parts(path, kinds="any")
    except SandboxError:
        return False
    return True


def workspace_file(agent: Agent, path: str) -> tuple[str, str]:
    """(file name, text) of one text file in the workspace, read through the jail."""
    jail = agent.roots()[0]
    try:
        if kind_of(path) == "product":
            raise WorkspaceFileError(f"{path} is a product file: open it with /api/workspace/product")
        parts = jail.parts(path)
        entry = _find(jail, parts)
        if entry is None:
            raise WorkspaceFileError(f"{'/'.join(parts)} doesn't exist", 404)
        if entry.is_dir:
            raise WorkspaceFileError(f"{entry.path} is a folder, not a file")
        if entry.size > jail.limits.max_file_bytes:
            raise WorkspaceFileError(
                f"{entry.path} is larger than {jail.limits.max_file_bytes // 1024} KB, so it can't be opened here"
            )
        return parts[-1], jail.read(entry.path)
    except Missing as exc:  # the agent deleted it (or its folder) while the owner looked
        raise WorkspaceFileError(str(exc), 404) from None
    except SandboxError as exc:
        raise WorkspaceFileError(str(exc)) from None


def workspace_product(agent: Agent, path: str) -> tuple[str, bytes, str]:
    """(file name, bytes, content type) of one product file (PDF, Word, Excel, PNG), read through the jail."""
    jail = agent.roots()[0]
    try:
        parts = jail.parts(path, kinds="product")
        entry = _find(jail, parts)
        if entry is None:
            raise WorkspaceFileError(f"{'/'.join(parts)} doesn't exist", 404)
        if entry.is_dir:
            raise WorkspaceFileError(f"{entry.path} is a folder, not a file")
        name = parts[-1]
        return name, jail.read_bytes(entry.path), PRODUCT_TYPES[name[name.rfind(".") :].lower()]
    except Missing as exc:
        raise WorkspaceFileError(str(exc), 404) from None
    except SandboxError as exc:
        raise WorkspaceFileError(str(exc)) from None


def upgrade_script(agent: Agent, upgrade_id: int) -> tuple[str, str] | None:
    """(file name, text) of the workshop script an upgrade request carries, or None."""
    scope = agent.scope()
    where, params = scope.where()
    with agent.db.connection() as conn:
        row = conn.execute(
            f"SELECT script_path, script_text FROM upgrades WHERE id = ? AND {where}", (upgrade_id, *params)
        ).fetchone()
    if row is None or row["script_text"] is None:
        return None
    return str(row["script_path"] or "script.py").rsplit("/", 1)[-1], str(row["script_text"])


def _find(jail: Jail, parts: list[str]) -> Entry | None:
    """The entry at ``parts``, looked up folder by folder in the jail's listings."""
    entry = None
    for depth in range(1, len(parts) + 1):
        if entry is not None and not entry.is_dir:
            return None
        wanted = "/".join(parts[:depth])
        entry = next((e for e in jail.listing("/".join(parts[: depth - 1])) if e.path == wanted), None)
        if entry is None:
            return None
    return entry
