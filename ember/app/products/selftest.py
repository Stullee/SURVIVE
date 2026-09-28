"""Make one of each product in a temporary folder, sealed like a tool call: ``python -m app.products.selftest``.

The image build runs this on every platform, so a library that doesn't work there (a missing native part, say)
fails the build instead of the owner's first product.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from ..agent import netguard
from ..agent.sandbox import Jail
from . import images, make

DOCUMENT = """---
title: Self-test
theme: bold
sidebar: left
footer: Page {page} of {pages}
---
::: sidebar
::: photo 35x45 Photo
## Contact
Grüße aus München · 你好
::: main
# Self-test
A paragraph with **bold**, *italic* and a [link](https://example.org).

- [x] a checklist
- [ ] with two items

| Day | Task |
|---|---:|
| Monday | 1 |

::: box
> A callout in a box.
:::
::: lines 3
::: pagebreak
## Page two
"""

SHEET = {
    "title": "Self-test",
    "notes": ["One line of notes."],
    "sheets": [
        {
            "name": "Data",
            "title": "Numbers",
            "columns": [
                {"title": "Item", "choices": ["A", "B"]},
                {"title": "Amount", "format": "eur"},
                {"title": "Double", "format": "eur", "formula": "=B{row}*2"},
            ],
            "rows": [["A", 1.5], ["B", 2]],
            "empty_rows": 2,
            "totals": {"Amount": "sum"},
            "chart": {"type": "bar", "labels": "Item", "values": "Amount"},
        }
    ],
}


def run() -> list[str]:
    """What was made, one line each; raises on the first failure."""
    with tempfile.TemporaryDirectory() as folder:
        jail = Jail(Path(folder))
        jail.ensure_root()
        jail.write("test.md", DOCUMENT)
        jail.write("test.json", json.dumps(SHEET))
        with netguard.sealed():
            made = [
                make.document(jail, "test.md", "out/test.pdf"),
                make.spreadsheet(jail, "test.json", "out/test.xlsx"),
                make.image(jail, "out/photo.png", "out/test.pdf#1, out/test.pdf#2", "Self-test", "A subtitle", "Badge"),
            ]
            if images.page_count(jail.read_bytes("out/test.pdf")) != 2:
                raise AssertionError("the test document should have two pages")
        return [line for m in made for line in m.report[:1]]


if __name__ == "__main__":
    try:
        for line in run():
            print(line)
    except Exception as exc:  # noqa: BLE001 - report any failure as a failed self-test
        print(f"products self-test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    print("products self-test passed")
