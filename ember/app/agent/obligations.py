"""What the agent owes (0.12.0), kept by Ember's code.

Promises were only prose: a venture cycle deferred the owner's quick fix, an answer that promised work for later was
forgotten, and a decision of the owner's was shown once. Now Ember's code keeps an obligations ledger:

* promises: what the agent promised its owner in a message (``message_owner`` commits), due on the day it named;
* decisions: a request of the agent's that the owner rejected or carried out, or that failed: it must react;
* misses: a milestone Ember's code closed missed (a metric's): the agent decides what now;

and reads the rest from their own records: the owner's messages waiting for an answer, people's emails waiting for
one (0.13.0, Phase E1: mailstore.inquiries), overdue milestones and live listings with too few photos (0.16.3, analysis
bug 1: and a backed venture's first test due within a week unmet, with what is at stake: stages.owed; 0.18.0: and a
strategy that names a parked or killed venture, ``stale_strategy``: live, it named dropshipping as the priority long
after the agent had parked it, and every plan read it). The plan shows
them first and never cuts them; a pressing one (``pressing``) makes a wake cycle an ordinary one rather than a
venture cycle (0.33.0: a promise or a decision, the owner's; a promise names the line it is about, and until 0.35.0
READY put a line with one due soon first). The agent closes a promise, decision or miss with
``obligation_done``, saying what it did; a promise only once its owner has heard from it since, and a miss also closes
when a milestone replaces it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher, mailstore, qa, site_publisher
from . import roadmap, stages, ventures
from .store import OPEN_STATUSES, AgentScope

KINDS = ("promise", "decision", "miss")
WHAT_CHARS = 100  # of a promise, request or milestone, as the plan shows it
NOTE_CHARS = 60  # of the owner's comment or a result note
LINE_CHARS = 160  # an obligation's line in the plan, at most
SHOWN = 5  # the obligations the plan lists one by one (the most urgent first)
# The plan's OBLIGATIONS is never cut, so it is bounded: its lines can't take more (JSON-escaped UTF-8 bytes).
MAX_BYTES = 2_600
PROMISE_DAYS = 14  # a promise is due within this many days
PRESSING_OVERDUE_DAYS = 3  # a promise overdue this long still makes the cycle an ordinary one
PRESSING_NEW_DAYS = 2  # a decision or a miss is pressing this long
# 0.33.0: what the owner said or was promised decides what a cycle is; a miss of a milestone is ranked, not forced
# (live, four misses on 2026-10-07 took the next cycles' lines and kept every marketing cycle away for two days)
FORCING = ("promise", "decision")
HEADING = "OBLIGATIONS (kept by Ember's code: deal with them first)"
SAME_PROMISE_DAYS = 2  # 0.24.0: a promise due this close to an open one, with most of its words, repeats it
SAME_WORDS = 0.6
STEM_LETTERS = 6  # 0.32.0: "propose", "proposal" and "proposed" are one word to the comparison
_COMMON = frozenset(
    {"and", "the", "for", "with", "from", "all", "again", "your", "you", "our", "this", "that", "then"}
    | {"und", "der", "die", "das", "den", "dem", "des", "mit", "von", "für", "bis"}
)
SINCE_KEY = "agent.{mode}.obligations_since"  # decisions and misses from then on (not the history before)


def keep(conn: sqlite3.Connection, scope: AgentScope, now: str, since: str) -> list[str]:
    """Record the decisions and misses the agent owes a reaction to (from ``since`` on), and close the misses a
    milestone replaced. Returns what happened, for the events."""
    where, params = scope.where()
    happened = []
    decided = conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND (status = 'rejected' AND decided_at >= ?"
        " OR status = 'failed' AND closed_at >= ? OR status = 'done' AND closed_at >= ?"
        " AND (executor IS NULL OR executor IN ('reddit_link', 'kdp_package')))"
        " AND NOT EXISTS (SELECT 1 FROM obligations o WHERE o.approval_id = approvals.id) ORDER BY id",
        (*params, since, since, since),
    ).fetchall()
    for r in decided:
        what = decision_what(r)
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, approval_id)"
            " VALUES (?, ?, 'decision', ?, ?, ?, ?)",
            (scope.mode, scope.session, what, now[:10], now, r["id"]),
        )
        happened.append(f"Obligation: react to request #{r['id']} ({r['status']})")
    missed = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'missed' AND closed_by = 'code'"
        " AND created_by <> 'code' AND closed_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM obligations o WHERE o.milestone_id = milestones.id) ORDER BY id",
        (*params, since),
    ).fetchall()
    for m in missed:
        what = f"milestone #{m['id']} {_q(m['title'])} was missed ({_flat(m['result'], NOTE_CHARS)}): decide what now"
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, milestone_id)"
            " VALUES (?, ?, 'miss', ?, ?, ?, ?)",
            (scope.mode, scope.session, what[:400], now[:10], now, m["id"]),
        )
        happened.append(f"Obligation: decide about missed milestone #{m['id']}")
    owed, owed_params = scope.where("o")
    replaced = conn.execute(
        "SELECT o.id, n.id AS by_id FROM obligations o JOIN milestones n ON n.replaces_id = o.milestone_id"
        f" WHERE {owed} AND o.status = 'open'",
        owed_params,
    ).fetchall()
    for r in replaced:
        close_one(conn, r["id"], f"milestone #{r['by_id']} replaces it", "code", None, now)
    return happened


def decision_what(r: sqlite3.Row) -> str:
    """What a decided request asks of the agent now."""
    title = _q(r["title"])
    if r["status"] == "rejected":
        said = f": {_q(_flat(r['decision_comment'], NOTE_CHARS))}" if r["decision_comment"] else ""
        return f"your owner rejected request #{r['id']} {title}{said}: take it into account"
    if r["status"] == "failed":
        return f"request #{r['id']} {title} failed ({_flat(r['result_note'], NOTE_CHARS)}): decide what now"
    note = f": {_q(_flat(r['result_note'], NOTE_CHARS))}" if r["result_note"] else ""
    return f"your owner carried out request #{r['id']} {title}{note}: take the next step"


def promise(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    message_id: int,
    what: str,
    due: str,
    now: str,
    project_id: int | None = None,
) -> int:
    """A promise the agent made its owner; 0.33.0: with the line it is about (``project_id``), if it named one."""
    cursor = conn.execute(
        "INSERT INTO obligations (mode, session, kind, what, due, created_at, cycle_id, message_id, project_id)"
        " VALUES (?, ?, 'promise', ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, what, due, now, cycle_id, message_id, project_id),
    )
    return int(cursor.lastrowid)


# 0.35.1: the number of one of Ember's listings in a promise's words (Etsy's are 10 digits), and the types a promise
# may name for its product when only one product of that type is open
LISTING_NUMBER = re.compile(r"(?<!\d)(\d{8,12})(?!\d)")
NAMED_TYPES = (
    ("kdp_book", re.compile(r"\bkdp\b", re.IGNORECASE)),
    ("printify_pod", re.compile(r"\bprintify\b", re.IGNORECASE)),
)


def promised_line(conn: sqlite3.Connection, scope: AgentScope, what: str) -> int | None:
    """0.35.1: the product line a promise's words name, when Ember named none: the line of one of Ember's listings whose
    number they name, else the one open product of a type they name (KDP, Printify). None when neither tells (a report
    to the owner, say). Live, a KDP book and two pins for a listing were promised without their line, so no step of
    the plan stood for them and the plan took other work first."""
    for number in LISTING_NUMBER.findall(what):
        line = _working(conn, scope, ventures.listing_project(conn, scope, int(number)))
        if line is not None:
            return line
    where, params = scope.where()
    for name, words in NAMED_TYPES:
        if not words.search(what):
            continue
        rows = conn.execute(
            f"SELECT project_id FROM plan_nodes WHERE {where} AND level = 'product' AND status = 'open'"
            " AND template LIKE ?",
            (*params, f"{name}@%"),
        ).fetchall()
        lines = {line for r in rows if (line := _working(conn, scope, r["project_id"])) is not None}
        if len(lines) == 1:
            return lines.pop()
    return None


def name_line(conn: sqlite3.Connection, obligation_id: int, project_id: int) -> bool:
    """0.33.0: a promise made without a line gets the one its repetition names (once: migration 0085 fixes it)."""
    cursor = conn.execute(
        "UPDATE obligations SET project_id = ? WHERE id = ? AND kind = 'promise' AND status = 'open'"
        " AND project_id IS NULL",
        (project_id, obligation_id),
    )
    return cursor.rowcount == 1


def repeated_promise(conn: sqlite3.Connection, scope: AgentScope, what: str, due: str) -> sqlite3.Row | None:
    """0.24.0: the open promise a new one repeats: due within SAME_PROMISE_DAYS of it, and sharing at least SAME_WORDS
    of the words the two hold (_promise_words). Live, "Bluesky reaction + view delta report for posts #43/#44" became
    promise #28 beside the same report's #20, both due 2026-10-11."""
    new = _promise_words(what)
    day = roadmap.parse_day(due)
    if not new or day is None:
        return None
    where, params = scope.where()
    for row in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind = 'promise' AND status = 'open' ORDER BY id", params
    ).fetchall():
        old = _promise_words(row["what"])
        other = roadmap.parse_day(row["due"])
        if not old or other is None or abs((other - day).days) > SAME_PROMISE_DAYS:
            continue
        named, earlier = {w for w in new if w.startswith("#")}, {w for w in old if w.startswith("#")}
        if named and earlier and not named & earlier:  # 0.32.0: about other listings or requests, not the same
            continue
        if len(new & old) >= SAME_WORDS * len(new | old):
            return row
    return None


def _promise_words(text: str) -> set[str]:
    """A promise's words that say what it is: lower case, a plural's s dropped, without the short and common ones;
    0.32.0: a word of letters by its first STEM_LETTERS (live, "Propose the Haushaltsbuch 2027 KDP book" and "Send the
    Haushaltsbuch 2027 KDP proposal" became promises #32 and #33), a number or a reference whole."""
    words = (w.removesuffix("s") for w in re.findall(r"[#\w/]{3,}", str(text).lower()) if w not in _COMMON)
    return {w[:STEM_LETTERS] if w.isalpha() else w for w in words}


def open_rows(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The open obligations, the most urgent first: the earliest due, promises before decisions and misses."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND status = 'open'"
        " ORDER BY due, CASE kind WHEN 'promise' THEN 0 WHEN 'decision' THEN 1 ELSE 2 END, id",
        params,
    ).fetchall()


def close_one(
    conn: sqlite3.Connection, obligation_id: int, result: str, by: str, cycle_id: int | None, now: str
) -> None:
    conn.execute(
        "UPDATE obligations SET status = 'closed', closed_at = ?, closed_by = ?, closed_cycle_id = ?, result = ?"
        " WHERE id = ? AND status = 'open'",
        (now, by, cycle_id, result[:300], obligation_id),
    )


def told_since(conn: sqlite3.Connection, scope: AgentScope, message_id: int) -> bool:
    """Whether the agent sent its owner a message after message ``message_id`` (the one that promised)."""
    where, params = scope.where()
    found = conn.execute(
        f"SELECT 1 FROM messages WHERE {where} AND sender = 'agent' AND id > ? LIMIT 1", (*params, message_id)
    ).fetchone()
    return found is not None


def pressing(conn: sqlite3.Connection, scope: AgentScope, today: date, messages: bool = True) -> list[str]:
    """What presses: the owner's messages waiting for an answer (0.19.3: only with ``messages``), a promise due by
    tomorrow (or overdue for PRESSING_OVERDUE_DAYS at most) or a decision of the last PRESSING_NEW_DAYS days (0.33.0:
    the owner's, FORCING; a miss presses no more). Empty when nothing presses. Until 0.35.3 it skipped the ventures'
    turn (loop._cycle_kind); since 0.36.0 it keeps the plan's Explore step waiting, so the cycle isn't a venture
    cycle."""
    found = []
    waiting = messages_waiting(conn, scope)
    if waiting and messages:
        found.append(f"{waiting} message{'s' if waiting != 1 else ''} of your owner's to answer")
    for r in open_rows(conn, scope):
        if r["kind"] in FORCING and presses(r, today):
            found.append(f"obligation #{r['id']} ({r['kind']})")
    return found


def messages_waiting(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """The owner's messages waiting for an answer."""
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
            " AND removed_at IS NULL",
            params,
        ).fetchone()[0]
    )


def presses(row: sqlite3.Row, today: date) -> bool:
    """Whether an open obligation makes a cycle an ordinary one (``pressing``): a promise due by tomorrow (or overdue
    for PRESSING_OVERDUE_DAYS at most), a decision or a miss of the last PRESSING_NEW_DAYS days."""
    if row["kind"] == "promise":
        first = (today - timedelta(days=PRESSING_OVERDUE_DAYS)).isoformat()
        return first <= row["due"] <= (today + timedelta(days=1)).isoformat()
    return row["due"] >= (today - timedelta(days=PRESSING_NEW_DAYS)).isoformat()


# 0.28.0: the requests whose work a marketing cycle does (a pin, a Bluesky post, a blog post, the link page, Reddit)
MARKETING_EXECUTORS = frozenset(
    {"pinterest_pin", "bluesky_post", site_publisher.POST, site_publisher.LINKS, "reddit_link"}
)


@dataclass(frozen=True)
class Owed:
    """0.28.0: what an open obligation is about: its product line (None: of no line, so any cycle may meet it) and
    whether it is a marketing cycle's work (else an ordinary cycle's). 0.33.0: whether it is the owner's (``forces``:
    a promise to them, or their decision): only those decide what a cycle is and take a line; a miss is ranked."""

    line: int | None
    marketing: bool = False
    forces: bool = False


def owed(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> Owed:
    """0.28.0: an open obligation's line and kind of work, from the records it points to: a decision by its request
    (ventures.request_line), a miss by its milestone (a line's bar, or a backed venture's milestone while the venture
    has one open project). A decision on a pin, a post, a blog post, the link page or a Reddit post is marketing work
    (until 0.34.0 a listing test bar's push to bring buyers was too). A promise is made in an answer to the owner,
    often about another line than the cycle's: 0.33.0, it has the line it names (project_id), and none without one. No
    obligation has the line of one that is closed, or that the owner's park or kill stopped: no cycle works on it, so
    any cycle may close it."""
    if row["kind"] == "promise":
        return Owed(_working(conn, scope, row["project_id"]), forces=True)
    if row["kind"] == "decision":
        request = conn.execute("SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
        if request is None:
            return Owed(None, forces=True)
        line = _working(conn, scope, ventures.request_line(conn, scope, request))
        return Owed(line, _markets(request), forces=True)
    milestone = conn.execute(
        "SELECT project_id, venture_id FROM milestones WHERE id = ?", (row["milestone_id"],)
    ).fetchone()
    if milestone is None:
        return Owed(None)
    project = milestone["project_id"]
    if project is None and milestone["venture_id"] is not None:
        project = only_project(conn, scope, int(milestone["venture_id"]))
    return Owed(_working(conn, scope, project))  # 0.35.0: no bar's push (a decide-by date makes marketing urgent)


def _markets(request: sqlite3.Row) -> bool:
    return request["executor"] in MARKETING_EXECUTORS


def _working(conn: sqlite3.Connection, scope: AgentScope, project_id: int | None) -> int | None:
    """A line some cycle works on: open, and not stopped by the owner's park or kill (None otherwise)."""
    if project_id is None:
        return None
    row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None or row["status"] not in OPEN_STATUSES:
        return None
    return None if ventures.project_stopped(conn, scope, project_id) is not None else project_id


def only_project(conn: sqlite3.Connection, scope: AgentScope, venture_id: int) -> int | None:
    """0.28.0: a venture's one open project (a backed venture's line), None with none or several."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT id FROM projects WHERE {where} AND venture_id = ? AND status IN {OPEN_STATUSES}",
        (*params, venture_id),
    ).fetchall()
    return int(rows[0]["id"]) if len(rows) == 1 else None


def stale_strategy(conn: sqlite3.Connection, scope: AgentScope, strategy: str) -> str:
    """0.18.0: the line OBLIGATIONS gives a strategy that names a parked or killed venture, or "". A venture counts as
    named by its whole title, or by its number ("#3") with the first long word of its title shortly before it."""
    lowered = strategy.lower()
    named = []
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] not in ("parked", "killed"):
            continue
        title = " ".join(str(v["title"]).split()).lower()
        word = next((w for w in re.findall(r"[^\W\d_]+", title) if len(w) >= 5), "")
        near = word and re.search(rf"{re.escape(word)}\W.{{0,40}}#{v['id']}\b", lowered, re.DOTALL)
        if (title and title in lowered) or near:
            named.append(f"#{v['id']} {_flat(v['title'], 40)} ({v['stage']})")
    if not named:
        return ""
    return (
        f"Your strategy names {', '.join(named[:3])}: rewrite it without it (memory_update strategy, replace), with"
        " what you learned."
    )


def text(conn: sqlite3.Connection, scope: AgentScope, today: date, strategy: str = "") -> str:
    """The plan's OBLIGATIONS, at most SHOWN obligations and a line each for the owner's messages, the overdue
    milestones, the listings with too few photos and (0.18.0) a stale ``strategy``: bounded, so it is never cut. Empty
    when nothing is owed."""
    lines = []
    where, params = scope.where()
    waiting = conn.execute(
        f"SELECT id, created_at FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
        " AND removed_at IS NULL ORDER BY id",
        params,
    ).fetchall()
    if waiting:
        ids = ", ".join(f"#{r['id']}" for r in waiting[:6]) + (
            f" and {len(waiting) - 6} more" if len(waiting) > 6 else ""
        )
        them = "them" if len(waiting) != 1 else "it"  # 0.32.0: live, 4 of 12 cycles gave one to obligation_done too
        lines.append(
            f"- Answer your owner's message{'s' if len(waiting) != 1 else ''} {ids} (FROM YOUR OWNER; waiting since"
            f" {str(waiting[0]['created_at'])[:16].replace('T', ' ')} UTC): message_owner naming {them} in answers"
            f" closes {them}."
        )
    mail = mailstore.inquiries(conn, scope)
    if mail:
        shown = mailstore.INQUIRIES_SHOWN
        ids = ", ".join(f"#{r['id']} ({_age(r['received_at'], today)})" for r in mail[:shown]) + (
            f" and {len(mail) - shown} more" if len(mail) > shown else ""
        )
        waits = "An email from a person waits" if len(mail) == 1 else "Emails from people wait"
        lines.append(
            f"- {waits} for your answer: {ids}: answer with propose_email and reply_to_email_id (guide 'email'),"
            " or inquiry_done when none is needed."
        )
    # 0.22.0 (analysis 0.20.1, FIX NOW 14): what presses first (what made the cycle an ordinary one: ``pressing``), then
    # the rest by due date. The oldest five were shown, and a new decision hid under "and 1 more, due later" while the
    # cycle was about it.
    rows = sorted(open_rows(conn, scope), key=lambda r: not presses(r, today))
    for r in rows[:SHOWN]:
        lines.append(f"- {tag(owed(conn, scope, r).line)}{line(r, today)}")  # 0.28.0: its line
    if len(rows) > SHOWN:
        hidden = sum(1 for r in rows[SHOWN:] if presses(r, today))
        lines.append(
            f"- and {len(rows) - SHOWN} more obligations, "
            + (f"{hidden} of them pressing too." if hidden else "none pressing.")
        )
    lines += [f"- {line}" for line in stages.owed(conn, scope, today)]  # 0.16.3 (analysis bug 1)
    overdue = [
        m
        for m in roadmap.open_milestones(conn, scope)
        if (roadmap.parse_day(m["due"]) or today) < today
        and not roadmap.waiting(m, today)
        and not ventures.is_first_test(m)  # its line is above: only Ember's code or the owner closes it
    ]
    if overdue:
        ids = ", ".join(f"#{m['id']}" for m in overdue[:6]) + (
            f" and {len(overdue) - 6} more" if len(overdue) > 6 else ""
        )
        lines.append(f"- Overdue milestones {ids}: close, move or drop each (YOUR PLAN).")
    few = etsy_publisher.few_photos(conn, scope)
    if few:
        shown = ", ".join(  # 0.28.0: with each listing's line
            f"#{listing_id} ({count}{_on_line(ventures.listing_project(conn, scope, listing_id))})"
            for listing_id, count in few[:4]
        )
        more = f" and {len(few) - 4} more" if len(few) > 4 else ""
        lines.append(
            f"- Live listings with fewer than {qa.MIN_PHOTOS} photos: {shown}{more}: give each the whole"
            " set with propose_etsy_edit."
        )
    stale = stale_strategy(conn, scope, strategy)
    if stale:
        lines.append(f"- {stale}")
    if not lines:
        return ""
    if rows:
        lines.append(
            "Close a promise, decision or miss you have met with obligation_done, saying what you did (a promise once "
            "your owner has heard from you since)."
        )
    while len(lines) > 1 and _bytes("\n".join(lines)) > MAX_BYTES:  # can't happen with the limits above
        lines.pop(-2)
    return "\n".join(lines)


def _age(stamp: str, today: date) -> str:
    try:
        days = (today - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return "?"
    return "today" if days <= 0 else f"{days} d"


def _bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def tag(line_id: int | None) -> str:
    """0.28.0: an obligation's line, first on its line in OBLIGATIONS ("" for one of no line)."""
    return f"[line #{line_id}] " if line_id is not None else ""


def _on_line(line_id: int | None) -> str:
    return f", line #{line_id}" if line_id is not None else ""


LINE_TAG = re.compile(r"\[line #(\d+)\]")


def for_line(text: str, line_id: int | None) -> str:
    """0.28.0: OBLIGATIONS as the work steps of a cycle on product line ``line_id`` see it: what another line owes
    waits for that line's own cycle (Ember's code refuses another line's work in this one)."""
    return LINE_TAG.sub(
        lambda m: m[0] if int(m[1]) == line_id else f"[line #{m[1]}: waits for its own cycle]",
        text,
    )


def line(r: sqlite3.Row, today: date) -> str:
    due = roadmap.parse_day(r["due"]) or today
    if r["kind"] == "promise":
        days = (due - today).days
        when = (
            f"OVERDUE since {r['due']}" if days < 0 else "due today" if days == 0 else f"due {r['due']} (in {days} d)"
        )
        return f"#{r['id']} promise to your owner, {when}: {_q(_flat(r['what'], WHAT_CHARS))}"
    return f"#{r['id']} {r['kind']} ({str(r['created_at'])[:10]}): {_flat(r['what'], LINE_CHARS)}"


def _q(text: Any) -> str:
    return json.dumps(_flat(text, WHAT_CHARS), ensure_ascii=False)


def _flat(text: Any, chars: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= chars else flat[: chars - 1] + "…"
