"""Shared setup for the platform reproductions: the tests' helpers, with conftest's autouse patches applied by hand."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
assert os.environ.get("EMBER_DATA_DIR"), "set EMBER_DATA_DIR"
os.environ.setdefault("EMBER_SCHEDULER", "off")
os.environ.setdefault("EMBER_FAKE_DELAY_MS", "0")

from app.agent import plan  # noqa: E402

# conftest.ventures_in_the_plan (not 'exploring'): the plan is the products' alone
plan._ventures = lambda conn, scope, now: []  # type: ignore[assignment]

DATA = Path(os.environ["EMBER_DATA_DIR"])
DATA.mkdir(parents=True, exist_ok=True)


def rows(agent, sql, params=()):
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, params)]
