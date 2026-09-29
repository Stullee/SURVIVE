"""Dashboard data for the agent's sections: Now, Projects, Ventures, Roadmap, Activity, Mind, Workspace, the owner
queues and the owner's standing instructions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..economy.clock import to_iso
from ..economy.costs import micros_to_usd
from ..integrations import etsy, etsy_publisher, executor, mailstore, reddit
from . import review, roadmap, store, ventures
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
        "ventures_proposed": ventures.count(conn, scope, ("proposed",)),  # business cases waiting for the owner
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
        where, params = scope.where()
        venture_choices = [  # what revenue and expenses can belong to, besides projects (0.12.0)
            {"id": v["id"], "title": v["title"], "stage": v["stage"]}
            for v in conn.execute(
                f"SELECT id, title, stage FROM ventures WHERE {where} AND stage <> 'killed' ORDER BY id", params
            )
        ]
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
        stamp = _ventures_stamp(conn, scope, simulated)
        today = agent.clock.today()
        overdue = sum(
            1 for m in roadmap.open_milestones(conn, scope) if m["due"] < today.isoformat()
        )  # dates are YYYY-MM-DD
        roadmap_stamp = _roadmap_stamp(conn, scope, simulated, today.isoformat())
    return {
        "badges": counts,
        "now": now,
        "projects": projects,
        "venture_choices": venture_choices,
        "activity": activity,
        "mind": {**agent.memory_files(), "journal": journal, "reviews": reviews},
        "approvals": approvals,
        "inbox": inbox,
        "upgrades": upgrades,
        "instructions": instructions,
        "last_will": {"text": will["text"], "cut_off": bool(will["cut_off"])} if will else None,
        # The venture tree is loaded apart (api/ventures) while its tab is open: this changes whenever it does.
        "ventures_stamp": stamp,
        # So is the roadmap (api/roadmap); its tab shows how many milestones are overdue.
        "roadmap": {"stamp": roadmap_stamp, "overdue": overdue},
    }


def _roadmap_stamp(conn: sqlite3.Connection, scope: store.AgentScope, simulated: int, today: str) -> str:
    """Changes when the roadmap or its effort may have: a milestone changed, a wake cycle ended, or a new day began."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM milestones WHERE {where}", params
    ).fetchone()
    cycle = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM cycles WHERE simulated = ? AND session = ? AND status <> 'running'",
        (simulated, scope.session),
    ).fetchone()
    return f"{int(row[0])}|{row[1]}|{int(cycle[0])}|{today}"


def roadmap_view(agent: Agent) -> dict[str, Any]:
    """The Roadmap tab: every milestone (the newest 300) with its dates, horizon, links, effort, result and the
    owner's word, counted from the owner's today."""
    scope = agent.scope()
    simulated = 1 if agent.mode == "dry_run" else 0
    today = agent.clock.today()
    with agent.db.connection() as conn:
        rows = roadmap.all_milestones(conn, scope)
        effort = roadmap.effort(conn, scope)
        where, params = scope.where()
        ventured = {
            int(r["id"]): r["title"]
            for r in conn.execute(f"SELECT id, title FROM ventures WHERE {where}", params).fetchall()
        }
        projects = {
            int(r["id"]): r["title"]
            for r in conn.execute(f"SELECT id, title FROM projects WHERE {where}", params).fetchall()
        }
        stamp = _roadmap_stamp(conn, scope, simulated, today.isoformat())
        total = roadmap.count(conn, scope)
    items = []
    for m in rows:
        due = roadmap.parse_day(m["due"]) or today
        cycles, spent = effort.get(m["id"], (0, 0))
        items.append(
            {
                "id": m["id"],
                "parent_id": m["parent_id"],
                "venture_id": m["venture_id"],
                "venture_title": ventured.get(m["venture_id"]) if m["venture_id"] else None,
                "project_id": m["project_id"],
                "project_title": projects.get(m["project_id"]) if m["project_id"] else None,
                "title": m["title"],
                "measure": m["measure"],
                "first_due": m["first_due"],
                "due": m["due"],
                "moves": m["moves"],
                "status": m["status"],
                "horizon": roadmap.horizon(due, today) if m["status"] == "open" else m["status"],
                "days": (due - today).days,
                "result": m["result"],
                "closed_at": m["closed_at"],
                "notes": m["notes"],
                "created_by": m["created_by"],
                "entered_by": m["entered_by"],
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
                "cycles": cycles,
                "spent_usd": _usd(spent),
                "owner_action": m["owner_action"],
                "owner_comment": m["owner_comment"],
                "owner_at": m["owner_at"],
                "owner_by": m["owner_by"],
                "owner_version": m["owner_version"],
                "seen_by_agent": m["seen_cycle_id"] is not None,
                "simulated": m["mode"] == "dry_run",
            }
        )
    return {
        "mode": agent.mode,
        "today": today.isoformat(),
        "horizons": [
            {"key": roadmap.OVERDUE[0], "label": roadmap.OVERDUE[1]},
            *({"key": key, "label": label, "days": last} for key, label, last in roadmap.HORIZONS),
            {"key": roadmap.LATER[0], "label": roadmap.LATER[1]},
        ],
        "limits": {
            "title": roadmap.LIMITS["title"],
            "measure": roadmap.LIMITS["measure"],
            "comment": roadmap.LIMITS["comment"],
            "ahead_days": roadmap.AHEAD_DAYS,
            "open": roadmap.MAX_OPEN,
        },
        "items": items,
        "total": total,
        "stamp": stamp,
    }


def _ventures_stamp(conn: sqlite3.Connection, scope: store.AgentScope, simulated: int) -> str:
    """Changes when the tree or its money may have: a venture changed, or a wake cycle ended."""
    where, params = scope.where()
    tree = conn.execute(
        f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM ventures WHERE {where}", params
    ).fetchone()
    cycle = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM cycles WHERE simulated = ? AND session = ? AND status <> 'running'",
        (simulated, scope.session),
    ).fetchone()
    return f"{int(tree[0])}|{tree[1]}|{int(cycle[0])}"


def ventures_view(agent: Agent) -> dict[str, Any]:
    """The Ventures tab: the whole tree with every venture's scores, business case, money and the owner's word, the
    criteria that weigh them, and today's share of the spending."""
    scope = agent.scope()
    simulated = 1 if agent.mode == "dry_run" else 0
    workspace, _ = agent.roots()
    with agent.db.connection() as conn:
        rows = ventures.all_ventures(conn, scope)
        paid = ventures.money(conn, scope)
        spent, ventured = ventures.day_spend(conn, scope, agent.clock.today())
        linked: dict[int, list[dict[str, Any]]] = {}
        where, params = scope.where()
        for p in conn.execute(
            f"SELECT id, title, status, venture_id FROM projects WHERE {where} AND venture_id IS NOT NULL ORDER BY id",
            params,
        ):
            linked.setdefault(int(p["venture_id"]), []).append(
                {"id": p["id"], "title": p["title"], "status": p["status"]}
            )
        cycles = conn.execute(
            "SELECT COUNT(*) FROM cycles WHERE simulated = ? AND session = ? AND venture = 1",
            (simulated, scope.session),
        ).fetchone()[0]
        stamp = _ventures_stamp(conn, scope, simulated)
    items = []
    for v in rows:
        m = paid.get(v["id"], ventures.Money())
        file = ventures.file_of(v["id"], v["title"])
        try:
            size = workspace.size_of(file, "text")
        except SandboxError:
            size = None
        items.append(
            {
                "id": v["id"],
                "parent_id": v["parent_id"],
                "title": v["title"],
                "pitch": v["pitch"],
                "stage": v["stage"],
                "created_by": v["created_by"],
                "entered_by": v["entered_by"],
                "created_at": v["created_at"],
                "updated_at": v["updated_at"],
                "scores": {name: v[name] for name in ventures.SCORE_FIELDS},
                "scores_by": v["scores_by"],
                "weight": ventures.weight(v),
                "case": {name: v[name] for name in ventures.CASE_FIELDS},
                "missing": ventures.missing_case(v),
                "next_question": v["next_question"],
                "notes": v["notes"],
                "proposed_at": v["proposed_at"],
                "file": file,
                "file_bytes": size,
                "spent_usd": _usd(m.spent),
                "earned_usd": _usd(m.earned),
                "projects": linked.get(v["id"], []),
                "owner_action": v["owner_action"],
                "owner_comment": v["owner_comment"],
                "owner_at": v["owner_at"],
                "owner_by": v["owner_by"],
                "owner_version": v["owner_version"],
                "seen_by_agent": v["seen_cycle_id"] is not None,
                "simulated": v["mode"] == "dry_run",
            }
        )
    return {
        "mode": agent.mode,
        "share": agent.settings.venture_share,
        "today": {"spent_usd": _usd(spent), "ventures_usd": _usd(ventured)},
        "venture_cycles": int(cycles),
        "decide_usd": ventures.DECIDE_USD,
        "criteria": [
            {"name": c.name, "label": c.label, "good": c.good, "factor": c.factor, "low": c.low, "high": c.high}
            for c in ventures.SCORES
        ],
        "case": [{"name": name, "label": label} for name, label, _ in ventures.CASE],
        "limits": {"title": ventures.LIMITS["title"], "pitch": ventures.LIMITS["pitch"], "comment": 1_000},
        "items": items,
        "stamp": stamp,
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
        "venture_id": p["venture_id"],
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
        "ventures": r["ventures"],
        "roadmap": r["roadmap"],
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
