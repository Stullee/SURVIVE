"""The agent's memory files: strategy, identity and lessons.

They live in the memory folder (a jail, like the workspace) and every version
is kept in ``memory_versions``, so the owner can see how the agent's thinking
changed. Each file has a size cap; appending to a full lessons file drops its
oldest lines, the other files must be rewritten shorter. Appended lines that are
already in the file are skipped. The constitution is not a memory file and can't
be reached from here.

0.12.0: the owner pins lessons (``lesson_pins``): a pinned lesson is never dropped,
a rewrite of the lessons must keep it, and every plan shows it. Once a day, after
the daily review, a call of its own consolidates the lessons (``consolidate``):
Ember's code checks its answer, keeps what it doesn't account for, and never lets
it drop a pinned lesson or one that states a number (a no backed by data).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from typing import Any

from .. import events
from ..db import Database
from .sandbox import Jail, SandboxError
from .store import AgentScope, sha256

log = logging.getLogger(__name__)

CAPS = {"strategy": 2_000, "identity": 800, "lessons": 4_000}
HEADING_REFUSAL = (
    "a line of {name} begins with '=', as only the headings of your context do (== ... ==): begin it otherwise, a "
    "heading of yours with '#'"
)
MAX_APPEND_LINES = 5
MAX_PINS = 6  # the owner's pinned lessons at once (every plan shows them all)
PIN_CHARS = 300  # a pinned lesson
LINE_CHARS = 300  # a lesson the consolidation writes
WHY_CHARS = 100  # why it drops one
CONSOLIDATE_FROM = 12  # lessons in the file before a consolidation is worth its call
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


# 0.12.0: characters that look like "=" at a line's start without being one (NFKC turns most others into "=").
_EQUALS_LOOKALIKES = frozenset("═゠꞊")


def heading_like(line: str) -> bool:
    """Whether a line begins like a heading of the agent's context ("== FROM YOUR OWNER =="): with "=" or a lookalike,
    after any spaces and invisible characters. The agent's own texts can't hold such a line (0.12.0: one could pose as
    its owner's words), and the context shows any other one quoted."""
    for char in line:
        if not (char.isspace() or unicodedata.category(char) == "Cf"):
            return char in _EQUALS_LOOKALIKES or unicodedata.normalize("NFKC", char).startswith("=")
    return False


def heading_line(text: str) -> bool:
    """Whether any line of ``text`` begins like a heading of the agent's context (every kind of line break counts)."""
    return any(heading_like(line) for line in text.splitlines())


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
        if heading_line(text):
            raise MemoryError_(HEADING_REFUSAL.format(name="the content"))
        dropped = skipped = 0
        pinned = {lesson_key(p["text"]): p["text"] for p in pins(conn, self.scope)} if name == "lessons" else {}
        if mode == "replace":
            new = text + "\n"
            if len(new.encode("utf-8")) > cap:
                raise MemoryError_(f"{path} can hold at most {cap:,} bytes; write it shorter")
            missing = [line for key, line in pinned.items() if key not in {lesson_key(n) for n in new.splitlines()}]
            if missing:
                raise MemoryError_(
                    "keep the lessons your owner pinned, word for word: " + "; ".join(json_quote(m) for m in missing)
                )
        else:
            lines = [_OWN_TAGS.sub("", line.strip().lstrip("-").strip()) for line in text.splitlines()]
            lines = [line.strip() for line in lines if line.strip()]
            if len(lines) > MAX_APPEND_LINES:
                raise MemoryError_(f"append at most {MAX_APPEND_LINES} lines at a time")
            current = self.read(name)
            known = {lesson_key(line) for line in current.splitlines()}
            fresh = []
            for line in lines:
                key = lesson_key(line)
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
                new, dropped = _drop_oldest(new, cap, set(pinned))
                if len(new.encode("utf-8")) > cap:
                    raise MemoryError_(f"{path} is full of the lessons your owner pinned: replace it shorter")
        self.jail.write(path, new)
        self._version(conn, name, cycle_id, "agent", new, now)
        size = len(new.encode("utf-8"))
        note = f", {skipped} line(s) already noted" if skipped else ""
        note += f", {dropped} oldest line(s) dropped" if dropped else ""
        return f"{path}: {'replaced' if mode == 'replace' else 'appended'}{note} ({size:,} of {cap:,} bytes)"

    def rewrite(self, conn: sqlite3.Connection, name: str, text: str, source: str, now: str) -> None:
        """0.12.0: Ember's code writes a memory file (the lessons' consolidation), as a new version of ``source``."""
        self.jail.write(self.filename(name), text)
        self._version(conn, name, None, source, text, now)

    def scrub(self, conn: sqlite3.Connection, clean: Callable[[str], str], now: str) -> int:
        """Rewrite the files that ``clean`` changes (the words the owner removed, 0.11.2), each as a new version;
        returns how many changed."""
        changed = 0
        for name in CAPS:
            text = self.read(name)
            new = clean(text)
            if new != text:
                self.jail.write(self.filename(name), new)
                self._version(conn, name, None, "external", new, now)
                changed += 1
        return changed

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


def lesson_key(line: str) -> str:
    """A line as compared for duplicates: without its "- [#cN] " prefix, case or extra spaces."""
    return " ".join(_PREFIX.sub("", line.strip()).split()).casefold()


def _drop_oldest(text: str, cap: int, keep: frozenset[str] | set[str] = frozenset()) -> tuple[str, int]:
    """Drop the oldest lesson lines (keeping the heading and, 0.12.0, the pinned ones: ``keep``) until the file
    fits; 0.12.0: those without numbers first, so a no backed by data outlives them."""
    lines = text.splitlines(keepends=True)
    head = [line for line in lines[:2] if not line.startswith("- ")]
    body = lines[len(head) :]
    dropped = 0
    while len("".join(head + body).encode("utf-8")) > cap:
        droppable = [i for i, line in enumerate(body) if lesson_key(line) not in keep]
        if not droppable:
            break
        body.pop(next((i for i in droppable if not _numbers(body[i])), droppable[0]))
        dropped += 1
    return "".join(head + body), dropped


# --- 0.12.0: the owner's pins and the daily consolidation ---


def json_quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def lesson_text(line: str) -> str:
    """A lesson line as it is pinned and compared: without its "- [#cN] " prefix, on one line."""
    return " ".join(_PREFIX.sub("", line.strip()).split())


def pins(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The lessons the owner pinned (still pinned), the oldest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM lesson_pins WHERE {where} AND unpinned_at IS NULL ORDER BY id", params
    ).fetchall()


def pin(conn: sqlite3.Connection, scope: AgentScope, lessons: str, line: str, who: str | None, now: str) -> int:
    """Pin one of the lessons in ``lessons`` (its text, with or without its tag). Raises MemoryError_."""
    text = lesson_text(line)
    if not text:
        raise MemoryError_("name the lesson to pin")
    if len(text) > PIN_CHARS:
        raise MemoryError_(f"a pinned lesson holds at most {PIN_CHARS} characters")
    if lesson_key(text) not in {lesson_key(n) for n in lessons.splitlines() if n.startswith("- ")}:
        raise MemoryError_("that isn't one of the lessons now")
    pinned = pins(conn, scope)
    if any(lesson_key(p["text"]) == lesson_key(text) for p in pinned):
        raise MemoryError_("that lesson is already pinned")
    if len(pinned) >= MAX_PINS:
        raise MemoryError_(f"at most {MAX_PINS} lessons are pinned at once: unpin one first")
    cursor = conn.execute(
        "INSERT INTO lesson_pins (mode, session, text, created_at, pinned_by) VALUES (?, ?, ?, ?, ?)",
        (scope.mode, scope.session, text, now, who),
    )
    return int(cursor.lastrowid)


def unpin(conn: sqlite3.Connection, scope: AgentScope, pin_id: int, now: str) -> str:
    """Unpin one; returns its text. Raises MemoryError_."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT text FROM lesson_pins WHERE id = ? AND {where} AND unpinned_at IS NULL", (pin_id, *params)
    ).fetchone()
    if row is None:
        raise MemoryError_("no such pinned lesson")
    conn.execute("UPDATE lesson_pins SET unpinned_at = ? WHERE id = ?", (now, pin_id))
    return str(row["text"])


def lesson_lines(text: str) -> tuple[str, list[str]]:
    """(the heading part, the lesson lines "- ...") of a lessons file."""
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("- ")), len(lines))
    head = "\n".join(lines[:first]).rstrip("\n")
    return (head + "\n\n" if head else ""), [line for line in lines[first:] if line.startswith("- ")]


def consolidation_input(text: str, pinned: set[str]) -> str:
    """The lessons as the consolidation reads them: numbered, the pinned ones and those with numbers marked."""
    _, lines = lesson_lines(text)
    shown = []
    for number, line in enumerate(lines, 1):
        marks = [m for m, on in (("pinned", lesson_key(line) in pinned), ("has numbers", _numbers(line))) if on]
        shown.append(f"{number}. {lesson_text(line)}" + (f" ({', '.join(marks)})" if marks else ""))
    return "\n".join(shown)


def consolidate(text: str, answer: Any, pinned: set[str], cap: int) -> tuple[str, str] | None:
    """The lessons file after the consolidation's ``answer`` ({"keep": [{"text", "from"}], "drop": [{"line",
    "why"}]}), and what changed; None when it changes nothing or can't be used. Ember's code keeps every line the
    answer doesn't account for, never drops or rewrites a pinned line, and never drops a line that states a number
    (it may merge it); a merged lesson keeps the newest tag of its lines, and the lessons stay newest last."""
    if not isinstance(answer, dict):
        return None
    head, lines = lesson_lines(text)
    count = len(lines)
    tags = [_tag(line) for line in lines]
    fixed = {i for i, line in enumerate(lines, 1) if lesson_key(line) in pinned}
    kept: list[tuple[int, int, str]] = []
    covered: set[int] = set()
    merged = 0
    for item in answer.get("keep") if isinstance(answer.get("keep"), list) else []:
        if not isinstance(item, dict):
            continue
        body = " ".join(str(item.get("text") or "").split())
        froms = item.get("from") if isinstance(item.get("from"), list) else []
        sources = sorted({i for i in froms if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= count})
        if not body or not sources or len(body) > LINE_CHARS or heading_like(body) or covered & set(sources):
            continue
        if fixed & set(sources):  # a pinned lesson stays as it is
            continue
        covered |= set(sources)
        merged += len(sources) - 1
        newest = max(tags[i - 1] for i in sources)
        kept.append((newest, sources[0], f"- [#c{newest}] {body}" if newest else f"- {body}"))
    dropped: list[str] = []
    for item in answer.get("drop") if isinstance(answer.get("drop"), list) else []:
        if not isinstance(item, dict):
            continue
        line, why = item.get("line"), " ".join(str(item.get("why") or "").split())[:WHY_CHARS]
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= count or line in covered:
            continue
        if line in fixed or _numbers(lines[line - 1]) or not why:
            continue  # a pinned lesson, or a no backed by data: kept
        covered.add(line)
        dropped.append(f"{json_quote(lesson_text(lines[line - 1])[:60])} ({why})")
    for i in range(1, count + 1):
        if i not in covered:
            kept.append((tags[i - 1], i, lines[i - 1]))
    kept.sort(key=lambda k: (k[0], k[1]))
    seen: set[str] = set()
    body_lines = []
    for _, _, line in kept:
        if lesson_key(line) not in seen:
            seen.add(lesson_key(line))
            body_lines.append(line)
    new = head + "".join(f"{line}\n" for line in body_lines)
    if len(new.encode("utf-8")) > cap or _key_lines(new) == _key_lines(text):
        return None
    summary = f"{count} lessons became {len(body_lines)} ({merged} merged into others, {len(dropped)} dropped)"
    if dropped:
        summary += ": dropped " + "; ".join(dropped[:3]) + (f" and {len(dropped) - 3} more" if len(dropped) > 3 else "")
    return new, summary


_DIGIT = re.compile(r"\d")
_TAG = re.compile(r"^-\s*\[#c(\d+)\]")


def _numbers(line: str) -> bool:
    """Whether a lesson states a number (its cycle tag aside): a no backed by data is never dropped."""
    return _DIGIT.search(lesson_text(line)) is not None


def _tag(line: str) -> int:
    found = _TAG.match(line.strip())
    return int(found[1]) if found else 0


def _key_lines(text: str) -> list[str]:
    return [lesson_key(line) for line in lesson_lines(text)[1]]
