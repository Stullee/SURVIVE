"""Etsy (0.8.0): Ember's code lists the products the owner approved in the owner's Etsy shop.

Nothing the agent does reaches Etsy. The agent proposes a listing (``propose_etsy_listing``): title, description,
price, tags, a category, the files buyers download and the listing photos, all from its workspace. The owner
approves it (or changes the words), and Ember's code creates it through the owner's own Etsy app (Open API v3): a
draft, its photos and files, then active. Every description ends with a fixed line saying AI helped make it, as
Etsy's rules require, and the files must still be exactly the ones the owner approved (their SHA-256).

The connection is made once, in the dashboard, with OAuth 2.0 and PKCE. Ember sits behind Home Assistant's Ingress
and can't receive Etsy's redirect, so the owner copies the address Etsy sends them to (the app's registered redirect
URI, which doesn't need to open) back into the dashboard; Ember checks its state and exchanges the code. The tokens
live in a file only Ember reads (``etsy/tokens.json``, mode 0600), never in the database, the logs or the
diagnostics, and are refreshed before they expire.

Ember's code talks to https://api.etsy.com only (an allowlist, like the model's); the owner's browser opens
etsy.com for the connection. In dry run a fake shop takes the listings, so the owner can try the whole flow.

Etsy's API terms (0.8.1): the dashboard and the docs show Etsy's trademark notice (``NOTICE``), research never reads
Etsy's website (only searches it), and the shop is read every hour while Ember runs, its categories daily.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from ..config import Settings
from ..economy.clock import Clock, from_iso, to_iso
from ..logging_setup import register_secret

API_HOST = "api.etsy.com"
API_URL = f"https://{API_HOST}"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
OAUTH_ENDPOINT = "/v3/public/oauth/token"  # where codes and refresh tokens are exchanged
# Reading and writing listings (with their photos and files), reading the shop and its orders.
SCOPES = ("listings_r", "listings_w", "shops_r", "transactions_r")
CONNECT_MINUTES = 15  # how long a started connection waits for the pasted address
REFRESH_EARLY = timedelta(minutes=5)  # an access token this close to expiring is refreshed first

TITLE_CHARS = 140
DESCRIPTION_CHARS = 4_000  # the approval's payload (at most 8,000) holds it with the rest
MAX_TAGS = 13
TAG_CHARS = 20
MAX_FILES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PHOTOS = 10
MIN_PRICE = Decimal("0.20")
MAX_PRICE = Decimal("1000")
QUANTITY = 999  # a digital download never sells out
FILE_KINDS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg"})
PHOTO_KINDS = frozenset({".png", ".jpg"})
DISCLOSURE = "This digital product was designed with the help of AI and reviewed by the seller before listing."
# Etsy's API terms require this notice, word for word and prominently, wherever the application shows Etsy.
NOTICE = (
    "The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified"
    " by Etsy, Inc."
)
# Etsy's rules: tags hold letters, digits, spaces, '-', "'" and ™©®; in a title % : & + may each appear once; a
# downloadable file's name has at most 70 characters.
_TAG = re.compile(r"^(?:[^\W_]|[ \-'™©®])+$")
_ONCE_IN_TITLE = "%:&+"
FILE_NAME_CHARS = 70
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")


class EtsyError(ValueError):
    """Something about a listing or the connection that the agent or the owner can fix; the message says what."""


class NotSent(EtsyError):
    """Etsy refused a request before anything changed."""


class Unclear(EtsyError):
    """A request may or may not have changed something at Etsy (a timeout, a lost connection)."""


# --- what a listing is -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Upload:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Listing:
    """A checked listing: exactly what Ember's code will create (the description without its AI line)."""

    title: str
    description: str
    price: str  # "4.90", in the shop's currency
    currency: str
    tags: tuple[str, ...]
    taxonomy_id: int
    category: str
    files: tuple[Upload, ...]
    photos: tuple[Upload, ...]

    def to_action(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["files"] = [asdict(f) for f in self.files]
        data["photos"] = [asdict(p) for p in self.photos]
        return data


def listing_from_action(raw: str | dict[str, Any]) -> Listing:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise EtsyError("the listing isn't readable")
    try:
        return Listing(
            title=str(data["title"]),
            description=str(data["description"]),
            price=str(data["price"]),
            currency=str(data["currency"]),
            tags=tuple(str(t) for t in data["tags"]),
            taxonomy_id=int(data["taxonomy_id"]),
            category=str(data["category"]),
            files=tuple(Upload(str(f["path"]), str(f["sha256"]), int(f["bytes"])) for f in data["files"]),
            photos=tuple(Upload(str(p["path"]), str(p["sha256"]), int(p["bytes"])) for p in data["photos"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EtsyError(f"the listing isn't readable ({type(exc).__name__})") from None


def check_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip():
        raise EtsyError("the title is empty")
    title = " ".join(title.split())
    if len(title) > TITLE_CHARS:
        raise EtsyError(f"the title has more than {TITLE_CHARS} characters")
    if _CONTROL.search(title):
        raise EtsyError("the title contains control or direction characters")
    for mark in _ONCE_IN_TITLE:
        if title.count(mark) > 1:
            raise EtsyError(f"Etsy allows '{mark}' only once in a title")
    return title


def check_description(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise EtsyError("the description is empty")
    text = text.replace("\r\n", "\n").strip()
    if text.endswith(DISCLOSURE):  # Ember's code adds it; a copy the agent wrote isn't doubled
        text = text[: -len(DISCLOSURE)].rstrip()
    if len(text) > DESCRIPTION_CHARS:
        raise EtsyError(f"the description has more than {DESCRIPTION_CHARS:,} characters")
    if _CONTROL.search(text.replace("\n", "").replace("\t", "")):
        raise EtsyError("the description contains control or direction characters")
    return text


def check_price(price: Any) -> str:
    try:
        value = Decimal(str(price).strip().replace(",", "."))
    except InvalidOperation:
        raise EtsyError("the price must be a number like 4.90") from None
    if not value.is_finite() or not MIN_PRICE <= value <= MAX_PRICE:
        raise EtsyError(f"the price must be between {MIN_PRICE} and {MAX_PRICE}")
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def check_tags(tags: Any) -> tuple[str, ...]:
    items = tags.split(",") if isinstance(tags, str) else tags if isinstance(tags, list) else None
    if items is None:
        raise EtsyError("tags must be words separated by commas")
    clean: list[str] = []
    for item in items:
        tag = " ".join(str(item).replace("’", "'").split())
        if not tag:
            continue
        if len(tag) > TAG_CHARS:
            raise EtsyError(f"the tag '{tag[:30]}' has more than {TAG_CHARS} characters")
        if not _TAG.match(tag):
            raise EtsyError(f"the tag '{tag[:30]}' may only hold letters, digits, spaces, '-' and apostrophes (')")
        if tag.lower() not in (t.lower() for t in clean):
            clean.append(tag)
    if not clean:
        raise EtsyError("give at least one tag")
    if len(clean) > MAX_TAGS:
        raise EtsyError(f"Etsy allows at most {MAX_TAGS} tags")
    return tuple(clean)


def upload(path: str, data: bytes, kinds: frozenset[str], what: str) -> Upload:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in kinds:
        raise EtsyError(f"{path}: {what} must be {', '.join(sorted(kinds))} files")
    if not data:
        raise EtsyError(f"{path} is empty")
    if len(data) > MAX_FILE_BYTES:
        raise EtsyError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    if len(PurePosixPath(path).name) > FILE_NAME_CHARS:
        raise EtsyError(f"{path}: Etsy allows file names of at most {FILE_NAME_CHARS} characters; rename it")
    return Upload(path, hashlib.sha256(data).hexdigest(), len(data))


def with_disclosure(description: str) -> str:
    return f"{description.rstrip()}\n\n{DISCLOSURE}"


def payload(listing: Listing) -> str:
    """The request as the owner reads and approves it."""

    def sized(u: Upload) -> str:
        return f"{u.path} ({_size(u.bytes)})"

    return "\n".join(
        [
            f"Title: {listing.title}",
            f"Price: {listing.price} {listing.currency}",
            f"Tags: {', '.join(listing.tags)}",
            f"Category: {listing.category} (#{listing.taxonomy_id})",
            f"Files buyers download: {'; '.join(sized(f) for f in listing.files)}",
            f"Photos: {'; '.join(sized(p) for p in listing.photos)}",
            "",
            with_disclosure(listing.description),
        ]
    )


def editable(listing: Listing) -> str:
    """What the owner may change when approving with changes: the words and the price, not the files."""
    return f"Title: {listing.title}\nPrice: {listing.price}\nTags: {', '.join(listing.tags)}\n\n{listing.description}"


def with_changes(listing: Listing, text: str) -> Listing:
    """``listing`` with the owner's version of its words (see ``editable``). Raises EtsyError."""
    head, sep, body = text.replace("\r\n", "\n").partition("\n\n")
    if not sep:
        raise EtsyError("keep the Title, Price and Tags lines, then an empty line, then the description")
    fields: dict[str, str] = {}
    for line in head.split("\n"):
        name, colon, value = line.partition(":")
        key = name.strip().lower()
        if not colon or key not in ("title", "price", "tags") or key in fields:
            raise EtsyError("the first lines must be Title:, Price: and Tags:, each once")
        fields[key] = value.strip()
    if set(fields) != {"title", "price", "tags"}:
        raise EtsyError("the first lines must be Title:, Price: and Tags:, each once")
    price = fields["price"].removesuffix(listing.currency).strip()
    return Listing(
        title=check_title(fields["title"]),
        description=check_description(body),
        price=check_price(price),
        currency=listing.currency,
        tags=check_tags(fields["tags"]),
        taxonomy_id=listing.taxonomy_id,
        category=listing.category,
        files=listing.files,
        photos=listing.photos,
    )


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


# --- the shop, live or fake --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShopInfo:
    shop_id: int
    name: str
    currency: str
    url: str


@dataclass(frozen=True)
class RemoteListing:
    listing_id: int
    state: str
    title: str
    url: str
    views: int | None
    favorites: int | None


@dataclass(frozen=True)
class Order:
    receipt_id: int
    ordered_at: str
    total_cents: int
    currency: str
    items: list[dict[str, Any]] = field(default_factory=list)  # listing_id, title, quantity


class Shop(Protocol):
    simulated: bool

    def info(self) -> ShopInfo: ...

    def taxonomy(self) -> list[tuple[int, str]]: ...  # every category: (id, "A > B > C")

    def create_draft(self, listing: Listing) -> int: ...

    def upload_photo(self, listing_id: int, name: str, data: bytes, rank: int) -> None: ...

    def upload_file(self, listing_id: int, name: str, data: bytes, rank: int) -> None: ...

    def activate(self, listing_id: int) -> str: ...

    def listings(self, listing_ids: list[int]) -> list[RemoteListing]: ...

    def orders(self, since: datetime) -> list[Order]: ...


def listing_url(listing_id: int) -> str:
    return f"https://www.etsy.com/listing/{listing_id}"


def edit_url(listing_id: int) -> str:
    """Where the owner finishes a listing that stayed a draft."""
    return f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"


# Categories (Etsy's seller taxonomy) for the fake shop, and the live shop's fallback when the list can't be read.
FAKE_CATEGORIES: tuple[tuple[int, str], ...] = (
    (1, "Paper & Party Supplies > Paper > Calendars & Planners"),
    (2, "Paper & Party Supplies > Paper > Stationery > Design & Templates"),
    (3, "Art & Collectibles > Prints > Digital Prints"),
    (4, "Home & Living > Office > Calendars & Planners"),
    (5, "Paper & Party Supplies > Party Supplies > Party Games"),
    (6, "Books, Movies & Music > Books > Guide Books"),
)


def search_categories(
    nodes: list[tuple[int, str]] | tuple[tuple[int, str], ...], search: str, limit: int = 10
) -> list[tuple[int, str]]:
    """The categories whose path holds every word of ``search``, shortest paths first."""
    words = [w for w in re.split(r"\W+", search.lower()) if w]
    found = [(i, p) for i, p in nodes if all(w in p.lower() for w in words)]
    return sorted(found, key=lambda item: (len(item[1]), item[1]))[:limit]


class FakeShop:
    """The dry run's Etsy shop: it takes listings and invents their views, favorites and a few orders, from the
    time since a listing went live, so the whole flow (and the reviews) can be tried without Etsy."""

    simulated = True

    def __init__(
        self,
        clock: Clock,
        state: dict[str, Any] | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self.state = state if state is not None else {"next_id": 900_000_001, "listings": {}}
        self._on_change = on_change or (lambda state: None)  # the owner of the state keeps it (the database)

    def info(self) -> ShopInfo:
        return ShopInfo(
            shop_id=4242, name="EmberTestShop", currency="EUR", url="https://www.etsy.com/shop/EmberTestShop"
        )

    def taxonomy(self) -> list[tuple[int, str]]:
        return list(FAKE_CATEGORIES)

    def create_draft(self, listing: Listing) -> int:
        with self._lock:
            listing_id = int(self.state["next_id"])
            self.state["next_id"] = listing_id + 1
            self.state["listings"][str(listing_id)] = {
                "title": listing.title,
                "price_cents": int(Decimal(listing.price) * 100),
                "state": "draft",
                "photos": 0,
                "files": 0,
                "live_since": None,
            }
            self._on_change(self.state)
            return listing_id

    def upload_photo(self, listing_id: int, name: str, data: bytes, rank: int) -> None:
        self._listing(listing_id)["photos"] += 1
        self._on_change(self.state)

    def upload_file(self, listing_id: int, name: str, data: bytes, rank: int) -> None:
        self._listing(listing_id)["files"] += 1
        self._on_change(self.state)

    def activate(self, listing_id: int) -> str:
        item = self._listing(listing_id)
        if not item["photos"] or not item["files"]:
            raise NotSent("a digital listing needs at least one photo and one file before it can go live")
        item["state"], item["live_since"] = "active", to_iso(self.clock.now())
        self._on_change(self.state)
        return "active"

    def listings(self, listing_ids: list[int]) -> list[RemoteListing]:
        found = []
        for listing_id in listing_ids:
            item = self.state["listings"].get(str(listing_id))
            if item is None:
                continue
            views, favorites = self._interest(listing_id, item)
            found.append(
                RemoteListing(listing_id, item["state"], item["title"], listing_url(listing_id), views, favorites)
            )
        return found

    def orders(self, since: datetime) -> list[Order]:
        """One order per listing on its second day live, for listings whose number says they sell."""
        found = []
        for key, item in self.state["listings"].items():
            listing_id = int(key)
            if item["live_since"] is None or listing_id % 3 != 0:
                continue
            ordered = from_iso(item["live_since"]) + timedelta(days=1, hours=listing_id % 7)
            if since <= ordered <= self.clock.now():
                found.append(
                    Order(
                        receipt_id=3_000_000_000 + listing_id,
                        ordered_at=to_iso(ordered),
                        total_cents=item["price_cents"],
                        currency="EUR",
                        items=[{"listing_id": listing_id, "title": item["title"], "quantity": 1}],
                    )
                )
        return found

    def _listing(self, listing_id: int) -> dict[str, Any]:
        item = self.state["listings"].get(str(listing_id))
        if item is None:
            raise NotSent(f"listing {listing_id} doesn't exist")
        return item

    def _interest(self, listing_id: int, item: dict[str, Any]) -> tuple[int, int]:
        if item["live_since"] is None:
            return 0, 0
        hours = max(0.0, (self.clock.now() - from_iso(item["live_since"])).total_seconds() / 3600)
        rate = 1 + listing_id % 5  # views an hour
        views = int(hours * rate)
        return views, views // (8 + listing_id % 5)


# --- the connection: PKCE, the pasted address and the token file ---------------------------------------------------


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: str
    refresh_expires_at: str
    user_id: int
    shop_id: int
    shop_name: str
    currency: str
    connected_at: str

    def secret_values(self) -> list[str]:
        return [self.access_token, self.refresh_token]


def pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the S256 method."""
    verifier = secrets.token_urlsafe(72)[:96]  # 43 to 128 characters of A-Z a-z 0-9 - _
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(settings: Settings, state: str, challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": settings.etsy_keystring,
        "redirect_uri": settings.etsy_redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def code_from(pasted: str, settings: Settings, state: str) -> str:
    """The authorization code in the address Etsy sent the owner to. Raises EtsyError."""
    text = pasted.strip()
    parts = urlsplit(text)
    expected = urlsplit(settings.etsy_redirect_uri)
    if (parts.scheme, parts.netloc, parts.path.rstrip("/")) != (
        expected.scheme,
        expected.netloc,
        expected.path.rstrip("/"),
    ):
        raise EtsyError(
            "that isn't the address Etsy sent you to: copy it from the address bar after you allowed access"
        )
    query = parse_qs(parts.query)
    if "error" in query:
        raise EtsyError(f"Etsy said: {' '.join(query.get('error_description', query['error']))[:200]}")
    got_state = (query.get("state") or [""])[0]
    if not secrets.compare_digest(got_state, state):
        raise EtsyError("that address belongs to another connection attempt: start again")
    code = (query.get("code") or [""])[0]
    if not code or len(code) > 1_000:
        raise EtsyError("the address has no authorization code")
    return code


class TokenFile:
    """The tokens, in a file only Ember reads. Every value is registered for log redaction."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Tokens | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            tokens = Tokens(**{k: data[k] for k in Tokens.__dataclass_fields__})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        for value in tokens.secret_values():
            register_secret(value)
        return tokens

    def save(self, tokens: Tokens) -> None:
        for value in tokens.secret_values():
            register_secret(value)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(asdict(tokens), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def config_problems(settings: Settings) -> list[str]:
    """What stops a live connection (the options' part)."""
    problems = []
    if not settings.etsy_keystring:
        problems.append("etsy_keystring is empty")
    if not settings.etsy_shared_secret.get_secret_value().strip():
        problems.append("etsy_shared_secret is empty")
    parts = urlsplit(settings.etsy_redirect_uri)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if not parts.netloc or not (parts.scheme == "https" or (parts.scheme == "http" and local)):
        problems.append(
            "etsy_redirect_uri must be an https:// address (or http://localhost), exactly as registered for your app"
        )
    return problems


class TaxonomyFile:
    """Etsy's categories, cached (the live shop fetches them at a sync, at most daily: Etsy's API terms allow keeping
    its content for a day); the tools search this copy, so they never need the network."""

    MAX_AGE = timedelta(days=1)

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[list[tuple[int, str]], str | None]:
        """(categories, when fetched); empty when there is no readable copy."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            nodes = [(int(i), str(p)) for i, p in data["nodes"]]
            return nodes, str(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return [], None

    def save(self, nodes: list[tuple[int, str]], fetched_at: str) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": fetched_at, "nodes": nodes}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def stale(self, now: datetime) -> bool:
        _, fetched = self.load()
        return fetched is None or now - from_iso(fetched) > self.MAX_AGE


def expired(stamp: str, now: datetime, early: timedelta = timedelta(0)) -> bool:
    return from_iso(stamp) - early <= now
