"""Which Etsy shop Ember may list in, and the one-time connection to the owner's.

In dry run it is always the fake shop (its state kept in the database, per dry-run session), so the owner can try
the flow. Live, it is the owner's shop when Etsy is switched on in the options, the app's keystring, shared secret
and redirect URI are set, and the owner connected it in the dashboard:

1. ``start`` makes a PKCE pair and a state and returns Etsy's page for the owner to allow access (the pair waits
   here, in memory, for CONNECT_MINUTES).
2. Etsy sends the owner's browser to the redirect URI. That page doesn't need to open: the owner copies its
   address into the dashboard, and ``finish`` checks the state, exchanges the code, reads which shop it is and
   keeps the tokens in the token file.
3. ``disconnect`` deletes the tokens (the owner can also remove the app's access at Etsy).
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, to_iso
from . import etsy, etsy_publisher
from .etsy import CONNECT_MINUTES, EtsyError, FakeShop, Shop, TaxonomyFile, TokenFile


class EtsyConnection:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        mode: str,
        session: int,
        tokens: TokenFile,
        taxonomy: TaxonomyFile,
        http_transport: Any = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.mode = mode
        self.tokens = tokens
        self.taxonomy = taxonomy
        self._http_transport = http_transport  # tests only
        self._lock = threading.Lock()
        self._pending: tuple[str, str, datetime] | None = None  # state, verifier, when
        self._fake_key = f"integrations.etsy.dry_run.fake.{session}"
        self._fake: FakeShop | None = None
        if mode == "dry_run":
            raw = db.get_meta(self._fake_key)
            state = json.loads(raw) if raw else None
            self._fake = FakeShop(clock, state, lambda s: db.set_meta(self._fake_key, json.dumps(s)))

    # --- the shop ---

    def shop(self) -> Shop | None:
        """The shop to list in now, or None (live: switched off, incomplete or not connected)."""
        if self._fake is not None:
            return self._fake
        if not self.settings.etsy_enabled or etsy.config_problems(self.settings):
            return None
        if self.tokens.load() is None:
            return None
        from .etsy_live import LiveShop  # the only module that talks to Etsy (and imports httpx2)

        return LiveShop(self.settings, self.clock, self.tokens, self._http_transport)

    # --- connecting (live) ---

    def start(self) -> str:
        """Etsy's page where the owner allows Ember's access. Raises EtsyError."""
        if self.mode != "live":
            raise EtsyError("in dry run the fake shop is always connected")
        if not self.settings.etsy_enabled:
            raise EtsyError("switch Etsy on in the app's options first")
        problems = etsy.config_problems(self.settings)
        if problems:
            raise EtsyError("; ".join(problems))
        verifier, challenge = etsy.pkce()
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._pending = (state, verifier, self.clock.now())
        return etsy.authorize_url(self.settings, state, challenge)

    def finish(self, pasted: str) -> etsy.ShopInfo:
        """Exchange the code in the address Etsy sent the owner to. Raises EtsyError."""
        with self._lock:
            pending = self._pending
        if pending is None or self.clock.now() - pending[2] > timedelta(minutes=CONNECT_MINUTES):
            raise EtsyError(f"start connecting again (a started connection waits {CONNECT_MINUTES} minutes)")
        state, verifier, _ = pending
        code = etsy.code_from(pasted, self.settings, state)
        from .etsy_live import connect  # the only module that talks to Etsy (and imports httpx2)

        info = connect(self.settings, self.clock, self.tokens, code, verifier, self._http_transport)
        with self._lock:
            self._pending = None
        self.db.set_meta(etsy_publisher.meta_key(self.mode, "last_error"), "")
        return info

    def disconnect(self) -> None:
        with self._lock:
            self._pending = None
        self.tokens.clear()

    # --- for the dashboard and the diagnostics (never a token) ---

    def status(self) -> tuple[str, str | None]:
        """ok, disabled, not_configured or not_connected, with a reason."""
        if self.mode == "dry_run":
            return "ok", "Dry run: a built-in fake shop; nothing reaches Etsy."
        if not self.settings.etsy_enabled:
            return "disabled", None
        problems = etsy.config_problems(self.settings)
        if problems:
            return "not_configured", "; ".join(problems)
        if self.tokens.load() is None:
            return "not_connected", "Connect your shop: System, Etsy, Connect."
        return "ok", None

    def describe(self) -> dict[str, Any]:
        status, reason = self.status()
        info: dict[str, Any] = {
            "mode": "fake" if self.mode == "dry_run" else "live",
            "status": status,
            "reason": reason,
            "shop_name": None,
            "shop_url": None,
            "connected_at": None,
            "refresh_expires_at": None,
            "connecting": self._pending is not None,
            "redirect_uri": self.settings.etsy_redirect_uri,
            "daily_limit": self.settings.etsy_listings_per_day,
            "last_sync_at": self.db.get_meta(etsy_publisher.meta_key(self.mode, "last_sync_at")),
            "last_error": self.db.get_meta(etsy_publisher.meta_key(self.mode, "last_error")) or None,
        }
        if self._fake is not None:
            shop = self._fake.info()
            info.update(shop_name=shop.name, shop_url=shop.url)
        else:
            tokens = self.tokens.load()
            if tokens is not None:
                info.update(
                    shop_name=tokens.shop_name,
                    shop_url=f"https://www.etsy.com/shop/{tokens.shop_name}",
                    connected_at=tokens.connected_at,
                    refresh_expires_at=tokens.refresh_expires_at,
                )
        return info

    def categories(self) -> list[tuple[int, str]]:
        """Etsy's categories for the tools: the fake shop's, or the cached copy of the live shop's."""
        if self._fake is not None:
            return self._fake.taxonomy()
        return self.taxonomy.load()[0]

    def refresh_categories(self, shop: Shop) -> None:
        """Fetch the live shop's categories when the cached copy is missing or a week old."""
        if shop.simulated or not self.taxonomy.stale(self.clock.now()):
            return
        nodes = shop.taxonomy()
        if nodes:
            self.taxonomy.save(nodes, to_iso(self.clock.now()))

    def shop_name(self) -> str | None:
        if self._fake is not None:
            return self._fake.info().name
        tokens = self.tokens.load()
        return tokens.shop_name if tokens else None

    def currency(self) -> str | None:
        if self._fake is not None:
            return self._fake.info().currency
        tokens = self.tokens.load()
        return tokens.currency if tokens else None
