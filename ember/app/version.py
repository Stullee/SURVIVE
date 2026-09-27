"""The app version, read from ``config.yaml`` (the single source of truth).

The Dockerfile copies ``config.yaml`` into the image next to the ``app``
package, so the running code always reports the version the Supervisor installed.
"""

from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path

from . import paths

_VERSION_LINE = re.compile(r"""^version:\s*["']?([^"'\s#]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


def read_version(manifest: Path) -> str | None:
    try:
        match = _VERSION_LINE.search(manifest.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


@cache
def app_version() -> str:
    return os.environ.get("EMBER_VERSION") or read_version(paths.MANIFEST_PATH) or "dev"
