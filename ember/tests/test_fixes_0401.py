"""0.40.1: the fixes of the diagnostics report of 2026-10-18 (analysis-0.40.0).

1. The KDP check draws the kind of page most of the book is (a planner's week) first, and the AI line's page holds
   "little else" only without ruled lines on it too (3.3).
2. A work conversation has room for two checks, and a folder's listing shows its newest files first, 40 at most
   (3.2, 3.3).
3. Ember's code keeps the file each PDF make_document made was made from, and a KDP check says it, whether that
   file changed since, and a newer PDF of the same book (3.2).
4. A request the owner rejected stays urgent until it is proposed again, not for 3 days (3.3).
5. A promise's step can wait on a date, until 2 days before the promise is due at the latest (3.4).
6. A request waiting for the owner cuts the sleep only while their decision on it wouldn't wake the agent (3.4).
7. etsy_listing shows the quality critic's newest check of the listing (3.5).
8. The diagnostics report shows a long workspace text file's start and its end (3.6).
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta
from pathlib import Path

import pypdfium2
import pytest

from app import diagnostics
from app.agent import loop, plan, quality, tools, weights
from app.agent.sandbox import Jail
from app.products import markup, pagecheck, pdf
from tests.test_agent import ROOMY, make_agent, rows
from tests.test_decision_wakes import asks_and_sleeps
from tests.test_etsy import call, listed, shop_context
from tests.test_fixes_0280 import lined, now
from tests.test_fixes_0340 import BOOK as BOOK_TITLE
from tests.test_fixes_0340 import TRACKER, keep
from tests.test_fixes_0340 import project as line
from tests.test_fixes_0350 import steered
from tests.test_fixes_0351 import promise
from tests.test_fixes_0376 import asked, candidate, closed_without_proposing, cycles, decided
from tests.test_kdp import project, publishing
from tests.test_kdp_pages import AI_LINE, SETTINGS, book, check, haushaltsbuch, month, rendered, week

# --- 1. the check draws a week ---


def planner(weeks: int = 4) -> str:
    """A planner like the Haushaltsbuch: one-off pages first (a title page, the year, its goals, the quarters), then a
    month and its weeks, and the AI line alone on the last page."""
    year = "# Jahresübersicht 2027\n\n| Monat | Einnahmen | Ausgaben |\n|---|---|---|\n" + "| | | |\n" * 12
    goals = "## Finanzielle Jahresziele 2027\n\n**Ziel 1:**\n::: lines 3\n\n**Ziel 2:**\n::: lines fill\n"
    quarters = "## Sparziele pro Quartal\n\n| Quartal | Sparziel |\n|---|---|\n" + "| | |\n" * 4
    quarters += "\n**Notizen**\n::: lines fill\n"
    first = date(2027, 1, 4)
    pages = ["# Haushaltsbuch 2027\n\nDein Jahr im Überblick\n", year, goals, quarters, month("Januar")]
    pages += [week(n, first + timedelta(days=7 * (n - 1)), "::: lines fill") for n in range(1, weeks + 1)]
    return SETTINGS + "\n::: pagebreak\n\n".join([*pages, AI_LINE])


def test_a_check_draws_a_week_before_the_one_off_pages() -> None:
    """Live (analysis-0.40.0, 3.3): of the Haushaltsbuch's 84 pages a check drew 1 to 5 and 83: the two marked pages,
    then the kinds in the order they first appear. The week, 53 of its pages, wasn't drawn."""
    checked = pagecheck.check(rendered(planner()))
    weeks = next(numbers for numbers in checked.kinds if len(numbers) == 4)
    last = len(checked.pages)
    assert len(checked.kinds) + 1 > pagecheck.PAGES_SHOWN  # more kinds than a check draws
    assert {1, last} <= {p.number for p in checked.pages if p.blank_share > pagecheck.BLANK_SHARE}  # title, AI page
    assert weeks[0] in checked.shown  # the week, first
    assert {1, last} <= set(checked.shown)  # then what is marked
    assert len(checked.shown) == pagecheck.PAGES_SHOWN


def test_a_book_with_few_kinds_draws_one_of_each_as_before() -> None:
    checked = pagecheck.check(rendered(SETTINGS + "\n::: pagebreak\n\n".join(["# Titel", AI_LINE, month("Januar")])))
    assert checked.shown == [1, 2, 3]


# --- 1. the AI line under writing lines ---


def test_the_ai_line_under_writing_lines_is_not_alone() -> None:
    """The AI line on a last page of writing lines holds few words, but the page is full: not "little else"."""
    lined = SETTINGS + "# Titel\n\n::: pagebreak\n\n## Ziele für 2028\n::: lines 12\n\n---\n\n" + AI_LINE
    checked = pagecheck.check(rendered(lined))
    page = checked.pages[-1]
    assert checked.ai_pages == [page.number] and page.words < pagecheck.ALONE_WORDS
    said = "\n".join(checked.measures())
    assert f"AI line: page {page.number}; page {page.number}, the last page, has {page.words} words." in said
    alone = pagecheck.check(rendered(SETTINGS + "# Titel\n\n::: pagebreak\n\n" + AI_LINE))
    assert "little else." in "\n".join(alone.measures())  # alone on its page, it still is


# --- 2. room for two checks ---


def test_a_cycle_has_room_for_two_checks_and_its_work() -> None:
    """Live, four of the five cycles on the KDP book reached 64,000 bytes before they proposed it, and a check (six
    pages of 2,700 bytes each and up to 4,000 characters of text) takes about 20,000 of it."""
    check = pagecheck.PAGES_SHOWN * 2_700 + tools.MAX_RESULT_CHARS
    needed = 64_000 - 10_000 + 2 * check  # what the cycles spent before a check, and two checks
    assert needed <= loop.MAX_CONVERSATION_BYTES


def test_a_folder_lists_its_newest_files_first(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.2), books/ held 185 files, versions v3 to v22: listed by name, its first 100 took 4 KB a
    listing, and four listings in one cycle didn't say which file was the newest."""
    _, ctx = publishing(data_dir)
    jail = ctx.workspace
    jail.write("books/drafts/old.md", "a draft")
    for n in range(1, 51):
        jail.write(f"books/hb-v{n}.md", f"version {n}")
        os.utime(jail.root / "books" / f"hb-v{n}.md", (1_760_000_000 + n * 60, 1_760_000_000 + n * 60))
    listed = call(ctx, "workspace_list", {"path": "books"})
    assert listed.ok
    lines = listed.text.splitlines()
    assert lines[0] == "books/drafts/"  # its folders first
    assert lines[1].startswith("books/hb-v50.md  10 B  ") and lines[2].startswith("books/hb-v49.md  10 B  ")
    assert lines[tools.FOLDER_SHOWN - 1].startswith("books/hb-v12.md")  # 40 entries: a folder and 39 files
    assert lines[tools.FOLDER_SHOWN] == "… 11 older"
    everything = call(ctx, "workspace_list", {})  # the whole workspace: by path, as before
    assert everything.ok and "books/drafts/old.md" in everything.text and " older\n" not in everything.text


# --- 3. the check names the file the PDF was made from ---


def made(ctx: tools.ToolContext, source: str, output: str) -> None:
    """A PDF made as the agent makes one: with the make_document tool."""
    done = call(ctx, "make_document", {"source": source, "output": output, "word": False, "pictures": False})
    assert done.ok, done.text


def older(jail: Jail, path: str) -> None:
    """``path`` written an hour earlier than it was."""
    file = jail.root / path
    os.utime(file, (file.stat().st_mtime - 3_600, file.stat().st_mtime - 3_600))


def test_the_check_says_what_the_interior_was_made_from(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.2), the PDFs v19 to v22 were all made from v18.md, the spec still named v15.pdf (the
    book the owner rejected), and each cycle on the book spent its first steps finding out which file the book was."""
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    spec = book(jail, haushaltsbuch(weeks=21), "books/hb-v15", project(agent))
    assert "Made with make_document" not in check(ctx, spec).text  # a PDF made otherwise: no source known
    made(ctx, "books/hb-v15.md", "books/hb-v15.pdf")
    text = jail.read("books/hb-v15.md")
    [row] = rows(agent, "SELECT path, source, source_sha256 FROM made_pdfs")
    assert row == {
        "path": "books/hb-v15.pdf",
        "source": "books/hb-v15.md",
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    assert "Made with make_document from books/hb-v15.md (unchanged since)." in check(ctx, spec).text
    jail.write("books/hb-v15.md", text + "\nNoch eine Zeile.\n")
    changed = check(ctx, spec).text
    assert "Made with make_document from books/hb-v15.md (changed since: make_document it again" in changed


def test_the_pdf_itself_names_no_workspace_file(data_dir: Path) -> None:
    """What the PDF was made from is kept in the database: the files Ember makes go to buyers and to KDP as they are."""
    _, ctx = publishing(data_dir)
    ctx.workspace.write("shop/cv.md", SETTINGS + "# Lebenslauf\n")
    made(ctx, "shop/cv.md", "shop/cv.pdf")
    document = pypdfium2.PdfDocument(ctx.workspace.read_bytes("shop/cv.pdf"))
    try:
        metadata = document.get_metadata_dict()
    finally:
        document.close()
    assert not any("shop/" in str(value) or "sha256" in str(value) for value in metadata.values())


def test_the_check_names_a_newer_pdf_of_the_same_book(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    spec = book(jail, haushaltsbuch(weeks=21), "books/hb-v15", project(agent))
    made(ctx, "books/hb-v15.md", "books/hb-v15.pdf")
    older(jail, "books/hb-v15.pdf")
    assert "is newer than" not in check(ctx, spec).text  # nothing newer yet
    jail.write("books/other.md", SETTINGS + "# Ein anderes Buch\n")
    made(ctx, "books/other.md", "books/other.pdf")
    assert "is newer than" not in check(ctx, spec).text  # another book's PDF is no newer version of this one
    made(ctx, "books/hb-v15.md", "books/haushaltsbuch-final.pdf")  # made from the same file, named otherwise
    said = check(ctx, spec).text
    assert "books/haushaltsbuch-final.pdf (" in said
    assert ") is newer than books/hb-v15.pdf: if it is the book, set the spec's manuscript to it." in said


def test_a_pdf_made_before_0401_is_known_by_its_name(data_dir: Path) -> None:
    """The live book's PDFs were made before 0.40.1, which kept no source: but v22.pdf is v15.pdf's name but for its
    version."""
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    spec = book(jail, haushaltsbuch(weeks=21), "books/hb-v15", project(agent))
    older(jail, "books/hb-v15.pdf")
    data, _ = pdf.render(markup.parse(jail.read("books/hb-v15.md")))
    jail.write_bytes("books/hb-v22.pdf", data)  # no record of what it was made from
    assert not rows(agent, "SELECT id FROM made_pdfs")
    assert ") is newer than books/hb-v15.pdf: if it is the book" in check(ctx, spec).text


# --- 4. the rejected book stays urgent ---


def test_the_rejected_book_stays_urgent_until_it_is_proposed_again(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.3), the urgency of the owner's rejection ended 3 days after it, while five cycles had
    run out of room before they proposed the book: from the fourth day it got one try a day, level with a review's
    step."""
    agent, _ = lined(data_dir, titles=())
    book = line(agent, *BOOK_TITLE)
    keep(agent)
    package = asked(agent, book, "kdp_package")
    keep(agent)
    decided(agent, package, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    agent.clock.advance(days=4)
    keep(agent)
    again = f"Propose the book again: request #{package} rejected"
    assert candidate(steered(agent), again, book).step.urgency == weights.REWORK
    taken = cycles(agent, 3)  # each takes it and ends before it proposes: still two tries a day
    assert [t.title for t in taken[:2]] == [again, again] and taken[2].title != again
    asked(agent, book, "kdp_package")  # proposed again: its step is done
    keep(agent)
    assert not [c for c in steered(agent).found if c.step.title == again]


# --- 5. a promise waits for its day ---


def test_a_promise_waits_on_a_date_until_two_days_before_it_is_due(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.4), three cycles took "Report milestone #18 outcome (sale or park) by 10-31", which
    can't be kept before the milestone is judged on 10-24; each was refused when it tried to make it wait."""
    agent, _ = lined(data_dir, titles=())
    coach = line(agent, *TRACKER)
    keep(agent)
    owed = promise(agent, "Report milestone #18 outcome (sale or park) by 10-31", days=13, line=coach)
    keep(agent)
    [step] = [c for c in steered(agent).found if c.step.title.startswith("Keep promise")]
    today, due = agent.clock.today(), agent.clock.today() + timedelta(days=13)
    latest = due - timedelta(days=weights.PROMISE_NEAR_DAYS)

    def wait(on: str, until: date | None = None, ref: int | None = None) -> str:
        with agent.db.transaction() as conn:
            day = until.isoformat() if until else None
            why = "judged on 10-24"
            return plan.wait_step(conn, agent.scope(), coach, step.step.id, on, ref, day, why, None, now(agent), today)

    with pytest.raises(plan.PlanError, match=f"keeps your promise due {due}: it waits only on a date, until {latest}"):
        wait("step", ref=step.step.id)
    with pytest.raises(plan.PlanError, match=f"it waits until {latest} at the latest"):
        wait("date", latest + timedelta(days=1))
    assert f"waits on {latest}" in wait("date", latest)
    assert next(c for c in steered(agent).found if c.step.id == step.step.id).waiting == "date"
    assert owed  # the promise itself stays open: it is kept, or told why not, from its day on


def test_a_promise_near_its_day_and_a_decision_wait_on_nothing(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    coach = line(agent, *TRACKER)
    keep(agent)
    promise(agent, "Report the coach bundle's views", days=2, line=coach)
    keep(agent)
    [step] = [c for c in steered(agent).found if c.step.title.startswith("Keep promise")]
    tomorrow = (agent.clock.today() + timedelta(days=1)).isoformat()

    def wait(step_id: int) -> str:
        with agent.db.transaction() as conn:
            return plan.wait_step(conn, agent.scope(), coach, step_id, "date", None, tomorrow, "later", None,
                                  now(agent), agent.clock.today())  # fmt: skip

    with pytest.raises(plan.PlanError, match="; now keep it, or tell your owner why you can't"):
        wait(step.step.id)
    package = asked(agent, coach, "etsy_listing")
    decided(agent, package, "rejected")
    keep(agent)
    [taken_in] = [c for c in steered(agent).found if c.step.title.startswith("Obligation #")]
    with pytest.raises(plan.PlanError, match=f"step #{taken_in.step.id} is your owner's"):
        wait(taken_in.step.id)  # their decision is theirs, as before


# --- 6. a waiting request and the sleep ---


def test_a_request_waiting_for_the_owner_leaves_the_sleep_when_their_decision_wakes_ember(data_dir: Path) -> None:
    """Live on 10-17 (analysis-0.40.0, 3.4), a pin waiting for the owner woke the agent after 4 of the 6 hours it chose,
    twice to nothing it could do; the owner's approval woke it anyway, at 04:43."""
    agent, _ = make_agent(data_dir, asks_and_sleeps(360), ROOMY.model_copy(update={"max_sleep_minutes": 360}))
    started = agent.clock.now()
    assert agent.run_cycle("schedule").status == "completed"
    assert agent._meta_time("next_wake_at") == started + timedelta(minutes=360)
    assert agent.agent_fields()["next_wake_reason"] == "Ember chose 360 min"


# --- 7. etsy_listing shows the critic ---

FIXES = (
    "Add an editable cover letter .docx: the description promises Word/Docs editing, but that file is PDF only. Drop"
    " the 'resell rights' and 'white label' tags, since the licence forbids both."
)


def test_a_listing_read_shows_what_the_critic_found(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.5), a plan named "the critic's findings" for the coach bundle, but YOUR STEP was another
    step's and no tool showed them: the cycle researched the niche instead and staged a fix without them."""
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    assert "quality critic" not in call(ctx, "etsy_listing", {"listing_id": listing_id}).text  # none yet
    with agent.db.transaction() as conn:
        answer = {"score": 5, "verdict": "improve", "fixes": FIXES}
        quality.save(conn, agent.scope(), project(agent), None, answer, None, now(agent), listing_id)
    full = call(ctx, "etsy_listing", {"listing_id": listing_id}).text
    day = agent.clock.today().isoformat()
    critic = f"The quality critic ({day}: 5/10, improve): {FIXES}"
    assert critic in full and full.index(critic) < full.index("Description (Ember adds")  # before a long description


# --- 8. the report shows a long file's end ---


def test_the_report_shows_a_long_text_file_s_start_and_end(tmp_path: Path) -> None:
    """Live (analysis-0.40.0, 3.6), the report cut the KDP book's source (27,517 characters) after its 37th week, and
    its back matter, where the AI line had gone alone onto a page, had to be pieced together from the cycles' reads."""
    jail = Jail(tmp_path)
    days = [date(2027, 1, 4) + timedelta(days=7 * n) for n in range(104)]  # two years of weeks: over 20,000 characters
    weeks = "".join(week(day.isocalendar().week, day, "::: lines fill") for day in days)
    book = f"# Haushaltsbuch 2027 und 2028\n\n{weeks}\n## Ziele für 2029\n::: lines fill\n"
    assert len(book) > diagnostics.WORKSPACE_FILE_CHARS
    jail.write("books/hb.md", book)
    shown = diagnostics._workspace_texts(jail, jail.walk(10).entries)
    assert "# Haushaltsbuch 2027 und 2028" in shown and "## Ziele für 2029" in shown  # its start and its end
    left_out = len(book) - diagnostics.WORKSPACE_FILE_CHARS
    assert f"… [{left_out:,} characters left out] …" in shown
    jail.write("notes/short.md", "Short.")
    assert "Short." in diagnostics._workspace_texts(jail, jail.walk(10).entries)  # a short file whole, as before
