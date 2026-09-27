"""Process-wide state shared by the web routes (and, later, the scheduler)."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from typing import Any

from .config import LoadedSettings
from .db import Database, utcnow
from .economy.service import Economy
from .events import DatabaseLogHandler
from .version import app_version

log = logging.getLogger(__name__)


@dataclass
class AppState:
    loaded: LoadedSettings
    db: Database
    dev_mode: bool = False
    db_error: str | None = None
    started_at: str = field(default_factory=utcnow)
    installed_at: str | None = None
    log_handler: DatabaseLogHandler | None = None
    economy: Economy | None = None
    economy_error: str | None = None

    def system_info(self) -> dict[str, Any]:
        settings = self.loaded.settings
        schema_version = None
        if self.db_error is None:
            try:
                schema_version = self.db.schema_version()
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                log.warning("Could not read schema version: %s", exc)
        return {
            "version": app_version(),
            "started_at": self.started_at,
            "installed_at": self.installed_at,
            "dry_run": settings.dry_run,
            "dev_mode": self.dev_mode,
            "safe_mode": self.loaded.safe_mode,
            "config_errors": list(self.loaded.errors),
            "config_source": self.loaded.source,
            "options": settings.public_dict(),
            "database": {"ok": self.db_error is None, "error": self.db_error, "schema_version": schema_version},
            "sensor_url": sensor_url(),
            "price_warnings": settings.price_warnings(),
            "economy_error": self.economy_error,
            "economy_broken": self.economy.health.broken if self.economy is not None else None,
        }

    def recent_events(self, limit: int = 30) -> list[dict[str, Any]]:
        if self.db_error is not None:
            return []
        if self.log_handler is not None:
            # Show warnings logged a moment ago (they are written in the background).
            self.log_handler.flush(timeout=0.5)
        try:
            return self.db.recent_events(limit=limit)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            log.warning("Could not read events: %s", exc)
            return []


def sensor_url() -> str:
    """Where a Home Assistant REST sensor can read /api/sensors.

    Inside Home Assistant the container hostname is the app's internal DNS name
    (``local-ember`` for a local install, ``<hash>-ember`` from a repository).
    """
    return f"http://{socket.gethostname()}:8099/api/sensors"
