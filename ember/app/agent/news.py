"""What changed for the agent since its last plan: the owner's decisions and messages, new versions.

Built only from database columns and the bundled CHANGELOG.md; everything the
owner or the agent wrote is JSON-quoted, so no text can pose as a heading of the
planner's context. The items shown are marked seen only after a plan succeeded
(``mark_seen``), so a failed or refused cycle doesn't lose news.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..db import Database
from .store import AgentScope

CHANGELOG_LIMIT = 6_000
_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\s*$")


def changelog_key(mode: str) -> str:
    return f"agent.{mode}.changelog_seen"


def _q(text: str | None) -> str:
    return json.dumps(text or "", ensure_ascii=False)


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
    if len(text) > CHANGELOG_LIMIT:
        text = text[:CHANGELOG_LIMIT].rsplit("\n", 1)[0] + "\n…(older changes cut)"
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
            head = f"Request #{r['id']} ({r['type']}) {_q(r['title'])}: {r['status'].replace('_', ' ')}"
            if r["status"] == "approved_with_changes":
                head += f". Use the owner's version, not yours: {_q(r['final_payload'])}"
            if r["decision_comment"]:
                head += f". Owner's comment: {_q(r['decision_comment'])}"
            if r["status"] in ("done", "failed"):
                if r["result_note"]:
                    head += f". Result: {_q(r['result_note'])}"
                if r["result_link"]:
                    head += f". Link: {_q(r['result_link'])}"
            elif r["status"] in ("approved", "approved_with_changes"):
                head += ". Your owner will carry it out and report back"
            lines.append(head + ".")
        return lines

    def upgrade_lines(self) -> list[str]:
        lines = []
        for r in self.upgrades:
            line = f"Upgrade request #{r['id']} {_q(r['title'])}: {r['status']}"
            if r["released_version"]:
                line += f" in version {r['released_version']}"
            if r["owner_note"]:
                line += f". Owner's note: {_q(r['owner_note'])}"
            lines.append(line + ".")
        return lines


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


def mark_seen(
    conn: sqlite3.Connection, db: Database, scope: AgentScope, cycle_id: int, news: News, message_ids: list[int]
) -> None:
    """After a successful plan: the items shown won't be shown again.

    An item the owner changed again meanwhile (decided, then closed) stays unseen.
    """
    for r in news.decided:
        conn.execute(
            "UPDATE approvals SET seen_cycle_id = ? WHERE id = ? AND version = ? AND seen_cycle_id IS NULL",
            (cycle_id, r["id"], r["version"]),
        )
    for r in news.upgrades:
        conn.execute(
            "UPDATE upgrades SET seen_cycle_id = ? WHERE id = ? AND status = ? AND seen_cycle_id IS NULL",
            (cycle_id, r["id"], r["status"]),
        )
    for message_id in message_ids:
        conn.execute(
            "UPDATE messages SET seen_cycle_id = ? WHERE id = ? AND seen_cycle_id IS NULL", (cycle_id, message_id)
        )
    if parse_version(news.running_version) is not None:
        db.set_meta(changelog_key(scope.mode), news.running_version)
