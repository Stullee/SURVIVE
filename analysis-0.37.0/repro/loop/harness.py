"""Shared setup for the loop/scheduling reproductions (read-only on the repo)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(".")
SCRATCH = Path("/tmp/ember-repro/loop")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EMBER_SCHEDULER", "off")
os.environ.setdefault("EMBER_FAKE_DELAY_MS", "0")


def fresh_dir(name: str) -> Path:
    base = SCRATCH / "data"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=base))
    data = path / "data"
    data.mkdir()
    os.environ["EMBER_DATA_DIR"] = str(data)
    os.environ.pop("EMBER_OPTIONS_PATH", None)
    os.environ.pop("EMBER_DEV_MODE", None)
    return data


def no_ventures() -> None:
    """As tests/conftest.py does for tests not marked exploring."""
    from app.agent import plan

    plan._ventures = lambda conn, scope, now: []


def cleanup() -> None:
    from tests.economy_helpers import close_databases

    close_databases()
