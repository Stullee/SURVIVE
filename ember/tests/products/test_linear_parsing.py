"""0.40.3 (parsing): the products' parsing, checks and prices, from the codebase analysis of 0.40.1.

1. P1: the workshop's PDF check takes linear time on a run of '%' (it took exponential time and froze the app) or of
   digits, and on stream keywords inside one stream (quadratic).
2. PR2: a heading's and a layout line's text, and a paragraph's emphasis, are read in linear time.
3. PR8: U+FFFE, U+FFFF and halves of surrogate pairs are left out of a document, the first line named, and its Word
   copy is made.
4. PR7: a spreadsheet's picture counts COUNTIF's and SUMIF's wildcards as Excel does.
5. PR9: KDP's flat printing cost for black ink ends at 108 pages.
6. 6.2: every regular expression of the products and the integrations takes linear time on long runs (0.42.2: of all
   of app/, in test_regex_timing.py), a street with many house numbers among them.

Tests from 0.40.3 (formerly test_fixes_0403_parsing.py)."""

from __future__ import annotations

import json
import time
import zlib
from decimal import Decimal

import pytest

from app.integrations import kdp
from app.products import checks, markup, pdf, sheets, site, word

# --- 1. P1: the workshop's PDF check ---


def a_pdf(head: bytes) -> bytes:
    body = zlib.compress(b"q Q")
    dictionary = b"<< /Length %d " % len(body) + head + b"\n>>"
    return b"%PDF-1.4\n1 0 obj\n" + dictionary + b"\nstream\n" + body + b"\nendstream\nendobj\n%%EOF\n"


@pytest.mark.parametrize(
    ("head", "message"),
    [
        (b"/Filter\n[" + b"%" * 40 + b"x", "names a stream's encoding in a way Ember can't check"),
        (b"/Subtype /Image /Filter /FlateDecode /Type " + b"%" * 40 + b"x", "can't be opened"),
        (b"/Filter /FlateDecode /DecodeParms << /Predictor " + b"1" * 5_000 + b" >>", "doesn't decode"),
    ],
    ids=["filter", "type", "digits"],
)
def test_a_workshop_pdf_with_40_percent_signs_is_checked_at_once(head: bytes, message: str) -> None:
    """25 '%' after /Filter took 5 s, each one more twice as long: 40 would take days, holding the app. 2,000 digits
    in its decoding parameters took 49 s (cubic)."""
    started = time.perf_counter()
    with pytest.raises(checks.Refused, match=message):
        checks.check("report.pdf", a_pdf(head))
    assert time.perf_counter() - started < 5  # days before; generous for a busy CI runner


def test_a_stream_named_indirectly_is_still_found() -> None:
    for value in (b"5 0 R", b"12 %a comment\n0 R", b"[/FlateDecode 7 0 R]", b"120R"):
        found = checks._FILTER_VALUE.match(value) or checks._PARMS_VALUE.match(b"<< /Predictor " + value + b" >>")
        assert found is not None and checks._REFERENCE.search(found.group(0)), value
    assert not checks._REFERENCE.search(b"<< /Predictor 12 /Columns 3 >>")
    assert checks._FILTER_VALUE.match(b"[ /AHx %one\n /Fl ]").group(0) == b"[ /AHx %one\n /Fl ]"
    assert checks._FILTER_VALUE.match(b"[/Fl %]") is None  # the ']' is in the comment, as pdfium reads it


def test_a_workshop_pdf_of_stream_keywords_without_an_end_is_refused_at_once() -> None:
    """88 KB of them took 2.6 s, and the time grew with the square of the size: a 15 MB file would take hours."""
    started = time.perf_counter()
    for data in (
        b"%PDF-1.4\n" + b"<<>>stream\n" * 1_400_000 + b"endstream\n%%EOF\n",
        b"%PDF-1.4\n" + b"<<>>stream " * 100_000 + b"\n%%EOF\n",
    ):
        with pytest.raises(checks.Refused, match="streams inside streams or without an end"):
            checks.check("report.pdf", data)
    assert time.perf_counter() - started < 20  # hours before; generous for a busy CI runner


# --- 2. PR2: headings and layout lines ---


def test_a_heading_or_layout_line_with_thousands_of_spaces_is_read_at_once() -> None:
    """One heading with 2,000 spaces inside took 12.5 s; a layout line with 16,000 took 1 s."""
    source = "---\ntitle: T\n---\n" + ("# a" + " " * 1_500 + "x ##\n\n") * 40 + "::: space" + " " * 16_000 + "8\n"
    started = time.perf_counter()
    document = markup.parse(source)
    assert time.perf_counter() - started < 5  # 267 s before; generous for a busy CI runner
    headings = [b for b in document.main if isinstance(b, markup.Heading)]
    assert len(headings) == 40 and markup.plain(headings[0].runs) == "a" + " " * 1_500 + "x"
    assert document.main[-1] == markup.Space(8.0)
    rows = markup.parse("## Costs ## \n\n####  Small  #\n\n:::  pagebreak  \n\n#  #\n").main
    assert [markup.plain(b.runs) if isinstance(b, markup.Heading) else b for b in rows] == [
        "Costs",
        "Small",
        markup.PageBreak(),
        "",
    ]


def test_a_paragraph_of_unpaired_emphasis_is_read_at_once() -> None:
    """A closing '_' looked through every open '*' before it: 16,000 of each took 7 s. Timed by how the time grows
    (four times the text: four to six times as long, where it was seventeen), not by a bound in seconds: on CI's
    runner the linear version took 1.05 s once, where it takes 0.3 s here."""

    def took(count: int) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            markup.inline("*a_ " * count)
            best = min(best, time.perf_counter() - started)
        return best

    assert took(16_000) < 10 * took(4_000) + 0.1  # linear: 4 to 6 here; the quadratic version: 17
    runs = markup.inline("*a_ " * 16_000)
    assert markup.plain(runs) == "*a_ " * 16_000
    assert markup.inline("**bold _and_ more** *x") == [
        markup.Run("bold ", bold=True),
        markup.Run("and", bold=True, italic=True),
        markup.Run(" more", bold=True),
        markup.Run(" *x"),
    ]


# --- 3. PR8: what a Word file can't hold ---


def test_characters_a_word_file_cant_hold_are_left_out_and_their_line_named() -> None:
    """U+FFFE in a source made the Word copy fail with "the tool failed (ValueError)", naming no line."""
    source = "---\ntitle: My ￾planner\n---\n# Week\n\nText ￿ here and \ud800 there\n"
    document = markup.parse(source)
    assert document.warnings == ["line 2: U+FFFE is no character a Word file can hold; left out, with 2 more like it"]
    assert document.settings.title == "My planner"
    assert markup.plain(document.main[1].runs) == "Text  here and  there"
    assert word.render(document)[:2] == b"PK"
    assert pdf.render(document)[0].startswith(b"%PDF")
    assert markup.parse("# Fine\n").warnings == []


# --- 4. PR7: COUNTIF's wildcards ---

TRACKER = {
    "sheets": [
        {
            "name": "Tracker",
            "columns": [{"title": "Company"}, {"title": "Status"}, {"title": "Amount"}],
            "rows": [["A", "Applied online", 10], ["B", "applied by mail", 20], ["C", "Interview", 30], ["D", "*", 5]],
        },
        {
            "name": "Counts",
            "columns": [{"title": "What"}, {"title": "Result"}],
            "rows": [
                ["Applied*", '=COUNTIF(Tracker!B:B,"Applied*")'],
                ["SUMIF Applied*", '=SUMIF(Tracker!B:B,"Applied*",Tracker!C:C)'],
                ["?nterview", '=COUNTIF(Tracker!B:B,"?nterview")'],
                ["not *view", '=COUNTIF(Tracker!B2:B5,"<>*view")'],
                ["a star", '=COUNTIF(Tracker!B:B,"~*")'],
                ["a tilde before a letter", '=COUNTIF(Tracker!B:B,"App~lied*")'],
            ],
        },
    ]
}


def test_a_spreadsheet_picture_counts_with_excel_s_wildcards() -> None:
    """The picture showed 0 where COUNTIF(..., "*Miete*") gives 2. A ~ before a letter, which Excel's help doesn't
    define, leaves the formula shown as written."""
    spec = sheets.parse(json.dumps(TRACKER), lambda path: "")
    shown = sheets.values(sheets.build(spec))["Counts"]
    assert [shown[(row, 2)] for row in range(2, 7)] == [2.0, 30.0, 1.0, 3.0, 1.0]
    assert shown[(7, 2)] == '=COUNTIF(Tracker!B:B,"App~lied*")'
    assert sheets._matches("10", "1*") and not sheets._matches(10.0, "1*")  # wildcards match texts only
    assert not sheets._matches(None, "*") and sheets._matches(None, "<>*")
    started = time.perf_counter()
    assert not sheets._like("a" * 20_000, "*aaaaaaaaaab")
    assert time.perf_counter() - started < 5  # generous for a busy CI runner


# --- 5. PR9: KDP's printing cost ---


def test_kdp_s_flat_printing_cost_ends_at_108_pages() -> None:
    assert kdp.printing_cost(108, "white", "6x9") == kdp.printing_cost(108, "cream", "6x9") == Decimal("2.30")
    assert kdp.printing_cost(109, "white", "6x9") == kdp.printing_cost(110, "cream", "6x9") == Decimal("2.32")
    assert kdp.printing_cost(110, "white", "8.5x11") == Decimal("2.87")  # 1.00 + 110 x 0.017 on a large trim
    assert kdp.printing_cost(40, "color", "6x9") == Decimal("3.60")


# --- 6. 6.2: every pattern on long runs (0.42.2: checked in test_regex_timing.py, for all of app/) ---


def test_a_street_with_many_numbers_is_read_at_once() -> None:
    """Found by the patterns' check: a space between two house numbers could end one or begin the next, so each
    further ' -1' doubled the time (20 took 0.5 s; site_address takes 300 characters)."""
    started = time.perf_counter()
    assert not site._STREET.match("Main St 1" + " -1" * 95 + "!")
    assert time.perf_counter() - started < 2  # each ' -1' doubled the time before; generous for a busy CI runner
    assert site._STREET.match("Hauptstraße 5 - 7a") and site._STREET.match("Am Markt 12/3 b")
    assert not site._STREET.match("Postfach") and not site._STREET.match("Main St 1 -")
