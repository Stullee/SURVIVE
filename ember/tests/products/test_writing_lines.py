"""Writing lines down to the bottom margin ('::: lines fill'): what they draw in the PDF and the Word copy,
what a book's check says of them, and what the guides teach.

Tests from 0.39.6 and 0.39.8 (formerly test_fixes_0396.py, test_fixes_0398.py)."""

from __future__ import annotations

import io

import pytest
from docx import Document

pytest.importorskip("httpx2")

from pathlib import Path  # noqa: E402

from app.agent import tools  # noqa: E402
from app.products import markup, pdf, word  # noqa: E402
from tests.channels.test_etsy import call  # noqa: E402
from tests.plan.test_cycle_line import lined, working  # noqa: E402
from tests.products.test_fill_in_pages import page_texts  # noqa: E402

# --- From 0.39.6 (formerly test_fixes_0396.py) ---


# --- 2. writing lines down to the bottom margin ---

BOOK = "---\npage: 6x9\nmargin: 16\nsize: 12.5\nline_height: 1.15\ntable: grid\nfooter: Seite {page}\n---\n\n"


def week(rows_: int, notes: str) -> str:
    """A week page of the Haushaltsbuch as v15 set it: a table to fill in, the week's total, then its notes."""
    table = "| Datum | Beschreibung | Kategorie | Betrag |\n|---|---|---|---|\n" + "| | | | |\n" * rows_
    total = "| | Betrag |\n|---|---|\n| Einnahmen | |\n| Ausgaben | |\n| Saldo | |\n"
    return f"## KW1 · 04.01.–10.01.\n\n{table}\n**Bilanz**\n\n{total}\nNotizen:\n{notes}\n"


def lines_drawn(document: markup.Document) -> list[int]:
    return [b.drawn for b in document.main if isinstance(b, markup.Lines)]


def test_lines_fill_rules_the_page_down_to_its_bottom_margin_and_the_word_copy_draws_as_many() -> None:
    fewer = markup.parse(BOOK + week(14, "::: lines fill"))
    data, report = pdf.render(fewer)
    assert len(page_texts(data)) == 1 and report.warnings == []
    more = markup.parse(BOOK + week(10, "::: lines fill"))
    pdf.render(more)
    [few], [many] = lines_drawn(fewer), lines_drawn(more)
    assert few >= markup.FILL_LEAST and many >= few + 4  # four rows fewer: room for about five more lines
    fixed = markup.parse(BOOK + week(10, "::: lines 4"))
    pdf.render(fixed)
    assert lines_drawn(fixed) == [0]  # a count is drawn as it is
    paragraphs = Document(io.BytesIO(word.render(more))).paragraphs  # the Word copy draws the PDF's count
    border = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr"
    assert len([p for p in paragraphs if p._p.pPr is not None and p._p.pPr.find(border) is not None]) == many


def test_lines_fill_belongs_in_the_main_text() -> None:
    for inside in (
        "::: box\n::: lines fill\n:::\n",
        "::: columns\n::: lines fill\n:::\n",
        "::: center\n::: lines fill\n:::\n",
    ):
        with pytest.raises(markup.DocumentError, match="belongs in the main text"):
            markup.parse(inside)
    with pytest.raises(markup.DocumentError, match="'::: lines fill'"):
        markup.parse("::: lines many\n")


def test_a_book_names_writing_lines_that_leave_white_space_and_lines_fill_leaves_none() -> None:
    """Live, request #120: "extend the lines for the notes so there isn't all that white space"."""
    _, short = pdf.render(
        markup.parse(BOOK + week(10, "::: lines 4") + "\n::: pagebreak\n\n" + week(10, "::: lines 4"))
    )
    assert any("the writing lines on pages 1, 2 end with room for 2 or more lines" in w for w in short.warnings)
    assert any("'::: lines fill'" in w for w in short.warnings)
    _, full = pdf.render(
        markup.parse(BOOK + week(10, "::: lines fill") + "\n::: pagebreak\n\n" + week(10, "::: lines fill"))
    )
    assert not any("writing lines on" in w for w in full.warnings)
    _, letter = pdf.render(markup.parse(BOOK.replace("6x9", "A4") + week(10, "::: lines 4")))
    assert not any("writing lines on" in w for w in letter.warnings)  # only a book is checked


def test_lines_fill_that_cant_draw_two_lines_starts_on_the_next_page_and_a_book_says_so() -> None:
    document = markup.parse(BOOK + week(15, "::: lines fill"))
    data, report = pdf.render(document)
    assert len(page_texts(data)) == 2 and "Notizen" in page_texts(data)[1]  # the label goes with its lines
    assert any("runs on past its page from page 1" in w for w in report.warnings)


def test_the_guides_teach_lines_fill() -> None:
    assert "::: lines 8 " in tools.guide_text("documents") and "'fill': down to the bottom margin" in tools.guide_text(
        "documents"
    )
    assert "'::: lines fill'" in tools.guide_text("kdp")


# --- From 0.39.8 (formerly test_fixes_0398.py) ---


# --- 3. the KDP book: what fill drew, and what the owner rejected comes first ---


def test_make_document_says_what_lines_fill_drew_and_that_it_reaches_the_bottom_margin(data_dir: Path) -> None:
    """Live, the agent read 2 lines under a week's table, ruled down to the bottom margin, as 'fill stops short'."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    source = BOOK + week(14, "::: lines fill") + "\n::: pagebreak\n\n" + week(10, "::: lines fill")
    ctx.workspace.write("books/weeks.md", source)
    made = call(ctx, "make_document", {"source": "books/weeks.md", "output": "books/weeks.pdf"})
    assert made.ok, made.text
    _, report = pdf.render(markup.parse(source))
    [(page_one, few), (page_two, many)] = report.fills
    assert (page_one, page_two) == (1, 2) and few == markup.FILL_LEAST and many > few
    assert (
        f"'::: lines fill' drew {few} lines on page 1; {many} lines on page 2: each is ruled down to the bottom margin"
        in made.text
    )
    ctx.workspace.write("books/counted.md", BOOK + week(14, "::: lines 2"))
    counted = call(ctx, "make_document", {"source": "books/counted.md", "output": "books/counted.pdf"})
    assert counted.ok and "'::: lines fill' drew" not in counted.text
