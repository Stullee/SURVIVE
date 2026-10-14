"""0.39.2: the fixes of the diagnostics' report of 2026-10-14 20:36 (analysis-0.39.1).

1. 0.39.1's book check named a page to fill in that ran on to one page more, and only tables with a row of empty cells
   were tables to fill in: the Haushaltsbuch's v14, its month overviews three pages each (the third holding "Notizen"
   alone) and their tables of labels with empty amounts breaking, got no word. Now a page to fill in is named however
   many pages it runs on to (a notebook's pages of lines excepted), and a table whose rows each have an empty cell is
   one to fill in. The guides say what a week page holds, and that a larger line_height fits fewer rows.
2. The first cycle under 0.39.1 judged its renderer by pictures of pages 0.38.0 had drawn two days before: look says
   when a picture was made, and that it came before the running version.
3. The workshop kept the book's v14 in workshop/out/, make_document was asked for it in books/: the refusal names a
   file of that name elsewhere, and the workshop's result says first where it kept a file its task put elsewhere.
4. A step to send pins was a "ship" step, which an ordinary cycle without propose_pin took: plan_step's kind says what
   market is, and request_approval's refusal of a pin says how to get it to a marketing cycle.
5. That cycle closed the step with the reason "placeholder": a done says what was done, in more than one word.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import prompts, tools, workshop  # noqa: E402
from app.products import markup, pdf  # noqa: E402
from app.version import app_version  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_fixes_0280 import lined, working  # noqa: E402
from tests.test_fixes_0340 import TRACKER, keep, project  # noqa: E402
from tests.test_fixes_0350 import plan_step, step_id  # noqa: E402
from tests.test_product_tools import png  # noqa: E402
from tests.test_rule_audit import SETTINGS, every_channel  # noqa: E402

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
    assert report.warnings == [OVERRUN.format(pages=2)]
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
    assert v14.pages == 3 and v14.warnings == [SPLIT.format(pages=1)]  # its "Ausgaben nach Kategorie" breaks
    head, rest = MONTH.split("### Ausgaben nach Kategorie")
    spending, goals = rest.split("### Monatsziel & Sparquote")
    goals, notes = goals.split("**Notizen**")
    fits = rendered(book(f"{head}### Monatsziel & Sparquote{goals}::: pagebreak\n\n### Ausgaben nach Kategorie"
                         f"{spending}**Notizen**{notes}"))  # fmt: skip
    assert fits.pages == 2 and fits.warnings == []


def test_the_guides_say_what_a_week_page_holds_and_the_renderer_agrees() -> None:
    kdp = " ".join(tools.guide_text("kdp").split())
    assert (
        "a 6x9 page at size 12.5 holds about 20 empty rows, 10 with a heading, a 3-row total and 4 writing lines;"
        " fewer at a larger line_height"
    ) in kdp
    documents = " ".join(tools.guide_text("documents").split())
    assert "a row with no words one line (about 7 to 8 mm): a larger line_height makes each row taller" in documents
    assert rendered(book(week(10, 4))).pages == 1
    assert OVERRUN.format(pages=1) in rendered(book(week(11, 4))).warnings
    assert rendered(book(week(15, 0))).pages == 1 and rendered(book(week(16, 0))).pages == 2
    assert rendered(book(week(6, 4), TALL)).pages == 1 and rendered(book(week(7, 4), TALL)).pages == 2  # v14's 1.9
    assert rendered(book(week(9, 0), TALL)).pages == 1  # v14's week: 9 rows, its notes dropped


# --- 2. a picture's age ---


def test_look_says_when_a_picture_was_made_and_that_it_came_before_this_version(data_dir: Path) -> None:
    """Live, books/check-kw-page6.png (made 10-12 under 0.38.0) showed the old 2.2 mm rows, and the first cycle under
    0.39.1 took them for its renderer's."""
    agent, _ = lined(data_dir, titles=())  # a cycle under this version, at the clock's start
    ctx = working(agent)
    jail = agent.roots()[0]
    started = agent.clock.now()
    before, after = started - timedelta(days=2), started + timedelta(minutes=5)
    for path, made in (("books/check-kw-page6.png", before), ("books/v14-page6.png", after)):
        jail.write_bytes(path, png(600, 900))
        os.utime(jail.root / path, (made.timestamp(), made.timestamp()))
    old = call(ctx, "look", {"path": "books/check-kw-page6.png"})
    assert old.ok and old.text == (
        f"books/check-kw-page6.png (600 x 900 pixels, made {before:%Y-%m-%d %H:%M}, before version {app_version()}"
        " ran: it shows what Ember's tools made then), shown here:"
    )
    new = call(ctx, "look", {"path": "books/v14-page6.png"})
    assert new.ok and new.text == f"books/v14-page6.png (600 x 900 pixels, made {after:%Y-%m-%d %H:%M}), shown here:"


# --- 3. a file kept in another folder ---


def test_a_missing_file_names_the_file_of_its_name_elsewhere(data_dir: Path) -> None:
    """Live, make_document was asked for books/…-v14.md, which the workshop had kept in workshop/out/."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    ctx.workspace.write("books/haushaltsbuch-v13.md", "# Haushaltsbuch\n")
    ctx.workspace.write("workshop/out/haushaltsbuch-v14.md", "# Haushaltsbuch\n")
    made = call(ctx, "make_document", {"source": "books/haushaltsbuch-v14.md", "output": "books/haushaltsbuch.pdf"})
    assert not made.ok and made.text == (
        "Error: books/haushaltsbuch-v14.md doesn't exist; a file of that name is at workshop/out/haushaltsbuch-v14.md."
    )
    deeper = call(ctx, "workspace_read", {"path": "drafts/new/haushaltsbuch-v14.md"})  # its folder is missing too
    assert not deeper.ok and deeper.text == (
        "Error: drafts doesn't exist; a file of that name is at workshop/out/haushaltsbuch-v14.md."
    )
    other = call(ctx, "workspace_read", {"path": "books/haushaltsbuch-v15.md"})
    assert not other.ok and other.text == "Error: books/haushaltsbuch-v15.md doesn't exist."


def test_the_workshop_says_first_where_it_kept_a_file_its_task_put_elsewhere() -> None:
    """Live, the helper answered "Files exported", and only the line after it said where the file was."""
    task = "Read books/haushaltsbuch-v13.md, keep 9 rows a week and write the result to books/haushaltsbuch-v14.md."
    script = "workshop/scripts/read-the-markdown-file-63.py"
    run = workshop.Run(
        task=task,
        script_used=None,
        inputs=["books/haushaltsbuch-v13.md"],
        kept=[("workshop/out/haushaltsbuch-v14.md", 24_728), (script, 2_048)],
        script_path=script,
        answer="Done. Files exported: haushaltsbuch-v14.md",
        cost=101_000,
    )
    ok, text, _ = workshop.report(run, lambda source, body: body)
    lines = text.split("\n")
    assert ok and lines[:3] == [
        "Workshop run (cost $0.1010).",
        "Your task named books/haushaltsbuch-v14.md: the workshop kept it as workshop/out/haushaltsbuch-v14.md (it"
        " keeps what it makes in its folder, whatever a task says: use that path, or copy the file with"
        " workspace_write mode copy).",
        "Done. Files exported: haushaltsbuch-v14.md",
    ]
    chart = workshop.Run(
        task="Make chart.png, 1200 x 800 pixels, from shop/sales.csv.",
        script_used=None,
        inputs=["shop/sales.csv"],
        kept=[("workshop/out/chart.png", 20_000)],
        answer="Made chart.png.",
    )
    assert "Your task named" not in workshop.report(chart, lambda source, body: body)[1]


# --- 4. a step for a marketing cycle ---


def test_a_steps_kind_says_what_market_is() -> None:
    request = prompts.work_request(SETTINGS, "brief", [], **every_channel())
    [spec] = [t for t in request["tools"] if t["name"] == "plan_step"]
    kind = spec["input_schema"]["properties"]["steps"]["items"]["properties"]["kind"]
    assert kind["description"] == "market: a pin, a post or a blog post (only a marketing cycle has their tools)."
    assert "market" in kind["enum"]


def test_a_pin_asked_of_the_owner_says_how_a_cycle_without_propose_pin_gets_one(data_dir: Path) -> None:
    """Live, a cycle without propose_pin asked for the pins with request_approval, then closed their step."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    ctx.pinterest = tools.PinterestAccess("ember-dry-run", 3)
    ctx.marketing_apart = True  # marketing cycles run: an ordinary cycle leaves their tools to them
    asked = {
        "type": "publish",
        "title": "Pins: Haushaltsbuch DE + Budget Planner EN, portrait",
        "description": "Resubmit both pins as portrait images, replacing #101 and #102.",
        "payload": "Pinterest pins: shop/pin_haushaltsbuch_de_v2.png -> https://www.etsy.com/listing/1",
        "expected_cost": "$0",
        "expected_benefit": "views",
    }
    refused = call(ctx, "request_approval", asked)
    assert not refused.ok
    assert refused.text.startswith("Error: a pin goes through propose_pin (a marketing cycle's tool)")
    assert refused.text.endswith(
        "; this cycle has no propose_pin: plan_step add a step of kind market for it, which a marketing cycle takes."
    )
    ctx.marketing = True
    again = call(ctx, "request_approval", asked)
    assert not again.ok and "plan_step add" not in again.text  # a marketing cycle has propose_pin


# --- 5. a done says what was done ---


def test_a_step_said_done_says_what_was_done(data_dir: Path) -> None:
    """Live, step #157 was closed with the reason "placeholder", its pins unsent."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    pictures = [{"title": "Make the two pin pictures", "kind": "create"}]
    assert plan_step(agent, tracker, action="add", step_id=demand, steps=pictures).ok
    mine = step_id(agent, tracker, "Make the two pin pictures")
    ctx = working(agent, tracker)
    ctx.state.step_id = mine  # YOUR STEP
    for why in ("placeholder", "done", "  ok  "):
        refused = call(ctx, "plan_step", {"action": "done", "why": why})
        assert not refused.ok and refused.text == (
            "Error: a done says in why what was done, in a few words: one word says nothing."
        )
    assert rows(agent, f"SELECT status FROM plan_nodes WHERE id = {mine}") == [{"status": "open"}]
    done = call(ctx, "plan_step", {"action": "done", "why": "made both pin pictures"})
    assert done.ok, done.text
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {mine}") == [
        {"status": "done", "result": "made both pin pictures"}
    ]
