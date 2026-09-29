"""What changed for the agent since its last plan: the owner's decisions and messages, new versions.

Built only from database columns and the bundled CHANGELOG.md; everything the
owner or the agent wrote is JSON-quoted, so no text can pose as a heading of the
planner's context. An item is marked seen (``mark_seen``) only once the agent has
worked with it: listed by the plan that succeeded and shown in full in the brief of
a work step that was answered, or shown in full by a plan with nothing to do. A
message counts as shown in full only unshortened, unless it is longer than the brief
can ever hold. What a prompt left out, cut or shortened, and what a cycle that ended
before showed, stays news for the next cycle. The owner's messages stay in the plans
after that too, until the agent answers them (0.9.1, ``store.open_messages``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..db import Database
from .store import AgentScope

# Bytes (JSON-escaped): the planner's YOUR SOFTWARE section holds this much uncut, and only then counts it as read.
CHANGELOG_LIMIT = 2_000
_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\s*$")
# An owner item as the agent is shown it: ("message", id, None), ("approval", id, version) or ("upgrade", id,
# status). A decision the owner changes again (a new version or status) is news again.
Item = tuple[str, int, int | str | None]


def changelog_key(mode: str) -> str:
    return f"agent.{mode}.changelog_seen"


def _q(text: str | None) -> str:
    return json.dumps(text or "", ensure_ascii=False)


def _json_bytes(text: str) -> int:
    return len(_q(text).encode("utf-8"))


def _column(row: sqlite3.Row, name: str) -> object:
    """A column that rows built by hand (tests, older callers) may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def changelog_sections(text: str) -> list[tuple[tuple[int, int, int], str]]:
    """``(version, body)`` for every exact ``## x.y.z`` heading, in file order."""
    sections: list[tuple[tuple[int, int, int], list[str]]] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append(((int(match[1]), int(match[2]), int(match[3])), []))
        elif sections:
            sections[-1][1].append(line)
    return [(v, "\n".join(body).strip()) for v, body in sections]


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in version)


def changelog_news(changelog: Path, seen: str | None, running: str) -> str:
    """The CHANGELOG sections the agent hasn't read yet (empty when nothing is new)."""
    current = parse_version(running)
    if current is None:
        return ""
    previous = parse_version(seen) if seen else None
    if previous == current:
        return ""
    try:
        sections = changelog_sections(changelog.read_text(encoding="utf-8"))
    except OSError:
        return ""
    if previous is not None and previous > current:
        header = f"Your software was downgraded from {_fmt(previous)} to {_fmt(current)} (maybe a backup was restored)."
        wanted = [(v, body) for v, body in sections if v == current]
    elif previous is None:
        header = f"You are running version {_fmt(current)}. What it brought:"
        wanted = [(v, body) for v, body in sections if v == current]
    else:
        header = f"Your software was upgraded from {_fmt(previous)} to {_fmt(current)}. What changed:"
        wanted = [(v, body) for v, body in sections if previous < v <= current]
    parts = [header]
    for version, body in sorted(wanted, reverse=True):
        parts.append(f"## {_fmt(version)}\n{body}")
    text = "\n\n".join(parts)
    if _json_bytes(text) > CHANGELOG_LIMIT:
        lines, note = text.split("\n"), "\n…(older changes cut)"
        while lines and _json_bytes("\n".join(lines) + note) > CHANGELOG_LIMIT:
            lines.pop()
        text = "\n".join(lines).rstrip() + note
    return text


@dataclass
class News:
    decided: list[sqlite3.Row] = field(default_factory=list)
    upgrades: list[sqlite3.Row] = field(default_factory=list)
    changelog: str = ""
    running_version: str = ""

    def approval_lines(self) -> list[str]:
        lines = []
        for r in self.decided:
            email = _column(r, "executor") == "email"
            head = f"Request #{r['id']} ({r['type']}) {_q(r['title'])}: {r['status'].replace('_', ' ')}"
            if r["status"] == "approved_with_changes":
                if email:
                    head += f". Your owner changed the email's text; this is what is sent: {_q(r['final_payload'])}"
                else:
                    head += f". Use the owner's version, not yours: {_q(r['final_payload'])}"
            if r["decision_comment"]:
                head += f". Owner's comment: {_q(r['decision_comment'])}"
            if r["status"] in ("done", "failed"):
                if r["result_note"]:
                    head += f". Result: {_q(r['result_note'])}"
                if r["result_link"]:
                    head += f". Link: {_q(r['result_link'])}"
            elif r["status"] in ("approved", "approved_with_changes"):
                head += (
                    ". Ember's code sends it and you'll hear the result"
                    if email
                    else ". Your owner will carry it out and report back"
                )
            lines.append(head + ".")
        return lines

    def upgrade_lines(self) -> list[str]:
        lines = []
        for r in self.upgrades:
            line = f"Upgrade request #{r['id']} {_q(r['title'])}: {r['status']}"
            if r["released_version"]:
                line += f" in version {_q(r['released_version'])}"
            if r["owner_note"]:
                line += f". Owner's note: {_q(r['owner_note'])}"
            lines.append(line + ".")
        return lines

    def items(self) -> list[Item]:
        """The items of ``approval_lines()`` and ``upgrade_lines()``, in the same order."""
        return [("approval", r["id"], r["version"]) for r in self.decided] + [
            ("upgrade", r["id"], r["status"]) for r in self.upgrades
        ]


@dataclass(frozen=True)
class Shown:
    """What one prompt showed of the owner's news: the items it showed in full (a message unshortened or, in the
    brief, as much as it can hold of one longer than that), whether it held the whole changelog, and the items whose
    lines it held whole, their quoted texts perhaps shortened (``listed``)."""

    items: frozenset[Item] = frozenset()
    changelog: bool = False
    listed: frozenset[Item] = frozenset()


def collect(conn: sqlite3.Connection, db: Database, scope: AgentScope, running_version: str) -> News:
    where, params = scope.where()
    decided = conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND status <> 'pending' AND seen_cycle_id IS NULL ORDER BY id LIMIT 10",
        params,
    ).fetchall()
    upgrades = conn.execute(
        f"SELECT * FROM upgrades WHERE {where} AND status <> 'new' AND seen_cycle_id IS NULL ORDER BY id LIMIT 10",
        params,
    ).fetchall()
    seen = db.get_meta(changelog_key(scope.mode))
    return News(decided, upgrades, changelog_news(paths.CHANGELOG_PATH, seen, running_version), running_version)


def mark_seen(conn: sqlite3.Connection, cycle_id: int, items: Iterable[Item]) -> None:
    """The owner's items the agent has worked with won't be shown again.

    An item the owner changed again meanwhile (decided, then closed) stays unseen.
    """
    for kind, item_id, version in items:
        if kind == "message":
            conn.execute(
                "UPDATE messages SET seen_cycle_id = ? WHERE id = ? AND seen_cycle_id IS NULL", (cycle_id, item_id)
            )
        elif kind == "approval":
            conn.execute(
                "UPDATE approvals SET seen_cycle_id = ? WHERE id = ? AND version = ? AND seen_cycle_id IS NULL",
                (cycle_id, item_id, version),
            )
        elif kind == "upgrade":
            conn.execute(
                "UPDATE upgrades SET seen_cycle_id = ? WHERE id = ? AND status = ? AND seen_cycle_id IS NULL",
                (cycle_id, item_id, version),
            )


def mark_changelog_seen(db: Database, scope: AgentScope, news: News) -> None:
    """After a plan that showed the whole changelog: it isn't shown again until the next version."""
    if news.changelog and parse_version(news.running_version) is not None:
        db.set_meta(changelog_key(scope.mode), news.running_version)
