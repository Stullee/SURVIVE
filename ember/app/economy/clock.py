"""Time for the economy: UTC timestamps, owner-local days.

Timestamps are stored in UTC. "Today" (daily cap, chart days) means the owner's
local calendar day, from the ``TZ`` environment variable the Supervisor sets to
Home Assistant's time zone. Days are computed through the time zone, so the
23- and 25-hour days around daylight-saving changes are handled correctly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

log = logging.getLogger(__name__)


def owner_timezone() -> tzinfo:
    name = os.environ.get("TZ", "").strip()
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # Days (the daily cap, the chart) then follow UTC instead of the owner's calendar.
        log.error("Unknown time zone %r; days are counted in UTC", name[:60])
        return UTC


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def normalized(text: str) -> str | None:
    """0.21.0: a time from outside in ISO 8601 (Printify's "2026-09-30 10:00:00+00:00", with fractions of a second or
    an offset) as Ember writes its own, in UTC; None when it isn't one. Without an offset it is UTC. Printify's was
    stored as it came, ``from_iso`` refused it, and no Printify order's cost was ever booked."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    return to_iso(moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC))


class Clock:
    """Current time and the owner's time zone; tests pass a fixed ``now``."""

    def __init__(self, now: Callable[[], datetime] = utc_now, tz: tzinfo | None = None) -> None:
        self._now = now
        self.tz = tz or owner_timezone()

    def now(self) -> datetime:
        return self._now()

    def today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def day_start(self, day: date) -> datetime:
        """The UTC moment the owner's local ``day`` begins."""
        return datetime.combine(day, time(0), self.tz).astimezone(UTC)

    def at(self, day: date, hour: int) -> datetime:
        """0.15.0: the UTC moment of ``hour`` o'clock on the owner's local ``day``. The day's start plus that many hours
        is an hour off on the days the clocks change."""
        return datetime.combine(day, time(hour), self.tz).astimezone(UTC)

    def day_bounds(self, day: date) -> tuple[str, str]:
        """UTC ISO timestamps [start, end) of the owner's local ``day``."""
        return to_iso(self.day_start(day)), to_iso(self.day_start(day + timedelta(days=1)))

    def local_day(self, timestamp: str) -> date:
        return from_iso(timestamp).astimezone(self.tz).date()
