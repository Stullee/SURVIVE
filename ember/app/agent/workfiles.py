"""0.26.0: which project or venture each file of the agent's workspace was written for.

Ember's code records it as a tool writes a file (tools._run, from the workspace's notices): the focus of the cycle
that wrote it, its plan's project or the venture it studies (a project names its venture itself). A file keeps the
first focus it was written under, and one written without a focus takes the next one's: a product stays filed under
the project it was made for, whichever cycle touches it later. The cycle and the tool of its last write are kept for
the owner's viewer. A file deleted goes from here too. A venture's knowledge file (ventures/<id>-<title>.md) is that
venture's, whatever wrote it, and the ideas of every brainstorm (ventures/ideas.md) no one's (``by_name``).

The files written before 0.26.0 are filed once (``backfill``) from the tool calls that named them: a path written,
copied or drafted, a product's output with the copy and the pictures made next to it, a book's cover, the files a
workshop run kept.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from . import ventures
from .sandbox import Jail
from .store import AgentScope

BACKFILLED = "workspace_files_filed"  # meta key, per mode and session: the files from before 0.26.0 were filed
# A venture's knowledge file and its parts (ventures.file_of): ventures/12-calm-planners.md, ...-2.md
KNOWLEDGE = re.compile(r"^ventures/(\d+)-[^/]*\.md$")
# What a product's maker writes next to its output (products/make.py): the Word copy, the pictures of its pages or
# sheets, a cost statement's cover picture, a KDP cover and its preview (kdp.cover_path).
_MADE_WITH = re.compile(r"^(.+?)(?:\.docx|-page\d{1,3}\.png|-preview\.png|-sheet\d{1,3}\.png|-cover\.(?:png|jpg|pdf))$")
_PRIMARY = (".pdf", ".xlsx", ".docx", ".png", ".jpg", ".json")
_OUTPUT_TOOLS = frozenset({"make_document", "make_spreadsheet", "make_image", "resize_image", "make_cost_statement"})
_TOOLS = (*_OUTPUT_TOOLS, "workspace_write", "draft", "propose_kdp_book")


def record(
    conn: sqlite3.Connection,
    scope: AgentScope,
    noticed: Iterable[tuple[str, str]],
    project_id: int | None,
    venture_id: int | None,
    cycle_id: int,
    tool: str,
    now: str,
) -> None:
    """The files a tool call wrote ("write") or deleted ("delete"), in order: filed under the call's focus."""
    for what, path in noticed:
        if what == "delete":
            conn.execute(
                "DELETE FROM workspace_files WHERE mode = ? AND session = ? AND path = ?",
                (scope.mode, scope.session, path),
            )
            continue
        project, venture = by_name(path) or (project_id, venture_id)
        conn.execute(
            "INSERT INTO workspace_files (mode, session, path, project_id, venture_id, cycle_id, tool, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (mode, session, path) DO UPDATE SET"
            " project_id = CASE WHEN project_id IS NULL AND venture_id IS NULL THEN excluded.project_id"
            " ELSE project_id END,"
            " venture_id = CASE WHEN project_id IS NULL AND venture_id IS NULL THEN excluded.venture_id"
            " ELSE venture_id END,"
            " cycle_id = excluded.cycle_id, tool = excluded.tool, updated_at = excluded.updated_at",
            (scope.mode, scope.session, path, project, venture, cycle_id, tool[:64], now, now),
        )


def filed(conn: sqlite3.Connection, scope: AgentScope) -> dict[str, sqlite3.Row]:
    """Each recorded file's row, by path."""
    rows = conn.execute(
        "SELECT path, project_id, venture_id, cycle_id, tool FROM workspace_files WHERE mode = ? AND session = ?",
        (scope.mode, scope.session),
    ).fetchall()
    return {str(r["path"]): r for r in rows}


def backfill(conn: sqlite3.Connection, scope: AgentScope, jail: Jail, now: str) -> int:
    """The files in the workspace from before 0.26.0, filed under the focus of the cycle whose tool call first wrote
    them (and the cycle and tool that wrote them last); once per mode and session. Returns how many were filed."""
    key = f"agent.{scope.mode}.{scope.session}.{BACKFILLED}"
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone() is not None:
        return 0
    present = {e.path for e in jail.walk(jail.limits.max_entries).files}
    simulated = 1 if scope.mode == "dry_run" else 0
    found: dict[str, list[Any]] = {}  # path -> [project_id, venture_id, cycle_id, tool]

    def wrote(path: str, row: sqlite3.Row, tool: str) -> None:
        entry = found.get(path)
        if entry is None:
            found[path] = [row["project_id"], row["venture_id"], row["cycle_id"], tool]
            return
        if entry[0] is None and entry[1] is None:
            entry[0], entry[1] = row["project_id"], row["venture_id"]
        entry[2], entry[3] = row["cycle_id"], tool

    calls = conn.execute(
        "SELECT t.id, t.tool, t.input, t.cycle_id, c.project_id, c.venture_id, 'call' AS kind FROM tool_calls t"
        " JOIN cycles c ON c.id = t.cycle_id WHERE c.simulated = ? AND c.session = ? AND t.status = 'ok'"
        f" AND t.tool IN ({', '.join('?' for _ in _TOOLS)})"
        " UNION ALL SELECT w.id, 'workshop', w.outputs, w.cycle_id, c.project_id, c.venture_id, 'run'"
        " FROM workshop_runs w JOIN cycles c ON c.id = w.cycle_id WHERE w.mode = ? AND w.session = ?",
        (simulated, scope.session, *_TOOLS, scope.mode, scope.session),
    ).fetchall()
    # Tool calls and workshop runs have ids of their own: in the order of their cycles, then of the calls in each.
    for row in sorted(calls, key=lambda r: (r["cycle_id"], r["kind"] == "run", r["id"])):
        for what, path in _named(row["tool"], row["input"]):
            if what == "delete":
                found.pop(path, None)
            else:
                wrote(path, row, row["tool"])
    # What a maker wrote next to a product it made: the same as the product.
    for path in present - set(found):
        made = _MADE_WITH.match(path)
        base = made.group(1) if made else None
        source = next((found[base + end] for end in _PRIMARY if base and base + end in found), None)
        if source is not None:
            found[path] = list(source)
    known = set(filed(conn, scope))
    new = sorted(present & set(found) - known)
    rows = [(path, *(by_name(path) or found[path][:2]), *found[path][2:]) for path in new]
    conn.executemany(
        "INSERT OR IGNORE INTO workspace_files (mode, session, path, project_id, venture_id, cycle_id, tool,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(scope.mode, scope.session, path, p, v, c, str(t)[:64], now, now) for path, p, v, c, t in rows],
    )
    conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)", (key, str(len(rows)), now))
    return len(rows)


def _named(tool: str, text: str) -> list[tuple[str, str]]:
    """The paths a tool call (or a workshop run's kept files) wrote or deleted, from what it was given."""
    try:
        data = json.loads(text or "null")
    except ValueError:
        return []
    if tool == "workshop":
        return [("write", str(o["path"])) for o in data if isinstance(o, dict) and isinstance(o.get("path"), str)]
    if not isinstance(data, dict):
        return []
    if tool in _OUTPUT_TOOLS and isinstance(data.get("output"), str):
        return [("write", data["output"].strip())]
    if tool == "propose_kdp_book" and isinstance(data.get("spec"), str):
        spec = data["spec"].strip().removesuffix(".json")
        return [("write", f"{spec}-cover{end}") for end in (".jpg", ".pdf", "-preview.png")]
    if tool in ("workspace_write", "draft") and isinstance(data.get("path"), str):
        return [("delete" if data.get("mode") == "delete" else "write", data["path"].strip())]
    return []


def by_name(path: str) -> tuple[int | None, int | None] | None:
    """(project, venture) a file is filed under by its name, whatever cycle wrote it: a venture's knowledge file is
    that venture's, the brainstorms' ideas no one's. None: by the focus it was written under."""
    match = KNOWLEDGE.match(path)
    if match:
        return None, int(match.group(1))
    return (None, None) if path == ventures.IDEAS_FILE else None
