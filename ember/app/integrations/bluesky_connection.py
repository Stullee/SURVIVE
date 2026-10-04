"""Which Bluesky account Ember posts to (0.19.0).

Nothing while Bluesky is switched off (the bluesky_enabled option). In dry run it is the fake account (its state kept
in the database per dry-run session), so the owner can try the flow. Live, it is the account the owner made for Ember,
once its handle and an app password are set in the options: Ember's code logs in with them when it first needs to and
keeps the login in memory only (``bluesky.Login``). While Bluesky refuses the login, the account isn't offered for
REFUSED_WAIT (the dashboard and the plan say why), so a wrong app password isn't tried again and again.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..db import Database
from ..economy.clock import Clock
from . import bluesky, bluesky_publisher
from .bluesky import Account, FakeBluesky, Login


class BlueskyConnection:
    def __init__(
        self, db: Database, clock: Clock, settings: Settings, mode: str, session: int, http_transport: Any = None
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.mode = mode
        self.login = Login()  # live: the login, in memory only
        self._http_transport = http_transport  # tests only
        self._fake: FakeBluesky | None = None
        if mode == "dry_run" and settings.bluesky_enabled:
            key = f"integrations.bluesky.dry_run.fake.{session}"
            raw = db.get_meta(key)
            self._fake = FakeBluesky(clock, json.loads(raw) if raw else None, lambda s: db.set_meta(key, json.dumps(s)))

    def account(self) -> Account | None:
        """The account to post to now, or None (switched off; live: incomplete, or Bluesky refused the login lately)."""
        if not self.settings.bluesky_enabled:
            return None
        if self._fake is not None:
            return self._fake
        if bluesky.config_problems(self.settings) or self.login.waiting(self.clock.now()):
            return None
        from .bluesky_live import LiveAccount  # the only module that talks to Bluesky (and imports httpx2)

        return LiveAccount(self.settings, self.clock, self.login, self._http_transport)

    def handle(self) -> str | None:
        """The account's handle (the fake's in dry run), or None while Bluesky is off."""
        if self._fake is not None:
            return self._fake.HANDLE
        if not self.settings.bluesky_enabled:
            return None
        return bluesky.handle_of(self.settings) or None

    def followers(self) -> int | None:
        """The account's followers at the last sync, or None before one."""
        raw = self.db.get_meta(bluesky_publisher.meta_key(self.mode, "followers"))
        return int(raw) if raw and raw.isdigit() else None

    # --- for the dashboard, the plan and the diagnostics (never the app password or a token) ---

    def status(self) -> tuple[str, str | None]:
        """ok, disabled, not_configured or not_connected, with a reason."""
        if not self.settings.bluesky_enabled:
            return "disabled", None
        if self.mode == "dry_run":
            return "ok", "Dry run: a built-in fake account; nothing reaches Bluesky."
        problems = bluesky.config_problems(self.settings)
        if problems:
            return "not_configured", "; ".join(problems)
        refused = self.login.waiting(self.clock.now())
        if refused:
            return "not_connected", (
                f"Bluesky refused Ember's login ({refused.rstrip('.')}): check the handle and the app password in the"
                " options, then restart the app."
            )
        return "ok", None

    def describe(self, scope: Any = None) -> dict[str, Any]:
        status, reason = self.status()
        handle = self.handle()
        meta = bluesky_publisher.meta_key
        info: dict[str, Any] = {
            "mode": "fake" if self.mode == "dry_run" else "live",
            "status": status,
            "reason": reason,
            "handle": handle,
            "profile_url": bluesky.profile_url(handle) if handle else None,
            "followers": self.followers(),
            "labels": self.db.get_meta(meta(self.mode, "labels")) or None,
            # the profile's automation label at the last sync (Bluesky's badge; None before one)
            "automated": {"1": True, "0": False}.get(self.db.get_meta(meta(self.mode, "automated")) or ""),
            "daily_limit": self.settings.bluesky_posts_per_day,
            "last_sync_at": self.db.get_meta(meta(self.mode, "last_sync_at")),
            "last_error": self.db.get_meta(meta(self.mode, "last_error")) or None,
            "posts": [],
        }
        if scope is not None and status != "disabled":
            with self.db.connection() as conn:
                info["posts"] = [bluesky_publisher.post_json(r) for r in bluesky_publisher.posts(conn, scope)]
        return info
