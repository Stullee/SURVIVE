"""The learning loop's memory (0.18.0, vision/learning.md): cases and the playbook.

The agent's lessons were a 4 KB file of which the plan saw the newest lines, written when the agent felt like it, so
what it learned was lost or never written, and nothing told a rule backed by many cases from a guess. Now:

* **settled**: Ember's code lists what settled since the last daily review (a bet, a listing bar or another metric
  milestone, a closed project, a parked or killed venture, a request the owner rejected), each with a subject the
  review names ("bet #3");
* **cases**: the review's retrospective of each: what was expected, what happened, why, the cause (worked, wrong idea,
  weak execution, no reach, too early, outside) and how sure; kept without a size limit;
* **principles** (the playbook): the weekly look (weekly.py) draws them from the cases, each with the cases for and
  against it. Ember's code sets the confidence (``confidence``): a hypothesis until ESTABLISHED cases support it and
  none is against it, disputed once a case is; a principle no case confirmed for FADE_DAYS (ESTABLISHED_FADE_DAYS once
  established) is retired;
* **recall**: the plan's LESSONS shows the established principles first, then the hypotheses and disputed ones, then
  the newest lessons; the work steps get the cases and principles that match their plan (``relevant``), and
  knowledge_search finds them.

0.30.0: the playbook grows every day. Only the weekly look wrote principles, once a week from the cases; the first look
after 0.18.0 came right after the first review, when hardly any case existed, a look whose view outgrew its budget
was skipped (weekly.VIEW_CHARS), and a principle the look drew from too_early cases was dropped without a word. Now
each retrospective's lesson joins the playbook as a hypothesis the day its case is kept (``adopt``; a lesson that says
the same as an active principle is one more case for it instead, and three make it established), and the weekly look
curates: it confirms, merges, disputes and retires. What the look's answer can't keep is said (``apply_principles``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..economy.clock import from_iso
from . import bets as bets_
from . import library
from .store import CLOSED_STATUSES, AgentScope

CAUSES = ("worked", "wrong_idea", "weak_execution", "no_reach", "too_early", "outside")
SURE = ("low", "medium", "high")
MAX_SETTLED = 10  # items the review is asked about at once (the most recent first)
MAX_RETROS = 10
ESTABLISHED = 3  # cases that support a principle, with none against it
FADE_DAYS = 42  # a hypothesis or disputed principle no case confirmed for this long is retired
ESTABLISHED_FADE_DAYS = 120
MAX_PRINCIPLES = 40  # active at once; the weekly look retires or merges past it
LIMITS = {"subject": 80, "expected": 300, "happened": 300, "why": 300, "lesson": 300}
# 0.30.0: a retrospective's lesson joins the playbook (adopt). It says the same as a principle only when nearly all its
# words and word pairs are the principle's and both deny the same things ("pins bring views" isn't "pins bring no
# views"): a lesson that merely looks alike becomes a hypothesis of its own, for the weekly look to merge.
ADOPTED_KEY = "agent.{mode}.{session}.adopted_case"  # the newest case whose lesson was weighed
SAME_WORDS = 0.8
SAME_PAIRS = 0.6
_WORD = re.compile(r"[a-z0-9äöüß]+(?:'[a-z]+)?")
_NEGATIONS = frozenset(
    {"no", "not", "never", "without", "none", "nothing", "nobody", "cannot", "kein", "keine", "nicht", "nie", "ohne"}
)
_STOP = frozenset(
    {"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is", "are", "be", "it", "its", "that", "this"}
    | {"with", "as", "at", "by", "from", "we", "you", "your", "i", "my", "our", "der", "die", "das", "und", "ein"}
)


@dataclass(frozen=True)
class Settled:
    subject: str  # how the review names it: "bet #3", "milestone #12", "project #5", "venture #2", "request #21"
    text: str
    project_id: int | None = None
    venture_id: int | None = None


def _one(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def settled(conn: sqlite3.Connection, scope: AgentScope, since: str) -> list[Settled]:
    """What settled since ``since`` (an ISO time), the newest first, at most MAX_SETTLED."""
    where, params = scope.where()
    found: list[tuple[str, Settled]] = []
    for b in bets_.settled_since(conn, scope, since):
        said = bets_.line(b, b["final"], str(b["status"]))
        found.append(
            (
                str(b["settled_at"]),
                Settled(f"bet #{b['id']}", f"project #{b['project_id']}, {b['status']}: {said}", int(b["project_id"])),
            )
        )
    for m in conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND metric IS NOT NULL AND status IN ('done', 'missed')"
        " AND closed_at >= ? ORDER BY closed_at",
        (*params, since),
    ).fetchall():
        what = f"{m['status']}: {_one(m['title'], 90)} ({_one(m['result'], 120)})"
        found.append((str(m["closed_at"]), Settled(f"milestone #{m['id']}", what, m["project_id"], m["venture_id"])))
    marks = ", ".join("?" for _ in CLOSED_STATUSES)
    for p in conn.execute(
        f"SELECT * FROM projects WHERE {where} AND status IN ({marks}) AND updated_at >= ? ORDER BY updated_at",
        (*params, *CLOSED_STATUSES, since),
    ).fetchall():
        what = f"{p['status']}: {_one(p['title'], 90)} (hypothesis: {_one(p['hypothesis'], 120)})"
        found.append((str(p["updated_at"]), Settled(f"project #{p['id']}", what, int(p["id"]), p["venture_id"])))
    for v in conn.execute(
        f"SELECT * FROM ventures WHERE {where} AND stage IN ('parked', 'killed') AND stage_at >= ? ORDER BY stage_at",
        (*params, since),
    ).fetchall():
        what = f"{v['stage']}: {_one(v['title'], 90)}"
        found.append((str(v["stage_at"]), Settled(f"venture #{v['id']}", what, None, int(v["id"]))))
    for a in conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND status = 'rejected' AND decided_at >= ? ORDER BY decided_at",
        (*params, since),
    ).fetchall():
        said = f': "{_one(a["decision_comment"], 120)}"' if a["decision_comment"] else ""
        what = f"rejected by your owner: {_one(a['title'], 90)}{said}"
        found.append((str(a["decided_at"]), Settled(f"request #{a['id']}", what, a["project_id"], None)))
    found.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in found[:MAX_SETTLED]]


def settled_text(items: list[Settled]) -> str:
    """The scorecard's SETTLED section ("" when nothing settled)."""
    if not items:
        return ""
    lines = ["SETTLED SINCE YOUR LAST REVIEW (write a retrospective of each: retros)"]
    lines += [f"- {i.subject}: {i.text}" for i in items]
    return "\n".join(lines)


def parse_retros(items: Any, subjects: set[str]) -> list[dict[str, str]]:
    """The review's retrospectives that name a settled subject, checked; at most MAX_RETROS, one a subject."""
    found: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(found) >= MAX_RETROS:
            continue
        subject = _one(item.get("subject"), LIMITS["subject"])
        if subject not in subjects or any(r["subject"] == subject for r in found):
            continue
        if item.get("cause") not in CAUSES or item.get("sure") not in SURE:
            continue
        retro = {key: _one(item.get(key), limit) for key, limit in LIMITS.items()}
        if not retro["why"]:
            continue
        retro["cause"], retro["sure"] = str(item["cause"]), str(item["sure"])
        found.append(retro)
    return found


def save_cases(
    conn: sqlite3.Connection,
    scope: AgentScope,
    review_id: int | None,
    retros: list[dict[str, str]],
    items: list[Settled],
    now: str,
) -> list[int]:
    by_subject = {i.subject: i for i in items}
    made = []
    for r in retros:
        item = by_subject.get(r["subject"])
        cursor = conn.execute(
            "INSERT INTO cases (mode, session, review_id, subject, project_id, venture_id, expected, happened, why,"
            " cause, sure, lesson, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.mode,
                scope.session,
                review_id,
                r["subject"],
                item.project_id if item else None,
                item.venture_id if item else None,
                r["expected"],
                r["happened"],
                r["why"],
                r["cause"],
                r["sure"],
                r["lesson"],
                now,
            ),
        )
        made.append(int(cursor.lastrowid))
    return made


def case_line(c: Any) -> str:
    lesson = f" Lesson: {c['lesson']}" if c["lesson"] else ""
    return f"case #{c['id']} ({c['subject']}, {c['cause'].replace('_', ' ')}, {c['sure']}): {c['why']}{lesson}"


def cases(conn: sqlite3.Connection, scope: AgentScope, since: str | None = None, limit: int = 200) -> list[Any]:
    where, params = scope.where()
    if since is None:
        return conn.execute(f"SELECT * FROM cases WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    return conn.execute(
        f"SELECT * FROM cases WHERE {where} AND created_at >= ? ORDER BY id DESC LIMIT ?", (*params, since, limit)
    ).fetchall()


# --- the playbook ---


def confidence(supports: list[int], against: list[int]) -> str:
    if against:
        return "disputed"
    return "established" if len(set(supports)) >= ESTABLISHED else "hypothesis"


def principles(conn: sqlite3.Connection, scope: AgentScope) -> list[Any]:
    """The active principles: established first, then hypotheses, then disputed ones; the newest first in each."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM principles WHERE {where} AND status = 'active' ORDER BY CASE confidence WHEN 'established'"
        " THEN 0 WHEN 'hypothesis' THEN 1 ELSE 2 END, confirmed_at DESC, id DESC",
        params,
    ).fetchall()


def _ids(text: Any) -> list[int]:
    try:
        items = json.loads(text or "[]")
    except ValueError:
        return []
    return [int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)]


def cited(p: Any) -> tuple[list[int], list[int]]:
    """0.30.0: a principle's cases for and against it (the owner's Mind → Playbook shows them)."""
    return _ids(p["supports"]), _ids(p["against"])


def principle_line(p: Any) -> str:
    supports, against = _ids(p["supports"]), _ids(p["against"])
    count = f"{len(supports)} case(s) for" + (f", {len(against)} against" if against else "")
    return f"[{p['confidence']}, {count}] {p['text']} (principle #{p['id']})"


def apply_principles(
    conn: sqlite3.Connection, scope: AgentScope, answer: Any, known_cases: set[int], now: str
) -> list[str]:
    """The weekly look's principles: new ones, ones it confirms or disputes (``id``), ones it retires; each cites
    cases that exist (others are dropped). Returns what happened."""
    happened: list[str] = []
    where, params = scope.where()
    active = {int(p["id"]): p for p in principles(conn, scope)}
    # 0.22.0 (analysis 0.20.1, FIX NOW 14): a too_early case is no evidence ("a small number is too_early, not a
    # lesson", as the review is told): it neither supports a principle nor counts against one
    early = {int(r[0]) for r in conn.execute(f"SELECT id FROM cases WHERE {where} AND cause = 'too_early'", params)}
    evidence = known_cases - early
    for item in answer if isinstance(answer, list) else []:
        if not isinstance(item, dict):
            continue
        supports = [i for i in _ids(json.dumps(item.get("supports") or [])) if i in evidence]
        against = [i for i in _ids(json.dumps(item.get("against") or [])) if i in evidence]
        text = _one(item.get("text"), 300)
        pid = item.get("id")
        retire = _one(item.get("retire"), 200)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid in active:
            old = active[pid]
            if retire:
                conn.execute(
                    "UPDATE principles SET status = 'retired', retired_at = ?, retired_why = ? WHERE id = ?",
                    (now, retire, pid),
                )
                happened.append(f"retired principle #{pid}: {retire}")
                continue
            all_for = sorted(set(_ids(old["supports"])) | set(supports))
            all_against = sorted(set(_ids(old["against"])) | set(against))
            level = confidence(_own(all_for, pid, active), all_against)
            if level == "hypothesis" and old["confidence"] == "established" and not all_against:
                level = "established"  # 0.23.1: what was established stays so; sharing cases only gates a promotion
            confirmed = now if set(supports) - set(_ids(old["supports"])) else old["confirmed_at"]
            # 0.22.0: only a hypothesis is reworded; what its cases established keeps its words (an established
            # principle's text could be swapped wholesale, its confidence kept)
            kept = text if text and old["confidence"] == "hypothesis" else old["text"]
            conn.execute(
                "UPDATE principles SET text = ?, supports = ?, against = ?, confidence = ?, confirmed_at = ?"
                " WHERE id = ?",
                (kept, json.dumps(all_for), json.dumps(all_against), level, confirmed, pid),
            )
            happened.append(f"principle #{pid} is {level}")
            continue
        if not text or not supports:
            if text:  # 0.30.0: said, so the next look and the owner see why the playbook didn't take it
                quoted = json.dumps(text[:60], ensure_ascii=False)
                happened.append(f"no principle from {quoted}: {_unbacked(item, known_cases)}")
            continue  # a new principle needs a case behind it
        if len(active) >= MAX_PRINCIPLES:
            happened.append(f"no room for a new principle ({MAX_PRINCIPLES} active): retire or merge one first")
            continue
        level = confidence(_own(supports, None, active), against)
        cursor = conn.execute(
            "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.mode,
                scope.session,
                text,
                json.dumps(sorted(set(supports))),
                json.dumps(sorted(set(against))),
                level,
                now,
                now,
            ),
        )
        active[int(cursor.lastrowid)] = conn.execute(
            f"SELECT * FROM principles WHERE {where} AND id = ?", (*params, cursor.lastrowid)
        ).fetchone()
        happened.append(f"new principle #{cursor.lastrowid} ({level}): {text[:80]}")
    return happened


def _unbacked(item: dict[str, Any], known: set[int]) -> str:
    """0.30.0: why a new principle of the weekly look's has no case behind it."""
    cited = _ids(json.dumps(item.get("supports") or []))
    if not cited:
        return "it cites no case for it"
    if not any(i in known for i in cited):
        return "no case of yours has those numbers"
    return "its cases are too_early, which is no evidence yet"


def adopt(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """0.30.0: the lessons of the cases kept since the last call join the playbook (before every plan, so the first
    plan after 0.30.0 adopts the cases kept before it): each as a hypothesis citing its case, or as one more case for
    an active principle that says the same (``_same``). A too_early case is no evidence, a low-sure one too thin, a case
    without a lesson teaches nothing beyond itself, and one a principle already cites was weighed by the weekly look.
    Returns what happened, for the events."""
    key = ADOPTED_KEY.format(mode=scope.mode, session=scope.session)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    after = int(row[0]) if row is not None and str(row[0]).isdigit() else 0
    where, params = scope.where()
    fresh = conn.execute(f"SELECT * FROM cases WHERE {where} AND id > ? ORDER BY id", (*params, after)).fetchall()
    if not fresh:
        return []
    cited: set[int] = set()
    for p in conn.execute(f"SELECT supports, against FROM principles WHERE {where}", params):
        cited |= set(_ids(p["supports"])) | set(_ids(p["against"]))
    happened: list[str] = []
    for c in fresh:
        lesson = _one(c["lesson"], LIMITS["lesson"])
        if int(c["id"]) in cited or c["cause"] == "too_early" or c["sure"] == "low" or not lesson:
            continue
        active = {int(p["id"]): p for p in principles(conn, scope)}
        same = _same(lesson, active)
        if same is not None:
            happened.append(_support(conn, active, same, int(c["id"]), now))
        elif len(active) >= MAX_PRINCIPLES:
            happened.append(
                f"case #{c['id']}'s lesson waits: the playbook holds {MAX_PRINCIPLES} principles (the weekly look"
                " retires or merges some)"
            )
        else:
            cursor = conn.execute(
                "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at, confirmed_at)"
                " VALUES (?, ?, ?, ?, '[]', 'hypothesis', ?, ?)",
                (scope.mode, scope.session, lesson, json.dumps([int(c["id"])]), now, now),
            )
            happened.append(f"new principle #{cursor.lastrowid} (hypothesis) from case #{c['id']}: {lesson[:80]}")
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(max(int(c["id"]) for c in fresh)), now),
    )
    return happened


def _support(conn: sqlite3.Connection, active: dict[int, Any], pid: int, case_id: int, now: str) -> str:
    """0.30.0: one more case for an active principle (``adopt``), its confidence set as the weekly look's confirmation
    sets it (apply_principles)."""
    old = active[pid]
    supports = sorted(set(_ids(old["supports"])) | {case_id})
    against = _ids(old["against"])
    level = confidence(_own(supports, pid, active), against)
    if level == "hypothesis" and old["confidence"] == "established" and not against:
        level = "established"  # 0.23.1's rule: what was established stays so
    conn.execute(
        "UPDATE principles SET supports = ?, confidence = ?, confirmed_at = ? WHERE id = ?",
        (json.dumps(supports), level, now, pid),
    )
    return f"case #{case_id} supports principle #{pid}, which is {level}"


def _shape(text: str) -> tuple[set[str], set[tuple[str, str]], frozenset[str]]:
    """A text's content words, its pairs of neighbouring content words, and the words that deny something."""
    words = _WORD.findall(text.lower())
    denies = frozenset(w for w in words if w in _NEGATIONS or w.endswith("n't"))
    content = [w for w in words if w not in _STOP]
    return set(content), set(zip(content, content[1:], strict=False)), denies


def _overlap(a: set[Any], b: set[Any]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def _same(text: str, active: dict[int, Any]) -> int | None:
    """0.30.0: the active principle a lesson says the same as (SAME_WORDS of their words and SAME_PAIRS of their word
    pairs in common, and the same denials), the oldest first; None if none does."""
    words, pairs, denies = _shape(text)
    for pid in sorted(active):
        other_words, other_pairs, other_denies = _shape(str(active[pid]["text"]))
        if denies != other_denies:
            continue
        if _overlap(words, other_words) >= SAME_WORDS and _overlap(pairs, other_pairs) >= SAME_PAIRS:
            return pid
    return None


def _own(supports: list[int], pid: int | None, active: dict[int, Any]) -> list[int]:
    """0.22.0 (analysis 0.20.1, FIX NOW 14): the cases that count toward a principle's confidence: those no older
    active principle cites in its support (``pid``: this one's, None for a new one). The same 3 cases established a
    principle and its opposite."""
    older = {i for p_id, p in active.items() if pid is None or p_id < pid for i in _ids(p["supports"])}
    return [i for i in supports if i not in older]


def fade(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """Retire the principles no case confirmed for FADE_DAYS (ESTABLISHED_FADE_DAYS once established)."""
    happened = []
    moment = from_iso(now)
    for p in principles(conn, scope):
        days = ESTABLISHED_FADE_DAYS if p["confidence"] == "established" else FADE_DAYS
        if from_iso(str(p["confirmed_at"])) + timedelta(days=days) <= moment:
            why = f"no case confirmed it for {days} days"
            conn.execute(
                "UPDATE principles SET status = 'retired', retired_at = ?, retired_why = ? WHERE id = ?",
                (now, why, p["id"]),
            )
            happened.append(f"Ember's code retired principle #{p['id']}: {why}")
    return happened


def playbook_text(rows: list[Any], budget: int) -> str:
    """The playbook as LESSONS shows it first, within ``budget`` characters ("" without principles)."""
    if not rows:
        return ""
    lines = ["Your playbook (from your cases; Ember's code sets the confidence):"]
    used = len(lines[0])
    for p in rows:
        line = f"- {principle_line(p)}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


# --- recall ---


def relevant(conn: sqlite3.Connection, scope: AgentScope, query: str, limit: int = 4) -> list[str]:
    """The principles and cases that match a plan (``query``), the best first, as lines for the work brief."""
    rows = [("principle", p) for p in principles(conn, scope)] + [("case", c) for c in cases(conn, scope)]
    if not rows:
        return []
    texts = [
        str(r["text"]) if kind == "principle" else f"{r['subject']} {r['why']} {r['lesson']} {r['happened']}"
        for kind, r in rows
    ]
    scored = library.rank(texts, library.terms(query))
    best = sorted(((score, i) for i, (score, _) in enumerate(scored) if score > 0), key=lambda x: (-x[0], x[1]))
    lines = []
    for _, i in best[:limit]:
        kind, r = rows[i]
        lines.append(principle_line(r) if kind == "principle" else case_line(r))
    return lines


def similar(conn: sqlite3.Connection, scope: AgentScope, text: str, limit: int = 2) -> list[str]:
    """The cases most like a new project or venture (its title and pitch), for the answer that creates it."""
    rows = cases(conn, scope)
    if not rows:
        return []
    texts = [f"{c['subject']} {c['expected']} {c['happened']} {c['why']} {c['lesson']}" for c in rows]
    scored = library.rank(texts, library.terms(text))
    best = sorted(((score, i) for i, (score, _) in enumerate(scored) if score > 0), key=lambda x: (-x[0], x[1]))
    return [case_line(rows[i]) for _, i in best[:limit]]
