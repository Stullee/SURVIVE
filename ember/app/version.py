"""The app version, read from ``config.yaml`` (the single source of truth).

The Dockerfile copies ``config.yaml`` into the image next to the ``app``
package, so the running code always reports the version the Supervisor installed.
"""

from __future__ import annotations

import hashlib
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


@cache
def build_id() -> str:
    """Identifies exactly what the dashboard is running: the version plus the static files' contents.

    Used as the cache key of every static file, and by an open dashboard page to
    notice that the app was updated underneath it (and reload itself), even for
    builds that changed files without changing the version.
    """
    digest = hashlib.sha256(app_version().encode())
    static = paths.WEB_DIR / "static"
    for path in sorted(static.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(static)).encode())
            digest.update(path.read_bytes())
    digest.update((paths.WEB_DIR / "index.html").read_bytes())
    return digest.hexdigest()[:16]
