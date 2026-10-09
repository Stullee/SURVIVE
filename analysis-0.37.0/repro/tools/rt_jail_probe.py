"""Review probe of the workspace jail (app/agent/sandbox.py): path validation, links, special files and quotas.

Run from ember/:  python <this file> <empty scratch folder>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.agent.sandbox import Jail, Limits, QuotaError, SandboxError

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
outside = root / "outside.txt"
outside.write_text("secret outside the workspace\n")
jail = Jail(root / "ws")
jail.ensure_root()

print("== path validation ==")
for path in [
    "/etc/passwd",
    "~/x.md",
    "../outside.txt",
    "a/../x.md",
    "./x.md",
    "a/./x.md",
    "a//x.md",
    "x.md\n",
    "x\x00.md",
    "ｘ.md",  # fullwidth x
    "x​.md",  # zero-width space
    "a\\b.md",
    ".x.md",
    "-x.md",
    "a/b/c/d/e.md",
    "n" * 65 + ".md",
    "/".join(["a" * 60] * 3) + "/x.md",
    "X.MD",
    "x.md/",
    "x.exe",
    "x.pdf",
]:
    try:
        jail.write(path, "hi")
        print(f"ACCEPTED {path!r}")
    except SandboxError as exc:
        print(f"refused  {path!r}: {exc}")

print("== links and special files ==")
ws = root / "ws"
(ws / "link.md").symlink_to(outside)
os.symlink(root, ws / "dirlink")
os.link(outside, ws / "hard.md")
os.mkfifo(ws / "fifo.md")
for path in ["link.md", "dirlink/outside.txt", "hard.md", "fifo.md"]:
    for op in ("read", "write", "delete"):
        try:
            if op == "read":
                jail.read(path)
            elif op == "write":
                jail.write(path, "x")
            else:
                jail.delete(path)
            print(f"ALLOWED {op} {path}")
        except (SandboxError, OSError) as exc:
            print(f"refused {op} {path}: {type(exc).__name__}: {exc}")
print("outside file intact:", outside.read_text() == "secret outside the workspace\n")

print("== quotas ==")
small = Jail(root / "small", Limits(max_file_bytes=100, max_files=3, max_folders=2, max_total_bytes=250,
                                    max_product_bytes=50, max_product_total_bytes=80))
small.ensure_root()
steps = [
    ("text 101 B", lambda: small.write("a.md", "x" * 101)),
    ("text 100 B", lambda: small.write("a.md", "x" * 100)),
    ("text #2 100 B", lambda: small.write("b.md", "x" * 100)),
    ("text #3 60 B (total 260 > 250)", lambda: small.write("c.md", "x" * 60)),
    ("product 51 B", lambda: small.write_bytes("p.png", b"x" * 51)),
    ("product 50 B", lambda: small.write_bytes("p.png", b"x" * 50)),
    ("4th file", lambda: small.write("d.md", "x")),
    ("3 new folders", lambda: small.write("f1/f2/f3.md", "x")),
]
for label, step in steps:
    try:
        step()
        print(f"ok       {label}")
    except QuotaError as exc:
        print(f"quota    {label}: {exc}")
    except SandboxError as exc:
        print(f"refused  {label}: {exc}")
