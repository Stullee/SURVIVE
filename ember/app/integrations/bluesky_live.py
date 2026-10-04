"""The Bluesky account Ember posts to, through the AT Protocol's XRPC API: the only module that talks to Bluesky.

Logins go to https://bsky.social, the entryway of the accounts Bluesky hosts; everything else goes to the account's own
server, which the login names (one of Bluesky's, a host under host.bsky.network: else the entryway takes it) and which
reads posts and profiles from Bluesky's app view. Any other address is refused before a request leaves, and redirects
aren't followed. Ember's code logs in with the handle and the app password from the options
(com.atproto.server.createSession), keeps the tokens in memory only (bluesky.Login: a restart logs in again, well
within Bluesky's 300 logins a day) and renews them with the refresh token before they expire. A login Bluesky refuses
(a wrong app password, an account taken down) is remembered, so it isn't tried again for REFUSED_WAIT, and a login
made with the account's own password rather than an app password, or to an account that isn't active, is refused
before anything is posted. Errors come back as ``NotSent`` (Bluesky refused: nothing changed; ``Refused`` for
the login, ``Gone`` for what isn't there) or ``Unclear`` (a timeout or a lost connection: something may have changed).
Responses are size-limited and never logged; the app password and the tokens are registered for log redaction.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx2

from ..config import Settings
from ..economy.clock import Clock
from ..logging_setup import register_secret
from . import bluesky
from .bluesky import (
    API_HOST,
    PDS_HOST,
    AccountInfo,
    BlueskyError,
    Gone,
    Login,
    NotSent,
    Picture,
    Post,
    PostRef,
    PostStats,
    Refused,
    Unclear,
)

log = logging.getLogger(__name__)

TIMEOUT = httpx2.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACCESS_DEFAULT = timedelta(minutes=30)  # an access token's life when the token doesn't say
REFRESH_DEFAULT = timedelta(days=30)  # a refresh token's
RENEW_EARLY = timedelta(minutes=5)
POST = "app.bsky.feed.post"
FULL_ACCESS = "com.atproto.access"  # the scope of a login made with the account's own password
FULL_ACCESS_REFUSED = (
    "the options hold the account's own password: make an app password for Ember in Bluesky's settings (Privacy and "
    "security, App passwords) and put that in bluesky_app_password"
)
# Bluesky's errors when it answers a login: the identifier or password, or the account itself (not a rate limit).
_LOGIN_REFUSALS = frozenset({400, 401, 403})
_EXPIRED = frozenset({"ExpiredToken", "InvalidToken"})
_GONE = frozenset({"RecordNotFound", "NotFound"})


class _Answer(NotSent):
    """Bluesky answered with an error: nothing changed. ``code`` is the XRPC error's name (e.g. ExpiredToken)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        detail = f": {code}" if code else ""
        detail += f" ({message})" if message else ""
        super().__init__(f"HTTP {status}{detail}")
        self.status = status
        self.code = code


def allowed(host: str) -> bool:
    """bsky.social, or one of the servers Bluesky hosts accounts on."""
    return host == API_HOST or PDS_HOST.match(host) is not None


class _Allowlist(httpx2.HTTPTransport):
    """Refuses every request that isn't HTTPS to bsky.social or one of Bluesky's servers (raised as a connect error)."""

    def handle_request(self, request: Any) -> Any:
        url = request.url
        if url.scheme != "https" or not allowed(str(url.host)) or url.port not in (None, 443):
            raise httpx2.ConnectError(f"Ember only talks to Bluesky's servers, not {url.scheme}://{url.host}")
        return super().handle_request(request)


def _client(transport: Any = None, host: str = API_HOST) -> httpx2.Client:
    return httpx2.Client(
        base_url=f"https://{host}/xrpc",
        transport=transport or _Allowlist(retries=0),
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "Ember (a Home Assistant app; https://github.com/Stullee/SURVIVE)"},
    )


def _send(client: httpx2.Client, method: str, path: str, *, changes: bool, **kwargs: Any) -> dict[str, Any]:
    try:
        with client.stream(method, path, **kwargs) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise NotSent("Bluesky's answer was too large")
            status = response.status_code
    except BlueskyError:
        raise
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
        raise NotSent(f"Bluesky couldn't be reached ({type(exc).__name__})") from None
    except httpx2.HTTPError as exc:
        error = f"the connection to Bluesky broke ({type(exc).__name__})"
        raise (Unclear(error) if changes else NotSent(error)) from None
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    if 200 <= status < 300:
        return data
    code = str(data.get("error") or "").strip()[:60]
    message = str(data.get("message") or "").strip()[:200]
    if status >= 500 and changes:
        raise Unclear(f"HTTP {status}" + (f": {code}" if code else ""))
    if status == 404 or code in _GONE:
        raise Gone(f"HTTP {status}" + (f": {code}" if code else ""))
    raise _Answer(status, code, message)


def _claims(token: str) -> dict[str, Any]:
    """What a token says of itself (its middle part), unverified: Ember's code only reads when it expires and what it
    may do. {} when it isn't readable."""
    try:
        middle = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _until(claims: dict[str, Any], now: datetime, default: timedelta) -> datetime:
    expires = claims.get("exp")
    if isinstance(expires, int | float) and not isinstance(expires, bool) and expires > now.timestamp():
        return datetime.fromtimestamp(expires, UTC)
    return now + default


def _pds(document: Any) -> str:
    """The host of the account's own server in its DID document ("" when it names none of Bluesky's servers)."""
    services = document.get("service") if isinstance(document, dict) else None
    for service in services if isinstance(services, list) else []:
        if not isinstance(service, dict) or not str(service.get("id") or "").endswith("#atproto_pds"):
            continue
        endpoint = urlsplit(str(service.get("serviceEndpoint") or ""))
        host = (endpoint.hostname or "").lower()
        if endpoint.scheme == "https" and endpoint.port is None and endpoint.path in ("", "/") and allowed(host):
            return host
    return ""


def _self_labelled(raw: Any, own: str, value: str) -> bool:
    """Whether the account put this label on itself (the profile's "bot": Bluesky's badge for automated accounts)."""
    return any(
        isinstance(label, dict)
        and str(label.get("src") or "") == own
        and str(label.get("val") or "") == value
        and not label.get("neg")
        for label in (raw if isinstance(raw, list) else [])
    )


def _labels(raw: Any, own: str) -> tuple[str, ...]:
    """The labels moderation put on a post or an account (not the ones it set on itself), each once."""
    found: list[str] = []
    for label in raw if isinstance(raw, list) else []:
        if not isinstance(label, dict) or label.get("neg") or str(label.get("src") or "") == own:
            continue
        value = str(label.get("val") or "")[:40]
        if value and value not in found:
            found.append(value)
    return tuple(found)


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0 else 0


class LiveAccount:
    simulated = False

    def __init__(self, settings: Settings, clock: Clock, login: Login, transport: Any = None) -> None:
        self.settings = settings
        self.clock = clock
        self.login = login
        self._transport = transport

    # --- logging in ---

    def _session(self) -> Login:
        """The login, made or renewed when needed. Raises Refused while Bluesky refuses it, NotSent or Unclear."""
        now = self.clock.now()
        login = self.login
        with login.lock:
            refused = login.waiting(now)
            if refused:
                raise Refused(f"Bluesky refused Ember's login: {refused}")
            if login.access and login.access_until is not None and login.access_until - RENEW_EARLY > now:
                return login
            if login.refresh and login.refresh_until is not None and login.refresh_until > now:
                try:
                    self._renew(now)
                    return login
                except _Answer:  # the refresh token is no longer good: log in again
                    login.forget()
            self._log_in(now)
            return login

    def _log_in(self, now: datetime) -> None:
        """com.atproto.server.createSession with the handle and the app password (called under the login's lock)."""
        password = self.settings.bluesky_app_password.get_secret_value().strip()
        register_secret(password)
        body = {"identifier": bluesky.handle_of(self.settings), "password": password}
        try:
            with _client(self._transport) as client:
                data = _send(client, "POST", "/com.atproto.server.createSession", changes=False, json=body)
        except _Answer as exc:
            if exc.status not in _LOGIN_REFUSALS:
                raise  # a rate limit: tried again later
            wrong = ": the handle or the app password is wrong" if exc.status == 401 else ""
            raise self._refusal(now, f"{exc}{wrong}") from None
        self._keep(data, now, first=True)

    def _renew(self, now: datetime) -> None:
        """com.atproto.server.refreshSession with the refresh token (called under the login's lock)."""
        with _client(self._transport) as client:
            data = _send(
                client,
                "POST",
                "/com.atproto.server.refreshSession",
                changes=False,
                headers={"Authorization": f"Bearer {self.login.refresh}"},
            )
        self._keep(data, now, first=False)

    def _refusal(self, now: datetime, why: str) -> Refused:
        """Remember that Bluesky refused the login (no new try for REFUSED_WAIT), and say so."""
        self.login.forget()
        self.login.refused, self.login.refused_at = why, now
        return Refused(f"Bluesky refused Ember's login: {why}")

    def _keep(self, data: dict[str, Any], now: datetime, *, first: bool) -> None:
        access, refresh = str(data.get("accessJwt") or ""), str(data.get("refreshJwt") or "")
        did, handle = str(data.get("did") or ""), str(data.get("handle") or "")
        if not access or not refresh or not bluesky.valid_did(did):
            raise NotSent("Bluesky answered the login without its tokens")
        for token in (access, refresh):
            register_secret(token)
        claims = _claims(access)
        if claims.get("scope") == FULL_ACCESS:
            raise self._refusal(now, FULL_ACCESS_REFUSED)
        if data.get("active") is False:  # deactivated, suspended or taken down: it can't post
            raise self._refusal(now, f"the account isn't active ({str(data.get('status') or 'deactivated')[:40]})")
        login = self.login
        login.did, login.handle = did, handle[:253]
        login.pds = _pds(data.get("didDoc")) or login.pds
        login.access, login.refresh = access, refresh
        login.access_until = _until(claims, now, ACCESS_DEFAULT)
        login.refresh_until = _until(_claims(refresh), now, REFRESH_DEFAULT)
        login.refused, login.refused_at = "", None
        if first:
            log.info("Logged in to Bluesky as %s", handle[:253])

    def keep_alive(self) -> None:
        """Log in, or renew the login, now (before anything is begun)."""
        self._session()

    # --- calls ---

    def _call(
        self, method: str, path: str, *, changes: bool = False, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """A call with the access token; once more with a renewed one if Bluesky says it expired (it was refused, so
        nothing changed)."""
        for attempt in (1, 2):
            login = self._session()
            headers = {**(headers or {}), "Authorization": f"Bearer {login.access}"}
            try:
                with _client(self._transport, login.pds or API_HOST) as client:
                    return _send(client, method, path, changes=changes, headers=headers, **kwargs)
            except _Answer as exc:
                if attempt == 2 or exc.code not in _EXPIRED:
                    raise
                with login.lock:
                    login.access_until = None  # renewed at the next _session
        raise NotSent("unreachable")  # pragma: no cover

    def info(self) -> AccountInfo:
        login = self._session()
        data = self._call("GET", "/app.bsky.actor.getProfile", params={"actor": login.did})
        handle = str(data.get("handle") or login.handle)[:253]
        return AccountInfo(
            handle=handle,
            did=login.did,
            followers=_count(data, "followersCount"),
            posts=_count(data, "postsCount"),
            labels=_labels(data.get("labels"), login.did),
            automated=_self_labelled(data.get("labels"), login.did, "bot"),
        )

    def _upload(self, picture: Picture) -> dict[str, Any]:
        """A picture as a blob for the post (com.atproto.repo.uploadBlob): nothing shows until a post uses it."""
        data = self._call(
            "POST",
            "/com.atproto.repo.uploadBlob",
            content=picture.data,
            headers={"Content-Type": picture.mime},
        )
        blob = data.get("blob")
        if not isinstance(blob, dict) or not blob.get("ref"):
            raise NotSent("Bluesky's answer about the picture wasn't readable")
        return blob

    def create_post(self, post: Post, image: Picture | None, thumb: Picture | None) -> PostRef:
        login = self._session()
        blob = self._upload(image) if image is not None else None
        card = self._upload(thumb) if thumb is not None and image is None else None
        size = (image.width, image.height) if image is not None else (0, 0)
        record = bluesky.record(post, self.clock.now(), blob, card, size)
        data = self._call(
            "POST",
            "/com.atproto.repo.createRecord",
            changes=True,
            json={"repo": login.did, "collection": POST, "record": record},
        )
        uri, cid = str(data.get("uri") or ""), str(data.get("cid") or "")
        if not uri.startswith(f"at://{login.did}/{POST}/") or not cid:
            raise Unclear("Bluesky's answer about the new post wasn't readable")
        return PostRef(uri, cid, bluesky.rkey_of(uri))

    def delete_post(self, rkey: str) -> None:
        login = self._session()
        self._call(
            "POST",
            "/com.atproto.repo.deleteRecord",
            changes=True,
            json={"repo": login.did, "collection": POST, "rkey": rkey},
        )

    def post_stats(self, uris: list[str]) -> dict[str, PostStats]:
        login = self._session()
        data = self._call("GET", "/app.bsky.feed.getPosts", params=[("uris", u) for u in uris])
        found = {}
        for item in data.get("posts") or []:
            if not isinstance(item, dict) or str(item.get("uri") or "") not in uris:
                continue
            found[str(item["uri"])] = PostStats(
                likes=_count(item, "likeCount"),
                reposts=_count(item, "repostCount"),
                replies=_count(item, "replyCount"),
                quotes=_count(item, "quoteCount"),
                labels=_labels(item.get("labels"), login.did),
            )
        return found
