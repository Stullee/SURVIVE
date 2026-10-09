"""Shared helpers for the product audit scripts (scratch only)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.agent.sandbox import Jail

SCRATCH = Path("/tmp/ember-repro/products")
LO_PROFILE = SCRATCH / "lo-profile"


def fresh_jail(name: str) -> Jail:
    root = SCRATCH / "out" / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    jail = Jail(root)
    jail.ensure_root()
    return jail


def _profile() -> str:
    """A LibreOffice user profile that recalculates every formula when it loads an OOXML file."""
    user = LO_PROFILE / "user"
    user.mkdir(parents=True, exist_ok=True)
    xcu = user / "registrymodifications.xcu"
    if not xcu.exists():
        xcu.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
            '<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" '
            'oor:op="fuse"><value>0</value></prop></item>\n'
            '<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" '
            'oor:op="fuse"><value>0</value></prop></item>\n'
            "</oor:items>\n"
        )
    return LO_PROFILE.as_uri()


def lo_convert(path: Path, fmt: str, outdir: Path) -> Path:
    """Convert ``path`` with LibreOffice headless (recalculating formulas) into ``outdir``."""
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "soffice",
        f"-env:UserInstallation={_profile()}",
        "--headless",
        "--norestore",
        "--convert-to",
        fmt,
        "--outdir",
        str(outdir),
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError(f"soffice failed: {result.stderr} {result.stdout}")
    stem = path.stem
    ext = fmt.split(":")[0]
    out = outdir / f"{stem}.{ext}"
    if not out.exists():
        raise RuntimeError(f"no output {out}: {result.stdout} {result.stderr}")
    return out
