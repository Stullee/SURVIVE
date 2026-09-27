"""A plain-text report of the whole system, for the owner to copy when something looks wrong.

It collects what is needed to understand the app's behaviour from outside: the
version and options, the database, the economy (balances, caps, guard state),
lives, the ledger, the scheduler, recent wake cycles with every model call and
tool call, the agent's records, and recent warnings and errors. Secrets never
appear: the options are the public ones, and the whole text goes through the
same redaction as the logs.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .db import utcnow
from .economy.costs import micros_to_usd
from .economy.ledger import Scope
from .logging_setup import redact
from .version import app_version, build_id

if TYPE_CHECKING:
    from .state import AppState

MAX_REPORT_CHARS = 400_000
TABLES = (
    "ledger",
    "llm_calls",
    "cycles",
    "lives",
    "life_transitions",
    "tool_calls",
    "projects",
    "journal",
    "approvals",
    "messages",
    "upgrades",
    "memory_versions",
    "events",
)


def report(state: AppState) -> str:
    sections: list[tuple[str, Callable[[], str]]] = [
        ("SYSTEM", lambda: _system(state)),
        ("OPTIONS (public)", lambda: _json(state.loaded.settings.public_dict())),
        ("DATABASE", lambda: _database(state)),
        ("ECONOMY", lambda: _economy(state)),
        ("LIVES AND STATE CHANGES", lambda: _lives(state)),
        ("LEDGER (latest 40)", lambda: _ledger(state)),
        ("SCHEDULER", lambda: _scheduler(state)),
        ("WAKE CYCLES (latest 8, with every call and tool)", lambda: _cycles(state)),
        ("AGENT RECORDS", lambda: _agent(state)),
        ("META", lambda: _meta(state)),
        ("EVENTS (latest 80)", lambda: _events(state)),
    ]
    out = [f"Ember diagnostics, generated {utcnow()} (UTC)", "=" * 72]
    for title, build in sections:
        out.append(f"\n## {title}")
        try:
            out.append(build())
        except Exception as exc:  # noqa: BLE001 - a broken section must not hide the others
            out.append(f"(could not be collected: {type(exc).__name__}: {exc})")
    text = redact("\n".join(out))
    if len(text) > MAX_REPORT_CHARS:
        text = text[:MAX_REPORT_CHARS] + "\n… [report cut]"
    return text


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False, default=str, sort_keys=True)


def _rows(rows: list[Any], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(_cell(row[c]) for c in columns))
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).replace("\n", " ⏎ ")
    return text if len(text) <= 160 else text[:157] + "…"


def _system(state: AppState) -> str:
    info = state.system_info()
    info.pop("options", None)
    info.update(
        {
            "version": app_version(),
            "build": build_id(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "tz": os.environ.get("TZ", ""),
            "fake_scenario": os.environ.get("EMBER_FAKE_SCENARIO", "founder"),
            "scheduler_env": os.environ.get("EMBER_SCHEDULER", ""),
            "agent_error": getattr(state, "agent_error", None),
        }
    )
    return _json(info)


def _database(state: AppState) -> str:
    if state.db_error:
        return f"unavailable: {state.db_error}"
    lines = [f"file: {state.db.path} ({_size(state.db.path)})"]
    with state.db.connection() as conn:
        lines.append(f"schema version: {conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]}")
        for table in TABLES:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
            except Exception:  # noqa: BLE001
                count = "missing"
            lines.append(f"  {table}: {count}")
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    lines.append(f"quick_check: {integrity}")
    return "\n".join(lines)


def _size(path: Any) -> str:
    try:
        return f"{os.path.getsize(path) / 1024:.0f} KB"
    except OSError:
        return "?"


def _economy(state: AppState) -> str:
    economy = state.economy
    if economy is None:
        return f"not running: {state.economy_error}"
    status = economy.life.evaluate()
    scope = economy.life.scope()
    books = economy.books
    data = {
        "mode": economy.mode,
        "life": {
            "id": status.life_id,
            "state": status.state,
            "reason": status.reason,
            "born_at": status.born_at,
            "critical_since": status.critical_since,
            "last_will_due": status.last_will_due,
            "last_will_at": status.last_will_at,
        },
        "balance_usd": micros_to_usd(status.balance),
        "settled_balance_usd": micros_to_usd(status.settled_balance),
        "real_balance_usd": micros_to_usd(books.balance(Scope("live"))),
        "pending_usd": micros_to_usd(status.pending),
        "provisional_excess_usd": micros_to_usd(books.provisional_excess(scope)),
        "runway": {
            "days": status.runway.days,
            "note": status.runway.note,
            "window_spend_usd": micros_to_usd(status.runway.window_spend),
            "active_days": status.runway.active_days,
        },
        "today_cap_spend_usd": micros_to_usd(books.cap_spend_on(scope, economy.clock.today())),
        "today_net_api_usd": micros_to_usd(books.api_spend_on(scope, economy.clock.today())),
        "caps": {"daily_usd": economy.settings.daily_spend_cap_usd, "cycle_usd": economy.settings.cycle_spend_cap_usd},
        "revive": {
            "needed_usd": micros_to_usd(status.revive_needed or 0),
            "suggested_usd": micros_to_usd(status.revive_suggested or 0),
        }
        if status.revive_needed is not None
        else None,
        "totals": {k: micros_to_usd(v) for k, v in books.totals(scope).items()},
        "health": {"lock_held": economy.health.lock_held, "broken": economy.health.broken},
        "warnings": economy.warnings(),
        "session": {"dry_run_session": economy.life.session(), "sim_mark": scope.sim_mark},
    }
    return _json(data)


def _lives(state: AppState) -> str:
    with state.db.connection() as conn:
        lives = conn.execute("SELECT * FROM lives ORDER BY id DESC LIMIT 10").fetchall()
        transitions = conn.execute("SELECT * FROM life_transitions ORDER BY id DESC LIMIT 30").fetchall()
    return (
        _rows(
            lives,
            [
                "id",
                "mode",
                "born_at",
                "state",
                "state_reason",
                "ended_at",
                "end_reason",
                "critical_since",
                "last_will_due",
                "last_will_at",
                "revived_from_life_id",
            ],
        )
        + "\n\n"
        + _rows(transitions, ["id", "ts", "life_id", "mode", "from_state", "to_state", "reason"])
    )


def _ledger(state: AppState) -> str:
    with state.db.connection() as conn:
        rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT 40").fetchall()
    return _rows(
        rows,
        [
            "id",
            "ts",
            "occurred_on",
            "type",
            "amount_micros",
            "simulated",
            "source",
            "note",
            "llm_call_id",
            "corrects_id",
            "orig_amount",
            "orig_currency",
            "created_by",
            "entered_by",
        ],
    )


def _scheduler(state: AppState) -> str:
    scheduler = getattr(state, "scheduler", None)
    agent = getattr(state, "agent", None)
    data: dict[str, Any] = {
        "scheduler": None
        if scheduler is None
        else {"status": scheduler.status, "last_error": scheduler.last_error, "busy": scheduler.busy},
    }
    if agent is not None:
        data["agent"] = {
            **agent.agent_fields(),
            "wake_requested": agent.wake_requested,
            "running_cycle": agent.running_cycle,
            "transport": type(agent.transport).__name__,
        }
        decision = agent.decide()
        data["decision_now"] = {
            "run": decision.run,
            "trigger": decision.trigger,
            "reason": decision.reason,
            "wait_until": str(decision.wait_until) if decision.wait_until else None,
        }
    return _json(data)


def _cycles(state: AppState) -> str:
    out = []
    with state.db.connection() as conn:
        cycles = conn.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 8").fetchall()
        for c in cycles:
            out.append(
                f"### cycle #{c['id']} {c['status']} trigger={c['trigger']} simulated={c['simulated']} "
                f"session={c['session']} started={c['started_at']} ended={c['ended_at']} cap={c['cap_micros']}"
                f"\n    note={_cell(c['note'])} phase={c['phase']} step={c['step']}/{c['max_steps']} "
                f"act_end={_cell(c['act_end_reason'])} sleep={c['sleep_minutes']}"
                f"\n    plan={_cell(c['plan'])}"
            )
            calls = conn.execute("SELECT * FROM llm_calls WHERE cycle_id = ? ORDER BY id", (c["id"],)).fetchall()
            out.append(
                _rows(
                    calls,
                    [
                        "id",
                        "purpose",
                        "model",
                        "status",
                        "estimate_micros",
                        "cost_micros",
                        "floor_micros",
                        "billing_uncertain",
                        "input_tokens",
                        "output_tokens",
                        "cache_write_5m_tokens",
                        "cache_read_tokens",
                        "web_search_requests",
                        "stop_reason",
                        "guard_reason",
                        "error",
                        "request_id",
                    ],
                )
            )
            tools = conn.execute("SELECT * FROM tool_calls WHERE cycle_id = ? ORDER BY id", (c["id"],)).fetchall()
            out.append(_rows(tools, ["id", "llm_call_id", "seq", "phase", "tool", "status", "summary", "input"]))
    return "\n".join(out) or "(no cycles yet)"


def _agent(state: AppState) -> str:
    agent = getattr(state, "agent", None)
    if agent is None:
        return "agent not running"
    scope = agent.scope()
    where, params = scope.where()
    out = [f"scope: mode={scope.mode} session={scope.session} life={scope.life_id}"]
    with state.db.connection() as conn:
        for table, columns in (
            ("projects", ["id", "status", "title", "next_step", "updated_at"]),
            ("journal", ["cycle_id", "author", "summary"]),
            ("approvals", ["id", "status", "type", "title"]),
            ("messages", ["id", "sender", "read_at", "text"]),
            ("upgrades", ["id", "status", "priority", "title"]),
        ):
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT 15",
                params,  # noqa: S608 - fixed names
            ).fetchall()
            out.append(f"-- {table}\n" + _rows(rows, columns))
    for name, text in agent.memory_files().items():
        out.append(f"-- memory/{name}.md ({len(text.encode())} B)\n{text[:1500]}")
    workspace, _ = agent.roots()
    try:
        files, total = workspace.usage()
        listing = "\n".join(f"  {e.path}{'/' if e.is_dir else f' ({e.size} B)'}" for e in workspace.listing()[:50])
        out.append(f"-- workspace: {files} files, {total} B\n{listing}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"-- workspace: unreadable ({exc})")
    return "\n".join(out)


def _meta(state: AppState) -> str:
    with state.db.connection() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM meta ORDER BY key").fetchall()
    return _rows(rows, ["key", "value", "updated_at"])


def _events(state: AppState) -> str:
    rows = state.db.recent_events(limit=80, min_level="info")
    lines = []
    for e in rows:
        lines.append(f"{e['ts']} {e['level'].upper():7} {e['kind']}: {e['message']}")
        details = e.get("details") or {}
        if isinstance(details, dict) and details.get("traceback"):
            lines.append("    " + str(details["traceback"]).strip().replace("\n", "\n    ")[-2000:])
    return "\n".join(lines) or "(none)"
