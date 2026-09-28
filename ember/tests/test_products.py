"""Ember's products: the Markdown dialect, the PDF, Word, Excel and PNG makers, and the make_ functions the tools call.

The agent writes text; Ember's code makes the files. These tests pin what the agent is promised in its guides (the
settings, the layout lines, the spreadsheet spec) and what a buyer gets: files that open, formulas that only reach
this workbook, and pictures of the right size.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pypdfium2
import pytest
from docx import Document as WordDocument
from docx.oxml.ns import qn
from openpyxl import load_workbook
from PIL import Image

from app.agent import netguard
from app.agent.sandbox import Jail, Limits, SandboxError
from app.products import images, make, markup, pdf, selftest, sheets, word
from app.products.markup import Box, Callout, Checklist, Columns, DocumentError, Heading, ListBlock, Photo, Table

CV = """---
title: CV
theme: modern
sidebar: left
accent: #1F3A5F
footer: Page {page} of {pages}
---
::: sidebar
::: photo 35x45 Your photo
## Contact
Musterstraße 12, München
::: main
# Anna Bergmann
*Accountant*

## Experience
| When | What |
|---|---|
| 2021 - today | **Accountant**, Müller GmbH |

- [x] DATEV
- [ ] SAP

> Replace the sample text with your own.

::: columns 1:2
::: column
Left
::: column
Right
:::
::: lines 3
"""


def pages_of(data: bytes) -> int:
    document = pypdfium2.PdfDocument(data)
    try:
        return len(document)
    finally:
        document.close()


def jail(tmp_path: Path, **limits: int) -> Jail:
    j = Jail(tmp_path / "ws", Limits(**limits) if limits else None)
    j.ensure_root()
    return j


# --- the Markdown dialect -------------------------------------------------------


def test_settings_and_blocks_parse() -> None:
    document = markup.parse(CV)
    s = document.settings
    assert (s.title, s.theme, s.sidebar, s.accent, s.footer) == (
        "CV",
        "modern",
        "left",
        "#1F3A5F",
        "Page {page} of {pages}",
    )
    assert isinstance(document.sidebar[0], Photo) and document.sidebar[0].width_mm == 35
    kinds = [type(b) for b in document.main]
    assert kinds[:3] == [Heading, markup.Paragraph, Heading]
    assert Table in kinds and Checklist in kinds and Callout in kinds and Columns in kinds and markup.Lines in kinds
    checklist = next(b for b in document.main if isinstance(b, Checklist))
    assert [done for done, _ in checklist.items] == [True, False]
    columns = next(b for b in document.main if isinstance(b, Columns))
    assert columns.ratios == [1.0, 2.0] and len(columns.columns) == 2


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("---\ntheme: fancy\n---\nText", "line 2: theme must be one of: modern, classic, minimal, bold"),
        ("---\nsize: 40\n---\nText", "line 2: size must be between 7 and 16"),
        ("---\naccent: red\n---\nText", "line 2: accent must be a colour like #2C3E50"),
        ("---\ncolour: #FFFFFF\n---\nText", "line 2: unknown setting 'colour'"),
        ("---\ntheme: modern\nText", "line 1: the settings block has no closing '---' line"),
        ("::: box\nText", "line 1: '::: box' is never closed; end it with a ':::' line"),
        (":::\n", "line 1: ':::' closes nothing"),
        ("::: sidebar\nText", "there is a '::: sidebar' but no sidebar"),
        ("::: photo big\n", "line 1: write '::: photo 35x45'"),
        ("::: wobble\n", "line 1: unknown layout line '::: wobble'"),
        ("| a | b |\n| c | d |\n", "line 1: a table needs a line like |---|---|"),
        ("---\ntheme: modern\n---\n", "the document is empty"),
    ],
)
def test_mistakes_name_the_line_to_fix(source: str, message: str) -> None:
    with pytest.raises(DocumentError) as error:
        markup.parse(source)
    assert message in str(error.value)


def test_links_are_only_web_and_mail_links() -> None:
    runs = markup.inline(
        "[shop](https://example.org) [x](javascript:alert(1)) [me](mailto:a@b.de) *it* **bo** snake_case"
    )
    links = [(r.text, r.url) for r in runs if r.url]
    assert links == [("shop", "https://example.org"), ("me", "mailto:a@b.de")]
    assert "javascript" in markup.plain(runs)  # shown as text, never a link
    assert any(r.italic and r.text == "it" for r in runs) and any(r.bold and r.text == "bo" for r in runs)
    assert "snake_case" in markup.plain(runs)


def test_control_and_direction_characters_are_dropped() -> None:
    document = markup.parse("# Title\u202e\x07\n\ufeffText")
    assert markup.plain(document.main[0].runs) == "Title"


def test_nested_containers_and_limits() -> None:
    nested = "::: box\n::: center\n::: box #F4EFE6\nDeep\n:::\n:::\n:::\n"
    box = markup.parse(nested).main[0]
    assert isinstance(box, Box) and box.background is None
    too_deep = "::: box\n" * 5 + "x\n" + ":::\n" * 5
    with pytest.raises(DocumentError, match="nested at most 4 deep"):
        markup.parse(too_deep)
    with pytest.raises(DocumentError, match="at most 4 columns"):
        markup.parse("::: columns\n" + "::: column\nx\n" * 5 + ":::\n")


def test_lists_keep_their_kind() -> None:
    blocks = markup.parse("- one\n- two\n\n1. first\n2. second\n").main
    assert [type(b) for b in blocks] == [ListBlock, ListBlock]
    assert [b.ordered for b in blocks] == [False, True]


# --- the PDF --------------------------------------------------------------------


def test_the_pdf_has_its_pages_and_a_report() -> None:
    data, report = pdf.render(markup.parse(CV))
    assert data.startswith(b"%PDF") and pages_of(data) == report.pages == 1
    assert report.sidebar_pages == 1
    assert report.main_end[0] == 1 and 0 < report.main_end[1] < 1
    assert pdf.describe(report)["pages"] == 1


def test_a_pagebreak_starts_a_new_page_and_footers_count_them() -> None:
    source = "---\nfooter: Page {page} of {pages}\n---\n# One\n::: pagebreak\n# Two\n::: pagebreak\n# Three\n"
    data, report = pdf.render(markup.parse(source))
    assert report.pages == pages_of(data) == 3
    document = pypdfium2.PdfDocument(data)
    try:
        texts = [document[i].get_textpage().get_text_range() for i in range(3)]
    finally:
        document.close()
    assert "Page 2 of 3" in texts[1] and "Three" in texts[2]


def test_too_long_documents_are_refused() -> None:
    source = "\n".join(f"# Page {n}\n::: pagebreak" for n in range(pdf.MAX_PAGES + 1))
    with pytest.raises(pdf.TooLong, match=f"longer than {pdf.MAX_PAGES} pages"):
        pdf.render(markup.parse(source))


def test_pages_drawn_on_again_keep_their_fonts() -> None:
    """The sidebar and the footers go back to earlier pages; their text must still be in the right font."""
    lines = "Sidebar line\n\n" * 60
    source = f"---\nfooter: Page {{page}} of {{pages}}\nsidebar: left\n---\n::: sidebar\n{lines}::: main\n# One\n"
    data, report = pdf.render(markup.parse(source + "::: pagebreak\n# Two\n"))
    document = pypdfium2.PdfDocument(data)
    try:
        texts = [document[i].get_textpage().get_text_range() for i in range(len(document))]
    finally:
        document.close()
    assert report.pages == 2
    assert texts[0].count("Sidebar line") > 20 and texts[1].count("Sidebar line") > 5
    assert "Page 1 of 2" in texts[0] and "Page 2 of 2" in texts[1]


def test_hard_to_read_colours_and_missing_glyphs_are_reported() -> None:
    source = "---\ntext: #EEEEEE\nbackground: #FFFFFF\n---\nHello 你好\n"
    _, report = pdf.render(markup.parse(source))
    assert any("hard to read" in w for w in report.warnings)
    assert any("no font has" in w for w in report.warnings)


# --- the Word copy --------------------------------------------------------------


def test_the_word_copy_opens_and_holds_the_text() -> None:
    data = word.render(markup.parse(CV))
    document = WordDocument(io.BytesIO(data))
    words = "".join(node.text or "" for node in document.element.body.iter(qn("w:t")))
    assert "Anna Bergmann" in words and "Contact" in words and "Müller GmbH" in words
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        styles = archive.read("word/styles.xml").decode()
        footer = "".join(archive.read(n).decode() for n in names if n.startswith("word/footer"))
    assert "Calibri" in styles  # Carlito's metrics, under the name Word knows
    assert "NUMPAGES" in footer
    assert not any("vbaProject" in n or "externalLink" in n for n in names)  # never macros or links


def test_every_table_cell_ends_with_a_paragraph() -> None:
    """Word refuses a document whose table cell doesn't end with a paragraph."""
    data = word.render(markup.parse(CV))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml").decode()
    from lxml import etree

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for cell in etree.fromstring(xml.encode()).iter(f"{{{ns['w']}}}tc"):
        assert etree.QName(cell[-1]).localname == "p"


# --- spreadsheets ---------------------------------------------------------------


def spec(**sheet: object) -> str:
    base = {
        "name": "Budget",
        "title": "Monthly budget",
        "columns": [
            {"title": "Category", "choices": ["Food", "Rent"]},
            {"title": "Planned", "format": "eur"},
            {"title": "Actual", "format": "eur"},
            {"title": "Left", "format": "eur", "formula": "=B{row}-C{row}"},
        ],
        "rows": [["Rent", 900, 900], ["Food", 400, 436.5]],
        "empty_rows": 3,
        "totals": {"Planned": "sum", "Left": "sum"},
        "chart": {"type": "bar", "labels": "Category", "values": "Actual"},
    }
    base.update(sheet)
    if "rows_csv" in sheet and "rows" not in sheet:
        del base["rows"]
    return json.dumps({"title": "Budget", "notes": ["Type your amounts."], "sheets": [base]})


def no_csv(path: str) -> str:
    raise AssertionError(f"no CSV expected, got {path}")


def test_a_workbook_has_rows_formulas_totals_and_choices() -> None:
    parsed = sheets.parse(spec(), no_csv)
    book = load_workbook(io.BytesIO(sheets.build(parsed)))
    assert book.sheetnames == ["How to use", "Budget"]
    ws = book["Budget"]
    assert ws["A1"].value == "Monthly budget" and ws["A3"].value == "Category"
    assert [ws.cell(row=4, column=c).value for c in range(1, 5)] == ["Rent", 900, 900, "=B4-C4"]
    assert ws["D6"].value == "=B6-C6" and ws["D8"].value == "=B8-C8"  # the empty rows get the column's formula
    assert ws["B9"].value == "=SUM(B4:B8)" and ws["D9"].value == "=SUM(D4:D8)" and ws["A9"].value == "Total"
    assert ws.freeze_panes == "A4" and ws.auto_filter.ref == "A3:D8"
    rule = ws.data_validations.dataValidation[0]
    assert rule.formula1 == '"Food,Rent"' and str(rule.sqref) == "A4:A8"
    assert len(ws._charts) == 1
    assert book.properties.creator in (None, "")


def test_placeholders_name_the_data_rows() -> None:
    parsed = sheets.parse(spec(rows=[["Rent", 1, 2, "=SUM(B{first}:B{last})"]], empty_rows=0), no_csv)
    assert parsed.sheets[0].rows[0][3] == "=SUM(B4:B4)"
    untitled = sheets.parse(spec(title="", rows=[["Rent", 1, 2]], empty_rows=0), no_csv)
    assert untitled.sheets[0].rows[0][3] == "=B2-C2"  # without a title the data starts on row 2


@pytest.mark.parametrize(
    "formula",
    [
        '=HYPERLINK("https://evil.example","click")',
        '=WEBSERVICE("https://evil.example")',
        "=cmd|' /C calc'!A0",
        "=[1]Sheet1!A1",
        "='Other'!A1",
        "=INDIRECT(A1)",
    ],
)
def test_formulas_only_reach_this_workbook(formula: str) -> None:
    with pytest.raises(sheets.SheetError):
        sheets.parse(spec(rows=[["Rent", 1, 2, formula]]), no_csv)


def test_rows_can_come_from_a_csv_file() -> None:
    parsed = sheets.parse(spec(rows_csv="data/rows.csv"), lambda path: "Rent,900,880\nFood,400,390\n")
    assert parsed.sheets[0].rows[1][:3] == ["Food", 400, 390]
    with pytest.raises(sheets.SheetError, match="rows or rows_csv, not both"):
        sheets.parse(spec(rows_csv="data/rows.csv", rows=[]), lambda path: "")


def test_spec_mistakes_say_where() -> None:
    with pytest.raises(sheets.SheetError, match=r"sheets\[0\].columns\[1\].format must be one of"):
        sheets.parse(spec(columns=[{"title": "A"}, {"title": "B", "format": "money"}]), no_csv)
    with pytest.raises(sheets.SheetError, match="isn't valid JSON"):
        sheets.parse("{", no_csv)
    with pytest.raises(sheets.SheetError, match="unknown key 'colour'"):
        sheets.parse(json.dumps({"sheets": [], "colour": 1}), no_csv)


def test_the_preview_shows_results_not_formulas() -> None:
    parsed = sheets.parse(spec(), no_csv)
    results = sheets._Results(parsed.sheets[0])
    assert results.cell(1, 3) == -36.5 and results.total(3) == -36.5
    picture = Image.open(io.BytesIO(sheets.preview(parsed)))
    assert picture.format == "PNG" and picture.width > 400


# --- pictures -------------------------------------------------------------------


@pytest.mark.parametrize("shape", images.SHAPE_NAMES)
def test_listing_photos_have_the_sizes_etsy_wants(shape: str) -> None:
    page = Image.new("RGB", (600, 850), (255, 255, 255))
    data = images.listing([page, page], "A title that is long enough to wrap", "Subtitle", "Badge", shape=shape)
    picture = Image.open(io.BytesIO(data))
    assert picture.size == images.SHAPES[shape] and picture.format == "PNG"


def test_listing_photos_refuse_what_they_cant_show() -> None:
    page = Image.new("RGB", (60, 85), (255, 255, 255))
    with pytest.raises(images.ImageError, match="shape must be one of"):
        images.listing([page], "T", shape="round")
    with pytest.raises(images.ImageError, match="one to three pages"):
        images.listing([page] * 4, "T")


def test_only_pngs_and_jpegs_are_opened_and_thumbnails_are_small() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1500), (1, 2, 3)).save(buffer, "PNG")
    small, width, height = images.thumbnail(buffer.getvalue(), 1_000)
    assert (width, height) == (1_000, 500) and Image.open(io.BytesIO(small)).size == (1_000, 500)
    jpeg = io.BytesIO()
    Image.new("RGB", (10, 10)).save(jpeg, "JPEG")  # the workshop's photos
    assert images.open_png(jpeg.getvalue()).size == (10, 10)
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    with pytest.raises(images.ImageError):
        images.open_png(gif.getvalue())


# --- make: what the tools call ----------------------------------------------------


def test_a_document_comes_with_a_word_copy_and_page_pictures(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    ws.write("drafts/cv.md", CV)
    with netguard.sealed():
        made = make.document(ws, "drafts/cv.md", "shop/cv.pdf")
    assert made.paths == ["shop/cv.pdf", "shop/cv.docx", "shop/cv-page1.png"]
    text = made.text()
    assert text.startswith("Made shop/cv.pdf: 1 page (A4 portrait)")
    assert "shop/cv.docx (Word, editable" in text and "page pictures shop/cv-page1.png" in text
    assert "The sidebar fills 1 page." in text
    picture = Image.open(io.BytesIO(ws.read_bytes("shop/cv-page1.png")))
    assert picture.size == (827, 1170)  # A4 at 100 dpi


def test_a_shorter_remake_removes_old_page_pictures(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    ws.write("d.md", "# One\n::: pagebreak\n# Two\n::: pagebreak\n# Three\n")
    make.document(ws, "d.md", "shop/d.pdf")
    assert ws.size_of("shop/d-page3.png") is not None
    ws.write("d.md", "# Only one page now\n")
    made = make.document(ws, "d.md", "shop/d.pdf", word_copy=False)
    assert made.removed == ["shop/d-page2.png", "shop/d-page3.png"]
    assert "shop/d.docx is from an earlier version" in made.text()


@pytest.mark.parametrize(
    ("source", "output", "message"),
    [
        ("d.md", "shop/d.docx", "output must be a path ending in .pdf"),
        ("d.json", "shop/d.pdf", "source must be the .md (or .txt) file"),
        ("bad.md", "shop/d.pdf", "bad.md: line 1: unknown layout line"),
    ],
)
def test_document_mistakes_are_product_errors(tmp_path: Path, source: str, output: str, message: str) -> None:
    ws = jail(tmp_path)
    ws.write("d.md", "# Fine\n")
    ws.write("bad.md", "::: wobble\n")
    with pytest.raises(make.ProductError, match=re.escape(message)):
        make.document(ws, source, output)


def test_a_spreadsheet_comes_with_a_picture(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    ws.write("b.json", spec())
    made = make.spreadsheet(ws, "b.json", "shop/budget.xlsx")
    assert made.paths == ["shop/budget.xlsx", "shop/budget-preview.png"]
    assert made.report[0] == "Made shop/budget.xlsx: 1 sheet: Budget (2 rows)."
    assert "2 formulas, a 'How to use' sheet with 1 lines" in made.report[1]
    with pytest.raises(make.ProductError, match=r"b\.json: .*must be one of"):
        ws.write("b.json", spec(columns=[{"title": "A", "format": "money"}]), append=False)
        make.spreadsheet(ws, "b.json", "shop/budget.xlsx")


def test_a_listing_photo_shows_pdf_pages_and_pictures(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    ws.write("d.md", "# One\n::: pagebreak\n# Two\n")
    make.document(ws, "d.md", "shop/d.pdf")
    made = make.image(ws, "shop/photo.png", "shop/d.pdf#2, shop/d-page1.png", "Title", "Sub", "Badge", accent="#2E7D5B")
    assert made.report == [
        "Made shop/photo.png: a landscape listing photo, 3000 x 2250 pixels, " + made.report[0].split(", ")[-1]
    ]
    for pages, message in (
        ("shop/d.pdf#3", "the PDF has 2 page"),
        ("shop/d.docx", "is not a page"),
        ("a.pdf, b.pdf, c.pdf, d.pdf", "1 to 3 pages"),
    ):
        with pytest.raises((make.ProductError, SandboxError), match=message):
            make.image(ws, "shop/photo.png", pages, "Title")
    with pytest.raises(make.ProductError, match="accent must be a colour"):
        make.image(ws, "shop/photo.png", "shop/d.pdf", "Title", accent="green")


def test_products_have_their_own_quota(tmp_path: Path) -> None:
    ws = jail(tmp_path, max_product_total_bytes=40_000)
    ws.write("d.md", "# One\n")
    with pytest.raises(SandboxError, match="products take at most 0 MB"):
        make.document(ws, "d.md", "shop/d.pdf")


def test_the_self_test_makes_one_of_each() -> None:
    lines = selftest.run()
    assert lines[0].startswith("Made out/test.pdf: 2 pages") and lines[1].startswith("Made out/test.xlsx")
    assert lines[2].startswith("Made out/photo.png: a landscape listing photo")
