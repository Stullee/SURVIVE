"""The daily business review (0.7.1): once a day, before the first plan, the agent judges its own numbers.

Ember's code builds the scorecard from its records (the ledger, the wake cycles and their calls, projects, the
owner's decisions, the workshop), so its facts can't be argued with. The planner's model reads it and decides for
each project: continue, change or stop, and says what works, what doesn't, what the owner's decisions tell it, one
lesson and today's focus. The review is kept (``reviews``), shown in every plan of the day and on the dashboard, and
the next scorecard holds the agent to its verdicts.

A review is due at the first wake cycle of a local day, once there is a completed cycle from an earlier day. It is
one call on the planner's model that counts toward the daily cap, not the cycle cap. A review the budget can't
cover now is tried again at the next cycle; after MAX_ATTEMPTS failed reviews a day, the day goes without one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..economy.clock import Clock, from_iso, to_iso
from ..economy.costs import micros_to_usd
from ..economy.ledger import Books, Scope
from ..economy.life import LifeStatus
from .store import CLOSED_STATUSES, OPEN_STATUSES, AgentScope

WINDOW_DAYS = 7
SCORECARD_MAX = 9_000  # characters: the review call's profile (pricing.REVIEW) is measured with a full scorecard
MAX_PROJECTS = 8
MAX_DECISIONS = 6
MAX_SALES = 6  # revenue entries listed in the scorecard
MAX_ATTEMPTS = 2  # reviews a day, failed ones included
VERDICTS = ("continue", "change", "stop")
# The dashboard's and the database's limits for the review's texts.
LIMITS = {"working": 600, "not_working": 600, "owner_feedback": 600, "lesson": 400, "focus": 400}
WHY_CHARS = 200
# Where the money went, by call purpose.
_PURPOSES = {
    "plan": "planning and work",
    "work": "planning and work",
    "reflect": "planning and work",
    "research": "research",
    "workshop": "the workshop",
    "review": "reviews",
    "last_will": "the last will",
}
_DECIDED = ("approved", "approved_with_changes", "rejected", "withdrawn", "expired")
_PRODUCT_TOOLS = {
    "make_document": "documents",
    "make_spreadsheet": "spreadsheets",
    "make_image": "listing photos",
    "workshop": "workshop runs that kept files",
}


@dataclass
class Scorecard:
    text: str
    project_ids: set[int] = field(default_factory=set)


@dataclass
class Verdict:
    project_id: int
    verdict: str
    why: str


@dataclass
class Review:
    verdicts: list[Verdict]
    working: str
    not_working: str
    owner_feedback: str
    lesson: str
    focus: str


def due(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> bool:
    """Is a review due now: none done today yet (or failed too often), and a completed cycle from an earlier day?"""
    today = clock.today()
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT status FROM reviews WHERE {where} AND day = ?", (*params, today.isoformat())
    ).fetchall()
    if any(r["status"] == "ok" for r in rows) or len(rows) >= MAX_ATTEMPTS:
        return False
    earlier = conn.execute(
        "SELECT 1 FROM cycles WHERE session = ? AND simulated = ? AND status = 'completed' AND started_at < ? LIMIT 1",
        (scope.session, 1 if scope.simulated else 0, to_iso(clock.day_start(today))),
    ).fetchone()
    return earlier is not None


def of_day(conn: sqlite3.Connection, scope: AgentScope, day: date) -> sqlite3.Row | None:
    """The day's review, if it was made."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM reviews WHERE {where} AND day = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (*params, day.isoformat()),
    ).fetchone()


def recent(conn: sqlite3.Connection, scope: AgentScope, limit: int = 14) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM reviews WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


# --- the scorecard ---


def scorecard(
    conn: sqlite3.Connection,
    scope: AgentScope,
    clock: Clock,
    books: Books,
    ledger_scope: Scope,
    status: LifeStatus,
    *,
    dry_run: bool,
) -> Scorecard:
    """The facts of the last WINDOW_DAYS days and today so far, as text for the review call."""
    today = clock.today()
    first = today - timedelta(days=WINDOW_DAYS)
    since = to_iso(clock.day_start(first))
    projects = _projects(conn, scope, since)
    parts = [  # the most important first: a scorecard over its limit loses its end
        _header(first, today, dry_run),
        _money(conn, scope, books, ledger_scope, status, since),
        _last_review(conn, scope, today),
        _project_lines(conn, projects, clock, since),
        _decisions(conn, scope, since),
        _cycles(conn, scope, since),
        _upgrades(conn, scope, since),
    ]
    text = "\n\n".join(p for p in parts if p)
    if len(text) > SCORECARD_MAX:
        text = text[: SCORECARD_MAX - 20].rstrip() + "\n[scorecard cut]"
    return Scorecard(text, {int(p["id"]) for p in projects})


def _header(first: date, today: date, dry_run: bool) -> str:
    period = f"{first:%a %Y-%m-%d} to {today - timedelta(days=1):%a %Y-%m-%d}, and today ({today:%a %Y-%m-%d}) so far"
    lines = [
        "YOUR NUMBERS (from Ember's records: exact)",
        f"Period: the last {WINDOW_DAYS} days, {period}.",
    ]
    if dry_run:
        lines.append("DRY RUN: the money is simulated.")
    return "\n".join(lines)


def _money(
    conn: sqlite3.Connection,
    scope: AgentScope,
    books: Books,
    ledger_scope: Scope,
    status: LifeStatus,
    since: str,
) -> str:
    series = books.day_series(ledger_scope, days=WINDOW_DAYS + 2)  # from the day before the period to today
    before, days, today = series[0], series[1:-1], series[-1]
    spent = sum(d["api_cost_usd"] for d in days) + today["api_cost_usd"]
    by_purpose: dict[str, int] = {}
    for row in conn.execute(
        "SELECT c.purpose, COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
        " WHERE y.session = ? AND y.simulated = ? AND c.ts >= ? GROUP BY c.purpose",
        (scope.session, 1 if scope.simulated else 0, since),
    ):
        label = _PURPOSES.get(row[0], row[0])
        by_purpose[label] = by_purpose.get(label, 0) + int(row[1])
    split = ", ".join(f"{label} ${micros_to_usd(m):.2f}" for label, m in sorted(by_purpose.items(), key=_largest) if m)
    per_day = " · ".join(f"{date.fromisoformat(d['date']):%a} ${d['api_cost_usd']:.2f}" for d in days)
    average = sum(d["api_cost_usd"] for d in days) / WINDOW_DAYS
    revenue = sum(d["revenue_usd"] for d in days) + today["revenue_usd"]
    expenses = sum(d["expense_usd"] for d in days) + today["expense_usd"]
    total_revenue = micros_to_usd(books.totals(ledger_scope)["revenue"])
    # Revenue isn't tied to a project in the ledger: the entries' sources say what sold.
    where, params = ledger_scope.where()
    sales = conn.execute(
        f"SELECT occurred_on, amount_micros, source, note FROM ledger WHERE type = 'revenue' AND occurred_on >= ?"
        f" AND {where} ORDER BY occurred_on DESC, id DESC LIMIT ?",
        (days[0]["date"], *params, MAX_SALES),
    ).fetchall()
    runway = f"{status.runway.days:.1f} days" if status.runway.days is not None else (status.runway.note or "unknown")
    return "\n".join(
        [
            "MONEY",
            f"API calls: ${spent:.2f} in the period" + (f" ({split})" if split else "") + ".",
            f"Per day: {per_day} · today so far ${today['api_cost_usd']:.2f} (on average ${average:.2f} a full day).",
            f"Revenue your owner recorded: ${revenue:.2f} in these days, ${total_revenue:.2f} in all."
            f" Expenses your owner paid for you: ${expenses:.2f}.",
            *(
                f"- {s['occurred_on']}: ${micros_to_usd(s['amount_micros']):.2f} from"
                f" {_one_line(s['source'] or s['note'] or 'an unnamed source', 120)}"
                for s in sales
            ),
            f"Balance ${micros_to_usd(status.balance):.2f} (${before['balance_usd']:.2f} when the period began)."
            f" Runway {runway}.",
        ]
    )


def _largest(item: tuple[str, int]) -> tuple[int, str]:
    return -item[1], item[0]


def _cycles(conn: sqlite3.Connection, scope: AgentScope, since: str) -> str:
    session = (scope.session, 1 if scope.simulated else 0)
    statuses = conn.execute(
        "SELECT status, COUNT(*) FROM cycles WHERE session = ? AND simulated = ? AND started_at >= ?"
        " AND status <> 'running' GROUP BY status ORDER BY COUNT(*) DESC",
        (*session, since),
    ).fetchall()
    count = sum(int(r[1]) for r in statuses)
    tools = conn.execute(
        "SELECT t.tool, t.status FROM tool_calls t JOIN cycles y ON y.id = t.cycle_id"
        " WHERE y.session = ? AND y.simulated = ? AND t.started_at >= ?",
        (*session, since),
    ).fetchall()
    errors = sum(1 for t in tools if t["status"] == "error")
    made: dict[str, int] = {}
    for t in tools:
        label = _PRODUCT_TOOLS.get(t["tool"])
        if label and t["status"] == "ok":
            made[label] = made.get(label, 0) + 1
    where, params = scope.where()
    runs = conn.execute(
        f"SELECT status, COUNT(*), COALESCE(SUM(cost_micros), 0) FROM workshop_runs WHERE {where}"
        " AND created_at >= ? GROUP BY status",
        (*params, since),
    ).fetchall()
    lines = ["WAKE CYCLES"]
    if not count:
        lines.append("No wake cycles in these days.")
    else:
        split = ", ".join(f"{int(r[1])} {r[0]}" for r in statuses)
        lines.append(f"{count} cycles ({split}); {len(tools)} tool calls, {errors} of them refused or failed.")
    lines.append(
        "Made: " + (", ".join(f"{n} {label}" for label, n in made.items()) if made else "no finished products") + "."
    )
    if runs:
        total = sum(int(r[1]) for r in runs)
        cost = sum(int(r[2]) for r in runs)
        split = ", ".join(f"{int(r[1])} {r[0]}" for r in runs)
        lines.append(f"Workshop: {total} runs ({split}), ${micros_to_usd(cost):.2f}.")
    return "\n".join(lines)


def _projects(conn: sqlite3.Connection, scope: AgentScope, since: str) -> list[sqlite3.Row]:
    """Open projects, most recently updated first, then those closed in the period."""
    where, params = scope.where()
    open_ = conn.execute(
        f"SELECT * FROM projects WHERE {where} AND status IN {OPEN_STATUSES} ORDER BY updated_at DESC, id DESC",
        params,
    ).fetchall()
    closed = conn.execute(
        f"SELECT * FROM projects WHERE {where} AND status IN {CLOSED_STATUSES} AND updated_at >= ?"
        " ORDER BY updated_at DESC, id DESC",
        (*params, since),
    ).fetchall()
    return [*open_, *closed][:MAX_PROJECTS]


def _project_lines(conn: sqlite3.Connection, projects: list[sqlite3.Row], clock: Clock, since: str) -> str:
    if not projects:
        return "PROJECTS\nNo projects: nothing is being tried."
    lines = ["PROJECTS (open ones, then those closed in the period)"]
    now = clock.now()
    for p in projects:
        pid = p["id"]
        cycles = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0), MAX(started_at)"
            " FROM cycles WHERE project_id = ?",
            (since, pid),
        ).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(c.cost_micros), 0),"
            " COALESCE(SUM(CASE WHEN c.ts >= ? THEN c.cost_micros ELSE 0 END), 0)"
            " FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id WHERE y.project_id = ?",
            (since, pid),
        ).fetchone()
        asked = conn.execute(
            "SELECT status, COUNT(*) FROM approvals WHERE project_id = ? GROUP BY status", (pid,)
        ).fetchall()
        age = _days(now, p["created_at"])
        state = p["status"] if p["status"] in OPEN_STATUSES else f"{p['status']} {p['updated_at'][:10]}"
        last = f"last worked on {cycles[2][:10]}" if cycles[2] else "never worked on in a cycle"
        requests = sum(int(r[1]) for r in asked)
        split = ", ".join(f"{int(r[1])} {r[0].replace('_', ' ')}" for r in asked)
        lines.append(
            f"#{pid} [{state}] {p['title']} · open {age} · {int(cycles[1])} cycles in the period ({int(cycles[0])} in"
            f" all) · spent ${micros_to_usd(int(spent[1])):.2f} in the period, ${micros_to_usd(int(spent[0])):.2f} in"
            f" all · requests to your owner: {requests}" + (f" ({split})" if split else "") + f" · {last}"
        )
        lines.append(f"   hypothesis: {_one_line(p['hypothesis'], 200)}")
        if p["next_step"]:
            lines.append(f"   next step: {_one_line(p['next_step'], 150)}")
    return "\n".join(lines)


def _days(now: datetime, since: str) -> str:
    days = max(0, (now - from_iso(since)).days)
    return "less than a day" if days == 0 else f"{days} day{'s' if days != 1 else ''}"


def _decisions(conn: sqlite3.Connection, scope: AgentScope, since: str) -> str:
    where, params = scope.where()
    decided = conn.execute(
        f"SELECT id, type, title, status, decision_comment, result_note, decided_at, closed_at FROM approvals"
        f" WHERE {where} AND (decided_at >= ? OR closed_at >= ?) ORDER BY COALESCE(closed_at, decided_at) DESC",
        (*params, since, since),
    ).fetchall()
    waiting = conn.execute(f"SELECT COUNT(*) FROM approvals WHERE {where} AND status = 'pending'", params).fetchone()[0]
    lines = ["YOUR OWNER'S DECISIONS IN THE PERIOD"]
    if not decided:
        lines.append(f"None. Requests still waiting for your owner: {int(waiting)}.")
        return "\n".join(lines)
    counts: dict[str, int] = {}
    for d in decided:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    split = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in counts.items())
    lines.append(f"{split}; still waiting: {int(waiting)}.")
    for d in decided[:MAX_DECISIONS]:
        said = d["result_note"] if d["status"] in ("done", "failed") else d["decision_comment"]
        quote = f': "{_one_line(said, 200)}"' if said else ""
        lines.append(f'- #{d["id"]} {d["status"].replace("_", " ")} ({d["type"]}) "{_one_line(d["title"], 80)}"{quote}')
    return "\n".join(lines)


def _upgrades(conn: sqlite3.Connection, scope: AgentScope, since: str) -> str:
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT id, title, status FROM upgrades WHERE {where} AND created_at >= ? ORDER BY id DESC LIMIT 5",
        (*params, since),
    ).fetchall()
    if not rows:
        return ""
    lines = ["UPGRADES YOU ASKED FOR IN THE PERIOD"]
    lines += [f'- #{r["id"]} "{_one_line(r["title"], 80)}" ({r["status"]})' for r in rows]
    return "\n".join(lines)


def _last_review(conn: sqlite3.Connection, scope: AgentScope, today: date) -> str:
    where, params = scope.where()
    last = conn.execute(
        f"SELECT * FROM reviews WHERE {where} AND status = 'ok' AND day < ? ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    if last is None:
        return "YOUR LAST REVIEW\nThis is your first review."
    lines = [f"YOUR LAST REVIEW ({last['day']})"]
    for v in _verdicts(last):
        project = conn.execute("SELECT status FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
        now = project["status"] if project else "gone"
        check = ""
        if v["verdict"] == "stop" and now in OPEN_STATUSES:
            check = f" → still {now}: you haven't carried it out"
        lines.append(f"- #{v['project_id']} {v['verdict']}: {_one_line(v['why'], WHY_CHARS)}{check}")
    if last["focus"]:
        lines.append(f"Your focus then: {_one_line(last['focus'], 300)}")
    return "\n".join(lines)


def _one_line(text: str | None, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# --- the answer ---


def parse(text: str, project_ids: set[int]) -> Review | None:
    """The review in the model's JSON answer; None if it holds nothing usable. Verdicts only for listed projects."""
    data = _json_object(text)
    if not isinstance(data, dict):
        return None
    verdicts: list[Verdict] = []
    seen: set[int] = set()
    items = data.get("verdicts")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        pid, verdict = item.get("project_id"), item.get("verdict")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid not in project_ids or pid in seen:
            continue
        if verdict not in VERDICTS:
            continue
        seen.add(pid)
        verdicts.append(Verdict(pid, verdict, _one_line(item.get("why"), WHY_CHARS)))
    texts = {key: str(data.get(key) or "").strip()[:limit] for key, limit in LIMITS.items()}
    if not verdicts and not any(texts.values()):
        return None
    return Review(verdicts=verdicts, **texts)


def _json_object(text: str) -> Any:
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _first_object(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def save(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    now: str,
    day: date,
    card: Scorecard,
    review: Review | None,
    note: str | None = None,
) -> int:
    verdicts = (
        [{"project_id": v.project_id, "verdict": v.verdict, "why": v.why} for v in review.verdicts] if review else []
    )
    texts = {key: getattr(review, key) if review else "" for key in LIMITS}
    cursor = conn.execute(
        "INSERT INTO reviews (mode, session, life_id, cycle_id, created_at, day, status, scorecard, verdicts, working,"
        " not_working, owner_feedback, lesson, focus, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id,
            cycle_id,
            now,
            day.isoformat(),
            "ok" if review else "failed",
            card.text[:12_000],
            json.dumps(verdicts, ensure_ascii=False),
            texts["working"],
            texts["not_working"],
            texts["owner_feedback"],
            texts["lesson"],
            texts["focus"],
            (note or "")[:300] or None,
        ),
    )
    return int(cursor.lastrowid)


def _verdicts(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        items = json.loads(row["verdicts"] or "[]")
    except ValueError:
        return []
    return [v for v in items if isinstance(v, dict)]


# --- what the plans of the day see ---


def planner_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Today's review for the planner: the verdicts first (with the project's title), then the focus and lesson."""
    lines = [f"Your review of today ({row['day']}), made before your first plan:"]
    for v in _verdicts(row):
        project = conn.execute("SELECT title, status FROM projects WHERE id = ?", (v["project_id"],)).fetchone()
        title = f" {_one_line(project['title'], 60)} [{project['status']}]" if project else ""
        lines.append(f"- #{v['project_id']}{title}: {v['verdict']}: {_one_line(v['why'], 160)}")
    if row["focus"]:
        lines.append(f"Focus today: {_one_line(row['focus'], 300)}")
    if row["lesson"]:
        lines.append(f"Lesson: {_one_line(row['lesson'], 300)}")
    lines.append(
        "Act on it: carry out every stop and change (project_update), and keep the lesson with memory_update if it"
        " is new."
    )
    return "\n".join(lines)
