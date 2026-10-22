"""The check names a book's page to fill in however many pages it runs on to, and a table whose rows each have
an empty cell is one to fill in; the guides say what a week page holds.

Tests from 0.39.2 (formerly test_fixes_0392.py)."""

from __future__ import annotations

import pytest

pytest.importorskip("httpx2")

from app.agent import tools  # noqa: E402
from app.products import markup, pdf  # noqa: E402

# --- From 0.39.2 (formerly test_fixes_0392.py) ---


# --- 1. a book's pages to fill in ---

HAUSHALTSBUCH = "---\npage: 6x9\nmargin: 16\nsize: 12.5\ntable: lines\nfooter: Seite {{page}}\n{extra}---\n"


TALL = "line_height: 1.9\n"  # v14's


MONTH = """# Januar 2027

## Monatsübersicht

### Einnahmen & Fixkosten

| Position | Betrag |
|---|---|
| Gehalt | |
| Nebeneinkünfte | |
| Sonstige Einnahmen | |
| Miete | |
| Nebenkosten | |
| Versicherungen | |
| Abonnements | |

### Ausgaben nach Kategorie

| Kategorie | Betrag |
|---|---|
| Miete | |
| Nebenkosten | |
| Lebensmittel | |
| Transport | |
| Versicherung | |
| Gesundheit | |
| Abonnements | |
| Freizeit | |
| Sparen | |
| Sonstiges | |

### Monatsziel & Sparquote

| | Betrag |
|---|---|
| Einnahmen gesamt | |
| Ausgaben gesamt | |
| Gespart diesen Monat | |

**Was ich diesen Monat erreichen möchte**
::: lines 2

**Notizen**
::: lines 4
"""


OVERRUN = (
    "a page to fill in (a table to fill in, or writing lines) runs on past its page from page {pages}: in a book fit"
    " each to its page (fewer rows or lines, a smaller size or line_height) or make two full pages of it with a"
    " pagebreak"
)


SPLIT = (
    "a table to fill in runs on to the next page from page {pages}: in a book keep each on its page (fewer rows, a"
    " smaller size or line_height, or a pagebreak before it)"
)


GAP = (  # 0.39.6: writing lines that end a page with white space under them
    "the writing lines on {pages} end with room for 2 or more lines below them: in a book end such a page with"
    " '::: lines fill', which rules it down to its bottom margin"
)


def book(body: str, extra: str = "") -> str:
    """The Haushaltsbuch's settings: a 6x9 paperback at size 12.5, margin 16, its pages numbered."""
    return HAUSHALTSBUCH.format(extra=extra) + body


def week(rows: int, notes: int) -> str:
    """A week as the Haushaltsbuch has it: its log of ``rows`` empty rows, the Bilanz and ``notes`` writing lines."""
    log = "| Datum | Beschreibung | Kategorie | Betrag |\n|---|---|---|---|\n" + "| | | | |\n" * rows
    bilanz = "**Bilanz**\n\n| | Betrag |\n|---|---|\n| Einnahmen | |\n| Ausgaben | |\n| Saldo | |\n"
    return f"## KW1 · 04.01.–10.01.\n\n{log}\n{bilanz}" + (f"\nNotizen:\n::: lines {notes}\n" if notes else "")


def rendered(source: str) -> pdf.Report:
    return pdf.render(markup.parse(source))[1]


def test_a_page_to_fill_in_is_named_however_many_pages_it_runs_on_to() -> None:
    """0.39.1 named a part that ran on to one page more; v14's year plan took three pages and went unnamed."""
    goals = "# Ziele 2027\n\n" + "".join(f"**Ziel {n}:**\n::: lines 2\n\n" for n in range(1, 21))
    report = rendered(book(f"# Haushaltsbuch\n\n::: pagebreak\n{goals}"))
    assert report.pages == 5  # the title, then the goals on four pages
    assert report.warnings == [
        OVERRUN.format(pages=2),
        GAP.format(pages="page 5"),
    ]  # 0.39.6: the last page's white space
    notebook = rendered(book("# Haushaltsbuch\n\n::: pagebreak\n# Notizen\n\n::: lines 60\n"))
    assert notebook.pages == 4 and notebook.warnings == []  # more lines than a page holds: a notebook's pages
    prose = "Ein Kapitel erklärt, wie ein Haushaltsbuch hilft, und es braucht dafür Absätze, die viel länger sind. " * 6
    chapter = f"# Kapitel 1\n\n{(prose + chr(10) + chr(10)) * 8}**Übung:** Was gibst du jede Woche aus?\n::: lines 5\n"
    workbook = rendered(book(f"# Ratgeber\n\n::: pagebreak\n{chapter}"))
    assert workbook.pages > 3 and workbook.warnings == []  # a chapter's prose runs on, its exercise with it
    a4 = rendered(book(f"# Haushaltsbuch\n\n::: pagebreak\n{goals}").replace("page: 6x9", "page: A4"))
    assert not any("to fill in" in w for w in a4.warnings)  # a printable's pages are the buyer's to print


def test_a_table_whose_rows_each_have_an_empty_cell_is_one_to_fill_in() -> None:
    """A month's overview: a label and its amount to fill in in each row ("Gehalt | "), no row empty."""

    def table(rows: str) -> markup.Table:
        [block] = markup.parse("| Posten | Betrag |\n|---|---|\n" + rows).main
        assert isinstance(block, markup.Table)
        return block

    assert pdf._to_fill(table("| Gehalt | |\n| Miete | |\n"))
    assert pdf._to_fill(table("| Gehalt | 2.000 |\n| | |\n"))  # 0.39.1's: a row with no words
    assert not pdf._to_fill(table("| Gehalt | 2.000 |\n| Miete | |\n"))  # a worded table with a gap
    assert not pdf._to_fill(table("| Gehalt | 2.000 |\n| Miete | 800 |\n"))
    labels = "".join(f"| Posten {n} | |\n" for n in range(1, 21))
    above = "".join(f"Zeile {n}\n\n" for n in range(12))
    report = rendered(book(f"# Übersicht\n\n{above}| Posten | Betrag |\n|---|---|\n{labels}"))
    assert report.pages == 2 and report.warnings == [SPLIT.format(pages=1)]
    worded = "".join(f"| Posten {n} | {n} EUR |\n" for n in range(1, 21))
    words = rendered(book(f"# Übersicht\n\n{above}| Posten | Betrag |\n|---|---|\n{worded}"))
    assert words.pages == 2 and not any("to fill in" in w for w in words.warnings)  # a table to read breaks freely


def test_the_haushaltsbuch_v14_month_is_named_and_a_month_that_fits_is_not() -> None:
    """Live, v14 (line height 1.9) made each month's overview three pages, the third holding "Notizen" alone, and
    make_document said nothing. Two pages split by a pagebreak, at the normal line height, fit."""
    v14 = rendered(book(MONTH, TALL))
    assert v14.pages == 3 and v14.warnings == [SPLIT.format(pages=1), GAP.format(pages="page 3")]  # its "Ausgaben nach
    # Kategorie" breaks; 0.39.6: and its third page, "Notizen" alone, is mostly white space
    head, rest = MONTH.split("### Ausgaben nach Kategorie")
    spending, goals = rest.split("### Monatsziel & Sparquote")
    goals, notes = goals.split("**Notizen**")
    first = f"{head}### Monatsziel & Sparquote{goals}::: pagebreak\n\n"
    fits_source = f"{first}### Ausgaben nach Kategorie{spending}**Notizen**{notes}"
    fits = rendered(book(fits_source))
    assert fits.pages == 2 and fits.warnings == [GAP.format(pages="pages 1, 2")]  # 0.39.6: white space under notes
    filled = fits_source.replace("::: lines 4", "::: lines fill").replace("::: lines 2", "::: lines fill")
    assert rendered(book(filled)).warnings == []  # each page's notes ruled down to its bottom margin


def test_the_guides_say_what_a_week_page_holds_and_the_renderer_agrees() -> None:
    kdp = " ".join(tools.guide_text("kdp").split())
    assert (
        "a 6x9 page at size 12.5 holds about 20 empty rows, 10 with a heading, a 3-row total and 4 writing lines."
    ) in kdp
    documents = " ".join(tools.guide_text("documents").split())
    assert "A row with no words is one line tall (7 to 8 mm; more at a larger line_height)" in documents  # 0.40.2
    assert rendered(book(week(10, 4))).pages == 1
    assert OVERRUN.format(pages=1) in rendered(book(week(11, 4))).warnings
    assert rendered(book(week(15, 0))).pages == 1 and rendered(book(week(16, 0))).pages == 2
    assert rendered(book(week(6, 4), TALL)).pages == 1 and rendered(book(week(7, 4), TALL)).pages == 2  # v14's 1.9
    assert rendered(book(week(9, 0), TALL)).pages == 1  # v14's week: 9 rows, its notes dropped
