"""Request filtering and security headers.

Inside Home Assistant the dashboard is only reachable through Ingress: the
Supervisor proxies authenticated users' requests from 172.30.32.2 (it also runs
the watchdog health check from there). Everything else is refused, with one
exception: ``GET /api/sensors`` is also answered for 172.30.32.1 so the owner
can set up a REST sensor. That address is the internal network's gateway, which
is where requests from the Home Assistant host network come from: Home Assistant
Core itself, but also any app that runs with host networking and processes on
the host. The sensor JSON therefore only ever contains non-sensitive numbers.

State-changing requests must also carry the ``X-Ember-Request`` header, which a
cross-site form or image tag can't add; that blocks cross-site request forgery.

In local development (``EMBER_DEV_MODE``) there is no Ingress proxy, so any
client may connect, but only with a ``localhost`` Host header: that stops a
DNS-rebinding web page from talking to a developer's instance.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from .logging_setup import printable

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

INGRESS_PROXY_IP = ipaddress.ip_address("172.30.32.2")
HOST_NETWORK_GATEWAY_IP = ipaddress.ip_address("172.30.32.1")
HOST_NETWORK_READ_PATHS = frozenset({"/api/sensors"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-ember-request"
DEV_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})

_INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_\-]{1,128}")
_HOST_PORT = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^:\[\]]+)(?::\d{1,5})?$")

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        b"connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        b"form-action 'self'; frame-ancestors 'self'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


def ingress_base_href(header_value: str | None) -> str:
    """The base URL for relative links, from the ``X-Ingress-Path`` header.

    Only a well-formed ingress path is trusted; anything else falls back to ``/``
    (direct access during local development).
    """
    if header_value and _INGRESS_PATH.fullmatch(header_value):
        return header_value + "/"
    return "/"


def is_local_host_header(value: str | None) -> bool:
    """True for ``localhost``, ``127.0.0.1`` or ``[::1]``, with or without a port."""
    match = _HOST_PORT.match((value or "").strip().lower())
    return bool(match) and match.group(1) in DEV_HOSTNAMES


class AccessPolicy:
    def __init__(self, dev_mode: bool = False) -> None:
        self.dev_mode = dev_mode

    def allows(self, client_host: str | None, path: str, method: str) -> bool:
        if self.dev_mode:
            return True
        try:
            ip = ipaddress.ip_address(client_host or "")
        except ValueError:
            return False
        if ip == INGRESS_PROXY_IP:
            return True
        return ip == HOST_NETWORK_GATEWAY_IP and method in ("GET", "HEAD") and path in HOST_NETWORK_READ_PATHS


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, policy: AccessPolicy) -> None:
        self.app = app
        self.policy = policy
        self._reported: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # The dashboard uses no websockets (and the image ships no websocket
            # library, so uvicorn refuses upgrades itself). Refuse them here too in
            # case a future dependency adds one.
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else None
        method = scope["method"]
        path = scope["path"]
        if not self.policy.allows(host, path, method):
            self._report(host, path)
            await _plain(send, 403, b"Forbidden")
            return
        if self.policy.dev_mode and not is_local_host_header(_header(scope, "host")):
            await _plain(send, 403, b"Development mode only answers requests for localhost")
            return
        if method not in SAFE_METHODS and _header(scope, CSRF_HEADER) != "1":
            await _plain(send, 403, b"Missing X-Ember-Request header")
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in names)
                if b"cache-control" not in names and not path.startswith("/static/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _report(self, host: str | None, path: str) -> None:
        key = host or "unknown"
        if key in self._reported or len(self._reported) > 100:
            return
        self._reported.add(key)
        log.warning(
            "Refused request from %s to %s (only Home Assistant Ingress may connect)",
            printable(key, 64),
            printable(path, 100),
        )


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


async def _plain(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
