"""Filesystem locations.

Everything the app writes lives under the data directory (``/data`` inside the
Home Assistant app container, which the Supervisor persists and backs up).
``EMBER_DATA_DIR`` overrides it for local development and tests.
"""

from __future__ import annotations

import os
from pathlib import Path

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


def data_dir() -> Path:
    return Path(os.environ.get("EMBER_DATA_DIR", "/data"))


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
