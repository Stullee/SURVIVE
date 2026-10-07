"""Pinterest (0.13.0, Phase E2): pins that bring buyers to the owner's Etsy shop, from the owner's account.

The owner creates the (business) account and a Pinterest app for it (0.30.1: with Standard access; a new app's Trial
access connects, but Pinterest refuses its pins), adds their Impressum link to the business profile, switches
Pinterest on and sets the app's id, secret and redirect URI in the options, and connects the account in the dashboard
(like Etsy: PKCE). 0.30.2: Standard access is asked for with a video of the app connecting and pinning, which the owner
records with the sandbox option on (pinterest_publisher.request_test). Then:

* the agent proposes a pin (``propose_pin``): an image it made, a title, a description, the link to one of Ember's
  live Etsy listings, and one of Ember's boards or a new one;
* the owner approves it (the pin that makes Ember's first board is a new public presence: never automatic);
* Ember's code creates it (pinterest_publisher.py): the board first when it is new, then the pin, each journaled; a
  pin can be undone (Ember's code deletes it);
* the sync reads each pin's numbers (impressions, saves, outbound clicks) for the metrics and the plan.

In dry run (with Pinterest switched on) a fake account stands in, its state kept in the database per dry-run session:
nothing reaches Pinterest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from ..config import Settings
from ..economy.clock import Clock, from_iso, to_iso
from ..logging_setup import register_secret
from .etsy import Upload

API_HOST = "api.pinterest.com"
API_URL = f"https://{API_HOST}/v5"
SANDBOX_HOST = "api-sandbox.pinterest.com"  # 0.30.2: Pinterest's API sandbox (pinterest_sandbox)
AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
SCOPES = ("boards:read", "boards:write", "pins:read", "pins:write", "user_accounts:read")
TITLE_MAX = 100
DESCRIPTION_MAX = 500
ALT_MAX = 500
BOARD_NAME_MAX = 50
IMAGE_KINDS = frozenset({".png", ".jpg"})  # the workspace's pictures
IMAGE_MAX_BYTES = 10 * 1024 * 1024
CONNECT_MINUTES = 15
# Added to every pin's description, as to every Etsy listing's.
DISCLOSURE = "Designed with the help of AI and reviewed by the seller."
DESCRIPTION_CHARS = DESCRIPTION_MAX - len(DISCLOSURE) - 2  # the agent's part of it


class PinterestError(Exception):
    """Something about a pin, a board or the account, in words for the owner and the agent."""


class NotSent(PinterestError):
    """Pinterest refused it: nothing changed there."""


class Unclear(PinterestError):
    """A timeout or a lost connection: something may have changed there."""


class Gone(NotSent):
    """Pinterest has no such pin (deleted there, by the owner or by Pinterest)."""


@dataclass(frozen=True)
class Pin:
    """A checked pin: exactly what Ember's code will create."""

    title: str
    description: str
    link: str  # one of Ember's live Etsy listings
    alt_text: str
    image: Upload
    width: int  # the image's pixels, for the QA registry (a portrait 2:3 image shows best)
    height: int
    board_id: str | None = None  # one of Ember's boards
    board_name: str | None = None  # a new board, made first

    def to_action(self) -> dict[str, Any]:
        data = asdict(self)
        data["image"] = asdict(self.image)
        return data

    def full_description(self) -> str:
        """The description as Pinterest shows it: with the AI line."""
        return f"{self.description.rstrip()}\n\n{DISCLOSURE}"


def one_line(text: str) -> str:
    """A title, alt text or board name: one line (a line break could pose as another line of the request)."""
    return " ".join(str(text).split())


def image(path: str, data: bytes) -> Upload:
    """A workspace picture for a pin, with its SHA-256: Ember's code sends exactly this file."""
    if PurePosixPath(path).suffix.lower() not in IMAGE_KINDS:
        raise PinterestError(f"{path}: a pin's image is a .png or .jpg file")
    if not data:
        raise PinterestError(f"{path} is empty")
    if len(data) > IMAGE_MAX_BYTES:
        raise PinterestError(f"{path} is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    return Upload(path, hashlib.sha256(data).hexdigest(), len(data))


def pin_from_action(raw: str | dict[str, Any]) -> Pin:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise PinterestError("the pin isn't readable")
    try:
        image = data["image"]
        return Pin(
            title=str(data["title"]),
            description=str(data["description"]),
            link=str(data["link"]),
            alt_text=str(data.get("alt_text") or ""),
            image=Upload(str(image["path"]), str(image["sha256"]), int(image["bytes"])),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            board_id=None if data.get("board_id") is None else str(data["board_id"]),
            board_name=None if data.get("board_name") is None else str(data["board_name"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PinterestError(f"the pin isn't readable ({type(exc).__name__})") from None


def api_host(settings: Settings) -> str:
    """0.30.2: where the owner's account is reached: Pinterest's API, or its sandbox while the owner records the video
    for their Standard access request (pinterest_sandbox)."""
    return SANDBOX_HOST if settings.pinterest_sandbox else API_HOST


def pin_url(pin_id: str) -> str:
    return f"https://www.pinterest.com/pin/{pin_id}/"


def payload(pin: Pin, board: str) -> str:
    """The request as the owner reads and approves it."""
    return "\n".join(
        [
            f"Board: {board}",
            f"Title: {pin.title}",
            f"Link: {pin.link}",
            f"Image: {pin.image.path} ({pin.width} x {pin.height} pixels)",
            f"Alt text: {pin.alt_text or '(none)'}",
            "",
            pin.full_description(),
        ]
    )


@dataclass(frozen=True)
class AccountInfo:
    username: str
    url: str


@dataclass(frozen=True)
class Board:
    board_id: str
    name: str


@dataclass(frozen=True)
class PinStats:
    impressions: int
    saves: int
    clicks: int  # outbound: to the link


class Account(Protocol):
    simulated: bool

    def info(self) -> AccountInfo: ...

    def create_board(self, name: str, description: str) -> Board: ...

    def create_pin(self, board_id: str, pin: Pin, image: bytes) -> str: ...  # the pin's id

    def delete_pin(self, pin_id: str) -> None: ...

    def pin_stats(self, pin_id: str) -> PinStats: ...

    def keep_alive(self) -> None: ...  # 0.15.0: renew the connection before it lapses unused


class FakePinterest:
    """The dry run's account: boards and pins kept in the database (``on_change``), numbers that grow a little each
    day a pin is up. Nothing reaches Pinterest."""

    simulated = True

    def __init__(self, clock: Clock, state: dict[str, Any] | None, on_change: Callable[[dict[str, Any]], None]) -> None:
        self.clock = clock
        self.state: dict[str, Any] = state or {"boards": {}, "pins": {}, "next": 1}
        self._on_change = on_change

    def _next(self) -> str:
        number = int(self.state["next"])
        self.state["next"] = number + 1
        return str(1_000_000_000 + number)

    def info(self) -> AccountInfo:
        return AccountInfo("ember-dry-run", "https://www.pinterest.com/ember-dry-run/")

    def keep_alive(self) -> None:
        return None  # the dry run's account never lapses

    def create_board(self, name: str, description: str) -> Board:
        if any(b["name"].lower() == name.lower() for b in self.state["boards"].values()):
            raise NotSent(f"a board named {name!r} exists already")
        board_id = self._next()
        self.state["boards"][board_id] = {"name": name, "description": description}
        self._on_change(self.state)
        return Board(board_id, name)

    def create_pin(self, board_id: str, pin: Pin, image: bytes) -> str:
        if board_id not in self.state["boards"]:
            raise NotSent("no such board")
        pin_id = self._next()
        self.state["pins"][pin_id] = {
            "board_id": board_id,
            "title": pin.title,
            "link": pin.link,
            "bytes": len(image),
            "created_at": to_iso(self.clock.now()),
        }
        self._on_change(self.state)
        return pin_id

    def delete_pin(self, pin_id: str) -> None:
        if self.state["pins"].pop(pin_id, None) is None:
            raise Gone("no such pin")
        self._on_change(self.state)

    def pin_stats(self, pin_id: str) -> PinStats:
        pin = self.state["pins"].get(pin_id)
        if pin is None:
            raise Gone("no such pin")
        days = max(0, (self.clock.now() - from_iso(pin["created_at"])).days)
        return PinStats(impressions=40 * days, saves=days // 2, clicks=days // 3)


# --- connecting ---


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: str
    refresh_expires_at: str
    username: str
    connected_at: str


def lapsed(tokens: Tokens, now: datetime) -> bool:
    """0.15.0: whether the connection has ended: its access token expired and its refresh token can't renew it."""
    if from_iso(tokens.expires_at) > now:
        return False
    return not tokens.refresh_token or from_iso(tokens.refresh_expires_at) <= now


class TokenFile:
    """The tokens, in a file only Ember reads (0600). Every value is registered for log redaction."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Tokens | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            tokens = Tokens(**{k: data[k] for k in Tokens.__dataclass_fields__})
        except (OSError, ValueError, KeyError, TypeError):
            return None
        for value in (tokens.access_token, tokens.refresh_token):
            register_secret(value)
        return tokens

    def save(self, tokens: Tokens) -> None:
        for value in (tokens.access_token, tokens.refresh_token):
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
            if os.path.exists(tmp):
                os.unlink(tmp)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def config_problems(settings: Settings) -> list[str]:
    problems = []
    if not settings.pinterest_app_id.strip():
        problems.append("pinterest_app_id is missing")
    if not settings.pinterest_app_secret.get_secret_value().strip():
        problems.append("pinterest_app_secret is missing")
    if not re.match(r"^https://[^\s/]+(/\S*)?$", settings.pinterest_redirect_uri):
        problems.append("pinterest_redirect_uri must be an https address")
    return problems


def authorize_url(settings: Settings, state: str, challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": settings.pinterest_app_id,
        "redirect_uri": settings.pinterest_redirect_uri,
        "scope": ",".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def code_from(pasted: str, settings: Settings, state: str) -> str:
    """The authorization code in the address Pinterest sent the owner to (checked against the state)."""
    parts = urlsplit(pasted.strip())
    expected = urlsplit(settings.pinterest_redirect_uri)
    if (parts.scheme, parts.netloc, parts.path) != (expected.scheme, expected.netloc, expected.path):
        raise PinterestError("paste the whole address your browser opened after you allowed access")
    query = parse_qs(parts.query)
    if query.get("state", [""])[0] != state:
        raise PinterestError("that address belongs to another attempt to connect: start again")
    if "error" in query:
        raise PinterestError(f"Pinterest said: {query['error'][0][:100]}")
    code = query.get("code", [""])[0]
    if not code:
        raise PinterestError("the address has no code: allow access at Pinterest first")
    return code
