"""The connector protocol (0.13.0): every action Ember's code can carry out, as a class with the same flags, and one
journal every executor writes through.

Each channel had its own executor and its own records, and nothing said across them which actions reach people, cost
money, publish under the owner's name or can be undone, what the policy engine and the audit feed of Phase D need.
Now:

* ``CLASSES``: every action class, the owner's own ones included (a Reddit post, an account, a payment), with its
  flags: reversible (Ember's code can undo it), reaches_people (someone other than the owner sees it), first_contact
  (it can be the first message to someone), costs_money and publishes_under_owner_identity (it appears under the
  owner's name, shop or account), and how it is undone;
* ``class_of``: a request's class, from its executor and action;
* ``begin`` / ``finish`` / ``record``: the shared journal (``action_journal``). The email executor and the Etsy
  publisher write through it at the moments they write their own records (their behaviour is unchanged): the class,
  the request, what it acts on, the state before and after, how it ended, and what would undo it (``undo_of``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..agent.store import AgentScope


@dataclass(frozen=True)
class ActionClass:
    name: str
    connector: str  # email, etsy, reddit, or owner (the owner carries it out)
    what: str  # in the owner's words
    reversible: bool
    reaches_people: bool
    first_contact: bool
    costs_money: bool
    publishes_under_owner_identity: bool
    undo: str = ""  # how Ember's code undoes it ("" if it can't)
    by_owner: bool = False  # the owner carries it out, Ember's code can't

    def flags(self) -> dict[str, Any]:
        return asdict(self)


def _class(name: str, what: str, flags: set[str], undo: str = "", by_owner: bool = False) -> ActionClass:
    return ActionClass(
        name=name,
        connector=name.split(".")[0],
        what=what,
        reversible=bool(undo),
        reaches_people="reaches_people" in flags,
        first_contact="first_contact" in flags,
        costs_money="costs_money" in flags,
        publishes_under_owner_identity="owner_identity" in flags,
        undo=undo,
        by_owner=by_owner,
    )


CLASSES: dict[str, ActionClass] = {
    c.name: c
    for c in (
        _class("email.send", "send an approved email from Ember's mailbox", {"reaches_people", "first_contact"}),
        _class(
            "etsy.create_listing",
            "create a listing in your Etsy shop (USD 0.20)",
            {"reaches_people", "costs_money", "owner_identity"},
            undo="deactivate it",
        ),
        _class(
            "etsy.edit_listing",
            "change a live listing of Ember's",
            {"reaches_people", "owner_identity"},
            undo="change it back",
        ),
        _class(
            "etsy.renew",
            "put an expired or deactivated listing live again (USD 0.20)",
            {"reaches_people", "costs_money", "owner_identity"},
        ),
        _class("etsy.deactivate", "take a listing of Ember's off Etsy", {"owner_identity"}, undo="renew it (USD 0.20)"),
        _class(
            "etsy.auto_renew",
            "turn on a sold listing's automatic renewal (your option)",
            {"costs_money", "owner_identity"},
            undo="turn it off",
        ),
        _class(
            "reddit.post",
            "a Reddit post you publish from your account",
            {"reaches_people", "first_contact", "owner_identity"},
            by_owner=True,
        ),
        _class("owner.create_account", "an account you create", {"owner_identity"}, by_owner=True),
        _class(
            "owner.spend_money", "money you spend", {"reaches_people", "costs_money", "owner_identity"}, by_owner=True
        ),
        _class("owner.other", "something only you can do", set(), by_owner=True),
    )
}
_EXECUTORS = {"email": "email.send", "etsy_listing": "etsy.create_listing", "reddit_link": "reddit.post"}
_STATES = {"renew": "etsy.renew", "deactivate": "etsy.deactivate"}


def class_of(executor: str | None, action: Any = None, request_type: str | None = None) -> ActionClass:
    """A request's action class, from its executor (and, for a change to a listing, its action), or for one the owner
    carries out, its type."""
    if executor == "etsy_edit":
        try:
            data = json.loads(action) if isinstance(action, str) else action
            state = data.get("state") if isinstance(data, Mapping) else None
        except ValueError:
            state = None
        return CLASSES[_STATES.get(str(state), "etsy.edit_listing")]
    if executor in _EXECUTORS:
        return CLASSES[_EXECUTORS[executor]]
    owned = {"create_account": "owner.create_account", "spend_money": "owner.spend_money"}
    return CLASSES[owned.get(str(request_type), "owner.other")]


def undo_of(name: str, status: str, subject: str | None) -> dict[str, Any] | None:
    """What would undo a finished action of class ``name`` on ``subject`` (a listing's number), or None."""
    if status not in ("done", "partial", "simulated") or not subject or not subject.isdigit():
        return None
    listing = int(subject)
    actions = {
        "etsy.create_listing": "deactivate",
        "etsy.edit_listing": "restore",  # the listing as it was before (the entry's before)
        "etsy.deactivate": "renew",
        "etsy.auto_renew": "auto_renew_off",
    }
    if name not in actions or (name == "etsy.create_listing" and status == "partial"):
        return None  # a draft Etsy holds is the owner's to finish or delete
    return {"action": actions[name], "listing_id": listing}


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def begin(
    conn: sqlite3.Connection,
    approval_id: int,
    now: str,
    subject: str | None = None,
    before: Any = None,
) -> int:
    """An executor starts carrying out a request (in the transaction that records its own 'running' row): journaled
    as the request's action class (``class_of``)."""
    row = conn.execute("SELECT mode, session, executor, action, type FROM approvals WHERE id = ?", (approval_id,))
    request = row.fetchone()
    name = class_of(request["executor"], request["action"], request["type"]).name
    cursor = conn.execute(
        "INSERT INTO action_journal (mode, session, approval_id, class, subject, started_at, status, before)"
        " VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
        (request["mode"], request["session"], approval_id, name, (subject or "")[:300] or None, now, _json(before)),
    )
    return int(cursor.lastrowid)


def finish(
    conn: sqlite3.Connection,
    approval_id: int,
    status: str,
    now: str,
    after: Any = None,
    note: str | None = None,
    subject: str | None = None,
) -> None:
    """How a request's action ended (``subject``: what it acted on, once known, like a new listing's number): its
    running entry is finished, with what would undo it. An action begun before the journal existed has none."""
    row = conn.execute(
        "SELECT id, class, subject FROM action_journal WHERE approval_id = ? AND status = 'running'", (approval_id,)
    ).fetchone()
    if row is None:
        return
    what = subject or row["subject"]
    conn.execute(
        "UPDATE action_journal SET status = ?, finished_at = ?, after = ?, undo = ?, note = ?, subject = ?"
        " WHERE id = ?",
        (
            status,
            now,
            _json(after),
            _json(undo_of(str(row["class"]), status, str(what) if what is not None else None)),
            (note or "")[:500] or None,
            str(what)[:300] if what is not None else None,
            row["id"],
        ),
    )


def record(
    conn: sqlite3.Connection,
    scope: AgentScope,
    name: str,
    subject: str,
    status: str,
    now: str,
    before: Any = None,
    after: Any = None,
    note: str | None = None,
) -> None:
    """An action Ember's code takes on its own (no request), begun and finished at once."""
    conn.execute(
        "INSERT INTO action_journal (mode, session, class, subject, started_at, finished_at, status, before, after,"
        " undo, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            name,
            subject[:300],
            now,
            now,
            status,
            _json(before),
            _json(after),
            _json(undo_of(name, status, subject)),
            (note or "")[:500] or None,
        ),
    )


def recent(conn: sqlite3.Connection, scope: AgentScope, limit: int = 30) -> list[sqlite3.Row]:
    """The journal's newest entries, for the dashboard and the audit feed."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM action_journal WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()
