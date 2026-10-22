"""0.40.0, upgrade request #12: a KDP paperback's pages drawn fresh and checked before every proposal.

The owner rejected the Haushaltsbuch's interior again and again, and twice the agent had trusted pictures of an
earlier version or a checklist it had passed days before ("KDP v13 already passed its checklist… resubmit directly,
not re-render", analysis-0.39.6, 3.2). What worked was a workshop script, workshop/scripts/render-page-3-of-71.py:
one page drawn fresh with pypdfium2 at 600 pixels wide, about $0.19 a run, then checked against the owner's words.
Now propose_kdp_book's check draws one page of each kind itself, free, measures every page (rows to write in, white
space, the calendar, the AI line) and lists what to check with the owner's last words; a paperback is proposed only in
a later reply of the cycle that checked that very file, with qa, which the owner reads on the request.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fpdf import FPDF
from PIL import Image

from app.agent import loop, tools
from app.agent.sandbox import Jail
from app.integrations import kdp
from app.products import make, markup, pagecheck, pdf
from tests.channels.test_etsy import call
from tests.cycle.test_agent import rows
from tests.owner.test_owner_loop import owner
from tests.products.test_kdp import front, next_reply, project, publishing

SETTINGS = "---\npage: 6x9\nmargin: 16\nsize: 12.5\nline_height: 1.15\ntable: grid\nfooter: Seite {page}\n---\n\n"
AI_LINE = "Dieses Buch wurde mit Hilfe von KI erstellt und vor der Veröffentlichung geprüft."
QA = (
    "Seite 1 Titel, Seite 2 KI-Hinweis mit Text. Wochen: 14 leere Zeilen von 7,3 mm, Notizlinien bis zum unteren "
    "Rand, keine Seite leer. KW 1 bis 21 sind 04.01. bis 30.05.2027, Montag bis Sonntag."
)


def week(number: int, monday: date, notes: str) -> str:
    """A week page of the Haushaltsbuch as v15 set it: a table to fill in, the week's total, then its notes."""
    table = "| Datum | Beschreibung | Kategorie | Betrag |\n|---|---|---|---|\n" + "| | | | |\n" * 14
    total = "| | Betrag |\n|---|---|\n| Einnahmen | |\n| Ausgaben | |\n| Saldo | |\n"
    sunday = monday + timedelta(days=6)
    heading = f"## KW{number} · {monday:%d.%m.}–{sunday:%d.%m.}"
    return f"{heading}\n\n{table}\n**Bilanz**\n\n{total}\nNotizen:\n{notes}\n"


def month(name: str) -> str:
    rows_ = "".join(f"| {c} | |\n" for c in ("Miete", "Strom", "Lebensmittel", "Versicherung", "Freizeit", "Sparen"))
    return f"# {name} 2027\n\n| Kategorie | Betrag |\n|---|---|\n{rows_}\nNotizen:\n::: lines fill\n"


def haushaltsbuch(
    weeks: int = 3, first: date = date(2027, 1, 4), notes: str = "::: lines fill", end: bool = False
) -> str:
    """The Haushaltsbuch 2027 in small: a title page, the AI page (or the AI line at the end), January and its weeks."""
    pages = ["# Haushaltsbuch 2027\n\nDein Jahr im Überblick\n", *([] if end else [AI_LINE]), month("Januar")]
    pages += [week(n, first + timedelta(days=7 * (n - 1)), notes) for n in range(1, weeks + 1)]
    return SETTINGS + "\n::: pagebreak\n\n".join([*pages, *([AI_LINE] if end else [])])


def rendered(source: str) -> bytes:
    data, _ = pdf.render(markup.parse(source))
    return data


# --- what Ember's code measures ---


def test_a_book_as_the_owner_asked_for_it_is_measured_page_by_page() -> None:
    checked = pagecheck.check(rendered(haushaltsbuch()), language="German")
    assert len(checked.pages) == 6  # title, AI page, January, three weeks
    assert checked.kinds == [[1, 2], [3], [4, 5, 6]]  # no ruled lines; the month's table; the weeks' tables
    assert checked.shown == [1, 2, 3, 4, 6]  # the first of each kind, the AI line's page and the last page
    for picture in checked.pictures:  # 600 pixels wide, as the workshop script drew them: 6x9 is 600 x 900
        assert Image.open(io.BytesIO(picture)).size == (pagecheck.PICTURE_WIDTH, 900)
    height, count = checked.pages[3].rows
    assert 7 <= height <= 7.6 and count == 14  # a week's 14 empty rows, each one line tall
    assert checked.calendar.findings == [] and checked.calendar.weeks == [1, 2, 3] and checked.calendar.year == 2027
    assert checked.ai_pages == [2] and checked.findings() == []
    said = "\n".join(checked.measures())
    assert "Kinds of page (by their ruled lines): A 1-2 (no ruled lines); B 3 (" in said
    assert "Rows to write in: the lowest 7." in said and "too low" not in said
    # the title, the AI page
    assert "White space over a quarter of a page between what it prints or under it: pages 2 (" in said
    assert "Calendar: 3 weeks (1-3), each its calendar week; 6 dates, each real, each weekday its date's." in said
    assert "AI line: page 2; page 2 has 15 words: little else." in said


def test_the_book_the_owner_rejected_shows_its_spilled_lines_shifted_weeks_and_lone_ai_line() -> None:
    """v15 with four note lines (two ran onto a page of their own every week), weeks counted from 1 January, and the
    AI line alone on the last page: what the owner rejected (#120) and analysis-0.39.6 found."""
    checked = pagecheck.check(rendered(haushaltsbuch(first=date(2027, 1, 1), notes="::: lines 4", end=True)))
    spilled = [p.number for p in checked.pages if len(p.rules) == 2]  # the lines run on: two on a page of their own
    assert spilled and spilled[0] in checked.shown  # a kind of its own, drawn
    assert checked.shown[-1] == len(checked.pages)  # the last page, with the AI line alone
    said = "\n".join(checked.measures())
    assert all(f"{n} (" in said or f"{n}" in said.split("also ")[-1] for n in spilled)  # named as white space
    week_one = "page 3: KW 1 (01.01.–07.01.) isn't week 1: week 1 of 2027 is Mon 2027-01-04 to Sun 2027-01-10"
    assert checked.findings()[0] == week_one  # for the owner's card too
    assert (
        f"AI line: page {len(checked.pages)}; page {len(checked.pages)}, the last page, has 15 words: little else."
        in said
    )


def ruled(gap: float, lines: int = 10, words: bool = False) -> bytes:
    """A 6x9 page of ruled lines ``gap`` mm apart (with a word in each row)."""
    document = FPDF(unit="mm", format=(152.4, 228.6))
    document.add_page()
    document.set_font("helvetica", size=8)
    document.set_line_width(0.2)
    for n in range(lines):
        document.line(16, 30 + n * gap, 136, 30 + n * gap)
        if words and n:
            document.text(20, 30 + n * gap - 1, "Miete")
    return bytes(document.output())


def test_rows_too_low_to_write_in_are_named_and_rows_with_words_are_none() -> None:
    low = pagecheck.check(ruled(2.2))  # 0.39.1's empty rows: 2.2 mm
    assert low.pages[0].rows is not None and 2.0 <= low.pages[0].rows[0] <= 2.4 and low.pages[0].rows[1] == 9
    assert "under 5.0 mm, too low to write in, on pages 1." in " ".join(low.measures())
    lined = pagecheck.check(ruled(8.0))
    assert 7.8 <= lined.pages[0].rows[0] <= 8.2 and "too low" not in " ".join(lined.measures())
    assert pagecheck.check(ruled(8.0, words=True)).pages[0].rows is None  # a row with a word in it is no empty row


# --- the calendar ---


@pytest.mark.parametrize(
    ("texts", "language", "found"),
    [
        (["Haushaltsbuch 2027", "KW 53 · 28.12.–03.01.", "KW 1 · 04.01.–10.01."], "German", []),  # across the year
        (["Planer 2027\nMo 01.01.2027"], "German", ["page 1: 01.01.2027 is a Friday, not a Monday"]),
        (["Planer 2027\nFr 01.01.2027\nSa 02.01.\nMo–So 04.01.–10.01."], "German", []),  # Mo–So: a span, no weekday
        (
            ["2027\n31.02.2027 und 29.02."],
            "German",
            ["page 1: 31.02.2027 doesn't exist", "page 1: 29.02. doesn't exist in 2027"],
        ),
        (["2027\nPreis 1.462,50 € und 3.99. Version 0.39.8"], "German", []),  # prices and versions are no dates
        (  # a week's days one by one (0.40.3: all 7 of them; Monday to Wednesday alone is named)
            ["2027\nKW 2\nMo 11.01.\nDi 12.01.\nMi 13.01.\nDo 14.01.\nFr 15.01.\nSa 16.01.\nSo 17.01."],
            "German",
            [],
        ),
        (
            ["2027\nKW 2\nMo 04.01.\nDi 05.01."],
            "German",
            ["page 1: KW 2 (04.01.–05.01.) isn't week 2: week 2 of 2027 is Mon 2027-01-11 to Sun 2027-01-17"],
        ),
        (["2027\nWoche: 01.01.–07.01."], "German", ["page 1: the week 01.01.–07.01. runs Friday to Thursday"]),
        (["2027 Planner\nWeek 1: December 27 – January 2"], "English", []),  # a US week holding 1 January
        (["2027 Planner\nWeek 1: January 3 – January 9, 2027"], "English", []),  # from the year's first Sunday
        (["2027 Planner\nWeek 1: Monday, January 4 - Sunday, January 10"], "English", []),  # ISO's
        (  # each day in a week 1 of its own way of counting is no week 1
            ["2027 Planner\nWeek 1: January 1 - January 7"],
            "English",
            ["page 1: Week 1 (January 1–January 7) isn't week 1: week 1 of 2027 is Mon 2027-01-04 to Sun 2027-01-10"],
        ),
        (
            ["2027\nMontag, 4. Januar 2027 / Dienstag, 4. Januar 2027"],
            "German",
            ["page 1: 4. Januar 2027 is a Monday, not a Tuesday"],
        ),
    ],
)
def test_the_calendar_is_read_from_the_pages_text(texts: list[str], language: str, found: list[str]) -> None:
    assert pagecheck.read_calendar(texts, language).findings == found


def test_a_week_missing_between_the_first_and_the_last_is_named() -> None:
    weeks = ["2027", "KW 1 04.01.–10.01.", "KW 2 11.01.–17.01.", "KW 4 25.01.–31.01.", "KW 5 01.02.–07.02."]
    assert pagecheck.read_calendar(weeks).findings == ["no page for week 3 (weeks 1 to 5 are there)"]


def test_the_ai_line_is_found_in_the_book_s_language() -> None:
    def found(text: str) -> bool:
        return bool(pagecheck._AI.search(text))

    assert found(AI_LINE) and found("Made with the help of AI.") and found("created with artificial intelligence")
    assert found("mit künstlicher Intelligenz erstellt") and not found("Kiste") and not found("Ski fahren ki")


def test_pages_names_page_numbers() -> None:
    assert pagecheck.named("6, 83 6") == [6, 83] and pagecheck.named("") == []
    with pytest.raises(ValueError, match="pages names page numbers"):
        pagecheck.named("six")
    with pytest.raises(ValueError, match=f"at most {pagecheck.PAGES_SHOWN} pages"):
        pagecheck.named("1 2 3 4 5 6 7")
    with pytest.raises(ValueError, match="the interior has 6 pages, no page 9"):
        pagecheck.check(rendered(haushaltsbuch()), [2, 9])
    assert pagecheck.check(rendered(haushaltsbuch()), [5, 2]).shown == [2, 5]


# --- the check and the proposal ---


def book(jail: Jail, source: str, path: str = "books/hb", project_id: int | None = None) -> str:
    """The Haushaltsbuch's interior made with make_document, its front and its spec: the spec's path."""
    jail.write(f"{path}.md", source)
    make.document(jail, f"{path}.md", f"{path}.pdf", word_copy=False, previews=False)
    front(jail)
    data = {
        "format": "paperback",
        "title": "Haushaltsbuch 2027",
        "description": "Ein Haushaltsbuch für 2027: jede Woche eine Seite, jeder Monat ein Überblick.",
        "keywords": ["haushaltsbuch 2027", "budget planer deutsch"],
        "categories": ["Business & Money > Personal Finance > Budgeting & Money Management"],
        "language": "German",
        "price": "8.99",
        "manuscript": f"{path}.pdf",
        "paper": "white",
        "low_content": True,
        "cover": {"front": "books/front.png", "back": "Dein Jahr im Überblick."},
        "reason": "Haushaltsbücher für 2027 werden ab November gesucht.",
        **({"project_id": project_id} if project_id else {}),
    }
    jail.write(f"{path}.json", json.dumps(data, ensure_ascii=False))
    return f"{path}.json"


def check(ctx: tools.ToolContext, path: str, **extra: Any) -> tools.Outcome:
    checked = call(ctx, "propose_kdp_book", {"spec": path, "check": True, **extra})
    assert checked.ok, checked.text
    return checked


def propose(ctx: tools.ToolContext, path: str, qa: str = QA) -> tools.Outcome:
    return call(ctx, "propose_kdp_book", {"spec": path, "qa": qa})


def test_a_paperback_is_proposed_only_after_a_check_drew_its_pages_in_an_earlier_reply(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    path = book(ctx.workspace, haushaltsbuch(weeks=21), "books/hb", project(agent))  # 24 pages: KDP's least
    early = propose(ctx, path)
    assert (
        not early.ok
        and "check its pages first: propose_kdp_book with check true draws books/hb.pdf's pages" in early.text
    )
    assert "Pictures, notes or a check from before are not this file's pages" in early.text
    checked = check(ctx, path)
    assert len(checked.pictures) == 5 and len(checked.text) < tools.MAX_RESULT_CHARS
    assert "Its pages, drawn now from books/hb.pdf as it is (24 pages): 1, 2, 3, 4, 24, shown below." in checked.text
    assert "Calendar: 21 weeks (1-21), each its calendar week" in checked.text
    assert "1. White space: no page your owner would call empty" in checked.text
    assert (
        "5. What your owner asked of it in their messages: each point done?" in checked.text
    )  # no words of theirs yet
    assert "Propose it with check false in a later reply of this cycle, with qa" in checked.text
    same = propose(ctx, path)  # in the reply that asked for the check: its pages hadn't reached the model
    assert not same.ok and "you checked books/hb.pdf in this same reply, before its pages reached you" in same.text
    next_reply(ctx)
    thin = propose(ctx, path, "Looks good, all fixed.")
    assert not thin.ok and f"point by point of the check's list (at least {tools.QA_WORDS} words)" in thin.text
    # 0.42.0: a longer qa is cut, not refused (test_tool_limits.py)
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'kdp_package'")[0]["n"] == 0
    other = check(ctx, path, pages="10")  # another page of the same file: the pages seen add up
    assert len(other.pictures) == 1 and "(24 pages): 10, shown below." in other.text
    next_reply(ctx)
    made = propose(ctx, path)
    assert made.ok and "Approval request #" in made.text, made.text
    [row] = rows(agent, "SELECT description FROM approvals WHERE executor = 'kdp_package'")
    cycle = ctx.cycle_id
    assert row["description"] == (
        "Haushaltsbücher für 2027 werden ab November gesucht.\n\n"
        f"Page check (pages 1, 2, 3, 4, 10, 24 of 24, drawn fresh in cycle #{cycle}): {QA}\n\n{tools.KDP_NOTE}"
    )


def test_a_changed_file_or_a_check_of_another_cycle_is_no_check(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    path = book(ctx.workspace, haushaltsbuch(weeks=21))
    check(ctx, path)
    next_reply(ctx)
    book(ctx.workspace, haushaltsbuch(weeks=22))  # v2: one more week
    changed = propose(ctx, path)
    assert (
        not changed.ok
        and "books/hb.pdf changed since this cycle's check drew its pages: check it again" in changed.text
    )
    check(ctx, path)
    ctx.state = tools.CycleTools()  # the next cycle: last cycle's check is a check from before
    next_reply(ctx)
    later = propose(ctx, path)
    assert not later.ok and "check its pages first" in later.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'kdp_package'")[0]["n"] == 0


def test_the_owner_reads_what_ember_s_code_found_and_the_check_quotes_their_last_words(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    rejected = haushaltsbuch(weeks=21, first=date(2027, 1, 1), notes="::: lines 4", end=True)
    path = book(ctx.workspace, rejected, "books/hb", project(agent))
    check(ctx, path)
    next_reply(ctx)
    assert propose(ctx, path).ok
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'kdp_package'")[0]["id"]
    description = rows(agent, f"SELECT description FROM approvals WHERE id = {request}")[0]["description"]
    assert (
        "Ember's code found: page 3: KW 1 (01.01.–07.01.) isn't week 1: week 1 of 2027 is Mon 2027-01-04" in description
    )
    words = "finally looking good. last small change is to extend the lines for the notes"
    assert owner(agent).decide(request, {"decision": "reject", "comment": words}, "Owner").status == 200
    ctx.state = tools.CycleTools()
    again = check(ctx, path)
    assert len(again.text) < tools.MAX_RESULT_CHARS  # every measure of a book with many faults, uncut
    day = rows(agent, f"SELECT decided_at FROM approvals WHERE id = {request}")[0]["decided_at"][:10]
    assert f'5. Your owner\'s last words on it, #{request} (rejected {day}): "{words}": each point done?' in again.text
    # a book KDP refused: the owner's note on it is their last word
    good = book(ctx.workspace, haushaltsbuch(weeks=21), "books/hb2", project(agent))
    ctx.state = tools.CycleTools()
    check(ctx, good)
    next_reply(ctx)
    assert propose(ctx, good).ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'kdp_package'")[0]["id"]
    who = owner(agent)
    assert who.decide(second, {"decision": "approve"}, "Owner").status == 200
    refused = "KDP: the interior's notes run into the gutter on page 5"
    assert who.close(second, {"outcome": "failed", "result_note": refused}, "Owner").status == 200
    closed = rows(agent, f"SELECT closed_at FROM approvals WHERE id = {second}")[0]["closed_at"][:10]
    assert f'last words on it, #{second} (not published {closed}): "{refused}"' in check(ctx, good, pages="5").text
    with agent.db.connection() as conn:
        assert not kdp.last_words(conn, agent.scope(), "Another Book", None)  # another book's words are not its


def test_an_ebook_has_no_pages_to_check(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    jail.write("books/guide.md", "# A Small Guide\n\nSome useful words.\n")
    make.document(jail, "books/guide.md", "books/guide.pdf", previews=False)
    front(jail)
    data = json.loads(jail.read(book(jail, haushaltsbuch())))
    data.update(format="ebook", manuscript="books/guide.docx", price="2.99", cover={"front": "books/front.png"})
    data.update(project_id=project(agent))
    del data["paper"], data["low_content"]
    jail.write("books/guide.json", json.dumps(data))
    checked = check(ctx, "books/guide.json")
    assert checked.pictures == () and "Nothing went to your owner: propose it with check false." in checked.text
    assert call(ctx, "propose_kdp_book", {"spec": "books/guide.json"}).ok  # no page check, no qa


def test_every_picture_of_a_result_reaches_the_model() -> None:
    first, second = (pagecheck.check(ruled(8.0)).pictures[0],) * 2
    outcome = tools.Outcome(True, "Its pages", "checked", pictures=(first, second))
    block = loop.CycleRunner._result_block({"id": "toolu_1"}, outcome)
    assert [part["type"] for part in block["content"]] == ["text", "image", "image"]


def test_the_tool_the_guide_and_the_fixed_prompt() -> None:
    spec = tools.SPECS["propose_kdp_book"]
    assert {"pages", "qa"} <= set(spec.fields) and "draw its pages" in spec.description
    manual = tools.guide_text("kdp")
    assert len(manual) <= tools.MAX_RESULT_CHARS
    flat = " ".join(manual.split())
    assert "A paperback's check also draws its pages fresh, one of each kind (or pages: '6, 83')" in flat
    assert "with qa: what you saw on its pages, point by point. Old pictures or checklists are no check." in flat
    assert "propose_kdp_book's check draws a paperback's pages" in " ".join(tools.guide_text("workshop").split())
