"""Which Printify account Ember works with, and which of its shops (0.13.0, Phase E4).

Nothing while Printify is switched off (the printify_enabled option). In dry run it is the fake account (its state
kept in the database per dry-run session), so the owner can try the flow. Live, it is the owner's account once their
personal access token is set; the shop is the one printify_shop_id names, or the only one connected to Etsy (found once
and kept while the app runs).
"""

from __future__ import annotations

import json
import threading
from typing import Any

from ..config import Settings
from ..db import Database
from ..economy.clock import Clock
from . import printify, printify_publisher
from .printify import Account, FakePrintify, PrintifyError


class PrintifyConnection:
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, mode: str, session: int, http_transport: Any = None
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.mode = mode
        self._http_transport = http_transport  # tests only
        self._lock = threading.Lock()
        self._shop: printify.ShopInfo | None = None
        self._fake: FakePrintify | None = None
        if mode == "dry_run" and settings.printify_enabled:
            key = f"integrations.printify.dry_run.fake.{session}"
            raw = db.get_meta(key)
            self._fake = FakePrintify(
                clock, json.loads(raw) if raw else None, lambda s: db.set_meta(key, json.dumps(s))
            )

    def account(self) -> Account | None:
        """The account to work with now, or None (switched off; live: no token)."""
        if not self.settings.printify_enabled:
            return None
        if self._fake is not None:
            return self._fake
        if printify.config_problems(self.settings):
            return None
        from .printify_live import LiveAccount  # the only module that talks to Printify (and imports httpx2)

        return LiveAccount(self.settings, self._http_transport)

    def shop(self) -> printify.ShopInfo | None:
        """The Printify shop Ember sells through (found once), or None. Raises PrintifyError when it can't be told."""
        account = self.account()
        if account is None:
            return None
        with self._lock:
            if self._shop is None:
                self._shop = printify.etsy_shop(account.shops(), self.settings.printify_shop_id)
            return self._shop

    def shop_id(self) -> int | None:
        """The shop's number, or None while it can't be told (the dashboard says why)."""
        try:
            found = self.shop()
        except PrintifyError as exc:
            self.db.set_meta(printify_publisher.meta_key(self.mode, "last_error"), str(exc)[:300])
            return None
        return found.shop_id if found is not None else None

    # --- for the dashboard and the diagnostics (never the token) ---

    def status(self) -> tuple[str, str | None]:
        """ok, disabled or not_configured, with a reason."""
        if not self.settings.printify_enabled:
            return "disabled", None
        if self.mode == "dry_run":
            return "ok", "Dry run: a built-in fake account with a small catalog; nothing reaches Printify."
        problems = printify.config_problems(self.settings)
        if problems:
            return "not_configured", "; ".join(problems)
        return "ok", None

    def describe(self, scope: Any = None) -> dict[str, Any]:
        status, reason = self.status()
        info: dict[str, Any] = {
            "mode": "fake" if self.mode == "dry_run" else "live",
            "status": status,
            "reason": reason,
            "shop": None,
            "currency": self.settings.printify_currency,
            "daily_limit": self.settings.printify_products_per_day,
            "last_sync_at": self.db.get_meta(printify_publisher.meta_key(self.mode, "last_sync_at")),
            "last_error": self.db.get_meta(printify_publisher.meta_key(self.mode, "last_error")) or None,
            "products": [],
            "orders": [],
        }
        with self._lock:
            known = self._shop
        if self._fake is not None:
            known = self._fake.shops()[0]
        if known is not None:
            info["shop"] = {"shop_id": known.shop_id, "title": known.title}
        if scope is not None and status != "disabled":
            with self.db.connection() as conn:
                info["products"] = [
                    printify_publisher.product_json(r) for r in printify_publisher.products(conn, scope)
                ]
                info["orders"] = printify_publisher.orders_json(conn, scope)
        return info
