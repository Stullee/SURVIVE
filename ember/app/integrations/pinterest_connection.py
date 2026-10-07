"""Which Pinterest account Ember pins to, and the one-time connection to the owner's (0.13.0, Phase E2).

Nothing while Pinterest is switched off (the pinterest_enabled option). In dry run it is the fake account (its state
kept in the database per dry-run session), so the owner can try the flow. Live, it is the owner's account once the
app's id, secret and redirect URI are set and the owner connected it in the dashboard:

1. ``start`` makes a PKCE pair and a state and returns Pinterest's page for the owner to allow access (the pair waits
   here, in memory, for CONNECT_MINUTES).
2. Pinterest sends the owner's browser to the redirect URI. That page doesn't need to open: the owner copies its
   address into the dashboard, and ``finish`` checks the state, exchanges the code, reads whose account it is and
   keeps the tokens in the token file.
3. ``disconnect`` deletes the tokens (the owner can also remove the app's access at Pinterest).

0.30.2: with the sandbox option on (live), the connection is to Pinterest's API sandbox, for the video of the owner's
Standard access request: its tokens are kept apart (the account's own connection stays for later), the agent has no
Pinterest account (``account`` is None: no tools, no pins of its, no numbers), and connecting puts a test pin on the
owner's list (``on_connect``), which Ember's code makes in the sandbox once they approve it (``sandbox_account``).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..db import Database
from ..economy.clock import Clock
from . import etsy, pinterest, pinterest_publisher
from .pinterest import CONNECT_MINUTES, Account, AccountInfo, FakePinterest, PinterestError, TokenFile

log = logging.getLogger(__name__)


class PinterestConnection:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        mode: str,
        session: int,
        tokens: TokenFile,
        http_transport: Any = None,
        on_connect: Callable[[], Any] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.mode = mode
        # 0.30.2: Pinterest's API sandbox, for the owner's Standard access request (live only: a dry run has its fake)
        self.sandbox = mode == "live" and settings.pinterest_enabled and settings.pinterest_sandbox
        self.tokens = TokenFile(tokens.path.with_name("sandbox_tokens.json")) if self.sandbox else tokens
        self._http_transport = http_transport  # tests only
        self._on_connect = on_connect  # 0.30.2: puts the sandbox's test pin on the owner's list
        self._lock = threading.Lock()
        self._pending: tuple[str, str, datetime] | None = None  # state, verifier, when
        self._fake: FakePinterest | None = None
        if mode == "dry_run" and settings.pinterest_enabled:
            key = f"integrations.pinterest.dry_run.fake.{session}"
            raw = db.get_meta(key)
            self._fake = FakePinterest(
                clock, json.loads(raw) if raw else None, lambda s: db.set_meta(key, json.dumps(s))
            )

    def account(self) -> Account | None:
        """The account to pin to now, or None (switched off; live: incomplete or not connected)."""
        if not self.settings.pinterest_enabled:
            return None
        if self._fake is not None:
            return self._fake
        return None if self.sandbox else self._live()

    def sandbox_account(self) -> Account | None:
        """0.30.2: the owner's account in Pinterest's API sandbox, for the test pin (None unless the sandbox option is
        on and the owner connected with it)."""
        return self._live() if self.sandbox else None

    def _live(self) -> Account | None:
        tokens = self.tokens.load()
        if pinterest.config_problems(self.settings) or tokens is None or pinterest.lapsed(tokens, self.clock.now()):
            return None
        from .pinterest_live import LiveAccount  # the only module that talks to Pinterest (and imports httpx2)

        return LiveAccount(self.settings, self.clock, self.tokens, self._http_transport)

    def username(self) -> str | None:
        """The account's name (the fake's in dry run), or None while none is connected."""
        if self._fake is not None:
            return self._fake.info().username
        tokens = self.tokens.load() if self.settings.pinterest_enabled and not self.sandbox else None
        return tokens.username if tokens is not None else None

    # --- connecting (live) ---

    def start(self) -> str:
        """Pinterest's page where the owner allows Ember's access. Raises PinterestError."""
        if self.mode != "live":
            raise PinterestError("in dry run the fake account is always connected")
        if not self.settings.pinterest_enabled:
            raise PinterestError("switch Pinterest on in the app's options first")
        problems = pinterest.config_problems(self.settings)
        if problems:
            raise PinterestError("; ".join(problems))
        verifier, challenge = etsy.pkce()
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._pending = (state, verifier, self.clock.now())
        return pinterest.authorize_url(self.settings, state, challenge)

    def finish(self, pasted: str) -> AccountInfo:
        """Exchange the code in the address Pinterest sent the owner to. Raises PinterestError."""
        with self._lock:
            pending = self._pending
        if pending is None or self.clock.now() - pending[2] > timedelta(minutes=CONNECT_MINUTES):
            raise PinterestError(f"start connecting again (a started connection waits {CONNECT_MINUTES} minutes)")
        state, verifier, _ = pending
        code = pinterest.code_from(pasted, self.settings, state)
        from .pinterest_live import connect

        info = connect(self.settings, self.clock, self.tokens, code, verifier, self._http_transport)
        with self._lock:
            self._pending = None
        self.db.set_meta(pinterest_publisher.meta_key(self.mode, "last_error"), "")
        if self.sandbox and self._on_connect is not None:
            try:
                self._on_connect()
            except Exception:  # noqa: BLE001 - connected all the same; connecting again asks for the test pin again
                log.exception("Putting the sandbox's test pin on the owner's list failed")
        return info

    def disconnect(self) -> None:
        with self._lock:
            self._pending = None
        self.tokens.clear()

    # --- for the dashboard and the diagnostics (never a token) ---

    def status(self) -> tuple[str, str | None]:
        """ok, disabled, not_configured, not_connected or (0.30.2) sandbox, with a reason."""
        if not self.settings.pinterest_enabled:
            return "disabled", None
        if self.mode == "dry_run":
            return "ok", "Dry run: a built-in fake account; nothing reaches Pinterest."
        problems = pinterest.config_problems(self.settings)
        if problems:
            return "not_configured", "; ".join(problems)
        tokens = self.tokens.load()
        if self.sandbox:  # 0.30.2: whether connected or not, no pins of the agent's until the owner turns it off
            then = "it waits for your approval" if tokens is not None else "connect your account (System, Pinterest)"
            return "sandbox", (
                f"Pinterest's API sandbox, for your Standard access request: a test pin only ({then}). No other pins "
                "until you turn the sandbox off, once Pinterest granted Standard access."
            )
        if tokens is None:
            return "not_connected", "Connect your account: System, Pinterest, Connect."
        if pinterest.lapsed(tokens, self.clock.now()):  # 0.15.0: unused for too long (the app was off)
            return "not_connected", "The connection expired: connect your account again (System, Pinterest)."
        return "ok", None

    def describe(self, scope: Any = None) -> dict[str, Any]:
        status, reason = self.status()
        info: dict[str, Any] = {
            "mode": "fake" if self.mode == "dry_run" else "sandbox" if self.sandbox else "live",
            "status": status,
            "reason": reason,
            "username": None,
            "profile_url": None,
            "connected_at": None,
            "connecting": self._pending is not None,
            "redirect_uri": self.settings.pinterest_redirect_uri,
            "daily_limit": self.settings.pinterest_pins_per_day,
            "last_sync_at": self.db.get_meta(pinterest_publisher.meta_key(self.mode, "last_sync_at")),
            "last_error": self.db.get_meta(pinterest_publisher.meta_key(self.mode, "last_error")) or None,
            "boards": [],
            "pins": [],
        }
        if self._fake is not None:
            account = self._fake.info()
            info.update(username=account.username, profile_url=account.url)
        else:
            tokens = self.tokens.load()
            if tokens is not None:
                info.update(
                    username=tokens.username,
                    profile_url=f"https://www.pinterest.com/{tokens.username}/",
                    connected_at=tokens.connected_at,
                )
        if scope is not None and status != "disabled":
            with self.db.connection() as conn:
                info["boards"] = [
                    {"board_id": b["board_id"], "name": b["name"]} for b in pinterest_publisher.boards(conn, scope)
                ]
                info["pins"] = [pinterest_publisher.pin_json(r) for r in pinterest_publisher.pins(conn, scope)]
        return info
