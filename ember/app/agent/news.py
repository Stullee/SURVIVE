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
dropping it: 0.11.0) is news like a decision. 0.16.3 (analysis bug 5): so is a change of a milestone's unlocks (the
owner's unlock or take-back, and Ember's code's take-back, which the agent never heard of): one line per milestone,
with what stands on it now (``unlock_line``), seen once a plan showed it (policy_grants_seen). The owner's unlocks were
written into the milestone's note instead, which nothing changed when Ember's code took them back.
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
from ..integrations import connectors
from . import policy, roadmap
from .store import REQUEST_DAYS, AgentScope
from .ventures import news_line

# Bytes (JSON-escaped): the planner's YOUR SOFTWARE section holds this much uncut, and only then counts it as read.
# 0.15.0: longer notes come in parts of this size, one a plan (the agent read 1,937 of 0.13.0's 8,456 characters).
CHANGELOG_LIMIT = 2_000
CONTINUED = "Your release notes, continued:\n"
MORE = "\n…(the rest of these notes comes in your next plan)"
_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\s*$")
# An owner item as the agent is shown it: ("message", id, None), ("approval", id, version), ("upgrade", id, status),
# ("venture", id, owner_version) or ("milestone", id, owner_version); 0.16.3 (analysis bug 5): ("unlock", milestone id,
# the newest grant shown). A decision the owner changes again (a new version or status) is news again.
Item = tuple[str, int, int | str | None]
# 0.16.3 (analysis bug 5): a grant the agent hasn't seen, and that changed something (taking back what wasn't unlocked
# is no news): for the news (the grant as ``g``) and the owner's decisions it wakes the agent for.
UNSEEN_GRANT = (
    "NOT EXISTS (SELECT 1 FROM policy_grants_seen s WHERE s.grant_id = g.id) AND (g.level <> 'manual'"
    " OR COALESCE((SELECT h.level FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule"
    " AND h.id < g.id ORDER BY h.id DESC LIMIT 1), 'manual') <> 'manual')"
)
UNLOCKS_SHOWN = 6  # milestones whose unlock changes a plan lists at most (the rest come in the next)


def changelog_key(mode: str) -> str:
    return f"agent.{mode}.changelog_seen"


def changelog_at_key(mode: str) -> str:
    """0.15.0: how far the agent has read the notes of its upgrade: "<seen>><running>@<character>"."""
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
    """0.15.0: the part of the unread notes ``text`` from character ``at`` on that one plan shows, at most
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
    changelog: str = ""  # the part of the unread release notes this plan shows (0.15.0: ``changelog_part``)
    running_version: str = ""
    ventures: list[sqlite3.Row] = field(default_factory=list)  # the owner's word on a venture (0.10.0)
    milestones: list[sqlite3.Row] = field(default_factory=list)  # the owner's word on a milestone (0.11.0)
    changelog_next: int | None = None  # 0.15.0: where the next part begins (None: this one ends the notes)
    changelog_from: str = ""  # 0.15.0: the version the notes begin after ("" when none was read before)
    # 0.16.3 (analysis bug 5): the unlock changes the agent hasn't seen (grants, with their milestone's title), oldest
    # first, and the unlocks that stand now on each milestone (policy.standing)
    unlocks: list[sqlite3.Row] = field(default_factory=list)
    standing: dict[int, list[sqlite3.Row]] = field(default_factory=dict)

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
                elif executor == "kdp_package":  # 0.24.0
                    head += ". Your owner publishes it at KDP and reports back with its link"
                elif executor in connectors.EXECUTORS:  # 0.19.2: a Bluesky post was "your owner will carry it out"
                    head += ". Ember's code carries it out and you'll hear the result"
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
        """The owner's word on ventures, then on milestones (their lines follow the decisions'), then (0.16.3, analysis
        bug 5) the changes of the milestones' unlocks."""
        return [
            *(news_line(r) for r in self.ventures),
            *(roadmap.news_line(r) for r in self.milestones),
            *(unlock_line(changes, self.standing.get(mid, [])) for mid, changes in self._unlocks()),
        ]

    def items(self) -> list[Item]:
        """The items of ``approval_lines()``, ``upgrade_lines()`` and ``venture_lines()``, in the same order."""
        return [
            *(("approval", r["id"], r["version"]) for r in self.decided),
            *(("upgrade", r["id"], r["status"]) for r in self.upgrades),
            *(("venture", r["id"], r["owner_version"]) for r in self.ventures),
            *(("milestone", r["id"], r["owner_version"]) for r in self.milestones),
            *(("unlock", mid, max(int(g["id"]) for g in changes)) for mid, changes in self._unlocks()),
        ]

    def _unlocks(self) -> list[tuple[int, list[sqlite3.Row]]]:
        """The unseen unlock changes by milestone (the milestone changed first, first), at most UNLOCKS_SHOWN."""
        found: dict[int, list[sqlite3.Row]] = {}
        for g in self.unlocks:
            found.setdefault(int(g["milestone_id"]), []).append(g)
        return list(found.items())[:UNLOCKS_SHOWN]


def unlock_line(changes: list[sqlite3.Row], standing: list[sqlite3.Row]) -> str:
    """0.16.3 (analysis bug 5): the unseen changes of a milestone's unlocks (``changes``, oldest first: the newest of
    each rule is said), who made them and why, and the other unlocks that stand on it (``standing``: all of them, from
    policy.standing), or that none does, for the news."""
    newest = {str(g["rule"]): g for g in changes}
    said: dict[tuple[str, str], list[str]] = {}  # (who did what, why): the rules, so a switch's are one clause
    for g in sorted(newest.values(), key=lambda g: int(g["id"])):
        if g["level"] != "manual":
            said.setdefault(("your owner unlocked", ""), []).append(policy.granted_text(g))
        elif g["by"] in policy.CODE:
            said.setdefault(("Ember's code took back", str(g["why"] or "")), []).append(policy.RULES[g["rule"]].label)
        else:
            how = policy.SWITCHES.get(str(g["why"] or ""), "")
            said.setdefault(("your owner took back", how), []).append(policy.RULES[g["rule"]].label)
    clauses = [f"{did} {_listed(rules)}" + (f" ({why})" if why else "") for (did, why), rules in said.items()]
    others = [g for g in standing if g["rule"] not in newest]
    if not standing:
        now = " Nothing is unlocked on it now: its requests wait for your owner's click."
    elif others:
        now = f" Still unlocked on it: {policy.unlocked_text(others)}."
    else:
        now = ""
    first = changes[0]
    return f"Unlocks of milestone #{first['milestone_id']} {_q(first['milestone_title'])}: {'; '.join(clauses)}.{now}"


def _listed(items: list[str]) -> str:
    """Items in words: "a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


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
    granted, granted_params = scope.where("g")
    unlocks = conn.execute(  # 0.16.3 (analysis bug 5)
        f"SELECT g.*, m.title AS milestone_title FROM policy_grants g JOIN milestones m ON m.id = g.milestone_id"
        f" WHERE {granted} AND {UNSEEN_GRANT} ORDER BY g.id LIMIT 100",
        granted_params,
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
        unlocks=unlocks,
        standing=policy.standing(conn, scope) if unlocks else {},
    )


def decided_unseen(conn: sqlite3.Connection, scope: AgentScope) -> bool:
    """0.12.0: whether the owner decided something the agent hasn't seen yet (a request decided or closed, their word
    on a venture or a milestone; 0.16.3, analysis bug 5: their unlock or take-back, not Ember's code's): what their
    decision wakes the agent for."""
    where, params = scope.where()
    if any(
        conn.execute(f"SELECT 1 FROM {table} WHERE {where} AND {condition} LIMIT 1", params).fetchone()
        for table, condition in (
            ("approvals", "status <> 'pending' AND seen_cycle_id IS NULL"),
            ("ventures", "owner_action IS NOT NULL AND seen_cycle_id IS NULL"),
            ("milestones", "owner_action IS NOT NULL AND seen_cycle_id IS NULL"),
        )
    ):
        return True
    granted, granted_params = scope.where("g")
    marks = ", ".join("?" for _ in policy.CODE)
    owners = conn.execute(
        f"SELECT 1 FROM policy_grants g WHERE {granted} AND g.by NOT IN ({marks}) AND {UNSEEN_GRANT} LIMIT 1",
        (*granted_params, *policy.CODE),
    )
    return owners.fetchone() is not None


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
        elif kind == "unlock":
            # 0.16.3 (analysis bug 5): the changes up to the newest one shown; a later one stays news
            conn.execute(
                "INSERT OR IGNORE INTO policy_grants_seen (grant_id, cycle_id) SELECT id, ? FROM policy_grants"
                " WHERE milestone_id = ? AND id <= ?",
                (cycle_id, item_id, version),
            )


def mark_changelog_seen(db: Database, scope: AgentScope, news: News) -> None:
    """After a plan that showed its part of the notes whole: the next plan shows the next part (0.15.0), and after the
    last part they aren't shown again until the next version."""
    if not news.changelog or parse_version(news.running_version) is None:
        return
    if news.changelog_next is None:
        db.set_meta(changelog_key(scope.mode), news.running_version)
    else:
        db.set_meta(changelog_at_key(scope.mode), f"{news.changelog_from}>{news.running_version}@{news.changelog_next}")
