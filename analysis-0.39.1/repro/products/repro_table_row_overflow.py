"""0.39.1 table paging: a row that fits a page alone, but not under the repeated header, is drawn where the cursor is.

pdf.py's Renderer.table: `if height > flow.bottom - flow.top - header_height: warn(...)` sends a row that would fit on
a fresh page (without the header) down the 'taller than a page' branch, which draws it in place, with no page break:
mid-page it runs past the bottom margin and off the page.
"""

from __future__ import annotations

import pypdfium2

from app.products import markup, pdf

HEAD = "---\npage: 5x8\nmargin: 15\ntheme: minimal\n---\n"


def source(words: int, above: int) -> str:
    body = "".join(f"Line {n}\n\n" for n in range(above))
    cell = " ".join("Haushaltsbuchwoche" for _ in range(words))
    return HEAD + body + f"| Posten | Betrag |\n|---|---:|\n| {cell} | 1 |\n"


def table_parts(words: int) -> tuple[float, float, float]:
    """(header height, row height, text area height) in mm, as the renderer measures them."""
    document = markup.parse(source(words, 0))
    renderer = pdf.Renderer(document)
    try:
        theme = renderer.theme
        area = theme.page_height - 2 * theme.margin
        table = document.main[-1]
        _, width = renderer.canvas.main_band()
        look = pdf.Look(theme.text, theme.accent, theme.accent)
        header_only = markup.Table(table.header, [], table.align)
        both = renderer.measure([table], width, look) - renderer.gap
        header = renderer.measure([header_only], width, look) - renderer.gap
        return header, both - header, area
    finally:
        renderer.canvas.close()


def lowest_text(data: bytes) -> list[tuple[int, float]]:
    found = []
    document = pypdfium2.PdfDocument(data)
    try:
        for index in range(len(document)):
            text = document[index].get_textpage()
            bottoms = [text.get_charbox(i)[1] for i in range(text.count_chars())]
            found.append((index + 1, min(bottoms) if bottoms else float("nan")))
    finally:
        document.close()
    return found


# find a row that fits a page alone but not under the header
for words in range(1, 400):
    header, row, area = table_parts(words)
    if area - header < row <= area:
        break
print(f"header {header:.1f} mm, row {row:.1f} mm, text area {area:.1f} mm: the row fits a page alone "
      f"(row <= area) but not under the header (row > area - header)")
for above in (0, 10):
    data, report = pdf.render(markup.parse(source(words, above)))
    print(f"with {above} paragraphs above the table: {report.pages} page(s)")
    for page, bottom in lowest_text(data):
        state = "OFF THE PAGE" if bottom < 0 else "in the bottom margin" if bottom < 15 * 72 / 25.4 else "ok"
        print(f"   page {page}: lowest text {bottom:.0f} pt above the page's foot ({state})")
    print("   warnings:", [w for w in report.warnings if "row" in w])

# the same row as the table's second row, the cursor a third of the way down the page (no label above it)
long_paragraph = "This paragraph runs over a few lines, so it is no label of one line at all. " * 6
cell = " ".join("Haushaltsbuchwoche" for _ in range(words))
second = HEAD + long_paragraph + "\n\n| Posten | Betrag |\n|---|---:|\n| Miete | 1 |\n" + f"| {cell} | 2 |\n"
data, report = pdf.render(markup.parse(second))
print(f"as the second row, below a paragraph: {report.pages} page(s)")
for page, bottom in lowest_text(data):
    state = "OFF THE PAGE" if bottom < 0 else "in the bottom margin" if bottom < 15 * 72 / 25.4 else "ok"
    print(f"   page {page}: lowest text {bottom:.0f} pt above the page's foot ({state})")
print("   warnings:", [w for w in report.warnings if "row" in w])
