"""What the agent sees when it wakes up: a compact, byte-budgeted picture.

Each section has a budget in UTF-8 bytes (measured on the JSON-escaped text,
which is what the budget guard's token count sees). Sections are cut at a line
boundary and marked, so the model knows something is missing. If a request
still doesn't fit the call profile the economy reserves money for, the context
is rebuilt with smaller budgets; a request never silently exceeds its profile.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..economy.costs import micros_to_usd
from ..economy.life import LifeStatus
from ..economy.metering import rough_token_count
from . import store
from .memory import Memory
from .news import News
from .sandbox import Jail
from .store import AgentScope

PLANNER_BUDGETS = {
    "status": 500,
    "news": 2_300,
    "software": 1_200,
    "projects": 2_000,
    "pending": 400,
    "strategy": 2_000,
    "identity": 600,
    "lessons": 1_300,
    "journal": 600,
    "workspace": 400,
}
BRIEF_BUDGET = 5_000
WILL_BUDGET = 4_500


def json_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def cut(text: str, budget: int) -> str:
    """At most ``budget`` bytes (JSON-escaped), cut at a line boundary with a marker."""
    if json_bytes(text) <= budget:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line])
        if json_bytes(candidate) > budget - 40:
            break
        kept.append(line)
    if not kept:  # one long line: cut it by characters
        chars = max(0, budget // 2 - 40)
        kept = [text[:chars]]
    removed = len(text.encode("utf-8")) - len("\n".join(kept).encode("utf-8"))
    return "\n".join(kept) + f"\n…[{removed} bytes cut]"


def fits(request: dict[str, Any], input_tokens: int) -> bool:
    return rough_token_count(request) <= input_tokens


@dataclass
class Snapshot:
    """Everything the context needs, read in one transaction."""

    status: LifeStatus
    local_time: str
    version: str
    agent_name: str
    today_spend: int
    daily_cap: float
    cycle_cap: float
    projects: list[sqlite3.Row] = field(default_factory=list)
    project_money: dict[int, tuple[int, int]] = field(default_factory=dict)
    owner_messages: list[sqlite3.Row] = field(default_factory=list)
    pending: list[sqlite3.Row] = field(default_factory=list)
    last_cycle: sqlite3.Row | None = None
    last_journal: sqlite3.Row | None = None
    memory: dict[str, str] = field(default_factory=dict)
    workspace: list[str] = field(default_factory=list)
    journal: list[sqlite3.Row] = field(default_factory=list)
    news: News = field(default_factory=News)


def snapshot(
    conn: sqlite3.Connection,
    scope: AgentScope,
    status: LifeStatus,
    memory: Memory,
    workspace: Jail,
    *,
    local_time: str,
    version: str,
    agent_name: str,
    today_spend: int,
    daily_cap: float,
    cycle_cap: float,
    news: News | None = None,
) -> Snapshot:
    projects = store.open_projects(conn, scope)
    money: dict[int, tuple[int, int]] = {}
    for p in projects:
        spent = conn.execute(
            "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
            " WHERE y.project_id = ?",
            (p["id"],),
        ).fetchone()[0]
        earned = conn.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'revenue' AND project_id = ?", (p["id"],)
        ).fetchone()[0]
        money[p["id"]] = (int(spent), int(earned))
    last_cycle = conn.execute(
        "SELECT * FROM cycles WHERE session = ? AND simulated = ? AND status <> 'running' ORDER BY id DESC LIMIT 1",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchone()
    journal = store.journal(conn, scope, 5)
    files = [f"{e.path}/" if e.is_dir else f"{e.path} ({e.size:,} B)" for e in _safe_listing(workspace)]
    return Snapshot(
        status=status,
        local_time=local_time,
        version=version,
        agent_name=agent_name,
        today_spend=today_spend,
        daily_cap=daily_cap,
        cycle_cap=cycle_cap,
        projects=projects,
        project_money=money,
        owner_messages=store.unseen(conn, "messages", scope, 8),
        pending=[r for r in store.queue(conn, "approvals", scope, 20) if r["status"] == "pending"],
        last_cycle=last_cycle,
        last_journal=journal[0] if journal else None,
        memory=memory.read_all(),
        workspace=files,
        journal=journal,
        news=news or News(),
    )


def _safe_listing(workspace: Jail) -> list[Any]:
    try:
        return workspace.listing()[:40]
    except Exception:  # noqa: BLE001 - the context must still be built
        return []


def status_text(s: Snapshot, dry_run: bool) -> str:
    st = s.status
    runway = f"{st.runway.days:.1f} days" if st.runway.days is not None else (st.runway.note or "unknown")
    lines = [
        f"Time: {s.local_time}. You are {s.agent_name}, version {s.version}."
        + (" DRY RUN (simulated money)." if dry_run else ""),
        f"State: {st.state}. Balance ${micros_to_usd(st.balance):.2f}. Runway {runway}.",
        f"Spent today ${micros_to_usd(s.today_spend):.2f} of ${s.daily_cap:.2f}."
        f" This cycle may spend up to ${s.cycle_cap:.2f}.",
    ]
    if st.last_will_due:
        lines.append("Your money is nearly gone: your last will is due.")
    return "\n".join(lines)


def project_lines(s: Snapshot) -> str:
    if not s.projects:
        return "No open projects."
    lines = []
    for p in s.projects[:8]:
        spent, earned = s.project_money.get(p["id"], (0, 0))
        lines.append(
            f"#{p['id']} [{p['status']}] {p['title']} · next: {p['next_step'] or '-'} · spent "
            f"${micros_to_usd(spent):.2f} · earned ${micros_to_usd(earned):.2f}"
        )
        lines.append(f"   hypothesis: {p['hypothesis']}")
    return "\n".join(lines)


def news_text(s: Snapshot) -> str:
    lines = []
    if s.last_cycle is not None:
        c = s.last_cycle
        lines.append(f"Last cycle #{c['id']} ended {c['status']}" + (f" ({c['note']})" if c["note"] else "") + ".")
    if s.last_journal is not None:
        lines.append(f"Your last journal summary: {s.last_journal['summary']}")
    lines.extend(s.news.approval_lines())
    for m in s.owner_messages:
        lines.append(f"Message from your owner ({m['created_at']}): {json.dumps(m['text'], ensure_ascii=False)}")
    lines.extend(s.news.upgrade_lines())
    return "\n".join(lines) or "Nothing new."


def planner_context(s: Snapshot, dry_run: bool, scale: float = 1.0) -> str:
    b = {k: int(v * scale) for k, v in PLANNER_BUDGETS.items()}
    pending = "\n".join(f"#{r['id']} {r['type']}: {r['title']}" for r in s.pending) or "None."
    parts = [
        ("STATUS", cut(status_text(s, dry_run), b["status"])),
        ("SINCE YOUR LAST WAKE", cut(news_text(s), b["news"])),
        *([("YOUR SOFTWARE", cut(s.news.changelog, b["software"]))] if s.news.changelog else []),
        ("OPEN PROJECTS", cut(project_lines(s), b["projects"])),
        ("WAITING FOR YOUR OWNER", cut(pending, b["pending"])),
        ("STRATEGY", cut(s.memory.get("strategy", ""), b["strategy"])),
        ("IDENTITY", cut(s.memory.get("identity", ""), b["identity"])),
        ("LESSONS (newest last)", cut(_newest_lines(s.memory.get("lessons", ""), b["lessons"]), b["lessons"])),
        ("WORKSPACE", cut("\n".join(s.workspace) or "Empty.", b["workspace"])),
        ("TASK", "Plan this wake cycle. Reply with the JSON plan only."),
    ]
    return "\n\n".join(f"== {title} ==\n{body}" for title, body in parts)


def brief(s: Snapshot, dry_run: bool, plan: dict[str, Any], focus: sqlite3.Row | None, max_steps: int) -> str:
    focus_text = "None."
    if focus is not None:
        focus_text = (
            f"Focus project: #{focus['id']} {focus['title']} [{focus['status']}]\n"
            f"Hypothesis: {focus['hypothesis']}\nNext step: {focus['next_step'] or '-'}\n"
            f"Notes: {focus['notes'][-600:] or '-'}"
        )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1))
    parts = [
        ("STATUS", status_text(s, dry_run)),
        ("PLAN", f"Goal: {plan.get('goal', '')}\n{steps}"),
        ("FOCUS", focus_text),
        ("LESSONS", _newest_lines(s.memory.get("lessons", ""), 800)),
        ("WORKSPACE", "\n".join(s.workspace[:20]) or "Empty."),
        ("LIMITS", f"At most {max_steps} steps this cycle and 4 tool calls per step. Stop when the goal is reached."),
    ]
    text = "\n\n".join(f"== {title} ==\n{body}" for title, body in parts)
    return cut(text, BRIEF_BUDGET)


def will_context(s: Snapshot, dry_run: bool) -> str:
    closed = "\n".join(f"- {j['summary']}" for j in reversed(s.journal)) or "No journal yet."
    parts = [
        ("STATUS", status_text(s, dry_run)),
        ("PROJECTS", project_lines(s)),
        ("RECENT JOURNAL", closed),
        ("LESSONS", _newest_lines(s.memory.get("lessons", ""), 1_200)),
        ("STRATEGY", s.memory.get("strategy", "")[:800]),
        ("TASK", "Write your last will now."),
    ]
    return cut("\n\n".join(f"== {title} ==\n{body}" for title, body in parts), WILL_BUDGET)


def _newest_lines(text: str, budget: int) -> str:
    """The newest lines of a file that fit the budget (lessons are appended at the end)."""
    lines = [line for line in text.splitlines() if line.strip()]
    kept: list[str] = []
    for line in reversed(lines):
        if json_bytes("\n".join([line, *kept])) > budget:
            break
        kept.insert(0, line)
    return "\n".join(kept)
