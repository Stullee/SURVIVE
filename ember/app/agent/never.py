"""NEVER (0.13.0): what an unlock of the owner's never carries out, in code and in the database.

The policy engine (policy.py) lets the owner unlock small, safe requests. Some kinds must never run on an unlock,
whatever is granted, and no option changes that (CLASSES):

* account creation: big platforms block automated sign-ups, and Anthropic's usage policy forbids them;
* first contact: writing to someone who never wrote to Ember (UWG section 7: advertising without consent);
* money: moving money or spending it (requests of type spend_money); what an unlock may spend is its budget of
  actions, which the database holds it to as well;
* a first publication: Ember's first listing in the shop, before anything of its went live there, the pin that
  makes its first Pinterest board (0.13.0, Phase E2), or its first Printify product (Phase E4: physical goods bring
  duties of the owner's own): a new public presence needs the owner's decision and their Impressum, DDG section 5;
* posts in third-party communities (Reddit): only the owner posts, from their account;
* tax, VAT, a Gewerbe and contracts: a request whose words touch them (LEGAL_WORDS, LEGAL_PARTS);
* what only the owner carries out: a request without an executor of Ember's code, or with one no rule covers yet;
* the policies themselves: only the owner unlocks; Ember's code only takes unlocks back (policy.set_grant).

``reasons`` says which apply to a request; the policy engine checks it before it holds or approves anything. The
database checks the same again on its own (migration 0051: the view ``approvals_never`` and its triggers), whatever
the code does: an unlock can't hold or approve such a request, can't approve one no standing unlock carries or go
beyond its budget, and Ember's code can't grant an unlock. Both read a request the same way, down to the letters
(SQLite's ``lower()`` lowers A to Z only), and test_never checks they agree.
"""

from __future__ import annotations

import json
import re
import sqlite3
import string
from collections.abc import Mapping
from typing import Any

CLASSES = {
    "account": "creating an account",
    "money": "moving or spending money",
    "first_contact": "a first contact (UWG section 7)",
    "first_publication": (
        "Ember's first publication in your shop, on Pinterest or through Printify (your decision and Impressum, DDG "
        "section 5)"
    ),
    "community_post": "a post in a third-party community",
    "legal": "tax, VAT, a Gewerbe or a contract",
    "owner_only": "something only you carry out",
    "policy": "changing the policies",
}
# The executors this check knows (the rest are what only the owner carries out).
EXECUTORS = ("email", "reddit_link", "etsy_listing", "etsy_edit", "pinterest_pin", "printify_product")
# The words of tax, VAT, a Gewerbe and contracts: whole words, or anywhere in a word (German compounds such as
# Umsatzsteuer, Kleingewerbe, Kaufvertrag). Both spellings of the umlaut, as lower() leaves it.
LEGAL_WORDS = ("tax", "taxes", "taxed", "vat", "ust", "mwst", "contract", "contracts")
LEGAL_PARTS = ("steuer", "gewerbe", "finanzamt", "vertrag", "verträg", "vertrÄg")
_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_WORDS = re.compile(rf"[^a-z](?:{'|'.join(LEGAL_WORDS)})[^a-z]")


def legal(text: str) -> bool:
    """Whether words touch tax, VAT, a Gewerbe or a contract, read as the database reads them."""
    lowered = f" {text.translate(_LOWER)} "
    return any(part in lowered for part in LEGAL_PARTS) or _WORDS.search(lowered) is not None


_WROTE = "SELECT 1 FROM emails WHERE mode = ? AND session = ? AND direction = 'in' AND lower(from_addr) = lower(?)"
_PUBLISHED = "SELECT 1 FROM etsy_listings WHERE mode = ? AND session = ? AND status = 'active'"
_BOARD = "SELECT 1 FROM pinterest_boards WHERE mode = ? AND session = ? AND status = 'active'"
_PRODUCT = "SELECT 1 FROM printify_products WHERE mode = ? AND session = ? AND status IN ('publishing', 'active')"


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> bool:
    return conn.execute(f"{sql} LIMIT 1", params).fetchone() is not None


def _to(row: Mapping[str, Any]) -> Any:
    try:
        data = json.loads(row["action"] or "null")
    except ValueError:
        return None
    return data.get("to") if isinstance(data, dict) else None


def reasons(conn: sqlite3.Connection, row: Mapping[str, Any]) -> list[str]:
    """The NEVER classes a request (a row of approvals) falls in (keys of CLASSES), none when an unlock may carry it.
    What Ember received and published counts in the request's own mode and session."""
    found = []
    if row["type"] == "create_account":
        found.append("account")
    if row["type"] == "spend_money":
        found.append("money")
    scope = (row["mode"], row["session"])
    if row["executor"] == "email":
        to = _to(row)
        wrote = isinstance(to, str) and to != "" and _one(conn, _WROTE, (*scope, to))
        if not wrote:
            found.append("first_contact")
    if row["executor"] == "etsy_listing" and not _one(conn, _PUBLISHED, scope):
        found.append("first_publication")
    if row["executor"] == "pinterest_pin" and not _one(conn, _BOARD, scope):
        found.append("first_publication")
    if row["executor"] == "printify_product" and not _one(conn, _PRODUCT, scope):
        found.append("first_publication")
    if row["executor"] == "reddit_link":
        found.append("community_post")
    if legal(" ".join(str(row[name]) for name in ("title", "description", "payload"))):
        found.append("legal")
    if row["executor"] not in EXECUTORS:
        found.append("owner_only")
    return found


def text(found: list[str]) -> str:
    """The NEVER classes in the owner's words ("" without any)."""
    return "; ".join(CLASSES[k] for k in found)
