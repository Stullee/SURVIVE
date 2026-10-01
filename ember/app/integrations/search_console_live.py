"""Google Search Console's API, for search_console.py (0.16.0): the only module that talks to Google.

Every request goes to oauth2.googleapis.com (an access token for the service account, from a JWT signed with its key)
or searchconsole.googleapis.com (Search Analytics, read-only): anything else is refused before it leaves, and redirects
aren't followed. Responses are size-limited and never logged; the access token is registered for log redaction.
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..economy.clock import Clock
from ..logging_setup import register_secret
from .search_console import API_URL, HOSTS, MAX_RESPONSE_BYTES, SCOPE, Row, SearchConsoleError, ServiceKey, _rows


class _Allowlist(httpx2.HTTPTransport):
    """Refuses every request that isn't HTTPS to Google's token endpoint or the Search Console API."""

    def handle_request(self, request: Any) -> Any:
        url = request.url
        if url.scheme != "https" or url.host not in HOSTS or url.port not in (None, 443):
            raise httpx2.ConnectError(f"Ember only talks to Google's Search Console API, not {url.host}")
        return super().handle_request(request)


class LiveProperty:
    """The owner's property, read with the service account (an access token for an hour, kept while it lasts)."""

    simulated = False

    def __init__(self, key: ServiceKey, site: str, clock: Clock, transport: Any = None) -> None:
        self.key = key
        self.site = site
        self.clock = clock
        self._transport = transport  # tests only
        self._token: tuple[str, Any] | None = None

    def _client(self) -> Any:
        return httpx2.Client(
            transport=self._transport or _Allowlist(retries=0),
            timeout=httpx2.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            with self._client() as client, client.stream(method, url, **kwargs) as response:
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SearchConsoleError("Google's answer was too large")
                status = response.status_code
        except SearchConsoleError:
            raise
        except httpx2.HTTPError as exc:
            raise SearchConsoleError(f"Google couldn't be reached ({type(exc).__name__})") from None
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            data = {}
        if 200 <= status < 300:
            return data
        raise SearchConsoleError(self._why(status, data))

    def _why(self, status: int, data: Any) -> str:
        error = data.get("error") if isinstance(data, dict) else None
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(data, dict):
            detail = str(data.get("error_description") or error or "")
        detail = " ".join(detail.split())[:200]
        if status == 403:
            return (
                f"Google refused ({detail or 'no permission'}): in Search Console, add {self.key.client_email} as a "
                f"user of {self.site} (Settings, Users and permissions), and enable the Search Console API in the "
                "key's Google Cloud project"
            )
        if status in (400, 401) and "invalid_grant" in json.dumps(data):
            return f"Google refused the key ({detail or 'invalid_grant'}): create a new key for the service account"
        return f"Google answered HTTP {status}" + (f": {detail}" if detail else "")

    def _access(self) -> str:
        now = self.clock.now()
        if self._token is not None and now < self._token[1]:
            return self._token[0]
        data = self._send(
            "POST",
            self.key.token_uri,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self._assertion(now)},
            headers={"Accept": "application/json"},
        )
        token = str(data.get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            raise SearchConsoleError("Google answered without an access token")
        register_secret(token)
        seconds = int(data.get("expires_in") or 3600)
        self._token = (token, now + timedelta(seconds=max(60, seconds - 120)))
        return token

    def _assertion(self, now: Any) -> str:
        """The signed JWT that proves the service account (RS256 with its private key)."""

        def part(value: dict[str, Any]) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        issued = int(now.timestamp())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key.private_key_id}
        claims = {
            "iss": self.key.client_email,
            "scope": SCOPE,
            "aud": self.key.token_uri,
            "iat": issued,
            "exp": issued + 3600,
        }
        signing = f"{part(header)}.{part(claims)}".encode()
        try:
            private = serialization.load_pem_private_key(self.key.private_key.encode(), password=None)
        except (ValueError, TypeError):
            raise SearchConsoleError("the key's private key can't be read: paste the whole key file again") from None
        if not isinstance(private, rsa.RSAPrivateKey):
            raise SearchConsoleError("the key's private key isn't an RSA key")
        signature = private.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        return signing.decode() + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def rows(self, dimension: str, start: date, end: date, limit: int) -> list[Row]:
        token = self._access()
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [dimension],
            "rowLimit": limit,
            "dataState": "all",  # the freshest numbers too (Google marks them as not final)
        }
        url = f"{API_URL}/webmasters/v3/sites/{quote(self.site, safe='')}/searchAnalytics/query"
        data = self._send("POST", url, json=body, headers={"Authorization": f"Bearer {token}"})
        return _rows(data)
