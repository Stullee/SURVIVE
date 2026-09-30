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
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
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


# --- what a change to a live listing is (0.9.0) ----------------------------------------------------------------------

# The parts of a listing a change can set, in the order Ember's code makes them: its renewal (0.12.0: an expired,
# sold-out or deactivated listing goes live again first), the listing's own fields (one request), its price (Etsy keeps
# it in the listing's inventory), the photos, the files buyers download, and its deactivation (0.12.0: on its own).
EDIT_PARTS = ("renew", "title", "description", "tags", "category", "price", "photos", "files", "deactivate")
LISTING_PARTS = frozenset({"title", "description", "tags", "category"})
STATES = {"renew": "active", "deactivate": "inactive"}  # what a change of its state sets at Etsy
LIVE_STATE = "active"
RENEWABLE = frozenset({"expired", "sold_out", "inactive"})  # Etsy's states of a listing that can be renewed
LISTING_DAYS = 120  # a listing lasts four months at Etsy
RENEWAL_FEE = "USD 0.20"  # Etsy's listing fee, charged again for a renewal
_WORDS = ("title", "price", "tags")  # the head lines of the words the owner may change


@dataclass(frozen=True)
class Edit:
    """A checked change to one of Ember's listings: only what changes (None stays as it is). Photos and files
    replace all of the listing's; the description gets Ember's AI line, like a new listing's. ``state`` (0.12.0):
    'renew' puts it live again, 'deactivate' takes it off Etsy."""

    listing_id: int
    currency: str
    title: str | None = None
    description: str | None = None
    price: str | None = None
    tags: tuple[str, ...] | None = None
    taxonomy_id: int | None = None
    category: str | None = None
    photos: tuple[Upload, ...] | None = None
    files: tuple[Upload, ...] | None = None
    state: str | None = None  # 'renew' or 'deactivate'

    def parts(self) -> list[str]:
        """What changes, in EDIT_PARTS order."""
        present = {
            "renew": True if self.state == "renew" else None,
            "deactivate": True if self.state == "deactivate" else None,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "category": self.taxonomy_id,
            "price": self.price,
            "photos": self.photos,
            "files": self.files,
        }
        return [part for part in EDIT_PARTS if present[part] is not None]

    def to_action(self) -> dict[str, Any]:
        data: dict[str, Any] = {"listing_id": self.listing_id, "currency": self.currency}
        for name in ("title", "description", "price", "taxonomy_id", "category", "state"):
            if getattr(self, name) is not None:
                data[name] = getattr(self, name)
        if self.tags is not None:
            data["tags"] = list(self.tags)
        for name in ("photos", "files"):
            uploads = getattr(self, name)
            if uploads is not None:
                data[name] = [asdict(u) for u in uploads]
        return data

    def listing_fields(self) -> dict[str, str]:
        """The listing's own fields that change, as Etsy's updateListing takes them."""
        fields: dict[str, str] = {}
        if self.title is not None:
            fields["title"] = self.title
        if self.description is not None:
            fields["description"] = with_disclosure(self.description)
        if self.tags is not None:
            fields["tags"] = ",".join(self.tags)
        if self.taxonomy_id is not None:
            fields["taxonomy_id"] = str(self.taxonomy_id)
        return fields


def listing_text(listing_id: int, listing: Listing, state: str = "") -> str:
    """One of Ember's listings in full, for the agent (``state``: how it stands at Etsy, 0.12.0)."""

    def names(uploads: tuple[Upload, ...]) -> str:
        return ", ".join(u.path for u in uploads) or "none"

    return "\n".join(
        [
            f"Listing #{listing_id}: {listing_url(listing_id)}",
            *([f"At Etsy: {state}"] if state else []),
            f"Title: {listing.title}",
            f"Price: {listing.price} {listing.currency}",
            f"Tags ({len(listing.tags)}): {', '.join(listing.tags)}",
            f"Category: {listing.category} (#{listing.taxonomy_id})",
            f"Photos ({len(listing.photos)}, the main one first): {names(listing.photos)}",
            f"Files buyers download: {names(listing.files)}",
            "Description (Ember adds the line about AI after it):",
            listing.description,
        ]
    )


def listing_line(listing_id: int, listing: Listing) -> str:
    """One of Ember's live listings in a line, for the agent."""
    photos = f"{len(listing.photos)} photo{'' if len(listing.photos) == 1 else 's'}"
    files = f"{len(listing.files)} file{'' if len(listing.files) == 1 else 's'}"
    return (
        f"- #{listing_id} {listing.title[:70]} · {listing.price} {listing.currency} · category #{listing.taxonomy_id}"
        f" · {photos}, {files}"
    )


def edit_from_action(raw: str | dict[str, Any]) -> Edit:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise EtsyError("the change isn't readable")
    try:

        def uploads(name: str) -> tuple[Upload, ...] | None:
            if data.get(name) is None:
                return None
            return tuple(Upload(str(u["path"]), str(u["sha256"]), int(u["bytes"])) for u in data[name])

        return Edit(
            listing_id=int(data["listing_id"]),
            currency=str(data["currency"]),
            title=None if data.get("title") is None else str(data["title"]),
            description=None if data.get("description") is None else str(data["description"]),
            price=None if data.get("price") is None else str(data["price"]),
            tags=None if data.get("tags") is None else tuple(str(t) for t in data["tags"]),
            taxonomy_id=None if data.get("taxonomy_id") is None else int(data["taxonomy_id"]),
            category=None if data.get("category") is None else str(data["category"]),
            photos=uploads("photos"),
            files=uploads("files"),
            state=_state(data.get("state")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EtsyError(f"the change isn't readable ({type(exc).__name__})") from None


def _state(value: Any) -> str | None:
    if value is not None and value not in STATES:
        raise ValueError("unknown state")
    return value


def edited(listing: Listing, edit: Edit, parts: frozenset[str] | set[str] | None = None) -> Listing:
    """``listing`` with the ``parts`` of ``edit`` made (all of them when None)."""
    made = set(edit.parts()) if parts is None else set(parts) & set(edit.parts())
    changes: dict[str, Any] = {}
    for name in ("title", "description", "price", "tags", "photos", "files"):
        if name in made:
            changes[name] = getattr(edit, name)
    if "category" in made:
        changes["taxonomy_id"], changes["category"] = edit.taxonomy_id, edit.category
    return replace(listing, **changes)


def edit_payload(edit: Edit, now: Listing, state: str = "") -> str:
    """The change as the owner reads and approves it: each part next to what it replaces (``now``: the listing as it
    is; ``state``: how it stands at Etsy, for a renewal or deactivation), the new description in full."""

    def sized(uploads: tuple[Upload, ...]) -> str:
        return "; ".join(f"{u.path} ({_size(u.bytes)})" for u in uploads)

    def names(uploads: tuple[Upload, ...]) -> str:
        return ", ".join(u.path for u in uploads) or "none"

    lines = [f"Listing #{edit.listing_id}: {now.title}"]
    if edit.state == "renew":
        lines.append(f"Renew it: it goes live at Etsy again for four months ({RENEWAL_FEE} at most).\n  (now: {state})")
    elif edit.state == "deactivate":
        lines.append(f"Deactivate it: buyers no longer find it at Etsy; it can be renewed later.\n  (now: {state})")
    if edit.title is not None:
        lines.append(f"Title: {edit.title}\n  (was: {now.title})")
    if edit.tags is not None:
        lines.append(f"Tags: {', '.join(edit.tags)}\n  (were: {', '.join(now.tags)})")
    if edit.taxonomy_id is not None:
        lines.append(f"Category: {edit.category} (#{edit.taxonomy_id})\n  (was: {now.category} (#{now.taxonomy_id}))")
    if edit.price is not None:
        lines.append(f"Price: {edit.price} {edit.currency} (was: {now.price} {now.currency})")
    if edit.photos is not None:
        lines.append(f"Photos, the main one first: {sized(edit.photos)}\n  (they replace: {names(now.photos)})")
    if edit.files is not None:
        lines.append(f"Files buyers download: {sized(edit.files)}\n  (they replace: {names(now.files)})")
    if edit.description is not None:
        lines += ["", "New description:", with_disclosure(edit.description)]
    return "\n".join(lines)


def edit_editable(edit: Edit) -> str | None:
    """The words and price of a change the owner may change when approving (None: nothing but photos, files or the
    category changes). The head lines (Title:, Price:, Tags:) that change, then an empty line and the description."""
    head = []
    if edit.title is not None:
        head.append(f"Title: {edit.title}")
    if edit.price is not None:
        head.append(f"Price: {edit.price}")
    if edit.tags is not None:
        head.append(f"Tags: {', '.join(edit.tags)}")
    if edit.description is None:
        return "\n".join(head) or None
    return "\n".join([*head, "", edit.description]) if head else edit.description


def edit_with_changes(edit: Edit, text: str) -> Edit:
    """``edit`` with the owner's version of its words and price (see ``edit_editable``). Raises EtsyError."""
    wanted = [name for name in _WORDS if getattr(edit, name) is not None]
    if not wanted and edit.description is None:
        raise EtsyError("this change has no words or price to change; approve or reject it")
    text = text.replace("\r\n", "\n").strip()
    head, body = "", text
    if wanted:
        head, sep, body = text.partition("\n\n")
        if edit.description is not None and not sep:
            raise EtsyError("keep the head lines, then an empty line, then the description")
        if edit.description is None and body.strip():
            raise EtsyError("this change keeps the description; change only the head lines")
    fields: dict[str, str] = {}
    for line in head.split("\n") if head else []:
        name, colon, value = line.partition(":")
        key = name.strip().lower()
        if not colon or key not in wanted or key in fields:
            raise EtsyError(f"the first lines must be {', '.join(n.title() + ':' for n in wanted)} each once")
        fields[key] = value.strip()
    if set(fields) != set(wanted):
        raise EtsyError(f"the first lines must be {', '.join(n.title() + ':' for n in wanted)} each once")
    return replace(
        edit,
        title=check_title(fields["title"]) if "title" in fields else edit.title,
        price=check_price(fields["price"].removesuffix(edit.currency).strip()) if "price" in fields else edit.price,
        tags=check_tags(fields["tags"]) if "tags" in fields else edit.tags,
        description=check_description(body) if edit.description is not None else None,
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
    ends_at: str | None = None  # 0.12.0: when the listing ends at Etsy (it lasts four months), and
    auto_renew: bool | None = None  # whether Etsy renews it then


@dataclass(frozen=True)
class Order:
    receipt_id: int
    ordered_at: str
    total_cents: int  # what the buyer paid for the whole receipt: tax, shipping and the owner's products included
    currency: str
    items: list[dict[str, Any]] = field(default_factory=list)  # listing_id, title, quantity, price_cents (a unit's)
    # 0.12.0: what Ember's share of a receipt is worth needs its status, the items' price before the coupon
    # (items_cents: every line's price times quantity), the coupon and what was refunded.
    status: str = "paid"
    items_cents: int = 0
    discount_cents: int = 0
    refunded_cents: int = 0

    @property
    def paid(self) -> bool:
        return self.status in PAID_ORDERS


PAID_ORDERS = frozenset({"paid", "completed", "partially refunded"})
DEAD_ORDERS = frozenset({"canceled", "fully refunded"})
COUNTED_ORDERS = "COALESCE(status, 'paid') NOT IN ('canceled', 'fully refunded')"  # SQL, on etsy_orders


def order_net(order: Order, lines: list[dict[str, Any]]) -> int:
    """What Ember's ``lines`` of a receipt earned, in cents (0.12.0: the whole receipt was stored): their price times
    quantity, less their share of the coupon and of the refunds. Tax, shipping and the owner's own products don't
    count; Etsy's fees are booked on their own."""
    if order.status in DEAD_ORDERS:
        return 0
    gross = sum(int(i.get("price_cents") or 0) * int(i.get("quantity") or 1) for i in lines)
    whole = order.items_cents or gross
    share = gross / whole if whole else 1.0
    return max(0, gross - round((order.discount_cents + order.refunded_cents) * share))


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

    # Changing a live listing (0.9.0).

    def update_listing(self, listing_id: int, fields: dict[str, str]) -> None: ...  # Edit.listing_fields()

    def set_price(self, listing_id: int, price: str) -> None: ...

    def photo_ids(self, listing_id: int) -> list[int]: ...  # in their order, the main photo first

    def delete_photo(self, listing_id: int, photo_id: int) -> None: ...

    def file_ids(self, listing_id: int) -> list[int]: ...  # in their order

    def delete_file(self, listing_id: int, file_id: int) -> None: ...

    # A listing's state and renewal (0.12.0).

    def set_state(self, listing_id: int, state: str) -> str: ...  # 'active' (renews it) or 'inactive'; the new state

    def set_auto_renew(self, listing_id: int, on: bool) -> None: ...


def listing_url(listing_id: int) -> str:
    return f"https://www.etsy.com/listing/{listing_id}"


def edit_url(listing_id: int) -> str:
    """Where the owner finishes a listing that stayed a draft."""
    return f"https://www.etsy.com/your/shops/me/listing-editor/edit/{listing_id}"


CATEGORY_JOIN = " > "  # between the names in a category's path: "Paper & Party Supplies > Paper > Stationery"
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
    """The categories whose path holds every word of ``search`` (in any case, with or without accents: 'resume'
    finds 'Résumé'), shortest paths first."""
    words = [w for w in re.split(r"\W+", _folded(search)) if w]
    found = [(i, p) for i, p in nodes if all(w in _folded(p) for w in words)]
    return sorted(found, key=lambda item: (len(item[1]), item[1]))[:limit]


def _folded(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def department(path: str) -> bool:
    """Whether a category is a whole top-level department of Etsy's (such as 'Accessories'): too broad for a listing,
    which buyers look for in the categories below it."""
    return CATEGORY_JOIN not in path


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
        self._add(listing_id, "photos", rank, MAX_PHOTOS)

    def upload_file(self, listing_id: int, name: str, data: bytes, rank: int) -> None:
        self._add(listing_id, "files", rank, MAX_FILES)

    def update_listing(self, listing_id: int, fields: dict[str, str]) -> None:
        item = self._listing(listing_id)
        if "title" in fields:
            item["title"] = fields["title"]
        self._on_change(self.state)

    def set_price(self, listing_id: int, price: str) -> None:
        self._listing(listing_id)["price_cents"] = int(Decimal(price) * 100)
        self._on_change(self.state)

    def photo_ids(self, listing_id: int) -> list[int]:
        return list(self._ids(listing_id, "photos"))

    def delete_photo(self, listing_id: int, photo_id: int) -> None:
        self._remove(listing_id, "photos", photo_id)

    def file_ids(self, listing_id: int) -> list[int]:
        return list(self._ids(listing_id, "files"))

    def delete_file(self, listing_id: int, file_id: int) -> None:
        self._remove(listing_id, "files", file_id)

    def _ids(self, listing_id: int, kind: str) -> list[int]:
        """A listing's photo or file numbers, in their order (older states kept only a count)."""
        item = self._listing(listing_id)
        key = f"{kind[:-1]}_ids"
        if key not in item:
            item[key] = [listing_id * 100 + i for i in range(1, int(item[kind]) + 1)]
        return item[key]

    def _add(self, listing_id: int, kind: str, rank: int, most: int) -> None:
        ids = self._ids(listing_id, kind)
        if len(ids) >= most:
            raise NotSent(f"a listing holds at most {most} {kind}")
        media_id = int(self.state.get("next_media_id", 1))  # its own count: listing numbers stay as they were
        self.state["next_media_id"] = media_id + 1
        ids.insert(max(0, min(rank - 1, len(ids))), media_id)
        self._listing(listing_id)[kind] = len(ids)
        self._on_change(self.state)

    def _remove(self, listing_id: int, kind: str, item_id: int) -> None:
        ids = self._ids(listing_id, kind)
        if item_id not in ids:
            raise NotSent(f"the listing has no such {kind[:-1]}")
        if len(ids) == 1 and self._listing(listing_id)["state"] == "active":
            raise NotSent(f"a live digital listing keeps at least one {kind[:-1]}")
        ids.remove(item_id)
        self._listing(listing_id)[kind] = len(ids)
        self._on_change(self.state)

    def activate(self, listing_id: int) -> str:
        item = self._listing(listing_id)
        if not item["photos"] or not item["files"]:
            raise NotSent("a digital listing needs at least one photo and one file before it can go live")
        now = self.clock.now()
        item["state"], item["live_since"] = "active", to_iso(now)
        item["ends_at"], item["auto_renew"] = to_iso(now + timedelta(days=LISTING_DAYS)), False
        self._on_change(self.state)
        return "active"

    def set_state(self, listing_id: int, state: str) -> str:
        """Like Etsy: 'active' renews an expired or sold-out listing for four months (a deactivated one keeps its
        end until then), 'inactive' takes it off the shop."""
        item = self._listing(listing_id)
        self._roll(item)
        if state == "active":
            if not item["photos"] or not item["files"]:
                raise NotSent("a digital listing needs at least one photo and one file before it can go live")
            if item["state"] in ("expired", "sold_out") or not item.get("ends_at"):
                item["ends_at"] = to_iso(self.clock.now() + timedelta(days=LISTING_DAYS))
                item["renewals"] = int(item.get("renewals", 0)) + 1
            item["live_since"] = item["live_since"] or to_iso(self.clock.now())
        elif state != "inactive":
            raise NotSent(f"a listing can't be set {state}")
        item["state"] = state
        self._on_change(self.state)
        return state

    def set_auto_renew(self, listing_id: int, on: bool) -> None:
        self._listing(listing_id)["auto_renew"] = on
        self._on_change(self.state)

    def listings(self, listing_ids: list[int]) -> list[RemoteListing]:
        found = []
        for listing_id in listing_ids:
            item = self.state["listings"].get(str(listing_id))
            if item is None:
                continue
            if self._roll(item):
                self._on_change(self.state)
            views, favorites = self._interest(listing_id, item)
            found.append(
                RemoteListing(
                    listing_id,
                    item["state"],
                    item["title"],
                    listing_url(listing_id),
                    views,
                    favorites,
                    item.get("ends_at"),
                    bool(item.get("auto_renew")),
                )
            )
        return found

    def _roll(self, item: dict[str, Any]) -> bool:
        """A live listing past its end: renewed for four months at a time when it renews itself, else expired.
        Returns whether it changed. (A state from before 0.12.0 ends four months after it went live.)"""
        if item["state"] != "active" or item["live_since"] is None:
            return False
        ends = from_iso(item.get("ends_at") or to_iso(from_iso(item["live_since"]) + timedelta(days=LISTING_DAYS)))
        before = item.get("ends_at")
        now = self.clock.now()
        while ends <= now and item.get("auto_renew"):
            ends += timedelta(days=LISTING_DAYS)
            item["renewals"] = int(item.get("renewals", 0)) + 1
        item["ends_at"] = to_iso(ends)
        if ends <= now:
            item["state"] = "expired"
            return True
        return item["ends_at"] != before

    def orders(self, since: datetime) -> list[Order]:
        """One order per listing on its second day live, for listings whose number says they sell."""
        found = []
        for key, item in self.state["listings"].items():
            listing_id = int(key)
            if item["live_since"] is None or listing_id % 3 != 0:
                continue
            ordered = from_iso(item["live_since"]) + timedelta(days=1, hours=listing_id % 7)
            if since <= ordered <= self.clock.now():
                price = item["price_cents"]
                line = {"listing_id": listing_id, "title": item["title"], "quantity": 1, "price_cents": price}
                found.append(
                    Order(
                        receipt_id=3_000_000_000 + listing_id,
                        ordered_at=to_iso(ordered),
                        total_cents=item["price_cents"],
                        currency="EUR",
                        items=[line],
                        items_cents=price,
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
