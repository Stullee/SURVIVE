"""A book's pages to fill in: a table's empty rows drawn as Word draws them, a header kept with its first row,
and the check naming each page to fill in that runs on to the next.

Tests from 0.39.1 (formerly test_fixes_0391.py)."""

from __future__ import annotations

from pathlib import Path

import pypdfium2
import pytest

pytest.importorskip("httpx2")

from app.agent import tools  # noqa: E402
from app.agent.sandbox import Jail  # noqa: E402
from app.products import make, markup, pdf  # noqa: E402
from tests.etsy_helpers import call  # noqa: E402
from tests.plan.test_cycle_line import lined, working  # noqa: E402

# --- From 0.39.1 (formerly test_fixes_0391.py) ---


# --- 1. a book's pages to fill in ---

BOOK = "---\npage: 6x9\nmargin: {margin}\nsize: 12.5\ntheme: minimal\n---\n"


LONG = "This paragraph runs over a few lines, so it is no label of one line at all, and it goes on and on. " * 3


def book(body: str, margin: int = 12) -> str:
    """A 6x9 paperback's interior at size 12.5, as the Haushaltsbuch was set."""
    return BOOK.format(margin=margin) + body


def week(count: int, cell: str = " ") -> str:
    """A week's table to fill in: its header and ``count`` rows, empty (or with ``cell`` in each cell)."""
    return "| Tag | Ausgabe | Betrag |\n|---|---|---:|\n" + f"| {cell} | {cell} | {cell} |\n" * count


def page_texts(data: bytes) -> list[str]:
    document = pypdfium2.PdfDocument(data)
    try:
        return [document[i].get_textpage().get_text_range() for i in range(len(document))]
    finally:
        document.close()


def page_of(texts: list[str], words: str) -> int:
    return next(number for number, text in enumerate(texts, 1) if words in text)


def test_a_row_with_no_words_is_one_line_tall_as_word_draws_it() -> None:
    """Live, each of the weekly table's 18 empty rows was 2.2 mm: 18 rows took less room than 4 rows with words."""
    _, empty = pdf.render(markup.parse(book(week(10))))
    _, worded = pdf.render(markup.parse(book(week(10, "x"))))
    assert empty.main_end == worded.main_end
    _, page = pdf.render(markup.parse(book("## Woche 1\n\n" + week(20))))  # the guides: about 20 rows to a 6x9 page
    assert page.pages == 1 and page.main_end[1] > 0.85
    _, more = pdf.render(markup.parse(book("## Woche 1\n\n" + week(24))))
    assert more.pages == 2


@pytest.mark.parametrize(("margin", "lines"), [(10, 19), (13, 18), (14, 17)])
def test_a_tables_header_stays_with_its_first_row(margin: int, lines: int) -> None:
    """Pages where 0.38.1 left a table's header alone at the foot, its rows on the next page."""
    above = "".join(f"Line {n}\n\n" for n in range(lines)) + LONG
    table = "\n\n| Datum | Betrag |\n|---|---:|\n| Erste | 1 |\n| Zweite | 2 |\n"
    data, _ = pdf.render(markup.parse(book(above + table, margin)))
    texts = page_texts(data)
    assert page_of(texts, "Datum") == page_of(texts, "Erste")


@pytest.mark.parametrize("margin", [13, 14, 15])
def test_a_one_line_label_moves_to_the_next_page_with_its_table(margin: int) -> None:
    """Live, a week's **Bilanz** stood at the foot of a page, its table on the next."""
    above = "".join(f"Line {n}\n\n" for n in range(21))
    table = "**Bilanz**\n\n| Posten | Betrag |\n|---|---:|\n| Einnahmen |  |\n| Ausgaben |  |\n"
    data, _ = pdf.render(markup.parse(book(above + table, margin)))
    texts = page_texts(data)
    assert page_of(texts, "Bilanz") == page_of(texts, "Posten") == 2


def test_a_book_says_which_pages_to_fill_in_run_on_and_other_documents_dont() -> None:
    """A week of 12 rows fits its page; one of 18 pushes its notes on to the next; one of 30 breaks its table."""
    weeks = "".join(
        f"::: pagebreak\n## Woche {n}\n\n{week(count)}\nNotizen:\n\n::: lines 4\n"
        for n, count in ((1, 12), (2, 18), (3, 30))
    )
    _, report = pdf.render(markup.parse(book("# Haushaltsbuch\n\n" + weeks)))
    assert report.pages == 6  # the title, week 1, week 2 and its notes, week 3 on two pages
    assert (  # 0.39.2's words (the pages are 0.39.1's)
        "a page to fill in (a table to fill in, or writing lines) runs on past its page from page 3: in a book fit"
        " each to its page (fewer rows or lines, a smaller size or line_height) or make two full pages of it with a"
        " pagebreak"
    ) in report.warnings
    assert (
        "a table to fill in runs on to the next page from page 5: in a book keep each on its page (fewer rows, a"
        " smaller size or line_height, or a pagebreak before it)"
    ) in report.warnings
    _, a4 = pdf.render(markup.parse(book("# Haushaltsbuch\n\n" + weeks).replace("page: 6x9", "page: A4")))
    assert not any("to fill in" in w for w in a4.warnings)  # a printable's pages are the buyer's to print
    guide = "".join(
        f"::: pagebreak\n## Kapitel {n}\n\n{LONG * 14}\n\n| Wort | Bedeutung |\n|---|---|\n| a | b |\n" for n in (1, 2)
    )
    _, words = pdf.render(markup.parse(book(guide)))
    assert words.pages > 3 and not any("to fill in" in w for w in words.warnings)  # a guide's chapters run on


def test_make_document_pictures_the_pages_named(tmp_path: Path) -> None:
    """Live, the book's weekly pages began on page 6, and its pictures showed pages 1 to 4 only."""
    jail = Jail(tmp_path)
    weeks = "".join(f"::: pagebreak\n## Woche {n}\n\n{week(12)}" for n in range(1, 7))
    jail.write("book.md", book("# Haushaltsbuch\n\n" + weeks))
    first = make.document(jail, "book.md", "book.pdf", word_copy=False)
    assert first.paths == ["book.pdf", "book-page1.png", "book-page2.png", "book-page3.png", "book-page4.png"]
    chosen = make.document(jail, "book.md", "book.pdf", word_copy=False, pages="6, 3; 9")
    assert chosen.paths == ["book.pdf", "book-page3.png", "book-page6.png"]
    assert (
        "Also: page pictures book-page3.png, book-page6.png (pages 3, 6 of 7); no page 9: the document has 7 pages."
    ) in chosen.report
    assert chosen.removed == ["book-page1.png", "book-page2.png", "book-page4.png"]  # they would show an old version
    for wrong, said in (("six", "pages names page numbers"), ("1 2 3 4 5", f"at most {make.PAGE_PREVIEWS} pages")):
        with pytest.raises(make.ProductError, match=said):
            make.document(jail, "book.md", "book.pdf", word_copy=False, pages=wrong)


def test_the_make_document_tool_takes_the_pages(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    ctx.workspace.write("drafts/book.md", book("# Haushaltsbuch\n\n" + f"::: pagebreak\n## Woche\n\n{week(12)}" * 5))
    made = call(ctx, "make_document", {"source": "drafts/book.md", "output": "books/book.pdf", "pages": "5"})
    assert made.ok, made.text
    assert "page pictures books/book-page5.png (page 5 of 6)" in made.text
    assert "pages" in tools.SPECS["make_document"].fields
