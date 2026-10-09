"""make_document edge cases with the real code: German text, very long words, an empty table, 41 pages, a tall table
row, every layout line; then the PDF's fonts, the Word copy's fonts and tables."""

from __future__ import annotations

import io
import re
import sys
import zipfile

sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail  # noqa: E402

from app.products import images, make  # noqa: E402

GERMAN = """---
title: Wochenplaner für Familien
theme: classic
page: A4
sidebar: right
sidebar_width: 55
footer: Seite {page} von {pages} · Mit KI-Unterstützung erstellt
table: grid
---
::: sidebar
## Kontakt
Straße 5, 80331 München
::: photo 35x45 Foto
- [ ] Einkäufe
- [x] Müll rausbringen
::: main
# Größenverhältnisse und Übersicht
Donaudampfschifffahrtselektrizitätenhauptbetriebswerkbauunterbeamtengesellschaft ist ein sehr langes Wort,
ebenso https://example.org/ein/sehr/langer/pfad/der/nicht/umbricht/und/weiter/geht/bis/zum/ende/des/textes.

| Tag | Aufgabe | Erledigt |
|---|:---:|---:|
| Montag | Wäsche | ☐ |
| Dienstag | Einkaufen | ✓ |

| | |
|---|---|

| Leer | Tabelle |
|---|---|

::: columns 1:2
::: column
**Links:** „Anführungszeichen“ und ‚einfache‘ – Gedankenstrich … Ellipse
::: column
> Ein Hinweis mit ß, Ä, Ö, Ü und €-Beträgen: 1.234,56 €
:::
::: box #F4EFE6
### Notizen
::: lines 4
:::
::: center
Zentrierter Text
:::
"""


def doc_pages(n: int, page: str = "A4") -> str:
    return f"---\npage: {page}\n---\n# Long\n\n" + "Text.\n\n::: pagebreak\n" * (n - 1) + "End.\n"


TALL_ROW = "---\npage: A4\n---\n# Tall\n\n" + "Intro paragraph.\n\n" * 30 + "| A | B |\n|---|---|\n| " + (
    "word " * 900) + "| x |\n"


def pdf_fonts(data: bytes) -> list[str]:
    names = sorted(set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9+\-_]+)", data)))
    embedded = len(re.findall(rb"/FontFile2", data))
    return [n.decode() for n in names] + [f"FontFile2 x{embedded}"]


def main() -> None:
    jail = fresh_jail("docs")
    jail.write("d/german.md", GERMAN)
    made = make.document(jail, "d/german.md", "shop/wochenplaner.pdf")
    print("\n".join(made.report))
    pdf = jail.read_bytes("shop/wochenplaner.pdf")
    print("PDF fonts:", pdf_fonts(pdf))
    docx = jail.read_bytes("shop/wochenplaner.docx")
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        doc = z.read("word/document.xml").decode()
        styles = z.read("word/styles.xml").decode()
    fonts = sorted(set(re.findall(r'w:ascii="([^"]+)"', doc + styles)))
    print("Word fonts named:", fonts)
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", doc, re.S)
    print("Word tables:", len(re.findall(r"<w:tbl>", doc)), "rows per top-level <w:tbl> (approx):",
          [t.count("<w:tr") for t in tables])
    empty = [m.start() for m in re.finditer(r"<w:tblGrid>(?:(?!</w:tbl>).)*?</w:tblGrid></w:tbl>", doc, re.S)]
    print("tables with a grid and no row:", len(empty))
    for n in (40, 41):
        jail.write(f"d/p{n}.md", doc_pages(n))
        try:
            m = make.document(jail, f"d/p{n}.md", f"shop/p{n}.pdf", word_copy=False, previews=False)
            print(n, "->", m.report[0])
        except make.ProductError as exc:
            print(n, "-> REFUSED:", exc)
    jail.write("d/tall.md", TALL_ROW)
    m = make.document(jail, "d/tall.md", "shop/tall.pdf", word_copy=False)
    print("tall row:", m.text())
    print("page count", images.page_count(jail.read_bytes("shop/tall.pdf")))


if __name__ == "__main__":
    main()
