"""What Ember shows others of the owner's and other people's data (0.11.2).

Two tools, for the diagnostics report above all (the owner shares it to get help):

- ``Masker`` masks email addresses, one-time codes and the tokens in links (a login link, an OAuth code) in a text,
  and leaves out other people's text (emails, web pages) unless the owner asks for everything.
- ``Redactor`` finds the words the owner removed. When the owner removes the text of a message (a password sent by
  mistake), its secret-looking words are registered as salted hashes, never as text (``register``). Any text can then
  be checked word by word: the report, and Ember's code scrubs the agent's memory, open projects and workspace files
  (``Agent.scrub_removed``). The history in the database can't change, so a copy there is only redacted when shown.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

REMOVED = "[removed]"  # a word the owner removed, where Ember keeps or shows it
CODE = "[masked]"  # a one-time code (no word that names a code: masking a masked text changes nothing)
SALT_KEY = "secret.redaction_salt"  # meta: made by migration 0015; the report never shows a secret.* value

# Addresses as they appear in mail headers and texts (no quoted local parts: they don't occur in practice).
_EMAIL = re.compile(r"(?<![\w.+%-])[\w.+%-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
# One-time codes: 4 to 8 digits (or 3 and 3), or 5 to 10 capitals and digits, next to a word that names a code.
_CODE_WORDS = (
    r"(?:codes?|otp|pin|tan|passcode|password|passwort|kennwort|verification|verify|verifizierung\w*|confirm\w*"
    r"|bestätigung\w*|sicherheits\w*|security|einmal\w*|anmelde\w*|login|log-in|sign-in|2fa|one-time|zugangs\w*)"
)
_CODE = r"(?:\d{4,8}|\d{3}[ -]\d{3}|(?-i:(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,10}))"
# Not a code: part of a word, a date or time, an amount, a number or an id ("#12"). Between the word and the
# code: no digit and no "[", so a code already masked ends the search as the code did.
_NOT_AFTER = r"(?<![\w.,:#$€£/-])"
_NOT_BEFORE = r"(?![\w]|[.,:/-]\d)"
_CODE_AFTER = re.compile(rf"(?i)(\b{_CODE_WORDS}\b[^\n\d\[]{{0,40}}?){_NOT_AFTER}({_CODE}){_NOT_BEFORE}")
_CODE_BEFORE = re.compile(rf"(?i){_NOT_AFTER}({_CODE}){_NOT_BEFORE}([^\n\d\[]{{0,30}}?\b{_CODE_WORDS}\b)")
# Links: everything after "?" or "#" can hold a token, and so can a long path segment of letters and digits.
_URL = re.compile(r"https?://[^\s\"'<>()\[\]{}⏎|]+", re.IGNORECASE)
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{20,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PHONE = re.compile(r"(?:\+|0)\d{7,}")
# Other people's text as the tools hand it to the agent: web research and emails (tools.wrap).
_THIRD_PARTY = re.compile(r'<data src="(research|email:[^"]*)" id="([0-9a-f]+)">(.*?)(?:</data id="\2">|\Z)', re.DOTALL)

# Words, as the redactor checks them: separated by spaces, quotes, brackets and the escapes of JSON text.
_SEPARATORS = re.compile(r"(?:\\[nrt\"]|[\s\"'`()\[\]{}<>,;|⏎])+")
_PARTS = re.compile(r"[:=/@]")  # a word's parts are checked too ("login:secret", "user@example.com")
_EDGES = ".:!?*"


def secretish(word: str) -> bool:
    """Whether a word looks like a secret: letters with digits, a long number, an address, or a long mixed token.

    Removing a message registers only these words, so its ordinary words ("the", "Etsy") stay readable elsewhere.
    """
    digits = sum(c.isdigit() for c in word)
    letters = any(c.isalpha() for c in word)
    if "@" in word and "." in word and len(word) >= 6:
        return True
    if digits and letters and len(word) >= 8:
        return True
    if _PHONE.fullmatch(word):  # a phone number; a date, an amount or an id (a listing's) is not a secret
        return True
    kinds = (any(c.islower() for c in word), any(c.isupper() for c in word), digits > 0, not word.isalnum())
    return len(word) >= 20 and sum(kinds) >= 3


def _words(text: str) -> list[tuple[int, int]]:
    """The spans of the words in a text and of their parts, edges without trailing punctuation."""
    spans = []
    start = 0
    for sep in [*_SEPARATORS.finditer(text), None]:
        end = sep.start() if sep else len(text)
        if end > start:
            spans.append((start, end))
            inner = start
            for part in [*_PARTS.finditer(text, start, end), None]:
                stop = part.start() if part else end
                if (inner, stop) != (start, end) and stop > inner:
                    spans.append((inner, stop))
                inner = part.end() if part else end
        start = sep.end() if sep else len(text)
    trimmed = []
    for a, b in spans:
        while a < b and text[a] in _EDGES:
            a += 1
        while b > a and text[b - 1] in _EDGES:
            b -= 1
        if b > a:
            trimmed.append((a, b))
    return trimmed


def _digest(salt: str, word: str) -> str:
    return hashlib.sha256(f"{salt}\x00{word}".encode()).hexdigest()


def salt(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SALT_KEY,)).fetchone()
    if row is None:
        raise RuntimeError("the redaction salt is missing (migration 0015)")
    return row[0]


def register(conn: sqlite3.Connection, message_id: int, text: str, now: str) -> int:
    """Register the secret-looking words of a message the owner removes; returns how many are new."""
    key = salt(conn)
    words = {text[a:b] for a, b in _words(text)}
    added = 0
    for word in sorted(w for w in words if secretish(w)):
        added += conn.execute(
            "INSERT INTO redactions (created_at, message_id, digest) VALUES (?, ?, ?) ON CONFLICT(digest) DO NOTHING",
            (now, message_id, _digest(key, word)),
        ).rowcount
    return added


@dataclass(frozen=True)
class Redactor:
    """Replaces the words the owner removed (known only by their salted hashes)."""

    salt: str = ""
    digests: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.digests)

    def apply(self, text: str) -> str:
        if not self.digests or not text:
            return text
        found = [
            (a, b) for a, b in _words(text) if secretish(text[a:b]) and _digest(self.salt, text[a:b]) in self.digests
        ]
        if not found:
            return text
        out, last = [], 0
        for a, b in sorted(found):
            if a < last:  # a part of a word already replaced
                continue
            out.append(text[last:a])
            out.append(REMOVED)
            last = b
        out.append(text[last:])
        return "".join(out)


def load(conn: sqlite3.Connection) -> Redactor:
    digests = frozenset(row[0] for row in conn.execute("SELECT digest FROM redactions"))
    return Redactor(salt(conn), digests) if digests else Redactor()


@dataclass
class Masker:
    """Masks a text for someone other than the owner: addresses, codes, link tokens and the removed words, and
    (unless ``full``) other people's text from emails and web pages, and their words wherever they were quoted
    (``others``: the emails' subjects and senders' names, and what shows instead)."""

    own_address: str = ""
    redactor: Redactor = field(default_factory=Redactor)
    full: bool = False
    others: dict[str, str] = field(default_factory=dict)
    addresses: dict[str, int] = field(default_factory=dict)  # each other address and its number in this report
    _quoted: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        words = sorted((w for w in self.others if w.strip()), key=len, reverse=True)  # the longest first
        if words:
            self._quoted = re.compile("|".join(rf"(?<!\w){re.escape(w)}(?!\w)" for w in words))

    def __call__(self, text: str) -> str:
        if not text:
            return text
        if not self.full:
            text = leave_out_third_party(text)
            if self._quoted is not None:
                text = self._quoted.sub(lambda m: self.others[m[0]], text)
        text = self.redactor.apply(text)
        text = _URL.sub(_mask_url, text)
        text = _EMAIL.sub(self._address, text)
        text = _CODE_AFTER.sub(lambda m: f"{m[1]}{CODE}", text)
        return _CODE_BEFORE.sub(lambda m: f"{CODE}{m[2]}", text)

    def _address(self, match: re.Match[str]) -> str:
        address = match[0].lower()
        if self.own_address and address == self.own_address.lower():
            return "[Ember's address]"
        number = self.addresses.setdefault(address, len(self.addresses) + 1)
        return f"[email {number}]"


def leave_out_third_party(text: str) -> str:
    """Emails and web research as the tools hand them to the agent, with their text left out (its length stays)."""
    return _THIRD_PARTY.sub(
        lambda m: f'<data src="{m[1]}">[{len(m[3].strip()):,} characters of other people\'s text left out]</data>',
        text,
    )


def _mask_url(match: re.Match[str]) -> str:
    url = match[0]
    tail = ""
    while url and url[-1] in ".,:;!?":  # punctuation after a link isn't part of it
        tail = url[-1] + tail
        url = url[:-1]
    for mark in "?#":
        base, found, rest = url.partition(mark)
        if found:
            url = base + mark + ("[…]" if rest else "")  # "?" alone: a link masked already ("?[…]")
            break
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if path:
        path = "/".join("[…]" if _token(segment) else segment for segment in path.split("/"))
    return f"{scheme}://{host}{slash}{path}{tail}"


def _token(segment: str) -> bool:
    """A path segment that looks like a token (a login link's), not a word or a slug ("haushaltsbuch-2027")."""
    if _UUID.fullmatch(segment):
        return True
    if not _SEGMENT.fullmatch(segment) or not any(c.isdigit() for c in segment):
        return False
    return any(c.isupper() for c in segment) or "-" not in segment
