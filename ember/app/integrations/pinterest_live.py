"""The owner's Pinterest account, through Pinterest's API v5: the only module that talks to Pinterest.

Every request goes to https://api.pinterest.com (anything else is refused before it leaves, and redirects aren't
followed), with the OAuth access token, refreshed when it is about to expire (the token endpoint takes the app's id and
secret as HTTP Basic). Errors come back as ``NotSent`` (Pinterest refused: nothing changed; ``Gone`` for what isn't
there) or ``Unclear`` (a timeout or a lost connection: something may have changed). Responses are size-limited and
never logged; tokens and the app secret are registered for log redaction.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import timedelta
from typing import Any

import httpx2

from ..config import Settings
from ..economy.clock import Clock, from_iso, to_iso
from ..logging_setup import register_secret
from .pinterest import (
    API_HOST,
    API_URL,
    AccountInfo,
    Board,
    Gone,
    NotSent,
    Pin,
    PinStats,
    PinterestError,
    TokenFile,
    Tokens,
    Unclear,
)

log = logging.getLogger(__name__)

TIMEOUT = httpx2.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACCESS_SECONDS = 3_600
REFRESH_DAYS = 365  # a continuous refresh token lasts a year from its last use
REFRESH_EARLY = timedelta(minutes=5)
MIME = {".png": "image/png", ".jpg": "image/jpeg"}
_REFRESH_LOCK = threading.Lock()


class _Allowlist(httpx2.HTTPTransport):
    """Refuses every request that isn't HTTPS to api.pinterest.com (raised as a connect error)."""

    def handle_request(self, request: Any) -> Any:
        url = request.url
        if url.scheme != "https" or url.host != API_HOST or url.port not in (None, 443):
            raise httpx2.ConnectError(f"Ember only talks to {API_URL} for Pinterest, not {url.scheme}://{url.host}")
        return super().handle_request(request)


def _client(transport: Any = None) -> httpx2.Client:
    return httpx2.Client(
        base_url=API_URL,
        transport=transport or _Allowlist(retries=0),
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )


def _send(client: httpx2.Client, method: str, path: str, *, changes: bool, **kwargs: Any) -> Any:
    try:
        with client.stream(method, path, **kwargs) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise NotSent("Pinterest's answer was too large")
            status = response.status_code
    except PinterestError:
        raise
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
        raise NotSent(f"Pinterest couldn't be reached ({type(exc).__name__})") from None
    except httpx2.HTTPError as exc:
        error = f"the connection to Pinterest broke ({type(exc).__name__})"
        raise (Unclear(error) if changes else NotSent(error)) from None
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        data = {}
    if 200 <= status < 300:
        return data
    detail = str(data.get("message") or "").strip()[:200] if isinstance(data, dict) else ""
    message = f"HTTP {status}" + (f": {detail}" if detail else "")
    if status >= 500 and changes:
        raise Unclear(message)
    raise (Gone if status == 404 else NotSent)(message)


def _basic(settings: Settings) -> str:
    secret = settings.pinterest_app_secret.get_secret_value().strip()
    register_secret(secret)
    raw = f"{settings.pinterest_app_id}:{secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _token_request(settings: Settings, form: dict[str, str], transport: Any) -> dict[str, Any]:
    with _client(transport) as client:
        data = _send(
            client,
            "POST",
            "/oauth/token",
            changes=False,
            data=form,
            headers={"Authorization": _basic(settings), "Accept": "application/json"},
        )
    if not isinstance(data, dict) or not data.get("access_token"):
        raise NotSent("Pinterest answered without tokens")
    for key in ("access_token", "refresh_token"):
        if data.get(key):
            register_secret(str(data[key]))
    return data


def connect(
    settings: Settings, clock: Clock, tokens: TokenFile, code: str, verifier: str, transport: Any = None
) -> AccountInfo:
    """Exchange the authorization code, read whose account it is and keep the tokens."""
    now = clock.now()
    data = _token_request(
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.pinterest_redirect_uri,
            "code_verifier": verifier,
            "continuous_refresh": "true",
        },
        transport,
    )
    access = str(data["access_token"])
    with _client(transport) as client:
        me = _send(client, "GET", "/user_account", changes=False, headers={"Authorization": f"Bearer {access}"})
    info = _account(me)
    tokens.save(
        Tokens(
            access_token=access,
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=to_iso(now + timedelta(seconds=int(data.get("expires_in") or ACCESS_SECONDS))),
            refresh_expires_at=to_iso(now + timedelta(days=REFRESH_DAYS)),
            username=info.username,
            connected_at=to_iso(now),
        )
    )
    return info


def _account(data: Any) -> AccountInfo:
    if not isinstance(data, dict) or not data.get("username"):
        raise NotSent("Pinterest's answer about the account wasn't readable")
    name = str(data["username"])[:100]
    return AccountInfo(name, f"https://www.pinterest.com/{name}/")


class LiveAccount:
    simulated = False

    def __init__(self, settings: Settings, clock: Clock, tokens: TokenFile, transport: Any = None) -> None:
        self.settings = settings
        self.clock = clock
        self.tokens = tokens
        self._transport = transport

    def _access(self) -> Tokens:
        tokens = self.tokens.load()
        if tokens is None:
            raise NotSent("Pinterest isn't connected")
        now = self.clock.now()
        if from_iso(tokens.expires_at) - REFRESH_EARLY > now:
            return tokens
        with _REFRESH_LOCK:
            tokens = self.tokens.load()
            if tokens is None:
                raise NotSent("Pinterest isn't connected")
            if from_iso(tokens.expires_at) - REFRESH_EARLY > now:
                return tokens
            if not tokens.refresh_token or from_iso(tokens.refresh_expires_at) <= now:
                raise NotSent("the connection to Pinterest expired: connect it again (System, Pinterest)")
            try:
                data = _token_request(
                    self.settings,
                    {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
                    self._transport,
                )
            except NotSent as exc:
                raise NotSent(f"Pinterest didn't renew the connection ({exc}): connect it again") from None
            tokens.access_token = str(data["access_token"])
            tokens.refresh_token = str(data.get("refresh_token") or tokens.refresh_token)
            tokens.expires_at = to_iso(now + timedelta(seconds=int(data.get("expires_in") or ACCESS_SECONDS)))
            tokens.refresh_expires_at = to_iso(now + timedelta(days=REFRESH_DAYS))
            self.tokens.save(tokens)
            return tokens

    def _call(self, method: str, path: str, *, changes: bool = False, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._access().access_token}", "Accept": "application/json"}
        with _client(self._transport) as client:
            return _send(client, method, path, changes=changes, headers=headers, **kwargs)

    def info(self) -> AccountInfo:
        return _account(self._call("GET", "/user_account"))

    def create_board(self, name: str, description: str) -> Board:
        data = self._call(
            "POST", "/boards", changes=True, json={"name": name, "description": description, "privacy": "PUBLIC"}
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise Unclear("Pinterest's answer about the new board wasn't readable")
        return Board(str(data["id"]), str(data.get("name") or name))

    def create_pin(self, board_id: str, pin: Pin, image: bytes) -> str:
        suffix = "." + pin.image.path.rsplit(".", 1)[-1].lower()
        body = {
            "board_id": board_id,
            "title": pin.title,
            "description": pin.full_description(),
            "link": pin.link,
            "alt_text": pin.alt_text or None,
            "media_source": {
                "source_type": "image_base64",
                "content_type": MIME.get(suffix, "image/png"),
                "data": base64.b64encode(image).decode("ascii"),
            },
        }
        data = self._call("POST", "/pins", changes=True, json={k: v for k, v in body.items() if v is not None})
        if not isinstance(data, dict) or not data.get("id"):
            raise Unclear("Pinterest's answer about the new pin wasn't readable")
        return str(data["id"])

    def delete_pin(self, pin_id: str) -> None:
        self._call("DELETE", f"/pins/{pin_id}", changes=True)

    def pin_stats(self, pin_id: str) -> PinStats:
        data = self._call("GET", f"/pins/{pin_id}", params={"pin_metrics": "true"})
        metrics = (data.get("pin_metrics") or {}) if isinstance(data, dict) else {}
        lifetime = metrics.get("lifetime_metrics") or metrics.get("all_time") or {}

        def number(key: str) -> int:
            value = lifetime.get(key)
            return int(value) if isinstance(value, int | float) else 0

        return PinStats(number("impression"), number("save"), number("outbound_click"))
