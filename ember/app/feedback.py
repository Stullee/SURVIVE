"""Feedback from an evaluation (0.44.0, vision/evaluation.md): what the owner gives Ember back from an evaluation.

An evaluation of her work is made outside the app, from the evaluation export (export.py). What it finds about her
own work belongs in her learning loop, which already has the slots for it:

* a **case** (what she expected, what happened, why, the cause, how sure, the lesson), added to her cases with a
  subject that begins with "evaluation <its name>": her daily playbook and her weekly look weigh it like her reviews'
  cases (learning.py), so three that agree make a principle established, and her own evidence can dispute it;
* a **correction**, a fact she had wrong with the right one and its source, which reaches her in one message of the
  owner's: it stays in her plans until she answers it;
* 0.47.0: a **proposal for the estate** (vision/generations.md): a verdict on a product she would hand on (adopt,
  passive or retire) with why, from the data. It goes to the owner, never to her: the System tab's Next generation card
  shows it beside the verdict Ember's code's rule suggests, and the owner decides.

The evaluator writes them as a feedback.json (FORMAT, the keys below; vision/evaluation.md, "Feedback"). The owner
uploads it on the Diagnostics tab, reads what Ember would get (``preview``) and gives it (``give``). What never goes
back: scores, the rubric, the exam questions (a score she saw would become a target), and nothing but those keys is
taken, so nothing else rides along. The file comes from an AI session that read text partly written by others:
nothing of it reaches her before the owner has read its preview and given it, and the same file can't be given twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from . import events
from .agent import estate
from .agent.learning import CAUSES, SURE
from .economy.clock import to_iso

if TYPE_CHECKING:
    from .agent.owner import Owner
    from .agent.store import AgentScope

FORMAT = 1
MAX_BYTES = 64_000  # the file
MAX_CASES = 8
MAX_CORRECTIONS = 10
MAX_ESTATE = 40  # 0.47.0: proposals for the estate
PREFIX = "evaluation"  # a case's subject begins with it and the evaluation's name
SUBJECT_CHARS = 80  # a case's subject (the cases table's limit), the prefix and the name included
CASE_LIMITS = {"subject": 56, "expected": 300, "happened": 300, "why": 300, "lesson": 300}
CORRECTION_LIMITS = {"right": 200, "wrong": 200, "source": 120}
MESSAGE_CHARS = 2_000  # an owner's message
NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z .:_-]{0,39}")
KEYS = frozenset({"format", "evaluation", "week", "cases", "corrections", "estate"})
ESTATE_KEYS = frozenset({"kind", "ref", "verdict", "why"})
ESTATE_WHY = 600
CASE_KEYS = frozenset({*CASE_LIMITS, "project_id", "venture_id", "cause", "sure"})
SHOWN = 5  # the feedback given, on the Diagnostics tab


class FeedbackError(ValueError):
    """A feedback that can't be given; ``field`` names what is wrong, the message is for the owner."""

    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


@dataclass(frozen=True)
class Feedback:
    evaluation: str
    week: tuple[str, str] | None
    cases: tuple[dict[str, Any], ...]
    corrections: tuple[dict[str, str], ...]
    payload: str  # its content as JSON with sorted keys: what is kept, and what its SHA-256 is of
    estate: tuple[dict[str, str], ...] = ()  # 0.47.0: proposals for the estate, the owner's (never in her message)

    @property
    def to_ember(self) -> bool:
        """Whether anything of it reaches her: a case or a correction (the estate's proposals don't)."""
        return bool(self.cases or self.corrections)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload.encode()).hexdigest()

    def subject(self, case: dict[str, Any]) -> str:
        return f"{PREFIX} {self.evaluation}: {case['subject']}"[:SUBJECT_CHARS]


def parse(
    conn: sqlite3.Connection, scope: AgentScope, body: Any, products: set[tuple[str, str]] | None = None
) -> Feedback:
    """A feedback.json's content, checked (``products``: the estate's, (kind, ref), which its proposals must name).
    Raises FeedbackError naming the first thing wrong."""
    if not isinstance(body, dict):
        raise FeedbackError("feedback", "a feedback is a JSON object (see vision/evaluation.md, Feedback)")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_BYTES:
        raise FeedbackError("feedback", f"a feedback holds at most {MAX_BYTES:,} characters")
    _keys(body, KEYS, "feedback")
    if body.get("format", FORMAT) != FORMAT:
        raise FeedbackError("format", f"this version of Ember reads feedback of format {FORMAT}")
    name = body.get("evaluation")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise FeedbackError("evaluation", "evaluation names it: 1 to 40 letters, digits, spaces and . : _ -")
    week = _week(body.get("week"))
    projects, ventures = _ids(conn, scope, "projects"), _ids(conn, scope, "ventures")
    cases = tuple(_case(c, n, projects, ventures) for n, c in enumerate(_list(body, "cases", MAX_CASES), start=1))
    corrections = tuple(_correction(c, n) for n, c in enumerate(_list(body, "corrections", MAX_CORRECTIONS), start=1))
    proposals = tuple(_proposal(p, n, products) for n, p in enumerate(_list(body, "estate", MAX_ESTATE), start=1))
    if len({(p["kind"], p["ref"]) for p in proposals}) < len(proposals):
        raise FeedbackError("estate", "estate proposes once for each product")
    if not cases and not corrections and not proposals:
        raise FeedbackError("feedback", "a feedback gives at least one case, one correction or one proposal")
    content = {"format": FORMAT, "evaluation": name, "week": list(week) if week else None}
    content |= {"cases": list(cases), "corrections": list(corrections)}
    if proposals:  # 0.47.0 (a feedback without them keeps the content, and the hash, it had)
        content["estate"] = list(proposals)
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True)
    found = Feedback(name, week, cases, corrections, payload, proposals)
    longest = message_text(found, [10**9] * len(cases))
    if len(longest) > MESSAGE_CHARS:
        raise FeedbackError(
            "corrections",
            f"the message that carries it to Ember would have {len(longest):,} characters of {MESSAGE_CHARS:,}:"
            " shorten the corrections, or give fewer",
        )
    return found


def _keys(item: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    other = sorted(set(item) - allowed)
    if other:
        raise FeedbackError(where, f"{where} takes only {', '.join(sorted(allowed))}, not {', '.join(other)}")


def _list(body: dict[str, Any], key: str, most: int) -> list[Any]:
    items = body.get(key, [])
    if not isinstance(items, list) or len(items) > most:
        raise FeedbackError(key, f"{key} is a list of at most {most}")
    return items


def _week(value: Any) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        first, last = (date.fromisoformat(v) for v in value)
    except (TypeError, ValueError):
        raise FeedbackError("week", 'week is ["YYYY-MM-DD", "YYYY-MM-DD"], its first and its last day') from None
    if first > last:
        raise FeedbackError("week", "the week's first day comes before its last")
    return first.isoformat(), last.isoformat()


def _ids(conn: sqlite3.Connection, scope: AgentScope, table: str) -> set[int]:
    where, params = scope.where()
    return {int(r[0]) for r in conn.execute(f"SELECT id FROM {table} WHERE {where}", params)}  # noqa: S608


def _text(item: dict[str, Any], key: str, limit: int, where: str, required: bool = False) -> str:
    value = item.get(key, "")
    if not isinstance(value, str):
        raise FeedbackError(where, f"{where}: {key} is a text")
    text = " ".join(value.split())
    if required and not text:
        raise FeedbackError(where, f"{where} needs its {key}")
    if len(text) > limit:
        raise FeedbackError(where, f"{where}: {key} has {len(text):,} characters, at most {limit:,}")
    return text


def _case(item: Any, number: int, projects: set[int], ventures: set[int]) -> dict[str, Any]:
    where = f"case {number}"
    if not isinstance(item, dict):
        raise FeedbackError(where, f"{where} is an object")
    _keys(item, CASE_KEYS, where)
    case: dict[str, Any] = {
        key: _text(item, key, limit, where, required=key in ("subject", "why")) for key, limit in CASE_LIMITS.items()
    }
    if item.get("cause") not in CAUSES:
        raise FeedbackError(where, f"{where}: cause is one of {', '.join(CAUSES)}")
    if item.get("sure") not in SURE:
        raise FeedbackError(where, f"{where}: sure is one of {', '.join(SURE)}")
    case["cause"], case["sure"] = item["cause"], item["sure"]
    for key, known in (("project_id", projects), ("venture_id", ventures)):
        value = item.get(key)
        if value is not None and (isinstance(value, bool) or value not in known):
            raise FeedbackError(where, f"{where}: {key} {value!r} is none of hers")
        case[key] = value
    return case


def _correction(item: Any, number: int) -> dict[str, str]:
    where = f"correction {number}"
    if not isinstance(item, dict):
        raise FeedbackError(where, f"{where} is an object")
    _keys(item, frozenset(CORRECTION_LIMITS), where)
    return {key: _text(item, key, limit, where, required=key == "right") for key, limit in CORRECTION_LIMITS.items()}


def _proposal(item: Any, number: int, products: set[tuple[str, str]] | None) -> dict[str, str]:
    """0.47.0: a proposal for the estate: a product of the hand-over (its kind and ref, as the Next generation card
    and the export's estate, etsy_listings, printify_products and approvals name them), a verdict and why."""
    where = f"estate {number}"
    if not isinstance(item, dict):
        raise FeedbackError(where, f"{where} is an object")
    _keys(item, ESTATE_KEYS, where)
    kind, ref = item.get("kind"), item.get("ref")
    if kind not in estate.KINDS:
        raise FeedbackError(where, f"{where}: kind is one of {', '.join(estate.KINDS)}")
    if isinstance(ref, int) and not isinstance(ref, bool):
        ref = str(ref)
    if not isinstance(ref, str) or not 1 <= len(ref) <= 40:
        raise FeedbackError(where, f"{where}: ref names the product (its listing, its Printify product, its request)")
    if item.get("verdict") not in estate.VERDICTS:
        raise FeedbackError(where, f"{where}: verdict is one of {', '.join(estate.VERDICTS)}")
    if products is not None and (kind, ref) not in products:
        raise FeedbackError(where, f"{where}: there is no {estate.KIND_WORDS[kind]} {ref} in the shop now")
    return {"kind": kind, "ref": ref, "verdict": item["verdict"], "why": _text(item, "why", ESTATE_WHY, where, True)}


def _products(conn: sqlite3.Connection, owner: Owner) -> set[tuple[str, str]]:
    return {(p.kind, p.ref) for p in estate.inventory(conn, owner.scope, owner.clock)}


def message_text(feedback: Feedback, case_ids: list[int]) -> str:
    """The owner's message that carries it to Ember: the corrections, and where the cases are."""
    week = f" of your week {feedback.week[0]} to {feedback.week[1]}" if feedback.week else ""
    lines = [f'From the evaluation{week} ("{feedback.evaluation}"):']
    if feedback.corrections:
        lines += ["", "Facts to correct, the right one first:"]
        for c in feedback.corrections:
            had = f" You had: {c['wrong']}." if c["wrong"] else ""
            source = f" Source: {c['source']}." if c["source"] else ""
            lines.append(f"- {c['right'].rstrip('.')}.{had}{source}")
    if case_ids:
        cases = f"#{case_ids[0]}" if len(case_ids) == 1 else f"#{case_ids[0]} to #{case_ids[-1]}"
        lines += [
            "",
            f"{len(case_ids)} case{'s' if len(case_ids) != 1 else ''} of your own work joined your cases ({cases},"
            f' subjects beginning "{PREFIX} {feedback.evaluation}"): your playbook weighs them like your reviews\''
            " cases. If your records say otherwise, tell me.",
        ]
    return "\n".join(lines)


def preview(owner: Owner, body: Any) -> dict[str, Any]:
    """What Ember would get, written nowhere: the cases as they'd be kept, the message, and whether it was given."""
    with owner.db.connection() as conn:
        found = parse(conn, owner.scope, body, _products(conn, owner))
        given = _given(conn, owner.scope, found.sha256)
    message = message_text(found, [0] * len(found.cases)).replace("#0 to #0", "#… to #…").replace("#0", "#…")
    return {
        "evaluation": found.evaluation,
        "week": list(found.week) if found.week else None,
        "cases": [{**c, "subject": found.subject(c)} for c in found.cases],
        "corrections": list(found.corrections),
        "estate": list(found.estate),  # 0.47.0: for the owner's card, not for her
        "message": message if found.to_ember else "",
        "given": given,
    }


def give(owner: Owner, body: Any, who: str | None) -> dict[str, Any]:
    """Gives Ember a feedback: its cases joins hers, its message is sent, and it is kept. Raises FeedbackError (409
    for a feedback given already); nothing is written then."""
    scope = owner.scope
    with owner.db.transaction() as conn:
        found = parse(conn, scope, body, _products(conn, owner))
        if _given(conn, scope, found.sha256):
            raise FeedbackError("feedback", "this feedback was given to Ember already", 409)
        now = to_iso(owner.clock.now())
        if found.estate:  # 0.47.0: beside the rule's suggestion on the owner's card
            estate.refresh(conn, scope, owner.clock)
            for p in found.estate:
                try:
                    estate.propose(
                        conn,
                        scope,
                        owner.clock,
                        p["kind"],
                        p["ref"],
                        p["verdict"],
                        p["why"],
                        f"{PREFIX} {found.evaluation}",
                    )
                except estate.EstateError as exc:
                    raise FeedbackError("estate", str(exc), exc.status) from exc
        ids = [
            int(
                conn.execute(
                    "INSERT INTO cases (mode, session, review_id, subject, project_id, venture_id, expected, happened,"
                    " why, cause, sure, lesson, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope.mode,
                        scope.session,
                        found.subject(c),
                        c["project_id"],
                        c["venture_id"],
                        c["expected"],
                        c["happened"],
                        c["why"],
                        c["cause"],
                        c["sure"],
                        c["lesson"],
                        now,
                    ),
                ).lastrowid
            )
            for c in found.cases
        ]
        message_id = None  # 0.47.0: a feedback of proposals for the estate only sends her nothing
        if found.to_ember:
            reply = owner.send_message({"text": message_text(found, ids)}, who)  # in this transaction
            if reply.status != 201:
                error = str(reply.body.get("error", "the message couldn't be sent"))
                raise FeedbackError("message", error, reply.status)
            message_id = int(reply.body["id"])
        cursor = conn.execute(
            "INSERT INTO evaluation_feedback (mode, session, created_at, entered_by, evaluation, week_from, week_to,"
            " sha256, cases, message_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.mode,
                scope.session,
                now,
                who,
                found.evaluation,
                found.week[0] if found.week else None,
                found.week[1] if found.week else None,
                found.sha256,
                json.dumps(ids),
                message_id,
                found.payload,
            ),
        )
        events.record(
            owner.db,
            "info",
            "owner",
            f'{who or "The owner"} gave Ember the feedback of the evaluation "{found.evaluation}":'
            f" {len(ids)} cases, {len(found.corrections)} corrections, {len(found.estate)} proposals for the estate",
        )
    return {"id": int(cursor.lastrowid), "cases": ids, "message_id": message_id}


def _given(conn: sqlite3.Connection, scope: AgentScope, sha256: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM evaluation_feedback WHERE mode = ? AND session = ? AND sha256 = ?",
            (scope.mode, scope.session, sha256),
        ).fetchone()
        is not None
    )


def given(conn: sqlite3.Connection, scope: AgentScope) -> list[dict[str, Any]]:
    """The newest feedback given, for the Diagnostics tab."""
    rows = conn.execute(
        "SELECT * FROM evaluation_feedback WHERE mode = ? AND session = ? ORDER BY id DESC LIMIT ?",
        (scope.mode, scope.session, SHOWN),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "evaluation": r["evaluation"],
            "week": [r["week_from"], r["week_to"]] if r["week_from"] else None,
            "cases": len(json.loads(r["cases"])),
            "corrections": len(json.loads(r["payload"]).get("corrections", [])),
            "estate": len(json.loads(r["payload"]).get("estate", [])),
            "message_id": r["message_id"],
        }
        for r in rows
    ]
