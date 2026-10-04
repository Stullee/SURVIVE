"""Bluesky (0.19.0): posts that bring people to Ember's work, from the account its owner made for it.

The owner creates the account (a handle such as ember-shop.bsky.social) with a profile that says it is run by an AI
agent and links their Impressum, makes an app password for Ember in the account's settings, and sets the handle and the
app password in the options (Bluesky on). Then:

* the agent proposes a post (``propose_bluesky_post``): its words with a few #hashtags, a link to one of Ember's live
  Etsy listings or a page of the owner's website if it likes, and one of its pictures if it likes;
* the owner approves it as it is, or rejects it;
* Ember's code posts it (bluesky_publisher.py) with a line saying an AI wrote it and a person approved it: a link
  clickable at the end of the words (with a picture) or as a card (without one: an Etsy listing's card shows its title
  and main photo); the owner's Undo deletes it;
* the sync reads the account's followers and each post's likes, reposts, replies and quotes, and the labels moderation
  put on them, for the plan.

Ember never mentions, replies to, follows, likes or messages anyone: it posts on its own account only what the owner
approved. In dry run (with Bluesky switched on) a fake account stands in, its state kept in the database per dry-run
session: nothing reaches Bluesky.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..config import Settings
from ..economy.clock import Clock, from_iso, to_iso
from .etsy import Upload

API_HOST = "bsky.social"  # the entryway of the accounts Bluesky hosts: logins go there
API_URL = f"https://{API_HOST}"
# The account's own server (its PDS, named in the login's DID document) takes the rest, as Bluesky recommends: only one
# of the servers Bluesky hosts (else the entryway takes everything).
PDS_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+host\.bsky\.network$")
APP_URL = "https://bsky.app"
TEXT_MAX = 300  # a post's text: Bluesky counts graphemes, Ember's code characters (never fewer than graphemes; and
# 300 characters are at most 1,200 UTF-8 bytes, well within Bluesky's 3,000)
LANGUAGES = ("de", "en")
# Added to every post, in its language (the constitution: disclose that an AI wrote it wherever it reaches people).
DISCLOSURE = {
    "de": "🤖 Von einer KI geschrieben, von einem Menschen freigegeben.",
    "en": "🤖 Written by an AI, approved by a human.",
}
TEXT_CHARS = TEXT_MAX - max(len(d) for d in DISCLOSURE.values()) - 2  # the agent's words at most (without a link)
ALT_MAX = 1_000  # a picture's alt text
CARD_TITLE_MAX = 300  # a link card's title
TAG_MAX = 64  # a hashtag's characters (Bluesky's limit, without the #)
LINK_MAX = 300
IMAGE_KINDS = frozenset({".png", ".jpg"})  # the workspace's pictures
IMAGE_MAX_BYTES = 10 * 1024 * 1024  # a picture proposed (Ember's code makes a smaller copy for Bluesky when needed)
BLOB_MAX_BYTES = 1_000_000  # a post's picture or a card's (Bluesky's own servers take 2 MB since 2026-04, others 1)
BLOB_LONGEST = 2_000  # pixels: the longer side of a picture as Bluesky's own app sends it
# A link as Bluesky's own app shows it: the address's host and path, cut after 13 characters of a long path.
SHOWN_PATH = 15
REFUSED_WAIT = timedelta(hours=1)  # after Bluesky refused the login, before Ember's code tries again
# What the agent's words may not hold: a link (the post's link is its own field) or an @mention (Ember never addresses
# anyone: they didn't ask to hear from it).
_URL = re.compile(r"(?:https?://|www\.)\S", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w.@])@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*")
_TAG = re.compile(r"(?:^|(?<=\s))[#＃]([^\s#＃]+)")
_TAG_END = re.compile(r"[.,;:!?)\]}\"'’”»…]+$")  # punctuation after a hashtag isn't part of it
_HANDLE = re.compile(r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_APP_PASSWORD = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
_RKEY = re.compile(r"^[A-Za-z0-9._:~-]{1,512}$")
_DID = re.compile(r"^did:[a-z]+:[A-Za-z0-9._:%-]{1,2000}$")


class BlueskyError(Exception):
    """Something about a post or the account, in words for the owner and the agent."""


class NotSent(BlueskyError):
    """Bluesky refused it: nothing changed there."""


class Refused(NotSent):
    """Bluesky refused the login: the handle or the app password (or the account) isn't right."""


class Unclear(BlueskyError):
    """A timeout or a lost connection: something may have changed there."""


class Gone(NotSent):
    """Bluesky has no such post (deleted there, by the owner or by moderation)."""


@dataclass(frozen=True)
class Post:
    """A checked post: exactly what Ember's code will post."""

    text: str  # the agent's words (Ember's code adds the link and the AI line)
    language: str  # one of LANGUAGES
    link: str | None = None  # one of Ember's live Etsy listings or a page of the owner's website
    link_title: str = ""  # its card's title (a listing's, a blog post's): "" for a link shown in the words
    link_description: str = ""  # and the card's description (a blog post's)
    image: Upload | None = None  # a picture of Ember's, shown with the post
    width: int = 0  # the picture's pixels
    height: int = 0
    alt_text: str = ""
    card_photo: Upload | None = None  # an Etsy listing's main photo, the picture on its card (without a picture)
    tags: tuple[str, ...] = field(default=())  # the #hashtags in the words, as Bluesky indexes them

    def card(self) -> bool:
        """Whether the link shows as a card (a post without a picture, linking a page Ember's records know the title
        of), else as a link at the end of the words."""
        return self.link is not None and self.image is None and bool(self.link_title)

    def to_action(self) -> dict[str, Any]:
        data = asdict(self)
        data["image"] = asdict(self.image) if self.image is not None else None
        data["card_photo"] = asdict(self.card_photo) if self.card_photo is not None else None
        data["tags"] = list(self.tags)
        return data


def _upload(raw: Any) -> Upload | None:
    if raw is None:
        return None
    return Upload(str(raw["path"]), str(raw["sha256"]), int(raw["bytes"]))


def post_from_action(raw: str | dict[str, Any]) -> Post:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise BlueskyError("the post isn't readable")
    try:
        language = str(data["language"])
        if language not in LANGUAGES:
            raise ValueError(language)
        return Post(
            text=str(data["text"]),
            language=language,
            link=None if data.get("link") is None else str(data["link"]),
            link_title=str(data.get("link_title") or ""),
            link_description=str(data.get("link_description") or ""),
            image=_upload(data.get("image")),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            alt_text=str(data.get("alt_text") or ""),
            card_photo=_upload(data.get("card_photo")),
            tags=tuple(str(t) for t in data.get("tags") or ()),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BlueskyError(f"the post isn't readable ({type(exc).__name__})") from None


def one_line(text: str) -> str:
    """Alt text or a card's title: one line."""
    return " ".join(str(text).split())


def words(text: str) -> str:
    """The agent's words as a post holds them: its lines kept, each without trailing spaces, at most one empty line
    between paragraphs, nothing around them."""
    lines = [" ".join(line.split()) for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def shown_link(url: str) -> str:
    """A link as the post shows it (Bluesky's own app shortens it the same way): its host and path, a long path cut."""
    parts = urlsplit(url)
    path = (parts.path if parts.path != "/" else "") + (f"?{parts.query}" if parts.query else "")
    path += f"#{parts.fragment}" if parts.fragment else ""
    return parts.netloc + (path[:13] + "..." if len(path) > SHOWN_PATH else path)


def hashtags(text: str) -> list[tuple[int, int, str]]:
    """The #hashtags in a text: (start, end, tag) by character, the tag without its # and the punctuation after it. A
    tag of digits alone (#1) isn't one, nor one longer than TAG_MAX."""
    found = []
    for match in _TAG.finditer(text):
        tag = _TAG_END.sub("", match.group(1))
        if not tag or tag.isdigit() or len(tag) > TAG_MAX:
            continue
        start = match.start(1) - 1  # the # itself
        found.append((start, start + 1 + len(tag), tag))
    return found


def check_words(text: str) -> None:
    """Refuses words with a link or an @mention (BlueskyError says which)."""
    if not text:
        raise BlueskyError("text is empty")
    if _URL.search(text):
        raise BlueskyError("the words hold a link: give it as link (Ember's code adds it to the post)")
    mention = _MENTION.search(text)
    if mention:
        raise BlueskyError(
            f"the words mention {mention.group(0)}: a post of Ember's mentions no one (they didn't ask to hear from it)"
        )
    long = next((m.group(1) for m in _TAG.finditer(text) if len(_TAG_END.sub("", m.group(1))) > TAG_MAX), None)
    if long:
        raise BlueskyError(f"#{long[:20]}... is longer than {TAG_MAX} characters: Bluesky takes no such hashtag")


def _bytes(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def layout(post: Post) -> tuple[str, list[dict[str, Any]]]:
    """The post's text as Bluesky shows it, and its facets (Bluesky's marks of what is a link and a hashtag, by UTF-8
    byte): the agent's words, the link (with a picture: in the words; without one it is a card), the AI line."""
    parts = [post.text]
    facets: list[dict[str, Any]] = []
    for start, end, tag in hashtags(post.text):
        facets.append(_facet(post.text, start, end, {"$type": "app.bsky.richtext.facet#tag", "tag": tag}))
    if post.link is not None and not post.card():
        shown = shown_link(post.link)
        start = len(post.text) + 2
        parts.append(shown)
        text = "\n\n".join(parts)
        link = {"$type": "app.bsky.richtext.facet#link", "uri": post.link}
        facets.append(_facet(text, start, start + len(shown), link))
    parts.append(DISCLOSURE[post.language])
    return "\n\n".join(parts), facets


def _facet(text: str, start: int, end: int, feature: dict[str, Any]) -> dict[str, Any]:
    return {"index": {"byteStart": _bytes(text, start), "byteEnd": _bytes(text, end)}, "features": [feature]}


def full_text(post: Post) -> str:
    return layout(post)[0]


def room(post: Post) -> int:
    """How many characters the post has left of Bluesky's TEXT_MAX (below 0: that many too many)."""
    return TEXT_MAX - len(full_text(post))


def record(
    post: Post,
    created_at: datetime,
    image: dict[str, Any] | None = None,
    thumb: dict[str, Any] | None = None,
    size: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """The post as Bluesky keeps it (app.bsky.feed.post): ``image`` and ``thumb`` are the blobs Bluesky gave for the
    picture and the card's photo, ``size`` the picture's pixels as sent."""
    text, facets = layout(post)
    moment = created_at.astimezone(UTC)
    made: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z",
        "langs": [post.language],
    }
    if facets:
        made["facets"] = facets
    if post.image is not None and image is not None:
        picture: dict[str, Any] = {"alt": post.alt_text, "image": image}
        if size[0] > 0 and size[1] > 0:
            picture["aspectRatio"] = {"width": size[0], "height": size[1]}
        made["embed"] = {"$type": "app.bsky.embed.images", "images": [picture]}
    elif post.card() and post.link is not None:
        external: dict[str, Any] = {"uri": post.link, "title": post.link_title, "description": post.link_description}
        if thumb is not None:
            external["thumb"] = thumb
        made["embed"] = {"$type": "app.bsky.embed.external", "external": external}
    return made


def image(path: str, data: bytes) -> Upload:
    """A workspace picture for a post, with its SHA-256: Ember's code sends exactly this picture (a smaller copy of it,
    when it is larger than Bluesky takes)."""
    if PurePosixPath(path).suffix.lower() not in IMAGE_KINDS:
        raise BlueskyError(f"{path}: a post's picture is a .png or .jpg file")
    if not data:
        raise BlueskyError(f"{path} is empty")
    if len(data) > IMAGE_MAX_BYTES:
        raise BlueskyError(f"{path} is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    return Upload(path, hashlib.sha256(data).hexdigest(), len(data))


def payload(post: Post, handle: str) -> str:
    """The request as the owner reads and approves it."""
    lines = [f"Account: @{handle}", f"Language: {post.language}"]
    if post.link is not None:
        if post.card():
            photo = f", with the photo {post.card_photo.path}" if post.card_photo is not None else ""
            lines.append(f"Link: {post.link} (a card: {post.link_title!r}{photo})")
        else:
            lines.append(f"Link: {post.link}")
    if post.image is not None:
        lines.append(f"Picture: {post.image.path} ({post.width} x {post.height} pixels)")
        lines.append(f"Alt text: {post.alt_text}")
    return "\n".join([*lines, "", full_text(post)])


def rkey_of(uri: str) -> str:
    """A post's record key: the end of its at:// address."""
    rkey = str(uri).rsplit("/", 1)[-1]
    if not _RKEY.match(rkey):
        raise BlueskyError("Bluesky's address of the post isn't readable")
    return rkey


def post_url(did: str, rkey: str) -> str:
    return f"{APP_URL}/profile/{did}/post/{rkey}"


def profile_url(handle: str) -> str:
    return f"{APP_URL}/profile/{handle}"


@dataclass(frozen=True)
class Picture:
    """A picture as Ember's code sends it to Bluesky (images.within made it fit): its bytes, type and pixels."""

    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class AccountInfo:
    handle: str
    did: str
    followers: int = 0
    posts: int = 0
    labels: tuple[str, ...] = ()  # what moderation says of the account (e.g. spam), Bluesky's own labels
    automated: bool = False  # the profile says it is an automated account (its "bot" self-label, Bluesky's badge)

    @property
    def url(self) -> str:
        return profile_url(self.handle)


@dataclass(frozen=True)
class PostRef:
    uri: str  # at://<did>/app.bsky.feed.post/<rkey>
    cid: str
    rkey: str


@dataclass(frozen=True)
class PostStats:
    likes: int
    reposts: int
    replies: int
    quotes: int
    labels: tuple[str, ...] = ()  # what moderation says of the post


class Login:
    """The live account's login, in memory only (never written to disk: a restart logs in again): Bluesky's tokens and,
    after Bluesky refused the login, why and when, so that Ember doesn't try again before REFUSED_WAIT has passed (the
    options can't change without a restart, and every refused try counts against the account)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.did = ""
        self.handle = ""
        self.pds = ""  # the host of the account's own server ("": the entryway's)
        self.access = ""  # Bluesky's access token (minutes to hours)
        self.refresh = ""  # and the one that renews it (weeks)
        self.access_until: datetime | None = None
        self.refresh_until: datetime | None = None
        self.refused = ""  # why Bluesky refused the last try ("" when it didn't)
        self.refused_at: datetime | None = None

    def waiting(self, now: datetime) -> str:
        """Why no login is tried now ("" when one may be)."""
        if not self.refused or self.refused_at is None or now - self.refused_at >= REFUSED_WAIT:
            return ""
        return self.refused

    def forget(self) -> None:
        self.did = self.handle = self.pds = self.access = self.refresh = ""
        self.access_until = self.refresh_until = None


class Account(Protocol):
    simulated: bool

    def info(self) -> AccountInfo: ...  # the account and its followers (logs in first)

    def create_post(self, post: Post, image: Picture | None, thumb: Picture | None) -> PostRef: ...

    def delete_post(self, rkey: str) -> None: ...

    def post_stats(self, uris: list[str]) -> dict[str, PostStats]: ...  # a post missing from it is gone

    def keep_alive(self) -> None: ...  # log in (or renew the login) now: Refused when Bluesky refuses it


class FakeBluesky:
    """The dry run's account: posts kept in the database (``on_change``), numbers that grow a little each day a post is
    up, followers that come with them. Nothing reaches Bluesky."""

    simulated = True
    HANDLE = "ember-dry-run.bsky.social"
    DID = "did:plc:emberdryrunfakeaccount0"

    def __init__(self, clock: Clock, state: dict[str, Any] | None, on_change: Callable[[dict[str, Any]], None]) -> None:
        self.clock = clock
        self.state: dict[str, Any] = state or {"posts": {}, "next": 1}
        self._on_change = on_change

    def _rkey(self) -> str:
        """A record key shaped like Bluesky's (13 letters and digits of base32)."""
        number = int(self.state["next"])
        self.state["next"] = number + 1
        alphabet = "234567abcdefghijklmnopqrstuvwxyz"
        digits = ""
        for _ in range(11):
            number, rest = divmod(number, 32)
            digits = alphabet[rest] + digits
        return "3m" + digits

    def info(self) -> AccountInfo:
        days = sum(self._days(p) for p in self.state["posts"].values())
        return AccountInfo(self.HANDLE, self.DID, followers=days // 2, posts=len(self.state["posts"]), automated=True)

    def keep_alive(self) -> None:
        return None  # the dry run's account never logs out

    def _days(self, post: dict[str, Any]) -> int:
        return max(0, (self.clock.now() - from_iso(post["created_at"])).days)

    def create_post(self, post: Post, image: Picture | None, thumb: Picture | None) -> PostRef:
        rkey = self._rkey()
        uri = f"at://{self.DID}/app.bsky.feed.post/{rkey}"
        self.state["posts"][rkey] = {
            "text": full_text(post),
            "link": post.link,
            "card": post.card(),
            "image_bytes": len(image.data) if image is not None else 0,
            "thumb_bytes": len(thumb.data) if thumb is not None else 0,
            "created_at": to_iso(self.clock.now()),
        }
        self._on_change(self.state)
        return PostRef(uri, f"bafyfake{rkey}", rkey)

    def delete_post(self, rkey: str) -> None:
        if self.state["posts"].pop(rkey, None) is None:
            raise Gone("no such post")
        self._on_change(self.state)

    def post_stats(self, uris: list[str]) -> dict[str, PostStats]:
        found = {}
        for uri in uris:
            post = self.state["posts"].get(uri.rsplit("/", 1)[-1])
            if post is not None:
                days = self._days(post)
                found[uri] = PostStats(likes=2 * days, reposts=days // 2, replies=days // 3, quotes=days // 5)
        return found


# --- the options ---


def handle_of(settings: Settings) -> str:
    """The account's handle as the options give it (without an @ or spaces, lower case)."""
    return settings.bluesky_handle.strip().lstrip("@").lower()


def config_problems(settings: Settings) -> list[str]:
    problems = []
    handle = handle_of(settings)
    if not handle:
        problems.append("bluesky_handle is missing")
    elif not _HANDLE.match(handle):
        problems.append("bluesky_handle must be the account's handle, such as ember-shop.bsky.social")
    password = settings.bluesky_app_password.get_secret_value().strip()
    if not password:
        problems.append("bluesky_app_password is missing")
    elif not _APP_PASSWORD.match(password):
        problems.append(
            "bluesky_app_password must be an app password made in Bluesky's settings (four groups of four letters"
            " and digits), never the account's own password"
        )
    return problems


def valid_did(value: str) -> bool:
    return bool(_DID.match(value))
