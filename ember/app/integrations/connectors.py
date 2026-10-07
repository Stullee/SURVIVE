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
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..agent.store import AgentScope


@dataclass(frozen=True)
class ActionClass:
    name: str
    connector: str  # email, etsy, reddit, kdp, or owner (the owner carries it out)
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
        # 0.13.0 (Phase E1): an answer in the thread of someone who wrote to Ember: never a first contact
        _class("email.reply", "answer someone who wrote to Ember, in their thread", {"reaches_people"}),
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
        _class("etsy.auto_renew_off", "turn off a listing's automatic renewal at Etsy", {"owner_identity"}),
        # 0.13.0 (Phase E2): Pinterest, the owner's account
        _class(
            "pinterest.create_pin",
            "pin one of your listings on your Pinterest account",
            {"reaches_people", "owner_identity"},
            undo="delete it",
        ),
        _class(
            "pinterest.create_board", "make a board on your Pinterest profile", {"reaches_people", "owner_identity"}
        ),
        _class("pinterest.delete_pin", "delete a pin of Ember's from Pinterest", {"owner_identity"}),
        # 0.30.2: the test pin of the owner's Standard access request, in Pinterest's API sandbox (seen by them only)
        _class("pinterest.test_pin", "make a test pin in Pinterest's sandbox, seen only by you", set()),
        # 0.19.0: Bluesky, the account the owner made for Ember
        _class(
            "bluesky.create_post",
            "post on Ember's Bluesky account",
            {"reaches_people", "owner_identity"},
            undo="delete it",
        ),
        _class("bluesky.delete_post", "delete a post of Ember's from Bluesky", {"owner_identity"}),
        # 0.13.0 (Phase E4): Printify, the owner's account and their Etsy shop
        _class(
            "printify.create_product",
            "create a product at Printify and publish it to your Etsy shop",
            {"reaches_people", "owner_identity"},
            undo="delete it",
        ),
        _class("printify.delete_product", "delete a product of Ember's at Printify", {"owner_identity"}),
        # 0.14.0: the blog on the owner's website, uploaded over SFTP (site_publisher.py)
        _class(
            "site.publish_post",
            "publish a blog post on your website (with the blog's list)",
            {"reaches_people", "owner_identity"},
            undo="take it down, or put back the version it replaced",
        ),
        _class(
            "site.publish_links",
            "change the link page on your website",
            {"reaches_people", "owner_identity"},
            undo="put back the page it replaced",
        ),
        _class("site.restore", "put back what an upload to your website replaced", {"owner_identity"}),
        # The last will on the live page, shown by Ember's code once the owner approved it (live_view.py)
        _class("site.live_will", "show Ember's last will on your live page", {"reaches_people", "owner_identity"}),
        _class(
            "reddit.post",
            "a Reddit post you publish from your account",
            {"reaches_people", "first_contact", "owner_identity"},
            by_owner=True,
        ),
        # 0.25.0: Amazon KDP has no API: Ember's code prepares the book, the owner publishes it from their account
        _class(
            "kdp.publish",
            "a book you publish at Amazon KDP from your account",
            {"reaches_people", "owner_identity"},
            by_owner=True,
        ),
        _class("owner.create_account", "an account you create", {"owner_identity"}, by_owner=True),
        _class(
            "owner.spend_money", "money you spend", {"reaches_people", "costs_money", "owner_identity"}, by_owner=True
        ),
        _class("owner.other", "something only you can do", set(), by_owner=True),
    )
}
_EXECUTORS = {
    "email": "email.send",
    "etsy_listing": "etsy.create_listing",
    "reddit_link": "reddit.post",
    "kdp_package": "kdp.publish",  # 0.25.0: the owner carries it out (never on an unlock: no rule names it)
    "pinterest_pin": "pinterest.create_pin",  # 0.13.0 (Phase E2)
    "pinterest_delete": "pinterest.delete_pin",  # the owner's Undo of a pin
    "pinterest_test_pin": "pinterest.test_pin",  # 0.30.2: never on an unlock (no rule of the policy engine names it)
    "bluesky_post": "bluesky.create_post",  # 0.19.0: never on an unlock (no rule of the policy engine names it)
    "bluesky_delete": "bluesky.delete_post",  # the owner's Undo of a post
    "printify_product": "printify.create_product",  # 0.13.0 (Phase E4)
    "printify_delete": "printify.delete_product",  # the owner's Undo of a product
    "site_post": "site.publish_post",  # 0.14.0
    "site_links": "site.publish_links",
    "site_restore": "site.restore",  # the owner's Undo of an upload
    "live_will": "site.live_will",  # never on an unlock: no rule of the policy engine names it (never.EXECUTORS)
}
# 0.13.0: every executor Ember's code has (the approvals table takes any short name since 0054: this is the list)
EXECUTORS = frozenset({*_EXECUTORS, "etsy_edit"})
_STATES = {"renew": "etsy.renew", "deactivate": "etsy.deactivate"}


def class_of(executor: str | None, action: Any = None, request_type: str | None = None) -> ActionClass:
    """A request's action class, from its executor (and, for a change to a listing, its action), or for one the owner
    carries out, its type."""
    if executor == "etsy_edit":
        try:
            data = json.loads(action) if isinstance(action, str) else action
        except ValueError:
            data = None
        data = data if isinstance(data, Mapping) else {}
        renewal = data.get("auto_renew")  # 0.13.0: the owner's Undo of an automatic renewal
        if isinstance(renewal, bool):
            return CLASSES["etsy.auto_renew" if renewal else "etsy.auto_renew_off"]
        return CLASSES[_STATES.get(str(data.get("state")), "etsy.edit_listing")]
    if executor == "email":
        try:
            data = json.loads(action) if isinstance(action, str) else action
        except ValueError:
            data = None
        replying = isinstance(data, Mapping) and bool(data.get("in_reply_to"))  # the tool sets it for answers only
        return CLASSES["email.reply" if replying else "email.send"]
    if executor in _EXECUTORS:
        return CLASSES[_EXECUTORS[executor]]
    owned = {"create_account": "owner.create_account", "spend_money": "owner.spend_money"}
    return CLASSES[owned.get(str(request_type), "owner.other")]


def undo_of(name: str, status: str, subject: str | None) -> dict[str, Any] | None:
    """What would undo a finished action of class ``name`` on ``subject`` (a listing's number, a pin's), or None."""
    if status not in ("done", "partial", "simulated") or not subject:
        return None
    if name in ("site.publish_post", "site.publish_links"):  # 0.14.0: subject is the page's path on the server
        return {"action": "restore_site", "path": subject}
    if name == "printify.create_product":  # 0.13.0 (Phase E4): Printify's numbers are hexadecimal
        return {"action": "delete_product", "product_id": subject} if re.fullmatch(r"[0-9a-f]{1,40}", subject) else None
    if name == "bluesky.create_post":  # 0.19.0: subject is the post's record key
        return {"action": "delete_post", "rkey": subject} if re.fullmatch(r"[A-Za-z0-9._:~-]{1,512}", subject) else None
    if not subject.isdigit():
        return None
    if name == "pinterest.create_pin":  # 0.13.0 (Phase E2)
        return {"action": "delete_pin", "pin_id": subject} if status != "partial" else None
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
    name: str | None = None,
) -> int:
    """An executor starts carrying out a request (in the transaction that records its own 'running' row): journaled
    as the request's action class (``class_of``), or ``name`` for a step of its own (a new board before its pin)."""
    row = conn.execute("SELECT mode, session, executor, action, type FROM approvals WHERE id = ?", (approval_id,))
    request = row.fetchone()
    name = name or class_of(request["executor"], request["action"], request["type"]).name
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
