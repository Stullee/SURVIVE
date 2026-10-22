from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.economy.clock import Clock, from_iso, owner_timezone, to_iso

BERLIN = ZoneInfo("Europe/Berlin")


def fixed(moment: str):
    return lambda: from_iso(moment)


def test_today_is_the_owners_local_day() -> None:
    # 23:30 UTC on 27 Sep is already 28 Sep in Berlin (UTC+2 in summer).
    clock = Clock(now=fixed("2026-09-27T23:30:00Z"), tz=BERLIN)
    assert clock.today() == date(2026, 9, 28)
    assert Clock(now=fixed("2026-09-27T23:30:00Z"), tz=UTC).today() == date(2026, 9, 27)


def test_day_bounds_in_summer_and_winter() -> None:
    clock = Clock(tz=BERLIN)
    assert clock.day_bounds(date(2026, 9, 28)) == ("2026-09-27T22:00:00Z", "2026-09-28T22:00:00Z")
    assert clock.day_bounds(date(2026, 12, 1)) == ("2026-11-30T23:00:00Z", "2026-12-01T23:00:00Z")


def test_daylight_saving_days_have_23_and_25_hours() -> None:
    clock = Clock(tz=BERLIN)
    start, end = clock.day_bounds(date(2026, 3, 29))  # clocks go forward
    assert (from_iso(end) - from_iso(start)).total_seconds() == 23 * 3600
    start, end = clock.day_bounds(date(2026, 10, 25))  # clocks go back
    assert (from_iso(end) - from_iso(start)).total_seconds() == 25 * 3600


def test_local_day_of_timestamp() -> None:
    clock = Clock(tz=BERLIN)
    assert clock.local_day("2026-09-27T22:30:00Z") == date(2026, 9, 28)
    assert clock.local_day("2026-09-27T21:59:59Z") == date(2026, 9, 27)


@pytest.mark.parametrize(
    ("value", "expected"), [("Europe/Berlin", BERLIN), ("", UTC), ("Not/AZone", UTC), ("../etc", UTC)]
)
def test_owner_timezone_from_env(monkeypatch: pytest.MonkeyPatch, value: str, expected) -> None:
    monkeypatch.setenv("TZ", value)
    assert owner_timezone() == expected


def test_iso_roundtrip() -> None:
    moment = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)
    assert to_iso(moment) == "2026-09-27T19:00:00Z"
    assert from_iso(to_iso(moment)) == moment
