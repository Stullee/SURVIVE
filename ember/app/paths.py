"""Filesystem locations.

Everything the app writes lives under the data directory (``/data`` inside the
Home Assistant app container, which the Supervisor persists and backs up).
``EMBER_DATA_DIR`` overrides it for local development and tests.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
# The app root inside the image (/opt/ember); config.yaml and CHANGELOG.md are copied here.
ROOT_DIR = APP_DIR.parent
WEB_DIR = APP_DIR / "web"
MIGRATIONS_DIR = APP_DIR / "migrations"
CONSTITUTION_PATH = APP_DIR / "agent" / "constitution.md"
# Facts the owner collected about the outside world; shipped with each release, shown in every plan and work step.
KNOWLEDGE_PATH = APP_DIR / "agent" / "knowledge.md"
MANIFEST_PATH = ROOT_DIR / "config.yaml"
CHANGELOG_PATH = ROOT_DIR / "CHANGELOG.md"
# 0.42.0 (analysis-0.40.1 codebase, 6.2 disk): what Ember leaves free on the data folder's disk for its database, which
# grows with every cycle and must never meet a full disk. The workspace's quotas are absolute (2 GB of products on a
# Raspberry Pi's card): a workspace write or a workshop file that would leave less is refused, and the dashboard warns.
FREE_FLOOR_BYTES = 1024 * 1024 * 1024


def data_dir() -> Path:
    return Path(os.environ.get("EMBER_DATA_DIR", "/data"))


def free_bytes(path: Path | None = None) -> int:
    """0.42.0: the bytes this process may still write on the disk that holds ``path`` (the data folder): one statvfs."""
    return shutil.disk_usage(path or data_dir()).free


def disk_full_text(free: int, floor: int = FREE_FLOOR_BYTES) -> str:
    """0.42.0: why a write is refused under the floor, for the agent."""
    return (
        f"the device has {size_text(free)} free; Ember keeps {size_text(floor)} free for its database: delete files "
        "you no longer need (workspace_write delete) or ask your owner"
    )


def storage() -> dict[str, Any]:
    """0.42.0: the free space on the data folder's disk (in whole MB: the dashboard redraws its System part when it
    changes) and whether it is under FREE_FLOOR_BYTES, for the dashboard and the diagnostics report."""
    try:
        free = free_bytes()
    except OSError as exc:  # reported, never raised
        return {"free_bytes": None, "floor_bytes": FREE_FLOOR_BYTES, "low": False, "error": type(exc).__name__}
    return {"free_bytes": free - free % (1024 * 1024), "floor_bytes": FREE_FLOOR_BYTES, "low": free < FREE_FLOOR_BYTES}


def size_text(size: int) -> str:
    """A disk's bytes as the owner and the agent read them: "812 MB", "1 GB", "14.6 GB"."""
    if size < 1024**3:
        return f"{size // 1024**2:,} MB"
    return f"{size / 1024**3:.1f}".removesuffix(".0") + " GB"


def options_path() -> Path:
    override = os.environ.get("EMBER_OPTIONS_PATH")
    return Path(override) if override else data_dir() / "options.json"


def db_path() -> Path:
    return data_dir() / "ember.db"


def backups_dir() -> Path:
    return data_dir() / "backups"


def etsy_dir() -> Path:
    """The Etsy connection's tokens (mode 0600) and the cached category list."""
    return data_dir() / "etsy"


def pinterest_dir() -> Path:
    """0.13.0: the Pinterest connection's tokens (mode 0600)."""
    return data_dir() / "pinterest"
