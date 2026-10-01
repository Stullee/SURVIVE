"""What changed for the agent since its last plan: the owner's decisions and messages, new versions.

Built only from database columns and the bundled CHANGELOG.md; everything the
owner or the agent wrote is JSON-quoted, so no text can pose as a heading of the
planner's context. An item is marked seen (``mark_seen``) only once the agent has
worked with it: listed by the plan that succeeded and shown in full in the brief of
a work step that was answered, or shown in full by a plan with nothing to do. A
message counts as shown in full only unshortened, unless it is longer than the brief
can ever hold. What a prompt left out, cut or shortened, and what a cycle that ended
before showed, stays news for the next cycle. The owner's messages stay in the plans
after that too, until the agent answers them (0.9.1, ``store.open_messages``). The owner's word on a venture (an idea
they added, backing, parking, killing, a note: 0.10.0) and on a milestone of the roadmap (one they added, a note,
dropping it: 0.11.0) is news like a decision.
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
from . import roadmap
from .store import REQUEST_DAYS, AgentScope
from .ventures import news_line

# Bytes (JSON-escaped): the planner's YOUR SOFTWARE section holds this much uncut, and only then counts it as read.
# 0.14.0: longer notes come in parts of this size, one a plan (the agent read 1,937 of 0.13.0's 8,456 characters).
CHANGELOG_LIMIT = 2_000
CONTINUED = "Your release notes, continued:\n"
MORE = "\n…(the rest of these notes comes in your next plan)"
_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\s*$")
# An owner item as the agent is shown it: ("message", id, None), ("approval", id, version), ("upgrade", id, status),
# ("venture", id, owner_version) or ("milestone", id, owner_version). A decision the owner changes again (a new version
# or status) is news again.
Item = tuple[str, int, int | str | None]


def changelog_key(mode: str) -> str:
    return f"agent.{mode}.changelog_seen"


def changelog_at_key(mode: str) -> str:
    """0.14.0: how far the agent has read the notes of its upgrade: "<seen>><running>@<character>"."""
    return f"agent.{mode}.changelog_at"


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
    """The CHANGELOG sections the agent hasn't read yet, whole (empty when nothing is new): ``changelog_part`` gives
    the part a plan shows."""
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
    return "\n\n".join(parts)


def changelog_part(text: str, at: int = 0) -> tuple[str, int | None]:
    """0.14.0: the part of the unread notes ``text`` from character ``at`` on that one plan shows, at most
    CHANGELOG_LIMIT bytes (whole lines while one fits, saying the rest comes next), and where the next part begins
    (None: this part ends the notes). The plan saw their first 2 KB, and the whole version counted as read."""
    if not 0 <= at < len(text.rstrip()):  # past their end (the notes changed): from the start
        at = 0
    while text[at : at + 1] == "\n":
        at += 1
    head = CONTINUED if at else ""
    rest = text[at:]
    if _json_bytes(head + rest) <= CHANGELOG_LIMIT:
        return head + rest, None
    lines: list[str] = []
    for line in rest.split("\n"):
        if _json_bytes(head + "\n".join([*lines, line]) + MORE) > CHANGELOG_LIMIT:
            break
        lines.append(line)
    shown = "\n".join(lines)
    if not shown.strip():  # a line longer than a part: as many characters as fit
        low, high = 1, len(rest)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (
                (middle, high) if _json_bytes(head + rest[:middle] + MORE) <= CHANGELOG_LIMIT else (low, middle - 1)
            )
        shown = rest[:low]
    return head + shown.rstrip() + MORE, at + len(shown)


@dataclass
class News:
    decided: list[sqlite3.Row] = field(default_factory=list)
    upgrades: list[sqlite3.Row] = field(default_factory=list)
    changelog: str = ""  # the part of the unread release notes this plan shows (0.14.0: ``changelog_part``)
    running_version: str = ""
    ventures: list[sqlite3.Row] = field(default_factory=list)  # the owner's word on a venture (0.10.0)
    milestones: list[sqlite3.Row] = field(default_factory=list)  # the owner's word on a milestone (0.11.0)
    changelog_next: int | None = None  # 0.14.0: where the next part begins (None: this one ends the notes)
    changelog_from: str = ""  # 0.14.0: the version the notes begin after ("" when none was read before)

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
            if r["status"] == "expired":  # 0.12.0: by Ember's code, not the owner's word
                head += (
                    f" after {REQUEST_DAYS.get(r['type'], 30)} days without a decision: ask again if it still matters"
                )
            elif r["decision_comment"]:
                head += f". Owner's comment: {_q(r['decision_comment'])}"
            if r["status"] in ("done", "failed"):
                if r["result_note"]:
                    head += f". Result: {_q(r['result_note'])}"
                if r["result_link"]:
                    head += f". Link: {_q(r['result_link'])}"
            elif r["status"] in ("approved", "approved_with_changes"):
                executor = _column(r, "executor")  # 0.12.0: Ember's code makes Etsy listings and changes itself
                if email:
                    head += ". Ember's code sends it and you'll hear the result"
                elif executor in ("etsy_listing", "etsy_edit"):
                    head += ". Ember's code carries it out in the Etsy shop and you'll hear the result"
                elif executor == "reddit_link":
                    head += ". Your owner posts it and reports back"
                else:
                    head += ". Your owner will carry it out and report back"
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

    def venture_lines(self) -> list[str]:
        """The owner's word on ventures, then on milestones (their lines follow the decisions')."""
        return [*(news_line(r) for r in self.ventures), *(roadmap.news_line(r) for r in self.milestones)]

    def items(self) -> list[Item]:
        """The items of ``approval_lines()``, ``upgrade_lines()`` and ``venture_lines()``, in the same order."""
        return [
            *(("approval", r["id"], r["version"]) for r in self.decided),
            *(("upgrade", r["id"], r["status"]) for r in self.upgrades),
            *(("venture", r["id"], r["owner_version"]) for r in self.ventures),
            *(("milestone", r["id"], r["owner_version"]) for r in self.milestones),
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
    ventures = conn.execute(
        f"SELECT * FROM ventures WHERE {where} AND owner_action IS NOT NULL AND seen_cycle_id IS NULL ORDER BY id"
        " LIMIT 10",
        params,
    ).fetchall()
    milestones = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND owner_action IS NOT NULL AND seen_cycle_id IS NULL ORDER BY id"
        " LIMIT 10",
        params,
    ).fetchall()
    seen = db.get_meta(changelog_key(scope.mode))
    notes = changelog_news(paths.CHANGELOG_PATH, seen, running_version)
    mark, _, at = (db.get_meta(changelog_at_key(scope.mode)) or "").rpartition("@")
    part, after = changelog_part(notes, int(at) if mark == f"{seen or ''}>{running_version}" and at.isdigit() else 0)
    return News(
        decided,
        upgrades,
        part,
        running_version,
        ventures,
        milestones,
        changelog_next=after,
        changelog_from=seen or "",
    )


def decided_unseen(conn: sqlite3.Connection, scope: AgentScope) -> bool:
    """0.12.0: whether the owner decided something the agent hasn't seen yet (a request decided or closed, their word
    on a venture or a milestone): what their decision wakes the agent for."""
    where, params = scope.where()
    return any(
        conn.execute(f"SELECT 1 FROM {table} WHERE {where} AND {condition} LIMIT 1", params).fetchone()
        for table, condition in (
            ("approvals", "status <> 'pending' AND seen_cycle_id IS NULL"),
            ("ventures", "owner_action IS NOT NULL AND seen_cycle_id IS NULL"),
            ("milestones", "owner_action IS NOT NULL AND seen_cycle_id IS NULL"),
        )
    )


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
        elif kind == "venture":
            conn.execute(
                "UPDATE ventures SET seen_cycle_id = ? WHERE id = ? AND owner_version = ? AND seen_cycle_id IS NULL",
                (cycle_id, item_id, version),
            )
        elif kind == "milestone":
            conn.execute(
                "UPDATE milestones SET seen_cycle_id = ? WHERE id = ? AND owner_version = ? AND seen_cycle_id IS NULL",
                (cycle_id, item_id, version),
            )


def mark_changelog_seen(db: Database, scope: AgentScope, news: News) -> None:
    """After a plan that showed its part of the notes whole: the next plan shows the next part (0.14.0), and after the
    last part they aren't shown again until the next version."""
    if not news.changelog or parse_version(news.running_version) is None:
        return
    if news.changelog_next is None:
        db.set_meta(changelog_key(scope.mode), news.running_version)
    else:
        db.set_meta(changelog_at_key(scope.mode), f"{news.changelog_from}>{news.running_version}@{news.changelog_next}")
