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
* tax, VAT, a Gewerbe and contracts: a request whose own act says words that touch them (LEGAL_WORDS,
  LEGAL_PREFIXES, LEGAL_PARTS). 0.14.0: the act is what is sent or said (an email's subject and text), never a
  listing's product copy or disclaimer ("Mietvertrag" in a checklist's copy is no contract), and its text is
  normalised first (``normalise``), so look-alike letters, invisible characters and other spellings don't slip past;
* what only the owner carries out: a request without an executor of Ember's code, or with one no rule covers yet;
* the policies themselves: only the owner unlocks; Ember's code only takes unlocks back (policy.set_grant).

``reasons`` says which apply to a request; the policy engine checks it before it holds or approves anything. The
database checks the same again on its own (migration 0051: the view ``approvals_never`` and its triggers), whatever
the code does: an unlock can't hold or approve such a request, can't approve one no standing unlock carries or go
beyond its budget, and Ember's code can't grant an unlock. Both read a request the same way, down to the letters
(SQLite's ``lower()`` lowers A to Z only), and test_never checks they agree. 0.14.0: the database can't normalise
text, so Ember's code keeps the normalised act of each request it stores (``act_words``, in the table act_words),
and both read that and the act's own words lowered.
"""

from __future__ import annotations

import json
import re
import sqlite3
import string
import unicodedata
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
# The words of tax, VAT, a Gewerbe and contracts: whole words, the start of a word, or anywhere in a word (German
# compounds such as Umsatzsteuer, Kleingewerbe, Kaufvertrag). Both spellings of the umlaut, as lower() leaves it.
# 0.14.0: tax status, invoices and legal acts in German and English, and VAT's names in the neighbours' languages.
LEGAL_WORDS = ("tax", "taxes", "taxed", "taxable", "taxation", "vat", "ust", "mwst", "gst", "iva", "tva", "btw", "agb")
LEGAL_WORDS += ("customs",)
LEGAL_PREFIXES = ("invoic", "agreement", "licen", "rechnung", "ustg", "umsatzst", "mehrwertst", "kleinunternehm")
LEGAL_PARTS = ("steuer", "gewerbe", "finanzamt", "vertrag", "verträg", "vertrÄg", "contract", "auftrag", "aufträg")
LEGAL_PARTS += ("auftrÄg", "angebot", "vereinbarung", "lizenz", "widerruf", "einfuhr", "verzoll")
_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_WORDS = re.compile(rf"[^a-z](?:{'|'.join(LEGAL_WORDS)})[^a-z]")
_PREFIXES = re.compile(rf"[^a-z](?:{'|'.join(LEGAL_PREFIXES)})")
# 0.14.0: Cyrillic, Greek and other letters that look like Latin ones (after casefolding), and what they look like.
_LOOKALIKES = str.maketrans(
    "авеікјмнорсѕтухһԁӏԛԝүαβεζηικμνορτυχıɑɡ",
    "abeikjmhopcstyxhdlqwyabeznikmvoptuxiag",
)
_GONE = {"Cc", "Cf", "Cn", "Co", "Cs", "Me", "Mn"}  # controls, invisible formats, unassigned, combining marks
_BLANKS = {"ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀"}  # letters and signs that show nothing


def normalise(text: str) -> str:
    """0.14.0: a text as NEVER reads it: compatibility forms folded (NFKC: ｔａｘ, 𝐭𝐚𝐱), casefolded, accents and
    invisible characters out (a soft hyphen or zero-width space inside a word joins it again), look-alike letters as
    the Latin ones, every space a plain one."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    kept = (
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or (unicodedata.category(ch) not in _GONE and ch not in _BLANKS)
    )
    return "".join(kept).translate(_LOOKALIKES)


def _legal(words: str) -> bool:
    return (
        any(part in words for part in LEGAL_PARTS)
        or _PREFIXES.search(words) is not None
        or _WORDS.search(words) is not None
    )


def act_text(executor: str | None, action: Any) -> str:
    """0.14.0: what a request says or sends, the words NEVER reads: an email's subject and text ("" for the rest, a
    listing's copy included). As the database reads them: a part that isn't text counts as empty."""
    if executor != "email":
        return ""
    try:
        data = json.loads(action) if isinstance(action, str) else action
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    subject, body = data.get("subject"), data.get("body")
    return f"{subject if isinstance(subject, str) else ''} {body if isinstance(body, str) else ''}"


def act_words(executor: str | None, action: Any) -> str | None:
    """0.14.0: a request's act, normalised, as Ember's code keeps it for the database (None: it says nothing)."""
    text = act_text(executor, action)
    return normalise(text) if text.strip() else None


def legal(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    """Whether a request's act touches tax, VAT, a Gewerbe or a contract, read as the database reads it: the
    normalised act Ember's code kept, and the act's own words with A to Z lowered."""
    kept = conn.execute("SELECT words FROM act_words WHERE approval_id = ?", (row["id"],)).fetchone()
    raw = act_text(row["executor"], row["action"]).translate(_LOWER)
    return _legal(f" {kept['words'] if kept else ''} {raw} ")


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
    if legal(conn, row):
        found.append("legal")
    if row["executor"] not in EXECUTORS:
        found.append("owner_only")
    return found


def text(found: list[str]) -> str:
    """The NEVER classes in the owner's words ("" without any)."""
    return "; ".join(CLASSES[k] for k in found)
