"""The generation change (0.47.0, vision/generations.md): Ember's life ends by the owner's choice, and the next
generation begins with a clean mind in the same world.

What starts empty is her mind: the records ``AgentScope.where`` reads (her projects, ventures, plan, requests,
messages, journal, bets, cases, playbook) and her memory and workspace folders, which move to
``generations/<n>/`` beside them. What carries on is the world (``AgentScope.world_where``): the money, the shop, the
accounts, the mailbox and its opt-outs, the owner's rulebook and library. The products pass as the estate the owner
sorted (agent/estate.py): an adopted one becomes a line of the new mind at her first cycle (``file``), with its files
copied into her workspace; a passive one isn't hers to work on. 0.48.0: what she learned passes as the inheritance
the owner gave (agent/inheritance.py): her principles as inherited hypotheses, the facts of her work, her craft files
and her letter, and a seed strategy and identity. 0.50.0: the old mind's open work closes as ended with its generation
(``close``), and the owner's goal carries over (``carry_goal``).

The change waits until nothing is under way: no cycle running, no request waiting for the owner or for Ember's code,
a verdict for every product, the retired ones down. It copies the database first, as a migration does."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import events, paths
from ..db import MAX_BACKUPS
from ..economy.clock import local_date, to_iso
from ..economy.life import LIVE_SESSION_KEY, SESSION_NO_KEY, WORLD_KEY
from ..integrations import etsy
from . import estate, inheritance, learning, policy, roadmap, store
from .store import AgentScope

if TYPE_CHECKING:
    from .service import Agent

log = logging.getLogger(__name__)

# What the old mind kept per mode that the new one starts without (agent/service.py's keys): her failures and backoff,
# the sleep her code cut, a wake for the owner's news she was to read, the hours her windows were paused, her next wake
# and the last will's retries (the will itself is the old life's).
RESET = (
    "failures",
    "backoff_until",
    "sleep_cut",
    "owner_wake_at",
    "owner_wake_by",
    "owner_wake_for",
    "owner_wake_kinds",
    "owner_woke_at",
    "windows_paused_at",
    "next_wake_at",
    "next_wake_reason",
    "will_retry_at",
    "will_attempts",
    "letter_asked",  # 0.48.0: the owner's ask for her letter (agent/service.py)
)
WAITING = ("pending", "approved", "approved_with_changes")  # a request not decided, or not carried out yet
BACKUP_PREFIX = "ember-generation"


class GenerationError(ValueError):
    def __init__(self, message: str, status: int = 409, checks: list[Check] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.checks = checks or []


@dataclass(frozen=True)
class Check:
    """One thing the change waits for: whether it holds, and what is in the way."""

    name: str
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _ids(rows: list[sqlite3.Row], key: str = "id", shown: int = 8) -> str:
    numbers = [f"#{r[key]}" for r in rows]
    return ", ".join(numbers[:shown]) + (f" and {len(numbers) - shown} more" if len(numbers) > shown else "")


def checks(conn: sqlite3.Connection, scope: AgentScope, running: bool) -> list[Check]:
    """What the change waits for, each with what is in the way now (the estate's rows read as ``estate.refresh`` left
    them)."""
    found = [
        Check("No wake cycle is running", not running, "a cycle is running: wait for it to end" if running else "")
    ]
    where, params = scope.where()
    marks = ", ".join("?" for _ in WAITING)
    waiting = conn.execute(
        f"SELECT id, status FROM approvals WHERE {where} AND status IN ({marks}) ORDER BY id", (*params, *WAITING)
    ).fetchall()
    decide = [r for r in waiting if r["status"] == "pending"]
    carry = [r for r in waiting if r["status"] != "pending"]
    detail = []
    if decide:
        detail.append(f"decide {_ids(decide)}")
    if carry:
        detail.append(f"mark done or failed, or let Ember's code carry out, {_ids(carry)}")
    found.append(Check("No request waits", not waiting, "; ".join(detail)))
    journal, journal_params = scope.where("j")
    running_actions = conn.execute(
        f"SELECT j.id FROM action_journal j WHERE {journal} AND j.status = 'running' ORDER BY j.id", journal_params
    ).fetchall()
    found.append(
        Check(
            "No action is under way",
            not running_actions,
            f"Ember's code is carrying out {_ids(running_actions)}: wait a minute" if running_actions else "",
        )
    )
    rows = estate.handover(conn, scope)
    open_rows = [r for r in rows if r["verdict"] is None and not json.loads(r["numbers"]).get("gone")]
    found.append(
        Check(
            "Every product has a verdict",
            not open_rows,
            f"adopt, passive or retire: {', '.join(_product(r) for r in open_rows[:8])}" if open_rows else "",
        )
    )
    up = [r for r in rows if r["verdict"] == "retire" and _still_up(r)]
    found.append(
        Check(
            "The retired products are down",
            not up,
            f"take down {', '.join(_product(r) for r in up[:8])} (Ember's code reads Etsy's and Printify's state at"
            " the next sync)"
            if up
            else "",
        )
    )
    return found


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _product(row: sqlite3.Row) -> str:
    return f"{estate.KIND_WORDS[row['kind']]} {row['ref']} ({row['title'][:40]})"


def _still_up(row: sqlite3.Row) -> bool:
    """Whether a retired product is still up where Ember's code can see it (a KDP book's state it can't: the owner
    unpublishes it at KDP, and their verdict says they did)."""
    if row["kind"] == "kdp_book":
        return False
    numbers = json.loads(row["numbers"])
    return not numbers.get("gone") and numbers.get("state") in (etsy.LIVE_STATE, "publishing")


def status(conn: sqlite3.Connection, agent: Agent) -> dict[str, Any]:
    """The System tab's card: which generation this is, the estate this mind would hand on, and what the change
    waits for."""
    scope = agent.scope()
    rows = estate.refresh(conn, scope, agent.clock)
    found = checks(conn, scope, agent.running_cycle)
    return {
        "generation": estate.generation(scope),
        "next": estate.generation(scope) + 1,
        "estate": [estate.to_json(r) for r in rows],
        "received": [estate.to_json(r) for r in estate.received(conn, scope)],
        "checks": [c.to_json() for c in found],
        "ready": all(c.ok for c in found),
        "inheritance": inheritance.status(conn, agent, scope),  # 0.48.0
    }


def close(conn: sqlite3.Connection, scope: AgentScope, number: int, by: str, now: str) -> dict[str, int]:
    """0.50.0 (vision/generations.md, "The change"): in the change's transaction, the old mind's open work closes as
    ended with its generation, so nothing of hers stands open forever where a later reading finds it: her lines
    (abandoned), ventures (parked by Ember's code: only the owner kills one), plan nodes and milestones (dropped; the
    owner's own by the owner, whose change it is), bets and predictions (void), promises (closed), and her unlocks
    (taken back: the next generation earns them again). Returns how many of each closed."""
    why = f"Ended with generation {number}: {by} began generation {number + 1}."
    where, params = scope.where()
    closed: dict[str, int] = {}
    lines = conn.execute(
        f"SELECT id, notes FROM projects WHERE {where} AND status IN ('idea', 'active', 'waiting')", params
    ).fetchall()
    for r in lines:
        conn.execute(
            "UPDATE projects SET status = 'abandoned', notes = ?, updated_at = ? WHERE id = ?",
            (_noted(r["notes"], why), now, r["id"]),
        )
    closed["lines"] = len(lines)
    ventures = conn.execute(
        f"SELECT id, notes FROM ventures WHERE {where} AND stage NOT IN ('parked', 'killed')", params
    ).fetchall()
    for r in ventures:
        conn.execute(
            "UPDATE ventures SET stage = 'parked', parked_by = 'code', notes = ?, updated_at = ? WHERE id = ?",
            (_noted(r["notes"], why), now, r["id"]),
        )
    closed["ventures"] = len(ventures)
    closed["plan_nodes"] = conn.execute(
        f"UPDATE plan_nodes SET status = 'dropped', closed_at = ?, closed_by = 'code', result = ?, updated_at = ?"
        f" WHERE {where} AND status = 'open'",
        (now, why[:300], now, *params),
    ).rowcount
    closed["milestones"] = conn.execute(
        "UPDATE milestones SET status = 'dropped', closed_at = ?, result = ?, updated_at = ?, closed_by = CASE"
        f" WHEN created_by = 'owner' THEN 'owner' ELSE 'code' END WHERE {where} AND status = 'open'",
        (now, why[:600], now, *params),
    ).rowcount
    closed["bets"] = conn.execute(
        f"UPDATE bets SET status = 'void', settled_at = ? WHERE {where} AND status = 'open'", (now, *params)
    ).rowcount
    closed["predictions"] = conn.execute(
        f"UPDATE predictions SET status = 'void', settled_at = ?, result = ? WHERE {where} AND status = 'open'",
        (now, why[:300], *params),
    ).rowcount
    closed["promises"] = conn.execute(
        f"UPDATE obligations SET status = 'closed', closed_at = ?, closed_by = 'code', result = ? WHERE {where}"
        " AND status = 'open'",
        (now, why[:300], *params),
    ).rowcount
    closed["unlocks"] = len(policy.revoke_all(conn, scope, now, by=policy.REVOKED_BY, why=f"generation {number} ended"))
    return closed


def _closed(closed: dict[str, int]) -> str:
    words = {
        "lines": "line",
        "ventures": "venture",
        "plan_nodes": "plan step",
        "milestones": "milestone",
        "bets": "bet",
        "predictions": "prediction",
        "promises": "promise",
        "unlocks": "unlock",
    }
    said = [_count(n, words[key]) for key, n in closed.items() if n]
    return ", ".join(said) if said else "nothing"


def _noted(notes: str | None, why: str, most: int = 2_000) -> str:
    """Notes with ``why`` at their end, the oldest of them cut to fit."""
    old = (notes or "").strip()
    room = most - len(why) - 1
    return (old[-room:] + "\n" + why if old else why) if room > 0 else why[:most]


def carry_goal(
    conn: sqlite3.Connection, goal: sqlite3.Row, number: int, new: AgentScope, by: str, today: date, now: str
) -> int | None:
    """0.50.0: the owner's goal (``goal``, the old mind's, read before it closed) is theirs, as the rulebook is: the
    next generation works toward the same amount by the same day (a goal in total counts from the change: her
    predecessor's revenue is hers no more). Its number, or None (one due too soon to set again)."""
    per = roadmap.goal_per(goal)
    if per is None or date.fromisoformat(goal["due"]) < today + timedelta(days=roadmap.GOAL_DAYS_MIN):
        return None
    goal_id, _ = roadmap.set_goal(
        conn,
        new,
        per=per,
        target=int(goal["target"]),
        due=date.fromisoformat(goal["due"]),
        today=today,
        now=now,
        who=by,
        comment=f"Carried from generation {number} (goal #{goal['id']}) at the change.",
    )
    return goal_id


def _backup(agent: Agent, number: int) -> Path:
    """A copy of the database before the change, beside the migrations' (BACKUP_PREFIX; the newest MAX_BACKUPS)."""
    folder = paths.backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{BACKUP_PREFIX}{number:03d}-{agent.clock.now():%Y%m%dT%H%M%SZ}.db"
    dest = sqlite3.connect(target)
    try:
        with agent.db.connection() as conn:
            conn.backup(dest)
    except sqlite3.Error:
        dest.close()
        target.unlink(missing_ok=True)
        raise
    finally:
        dest.close()
    for old in sorted(folder.glob(f"{BACKUP_PREFIX}*.db"))[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _archive(agent: Agent, number: int) -> list[tuple[Path, Path]]:
    """Move the mind's folders aside, to ``generations/<number>/`` next to them; returns what moved where."""
    moved = []
    for jail in agent.roots():
        root = Path(jail.root)
        if not root.exists():
            continue
        target = root.parent / "generations" / str(number) / root.name
        if target.exists():
            raise GenerationError(f"{target} exists already: move it away first", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        root.rename(target)
        moved.append((root, target))
    return moved


def _restore(moved: list[tuple[Path, Path]]) -> None:
    for root, target in reversed(moved):
        if target.exists() and not root.exists():
            target.rename(root)


def _uploads(value: Any) -> list[str]:
    """The paths of the files a request's action names ({path, sha256, bytes}: a listing's photos and files, a book's
    manuscript and cover, a product's design)."""
    if isinstance(value, dict):
        own = [str(value["path"])] if isinstance(value.get("path"), str) and "sha256" in value else []
        return own + [p for item in value.values() for p in _uploads(item)]
    if isinstance(value, list):
        return [p for item in value for p in _uploads(item)]
    return []


def _carry_files(agent: Agent, conn: sqlite3.Connection, rows: list[sqlite3.Row], old: Path | None) -> list[str]:
    """The adopted products' files, copied from the old mind's workspace to the same paths in the new one, so a change
    of the listing or a book's new cover can name them as before. Returns the paths copied."""
    if old is None:
        return []
    old = old.resolve()
    new = Path(agent.roots()[0].root)
    copied = []
    for row in rows:
        if row["verdict"] != "adopt":
            continue
        approval = conn.execute("SELECT action FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
        edits = (
            conn.execute(
                "SELECT listing FROM etsy_edits WHERE listing_id = ? AND listing IS NOT NULL ORDER BY id DESC LIMIT 1",
                (row["listing_id"],),
            ).fetchone()
            if row["listing_id"]
            else None
        )
        names = _uploads(json.loads(approval["action"] or "null")) if approval else []
        if edits is not None:
            names += _uploads(json.loads(edits["listing"]))
        for name in dict.fromkeys(names):
            source, target = old / name, new / name
            if old not in source.resolve().parents or not source.is_file() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(name)
    return copied


def begin(agent: Agent, by: str) -> dict[str, Any]:
    """The change, while the caller holds the agent's cycle lock: the checks, a copy of the database, the folders moved
    aside, then in one transaction the estate handed on, the inherited principles written, the life ended and the next
    begun, the mind's session counted up and its per-mode keys reset; then the new mind's first files (Agent.recover),
    the inheritance's (its seed strategy and identity, the facts, her letter, the craft) and the adopted products'."""
    scope = agent.scope()
    with agent.db.transaction() as conn:
        estate.refresh(conn, scope, agent.clock)
        found = checks(conn, scope, running=False)
        handed = inheritance.passing(conn, agent, scope)  # 0.48.0: what passes, read while the old mind is here
    if not all(c.ok for c in found):
        raise GenerationError("The next generation can't begin yet", 409, found)
    number = estate.generation(scope)
    backup = _backup(agent, number)
    moved = _archive(agent, number)
    now = to_iso(agent.clock.now())
    new_session = scope.session + 1
    try:
        with agent.db.transaction() as conn:
            rows = estate.handover(conn, scope)
            conn.execute(
                "UPDATE estate SET to_session = ?, updated_at = ? WHERE mode = ? AND session = ? AND from_session = ?"
                " AND to_session IS NULL",
                (new_session, now, scope.mode, scope.world_session, scope.session),
            )
            adopted = sum(1 for r in rows if r["verdict"] == "adopt")
            passive = sum(1 for r in rows if r["verdict"] == "passive")
            inherited = inheritance.write_principles(conn, scope, handed, new_session, now)
            goal = roadmap.owner_goal(conn, scope)  # 0.50.0: carried after the old mind's work closes
            closed = close(conn, scope, number, by or "the owner", now)
            ended, born = agent.economy.life.succeed(
                conn,
                now,
                f"succeeded by generation {number + 1}: {by or 'the owner'} ended this life so the next could begin",
                f"generation {number + 1}, by {by or 'the owner'}: {_count(adopted, 'product')} adopted, {passive}"
                f" passive, {_count(len(inherited), 'principle')} inherited",
            )
            if scope.mode == "live":
                agent.db.set_meta(LIVE_SESSION_KEY, str(new_session))
            else:
                agent.db.set_meta(WORLD_KEY, str(scope.world_session))
                agent.db.set_meta(SESSION_NO_KEY, str(new_session))
            for name in RESET:
                conn.execute("DELETE FROM meta WHERE key = ?", (f"agent.{scope.mode}.{name}",))
            carried: int | None = None
            if goal is not None:
                new = AgentScope(scope.mode, new_session, born, scope.world)
                carried = carry_goal(conn, goal, number, new, by or "the owner", agent.clock.today(), now)
    except BaseException:
        _restore(moved)
        raise
    agent.economy.life.evaluate_and_persist()  # the new life is alive (or unfunded) from now
    agent.recover()  # the new mind's first files, the venture tree's seeds, as at a start
    old = next((target for root, target in moved if root.name == "workspace"), None)
    written = inheritance.write_files(agent, agent.scope(), handed, old, now)
    with agent.db.connection() as conn:
        copied = _carry_files(agent, conn, rows, old)
    events.record(
        agent.db,
        "warning",
        "life",
        f"{by or 'The owner'} began generation {number + 1} of {agent.settings.agent_name}: life #{ended} ended, life"
        f" #{born} began with {_count(adopted, 'product')} adopted and {passive} passive,"
        f" {_count(len(inherited), 'principle')} inherited; of the old mind's work {_closed(closed)} closed"
        + (f", your goal carried as #{carried}" if carried else ""),
        {
            "ended": ended,
            "born": born,
            "session": new_session,
            "backup": backup.name,
            "copied": len(copied),
            "inherited": len(inherited),
            "written": written,
            "closed": closed,
            "goal": carried,
        },
    )
    return {
        "generation": number + 1,
        "ended": ended,
        "born": born,
        "backup": backup.name,
        "archived": [str(target) for _, target in moved],
        "copied": copied,
        "inherited": inherited,
        "written": written,
        "closed": closed,  # 0.50.0
        "goal": carried,
    }


# --- in the new mind ---


def file(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str) -> list[int]:
    """At a cycle of the mind that received them (before its plan): the adopted products without a line become lines
    of hers, filed under this cycle, each with its listing moved to it. Returns the new lines."""
    made = []
    for row in estate.received(conn, scope):
        if row["verdict"] != "adopt" or row["project_id"] is not None:
            continue
        numbers = json.loads(row["numbers"])
        test = f"{row['test_target']} {row['test_metric']} by {row['test_due']}"
        hypothesis = (
            f"Adopted from generation {estate.generation(scope) - 1}: {estate.KIND_WORDS[row['kind']]} {row['ref']}"
            f" ({_numbers(row['kind'], numbers)}). Its first test, your owner's: {test}. Missed, the line waits on"
            " hold until your owner keeps or drops it."
        )
        project_id = store.create_project(
            conn,
            scope,
            cycle_id=cycle_id,
            title=row["title"][:80],
            hypothesis=hypothesis[:400],
            next_step="",
            status="active",
            now=now,
        )
        if row["listing_id"] is not None:
            conn.execute(
                "INSERT INTO listing_lines (mode, session, listing_id, project_id, cycle_id, created_at, why)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.mode,
                    scope.session,
                    row["listing_id"],
                    project_id,
                    cycle_id,
                    now,
                    f"adopted from generation {estate.generation(scope) - 1} (estate #{row['id']})",
                ),
            )
        conn.execute("UPDATE estate SET project_id = ?, updated_at = ? WHERE id = ?", (project_id, now, row["id"]))
        made.append(project_id)
    return made


def _numbers(kind: str, n: dict[str, Any]) -> str:
    if kind == "kdp_book":
        return f"published {n.get('days')} days, {n.get('royalties', 0)} royalty entries"
    views, favorites, sold = n.get("views", 0), n.get("favorites", 0), n.get("sold", 0)
    return f"{n.get('days')} days live, {views} views, {favorites} favorites, {sold} sold"


def status_line(conn: sqlite3.Connection, scope: AgentScope, today: date, born: str | None) -> str:
    """STATUS's line for a mind after the first (""): which generation she is, and in her first days what passed to
    her."""
    number = estate.generation(scope)
    if number <= 1:
        return ""
    since = local_date(born) if born else ""
    line = f"You are generation {number} of this agent, since {since}: your owner ended your predecessor's life so you"
    line += " could begin with a clean mind."
    if since and (today - date.fromisoformat(since)).days > 7:
        return line
    rows = estate.received(conn, scope)
    adopted = [r for r in rows if r["verdict"] == "adopt"]
    passive = [r for r in rows if r["verdict"] != "adopt"]
    line += (
        f" What passed to you: the money, the shop and the accounts, your owner's rulebook and library;"
        f" {len(adopted)} of her products as lines of yours, each with its first test, and {len(passive)} that stay"
        " live but aren't yours to work on."
    )
    got = inheritance.received(conn, scope)  # 0.48.0
    content = json.loads(got["payload"]) if got is not None else {}
    principles = len(content.get("principles", []))
    if principles:
        line += (
            f" {_count(principles, 'principle')} of hers in your playbook as inherited hypotheses: yours to test, not"
            f" to obey (two cases of yours establish one, one against disputes it, {learning.INHERITED_FADE_DAYS} days"
            " without one retire it)."
        )
    letter = ", her letter" if inheritance.letter_passed(conn, scope) else ""
    return (
        line + f" The facts of her work{letter} and what else passed are in {inheritance.FOLDER}/ in your workspace."
        " Nothing of her plans, requests, cases or lessons: the rest of your playbook begins with what you learn."
    )
