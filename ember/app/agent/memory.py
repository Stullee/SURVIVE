"""The agent's memory files: strategy, identity and lessons.

They live in the memory folder (a jail, like the workspace) and every version
is kept in ``memory_versions``, so the owner can see how the agent's thinking
changed. Each file has a size cap; appending to a full lessons file drops its
oldest lines, the other files must be rewritten shorter. Appended lines that are
already in the file are skipped. The constitution is not a memory file and can't
be reached from here.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from .. import events
from ..db import Database
from .sandbox import Jail, SandboxError
from .store import AgentScope, sha256

log = logging.getLogger(__name__)

CAPS = {"strategy": 2_000, "identity": 800, "lessons": 4_000}
MAX_APPEND_LINES = 5
_PREFIX = re.compile(r"^[-\s]*(?:\[#c\d+\]\s*)?")
_OWN_TAGS = re.compile(r"^(?:\[#c\d+\]\s*)+")  # cycle tags the model wrote itself: the code adds the real one
SEEDS = {
    "strategy": (
        "# Strategy\n\n"
        "No strategy yet. Write your strategy here; it is the one you see when planning.\n"
        "Start small: find one honest way to be useful that someone might pay for,\n"
        "test it cheaply, and write down what you learn.\n"
    ),
    "identity": "# Identity\n\nI am an AI agent. I work honestly and in the open, with my owner's approval.\n",
    "lessons": "# Lessons\n\n",
}


class MemoryError_(ValueError):  # noqa: N801 - "MemoryError" is a builtin
    """A refused memory update; the message is shown to the agent."""


class Memory:
    def __init__(self, db: Database, jail: Jail, scope: AgentScope) -> None:
        self.db = db
        self.jail = jail
        self.scope = scope

    @staticmethod
    def filename(name: str) -> str:
        if name not in CAPS:
            raise MemoryError_("the memory files are strategy, identity and lessons")
        return f"{name}.md"

    def read(self, name: str) -> str:
        try:
            return self.jail.read(self.filename(name))
        except SandboxError:
            return ""

    def read_all(self) -> dict[str, str]:
        return {name: self.read(name) for name in CAPS}

    def ensure(self, conn: sqlite3.Connection, now: str) -> None:
        """Create missing files, and record files that were changed outside Ember."""
        for name in CAPS:
            path = self.filename(name)
            if not self.jail.exists(path):
                self.jail.write(path, SEEDS[name])
                self._version(conn, name, None, "seed", SEEDS[name], now)
                continue
            content = self.read(name)
            latest = self._latest(conn, name)
            if latest is None or latest["sha256"] != sha256(content):
                self._version(conn, name, None, "external", content, now)
                if latest is not None:
                    events.record(self.db, "warning", "agent", f"The memory file {path} was changed outside Ember")

    def update(self, conn: sqlite3.Connection, name: str, mode: str, content: str, cycle_id: int, now: str) -> str:
        """Change a memory file for the agent; returns the tool result text."""
        path = self.filename(name)
        cap = CAPS[name]
        if mode not in ("replace", "append"):
            raise MemoryError_("mode must be replace or append")
        text = content.strip()
        if not text:
            raise MemoryError_("the content is empty")
        dropped = skipped = 0
        if mode == "replace":
            new = text + "\n"
            if len(new.encode("utf-8")) > cap:
                raise MemoryError_(f"{path} can hold at most {cap:,} bytes; write it shorter")
        else:
            lines = [_OWN_TAGS.sub("", line.strip().lstrip("-").strip()) for line in text.splitlines()]
            lines = [line.strip() for line in lines if line.strip()]
            if len(lines) > MAX_APPEND_LINES:
                raise MemoryError_(f"append at most {MAX_APPEND_LINES} lines at a time")
            current = self.read(name)
            known = {_key(line) for line in current.splitlines()}
            fresh = []
            for line in lines:
                key = _key(line)
                if key not in known:
                    known.add(key)
                    fresh.append(line)
            if not fresh:  # nothing to write, and no new version
                return f"already noted: every line is already in {path}; nothing was added"
            skipped = len(lines) - len(fresh)
            added = "".join(f"- [#c{cycle_id}] {line}\n" for line in fresh)
            new = (current if current.endswith("\n") or not current else current + "\n") + added
            if len(new.encode("utf-8")) > cap:
                if name != "lessons":
                    raise MemoryError_(f"{path} is full ({cap:,} bytes); replace it with a shorter version")
                new, dropped = _drop_oldest(new, cap)
        self.jail.write(path, new)
        self._version(conn, name, cycle_id, "agent", new, now)
        size = len(new.encode("utf-8"))
        note = f", {skipped} line(s) already noted" if skipped else ""
        note += f", {dropped} oldest line(s) dropped" if dropped else ""
        return f"{path}: {'replaced' if mode == 'replace' else 'appended'}{note} ({size:,} of {cap:,} bytes)"

    def _latest(self, conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
        where, params = self.scope.where()
        return conn.execute(
            f"SELECT sha256 FROM memory_versions WHERE {where} AND file = ? ORDER BY id DESC LIMIT 1", (*params, name)
        ).fetchone()

    def _version(
        self, conn: sqlite3.Connection, name: str, cycle_id: int | None, source: str, content: str, now: str
    ) -> None:
        conn.execute(
            "INSERT INTO memory_versions (mode, session, file, cycle_id, created_at, source, sha256, content)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.scope.mode, self.scope.session, name, cycle_id, now, source, sha256(content), content[:8000]),
        )


def _key(line: str) -> str:
    """A line as compared for duplicates: without its "- [#cN] " prefix, case or extra spaces."""
    return " ".join(_PREFIX.sub("", line.strip()).split()).casefold()


def _drop_oldest(text: str, cap: int) -> tuple[str, int]:
    """Drop the oldest lesson lines (keeping the heading) until the file fits."""
    lines = text.splitlines(keepends=True)
    head = [line for line in lines[:2] if not line.startswith("- ")]
    body = lines[len(head) :]
    dropped = 0
    while body and len("".join(head + body).encode("utf-8")) > cap:
        body.pop(0)
        dropped += 1
    return "".join(head + body), dropped
