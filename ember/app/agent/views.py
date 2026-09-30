"""Dashboard data for the agent's sections: Now, Projects, Ventures, Roadmap, Activity, Mind, Workspace, the owner
queues and the owner's standing instructions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..economy import burn
from ..economy.clock import to_iso
from ..economy.costs import micros_to_usd
from ..integrations import (
    connectors,
    etsy,
    etsy_publisher,
    executor,
    mailstore,
    pinterest,
    pinterest_publisher,
    printify,
    printify_publisher,
    qa,
    reddit,
)
from . import (
    audit,
    critic,
    desk,
    digest,
    evidence,
    knockouts,
    library,
    memory,
    metrics,
    never,
    policy,
    predictions,
    prompts,
    research_check,
    review,
    roadmap,
    store,
    ventures,
)
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


# 0.13.0: what the sensors' waiting_on_you adds up: everything that waits for the owner's decision or action.
WAITING_ON_YOU = (
    "approvals_pending",
    "approvals_todo",
    "inbox_unread",
    "upgrades_new",
    "ventures_proposed",
    "milestone_proposals",
)


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
        # 0.13.0: the agent's proposed dates for the owner's milestones, until the owner accepts or rejects them
        "milestone_proposals": store.count_rows(
            conn, "milestones", scope, "status = 'open' AND proposed_due IS NOT NULL"
        ),
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
                "handoff": j["handoff"],  # 0.12.0: what the next cycle should do first
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
                "expires_at": store.expires_at(r) if r["status"] == "pending" else None,  # 0.12.0
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
            for r in store.approvals_for_owner(conn, scope)
        ]
        where, params = scope.where()
        promised: dict[int, list[dict[str, Any]]] = {}  # 0.12.0: what a message of the agent's promised
        for o in conn.execute(
            f"SELECT * FROM obligations WHERE {where} AND kind = 'promise' ORDER BY id", params
        ).fetchall():
            promised.setdefault(int(o["message_id"]), []).append(
                {"id": o["id"], "what": o["what"], "due": o["due"], "status": o["status"], "result": o["result"]}
            )
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
                "promises": promised.get(int(r["id"]), []),
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
        open_milestones = roadmap.open_milestones(conn, scope)
        overdue = sum(1 for m in open_milestones if m["due"] < today.isoformat())  # dates are YYYY-MM-DD
        proposals = sum(1 for m in open_milestones if m["proposed_due"])  # the agent's dates for the owner's (0.12.0)
        roadmap_stamp = _roadmap_stamp(conn, scope, simulated, today.isoformat())
        library_stamp = _library_stamp(conn, scope)
        pinned = [{"id": r["id"], "text": r["text"], "created_at": r["created_at"]} for r in memory.pins(conn, scope)]
        models = _models(agent, conn, scope)
        audit_view = audit.view(conn, scope)
    return {
        "badges": counts,
        "now": now,
        "projects": projects,
        "venture_choices": venture_choices,
        "activity": activity,
        "mind": {**agent.memory_files(), "journal": journal, "reviews": reviews, "lesson_pins": pinned},
        "models": models,
        "approvals": approvals,
        "audit": audit_view,  # 0.13.0: what Ember's code did, the owner's Undo, the daily digest
        "inbox": inbox,
        "upgrades": upgrades,
        "instructions": instructions,
        "last_will": {"text": will["text"], "cut_off": bool(will["cut_off"])} if will else None,
        # The venture tree is loaded apart (api/ventures) while its tab is open: this changes whenever it does.
        "ventures_stamp": stamp,
        # So is the roadmap (api/roadmap); its tab shows how many milestones are overdue and how many proposed dates
        # wait for the owner.
        "roadmap": {"stamp": roadmap_stamp, "overdue": overdue, "proposals": proposals},
        # And the library (api/library, 0.12.0): this changes whenever a document or its study does.
        "library": {"stamp": library_stamp},
    }


def _roadmap_stamp(conn: sqlite3.Connection, scope: store.AgentScope, simulated: int, today: str) -> str:
    """Changes when the roadmap or its effort may have: a milestone changed, a wake cycle ended, a new day began, or
    (0.13.0) a prediction was made or settled."""
    where, params = scope.where()
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), '') || COALESCE(MAX(checked_at), '') FROM milestones"
        f" WHERE {where}",
        params,
    ).fetchone()
    cycle = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM cycles WHERE simulated = ? AND session = ? AND status <> 'running'",
        (simulated, scope.session),
    ).fetchone()
    called = conn.execute(
        f"SELECT COUNT(*), COALESCE(MAX(settled_at), '') FROM predictions WHERE {where}", params
    ).fetchone()
    granted = conn.execute(  # 0.13.0: the owner's unlocks and what they carried
        f"SELECT COUNT(*), (SELECT COUNT(*) FROM policy_uses) FROM policy_grants WHERE {where}", params
    ).fetchone()
    return (
        f"{int(row[0])}|{row[1]}|{int(cycle[0])}|{today}|{int(called[0])}|{called[1]}|{int(granted[0])}"
        f"|{int(granted[1])}"
    )


def _library_stamp(conn: sqlite3.Connection, scope: store.AgentScope) -> str:
    where, params = scope.where()
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(studied_parts), 0), COALESCE(SUM(study_failures), 0),"
        f" COALESCE(MAX(removed_at), ''), (SELECT COUNT(*) FROM learnings WHERE {where})"
        f" FROM library_documents WHERE {where}",
        (*params, *params),
    ).fetchone()
    return "|".join(str(v) for v in tuple(row))


def library_view(agent: Agent) -> dict[str, Any]:
    """The Library tab (0.12.0): the owner's documents, the newest first, with how far their study got and what it
    cost, and today's study budget. The texts and learnings load per document (``library_document``)."""
    scope = agent.scope()
    with agent.db.connection() as conn:
        rows = library.documents(conn, scope)
        counts = library.learning_counts(conn, scope)
        spent = library.study_spent(conn, scope, agent.clock.today())
        where, params = scope.where()
        titles = {
            table: {int(r[0]): r[1] for r in conn.execute(f"SELECT id, title FROM {table} WHERE {where}", params)}
            for table in ("ventures", "projects")
        }
        stamp = _library_stamp(conn, scope)
    items = [
        {
            "id": r["id"],
            "title": r["title"],
            "source": r["source"],
            "note": r["note"],
            "venture_id": r["venture_id"],
            "venture_title": titles["ventures"].get(r["venture_id"]) if r["venture_id"] else None,
            "project_id": r["project_id"],
            "project_title": titles["projects"].get(r["project_id"]) if r["project_id"] else None,
            "file_name": r["file_name"],
            "chars": r["chars"],
            "parts": r["parts"],
            "added_at": r["added_at"],
            "added_by": r["added_by"],
            "study": r["study"],
            "studied_parts": r["studied_parts"],
            "study_note": r["study_note"],
            "study_usd": _usd(r["study_micros"]),
            "summary": r["summary"],
            "studied_at": r["studied_at"],
            "learnings": counts.get(int(r["id"]), 0),
            "simulated": r["mode"] == "dry_run",
        }
        for r in rows
    ]
    return {
        "mode": agent.mode,
        "items": items,
        "stamp": stamp,
        "totals": {
            "documents": len(rows),
            "chars": sum(int(r["chars"]) for r in rows),
            "learnings": sum(counts.get(int(r["id"]), 0) for r in rows),
        },
        "study": {"budget_usd": agent.settings.library_study_usd_per_day, "spent_today_usd": _usd(spent)},
        "limits": {
            **library.LIMITS,
            "document_chars": library.DOCUMENT_CHARS,
            "library_chars": library.LIBRARY_CHARS,
            "documents": library.MAX_DOCUMENTS,
            "file_bytes": library.FILE_BYTES,
            "file_types": list(library.FILE_TYPES),
        },
    }


def library_document(agent: Agent, document_id: int) -> dict[str, Any] | None:
    """One document of the library with its text and what Ember learned from it (None: no such document)."""
    scope = agent.scope()
    with agent.db.connection() as conn:
        row = library.get(conn, scope, document_id)
        if row is None or row["removed_at"] is not None:
            return None
        text = library.full_text(conn, document_id)
        learned = library.learnings_of(conn, document_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "text": text,
        "learnings": [
            {"id": r["id"], "part": r["part"], "topic": r["topic"], "text": r["text"], "created_at": r["created_at"]}
            for r in learned
        ],
    }


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
        overhead = roadmap.overhead(conn, scope)
        digests = digest.newest_by(conn, scope, "milestone_id")  # 0.12.0
        odds = predictions.of_milestones(conn, [int(m["id"]) for m in rows])  # 0.13.0: the agent's odds on them
        record = predictions.calibration(conn, scope)
        # 0.13.0: the owner's unlocks (policy.py) on each open milestone, and the promotions Ember's code proposes
        autonomy = {
            int(m["id"]): policy.view(conn, scope, agent.clock, int(m["id"])) for m in rows if m["status"] == "open"
        }
        promotions = policy.suggestions(conn, scope, agent.clock)
    items = []
    for m in rows:
        due = roadmap.parse_day(m["due"]) or today
        called = odds.get(int(m["id"]))
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
                "closed_by": m["closed_by"],  # "agent": its word only, shown as self-reported (0.12.0)
                # 0.12.0: a metric Ember's code checks it by, its target and where it stands
                "metric": m["metric"],
                "checked": metrics.progress_text(m) or None,
                "notes": m["notes"],
                "created_by": m["created_by"],
                "entered_by": m["entered_by"],
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
                "cycles": cycles,
                "spent_usd": _usd(spent),  # 0.12.0: the work of its cycles; their plans are overhead
                "last_digest": digests.get(m["id"]),  # 0.12.0: the digest of the last cycle aimed at it
                # 0.12.0: what it may cost, and its wait
                "budget_usd": _usd(m["budget_micros"]) if m["budget_micros"] else None,
                "cash_eur": f"{m['cash_cents'] / 100:.2f}" if m["cash_cents"] else None,
                "owner_hours": m["owner_minutes"] / 60 if m["owner_minutes"] else None,
                "wait_for": m["wait_for"],
                "check_at": m["check_at"],
                "replaces_id": m["replaces_id"],  # 0.12.0: the dropped or missed milestone it takes the place of
                "waiting": roadmap.waiting(m, today) if m["status"] == "open" else False,
                "owner_action": m["owner_action"],
                "owner_comment": m["owner_comment"],
                "owner_at": m["owner_at"],
                "owner_by": m["owner_by"],
                "owner_version": m["owner_version"],
                "seen_by_agent": m["seen_cycle_id"] is not None,
                # 0.12.0: the agent's proposed date for the owner's milestone, until they accept or reject it
                "proposed_due": m["proposed_due"],
                "proposed_note": m["proposed_note"],
                "proposed_at": m["proposed_at"],
                # 0.13.0: the agent's odds that it is met by its first date, and how Ember's code settled them
                "prediction": _prediction(called),
                "autonomy": autonomy.get(int(m["id"])),  # 0.13.0: what the owner unlocked for it (open ones)
                "simulated": m["mode"] == "dry_run",
            }
        )
    return {
        "mode": agent.mode,
        "today": today.isoformat(),
        "forecasts": record or None,  # 0.13.0: the record of the agent's forecasts (predictions.calibration)
        "autonomy_levels": list(policy.LEVELS),  # 0.13.0
        "autonomy_suggestions": promotions,
        "overhead_usd": _usd(overhead),  # 0.12.0: plans, reviews, brainstorms and the rest no milestone is charged
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
            "owner_slots": roadmap.OWNER_SLOTS,
            "moves": roadmap.MAX_MOVES,
        },
        "items": items,
        "total": total,
        "stamp": stamp,
    }


def _ventures_stamp(conn: sqlite3.Connection, scope: store.AgentScope, simulated: int) -> str:
    """Changes when the tree or its money may have: a venture changed, a wake cycle ended, something was recorded in
    the ledger (0.12.0: its P&L), a claim was saved as evidence (0.12.0), a numeric case or a critique (0.13.0)."""
    where, params = scope.where()
    tree = conn.execute(
        f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM ventures WHERE {where}", params
    ).fetchone()
    cycle = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM cycles WHERE simulated = ? AND session = ? AND status <> 'running'",
        (simulated, scope.session),
    ).fetchone()
    booked = conn.execute("SELECT COALESCE(MAX(id), 0) FROM ledger").fetchone()
    claims = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM evidence WHERE {where}", params).fetchone()
    numbers = conn.execute(
        f"SELECT COALESCE(MAX(c.id), 0) FROM venture_cases c JOIN ventures ON ventures.id = c.venture_id WHERE {where}",
        params,
    ).fetchone()
    judged = conn.execute(
        f"SELECT COALESCE(MAX(k.id), 0) FROM venture_critiques k JOIN ventures ON ventures.id = k.venture_id"
        f" WHERE {where}",
        params,
    ).fetchone()
    picked = conn.execute("SELECT COALESCE(MAX(id), 0) FROM desk_picks").fetchone()  # 0.13.0
    return (
        f"{int(tree[0])}|{tree[1]}|{int(cycle[0])}|{int(booked[0])}|{int(claims[0])}|{int(numbers[0])}|{int(judged[0])}"
        f"|{int(picked[0])}"
    )


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
        digests = digest.newest_by(conn, scope, "venture_id")  # 0.12.0
        found = evidence.by_venture(conn, scope)  # 0.12.0
        cases = {v["id"]: ventures.latest_case(conn, v["id"]) for v in rows if v["cases"]}  # 0.13.0
        judged = {vid: (critic.latest(conn, vid), critic.failures(conn, vid)) for vid in cases}  # 0.13.0
        # 0.13.0: what rules out a venture with a case that isn't backed yet (the owner lifts or restores each)
        status = agent.economy.life.evaluate()
        net_days = status.runway.net_days
        knocked = {
            v["id"]: knockouts.check(conn, v, cash_eur=agent.settings.venture_cash_eur, net_days=net_days)
            for v in rows
            if v["cases"] and v["stage"] in ventures.EXPLORING
        }
        # 0.13.0: the decision desk: READY as a venture cycle would get it now, and what the last venture plans took
        mode = burn.peek(agent.db, status).mode
        ready_now = desk.ready(
            conn,
            scope,
            mode=mode,
            today=agent.clock.today(),
            cash_eur=agent.settings.venture_cash_eur,
            net_days=net_days,
        )
        picks = desk.recent(conn, scope, 8)
        week = desk.decided(conn, scope, to_iso(agent.clock.now() - timedelta(days=7)))
        record = predictions.calibration(conn, scope)  # 0.13.0: the forecasts' record
        sales = {  # 0.13.0: each backed venture's first sale, as its case predicted it (the newest)
            int(r["venture_id"]): r
            for r in conn.execute(
                "SELECT * FROM predictions WHERE mode = ? AND session = ? AND kind = 'first_sale' ORDER BY id",
                (scope.mode, scope.session),
            ).fetchall()
        }
    items = []
    for v in rows:
        m = paid.get(v["id"], ventures.Money())
        left = ventures.research_left(v)  # 0.12.0: None without a research budget
        parts = ventures.knowledge_parts(workspace, v["id"], v["title"])  # 0.12.0: the newest part
        file = parts[-1] if parts else ventures.file_of(v["id"], v["title"])
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
                "parked_by": v["parked_by"],  # who parked it (0.12.0): only the owner takes up what they parked
                # 0.12.0: when its stage last changed, its first test's milestone, and its stage's rule
                "stage_at": v["stage_at"],
                "test_milestone_id": v["test_milestone_id"],
                "stage_rule": ventures.stage_rule(v) or None,
                "created_by": v["created_by"],
                "entered_by": v["entered_by"],
                "created_at": v["created_at"],
                "updated_at": v["updated_at"],
                "scores": {name: v[name] for name in ventures.SCORE_FIELDS},
                "scores_by": v["scores_by"],
                "researched": ventures.researched(v),  # research calls for it that found something (0.12.0)
                # 0.12.0: what its research cost since its research budget began, and what is left (None: no budget)
                "research_spent_usd": _usd(int(v["research_spent"])),
                "research_left_usd": None if left is None else _usd(left),
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
                # 0.12.0: its P&L: revenue before refunds, the refunds, its expenses (Etsy's fees, say) and the net
                "revenue_usd": _usd(m.revenue),
                "refunds_usd": _usd(m.refunds),
                "expenses_usd": _usd(m.expenses),
                "net_usd": _usd(m.net),
                "last_digest": digests.get(v["id"]),  # 0.12.0: the digest of the last cycle aimed at it
                "numbers": _numbers(cases.get(v["id"])),  # 0.13.0: its newest numeric business case
                # 0.13.0: the critic's review of that case, and the expected net it ranks by (the lower of the two)
                "critique": _critique(*judged.get(v["id"], (None, 0))),
                "ranking_ev_eur": critic.ranking_ev(cases.get(v["id"]), judged.get(v["id"], (None, 0))[0]),
                "first_sale": _prediction(sales.get(v["id"])),  # 0.13.0: its case's first sale, settled by code
                "knockouts": [
                    {"rule": k.rule, "label": k.label, "why": k.why, "overridden": k.overridden}
                    for k in knocked.get(v["id"], [])
                ],
                # 0.12.0: its evidence: the claims by grade and the newest (see evidence.py)
                "evidence": found.get(v["id"], {"counts": dict.fromkeys(evidence.GRADES, 0), "items": []}),
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
        "research_budget_usd": ventures.RESEARCH_BUDGET_USD,  # 0.12.0: each venture's, while it isn't backed
        "research_to_propose": ventures.RESEARCH_TO_PROPOSE,
        "criteria": [
            {"name": c.name, "label": c.label, "good": c.good, "factor": c.factor, "low": c.low, "high": c.high}
            for c in ventures.SCORES
        ],
        "case": [{"name": name, "label": label} for name, label, _ in ventures.CASE],
        "limits": {"title": ventures.LIMITS["title"], "pitch": ventures.LIMITS["pitch"], "comment": 1_000},
        "items": items,
        "desk": {  # 0.13.0
            "mode": mode,
            "ready": [i.to_json() for i in ready_now],
            "picks": [
                {
                    "cycle_id": p["cycle_id"],
                    "created_at": p["created_at"],
                    "pick": p["pick"],
                    "venture_id": p["venture_id"],
                    "why_not": p["why_not"],
                    "shown": len(json.loads(p["items"])),
                }
                for p in picks
            ],
            "decided_week": week,
            "forecasts": record or None,  # 0.13.0
        },
        "stamp": stamp,
    }


def _prediction(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A prediction for the tabs (0.13.0): its odds, when it is due, and how Ember's code settled it."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "claim": row["claim"],
        "likely": round(float(row["probability"]) * 100),
        "due": row["due"],
        "status": row["status"],
        "result": row["result"],
        "settled_at": row["settled_at"],
    }


def _critique(row: sqlite3.Row | None, failed: int) -> dict[str, Any] | None:
    """The critic's review of a venture's newest case for its card (0.13.0): None before one (``failed``: how many
    attempts failed; after critic.MAX_ATTEMPTS the case goes without)."""
    if row is None:
        return {"failed": failed, "gave_up": failed >= critic.MAX_ATTEMPTS} if failed else None
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "created_at": row["created_at"],
        "verdict": row["verdict"],
        "fatal_flaw": row["fatal_flaw"],
        "change_mind": row["change_mind"],
        "price_eur": row["price_eur"],
        "unit_cost_eur": row["unit_cost_eur"],
        "monthly_costs_eur": row["monthly_costs_eur"],
        "sales": [row["sales_low"], row["sales_mid"], row["sales_high"]],
        "first_sale_months": row["first_sale_months"],
        "net_eur": row["net_eur"],
        "break_even": row["break_even"],
        "ev_eur": row["ev_eur"],
    }


def _numbers(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A venture's newest numeric business case for its card (0.13.0): the agent's numbers and Ember's code's."""
    if row is None:
        return None
    case, result = ventures.case_of(row)
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "channel": case.channel,
        "price_eur": case.price_eur,
        "unit_cost_eur": case.unit_cost_eur,
        "monthly_costs_eur": case.monthly_costs_eur,
        "sales": list(case.sales),
        "setup_eur": case.setup_eur,
        "owner_hours": case.owner_hours,
        "first_sale_months": case.first_sale_months,
        "api_usd": case.api_usd,
        "fees_eur": result.fees_eur,
        "net_eur": result.net_eur,
        "break_even": result.break_even,
        "net": list(result.net),
        "ev_eur": result.ev_eur,
        "ev_per_api_usd": result.ev_per_api_usd,
        "ev_per_hour": result.ev_per_hour,
        "text": result.text(case),
    }


def _never(conn: sqlite3.Connection, r: sqlite3.Row) -> list[str]:
    """0.13.0: why no unlock carries a waiting request that fits a rule (never.py), in the owner's words."""
    fits = conn.execute("SELECT 1 FROM policy_candidates WHERE approval_id = ?", (r["id"],)).fetchone()
    if r["status"] != "pending" or fits is None:
        return []
    return [never.CLASSES[k] for k in never.reasons(conn, r)]


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
    shortfalls: list[str] = []  # 0.13.0: what the QA registry finds short in it
    kind = connectors.class_of(r["executor"], action, r["type"])
    if r["executor"] == "email" and action is not None:
        shortfalls = qa.defects(kind.name, action)  # 0.13.0 (Phase E1): an answer's checks
    execution = executor.execution(conn, r, scope, agent.clock, agent.settings.email_daily_limit)
    if r["executor"] == "etsy_listing" and action is not None:
        execution = etsy_publisher.execution(conn, r, scope, agent.clock, agent.settings.etsy_listings_per_day)
        try:
            listing = etsy.listing_from_action(action)
            editable = etsy.editable(listing)
            shortfalls = qa.defects(kind.name, listing)
        except etsy.EtsyError:
            editable = None
    if r["executor"] in ("pinterest_pin", "pinterest_delete") and action is not None:  # 0.13.0 (Phase E2)
        execution = pinterest_publisher.execution(conn, r, scope, agent.clock, agent.settings.pinterest_pins_per_day)
        if r["executor"] == "pinterest_pin":
            try:
                shortfalls = qa.defects(kind.name, pinterest.pin_from_action(action))
            except pinterest.PinterestError:
                shortfalls = []
    if r["executor"] in ("printify_product", "printify_delete") and action is not None:  # 0.13.0 (Phase E4)
        daily = agent.settings.printify_products_per_day
        execution = printify_publisher.execution(conn, r, scope, agent.clock, daily)
        if r["executor"] == "printify_product":
            try:
                shortfalls = qa.defects(kind.name, printify.product_from_action(action))
            except printify.PrintifyError:
                shortfalls = []
    if r["executor"] == "etsy_edit" and action is not None:
        execution = etsy_publisher.edit_execution(conn, r)
        try:
            edit = etsy.edit_from_action(action)
            editable = etsy.edit_editable(edit)  # None: no words or price change
            shortfalls = qa.defects(kind.name, edit)
        except etsy.EtsyError:
            editable = None
    return {
        "executor": r["executor"],
        "action_class": kind.flags(),  # 0.13.0: the connector protocol's class and its flags
        "qa": shortfalls,
        "veto_until": policy.veto_until(conn, int(r["id"])),  # 0.13.0: held by the owner's unlock until then
        "never": _never(conn, r),
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
        "milestones": review.milestone_verdicts(r),  # 0.12.0: its verdicts on milestones, and what came of them
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
        "app_version": c["app_version"],  # which release ran it (0.12.0)
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
        written = conn.execute("SELECT text FROM cycle_digests WHERE cycle_id = ?", (cycle_id,)).fetchone()
    return {
        "cycle": summary,
        "digest": written["text"] if written else None,  # 0.12.0: what it did and didn't, from Ember's records
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
    tree = jail.walk(jail.limits.max_entries)
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


def _models(agent: Agent, conn: Any, scope: Any) -> dict[str, Any]:
    """0.12.0: which model does what, and where the research model's check stands (research_check)."""
    s = agent.settings
    research, check = s.worker_model, None
    if s.research_model and s.research_model != s.worker_model:
        state = research_check.check(conn, scope, s.research_model)
        research = s.research_model if state.passed else s.worker_model
        check = {
            "model": state.model,
            "compared": state.compared,
            "needed": research_check.QUESTIONS,
            "done": state.done,
            "passed": state.passed,
            "text": state.text(),
        }
    return {
        "planner": s.planner_model,
        "strategy": prompts.strategy_model(s),
        "worker": s.worker_model,
        "research": research,
        "research_check": check,
    }
