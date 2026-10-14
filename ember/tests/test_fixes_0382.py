"""0.38.2: the fixes of the diagnostics' report of 2026-10-14 (analysis-0.38.0).

1. A table row with no words was 2.2 mm tall, its padding alone: the KDP Haushaltsbuch's weekly table of 18 rows to
   fill in was a band of lines no one could write in, the owner's fifth rejection of the book, and the page pictures
   showed only its first 4 pages. Now such a row is one line tall, a table's header stays with its first row and a
   one-line label with what it labels, a book says which of its pages to fill in run on, and make_document pictures
   the pages the agent names.
2. Nothing showed the agent the day's messages that count toward the owner's limit of two: nine cycles from 04:20 on
   tried to send a heads-up two messages had given. STATUS lists them, and a first test's heads-up, once given, isn't
   owed any more.
3. Six cycles parked a brainstorm's six ideas one by one ($1.03), the empty funnel brought the next brainstorm at once,
   and an idea "paused until 10-20" came back the next day. A triage's YOUR STEP names the other ideas waiting, a
   venture's step can wait for a day, and no brainstorm comes within a week of one whose ideas had no research.
4. A channel's line (the Pinterest test) got weekly pins and posts the tools refuse it: they drop out.
5. The review said stop, which only the owner can carry out (only they close a line): it is their decision now.
6. plan_step without step_id meant YOUR STEP four times, each refused; a step can't wait on itself.
7. The critic's suggestions for listings no one saw took 8 of the 10 cycles #257 to #266: below 10 views, or after 2
   checks without a pass, they weigh as a chore (a defect still comes first).
8. message_owner's commits "none - informational only" was refused as a promise without its day; a text too long says
   how much to cut.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pypdfium2
import pytest

pytest.importorskip("httpx2")

from app.agent import context, desk, obligations, plan, review, stages, store, tools, ventures, weights  # noqa: E402
from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: E402
from app.agent.sandbox import Jail  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.products import make, markup, pdf  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_fixes_0161 import backed_pod, first_test_of, grade, on, warn  # noqa: E402
from tests.test_fixes_0280 import IDLE, cycle, lined, now, texts, working  # noqa: E402
from tests.test_fixes_0300 import judged  # noqa: E402
from tests.test_fixes_0340 import TRACKER, keep, project  # noqa: E402
from tests.test_fixes_0350 import WORK, plan_step, step_id  # noqa: E402
from tests.test_fixes_0353 import CRITIC, critic, launched  # noqa: E402
from tests.test_fixes_0360 import explored, exploring_agent  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import DROPSHIPPING, JOURNAL  # noqa: E402

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
    assert (
        "a page to fill in (a table with empty rows, or writing lines) runs on to a second page from page 3: in a"
        " book shorten each to its page (fewer rows or lines, a smaller size)"
    ) in report.warnings
    assert (
        "a table to fill in runs on to the next page from page 5: in a book keep each on its page (fewer rows, a"
        " smaller size, or a pagebreak before it)"
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


# --- 2. the day's messages and a first test's heads-up ---


def test_status_lists_the_days_messages_that_count_toward_the_owners_limit(data_dir: Path) -> None:
    """Live, nine cycles from 04:20 on tried to send what two messages had told the owner hours before, each refused."""
    agent, fake = lined(data_dir, titles=())
    day_start = to_iso(agent.clock.day_start(agent.clock.today()))

    def line() -> str:
        with agent.db.connection() as conn:
            sent = store.unasked_messages(conn, agent.scope(), day_start)
        return context.messages_line(sent, lambda at: at[11:16])

    assert line() == "Your messages today that answer none of your owner's: 0 of 2."
    with agent.db.transaction() as conn:
        asked = store.insert_message(conn, agent.scope(), None, "How is the book going?", now(agent))  # the owner's
        answer = store.insert_message(conn, agent.scope(), 1, "Version 13 is ready for you.", now(agent))
        conn.execute("UPDATE messages SET answered_by = ? WHERE id = ?", (answer, asked))  # an answer: not counted
        heads_up = "Heads-up: milestones #15 and #16 will likely miss on 10-21, as the traffic is thin."
        first = store.insert_message(conn, agent.scope(), 1, heads_up, now(agent))
    assert line() == (
        f"Your messages today that answer none of your owner's: 1 of 2 (#{first} at 12:00 \"Heads-up: milestones #15"
        ' and #16 will likely miss on 10-21,…").'
    )
    with agent.db.transaction() as conn:
        second = store.insert_message(conn, agent.scope(), 1, "The pins are live.", now(agent))
    full = line()
    assert full.startswith("Your messages today that answer none of your owner's: 2 of 2 (#")
    assert full.endswith(
        f'; #{second} at 12:00 "The pins are live."): the next waits for tomorrow, or goes in your answer when they'
        " write."
    )
    fake.script.append(Plan(IDLE))
    agent.run_cycle("schedule")
    assert full in texts(fake, "plan")[-1]  # STATUS, as the plan sees it


def test_status_quotes_whole_words_without_what_the_owner_removed() -> None:
    """A word cut in half escapes the report's redaction, which knows whole words only: a removed password, say."""
    secret = "aaaaaa-0bbbbb-cccccc-dddddd-eeeeee-ffffff"
    sent = [{"id": 7, "created_at": "2026-10-14T00:38:00Z", "text": f"So I won't use example-user / {secret}, ever."}]
    assert '#7 at 00:38 "So I won\'t use example-user /…"' in context.messages_line(sent, lambda at: at[11:16])
    removed = context.messages_line(sent, lambda at: at[11:16], lambda text: text.replace(secret, "[removed]"))
    assert '#7 at 00:38 "So I won\'t use example-user / [removed], ever."' in removed


def test_a_first_tests_heads_up_once_given_is_owed_no_more(data_dir: Path) -> None:
    """Live, OBLIGATIONS kept asking for the heads-up two messages had given, and the cycles tried to give it again."""
    agent, pod = backed_pod(data_dir)
    test = first_test_of(agent, pod)
    on(agent, date.fromisoformat(test["due"]) - timedelta(days=stages.WARN_DAYS))
    grade(agent)
    [said] = warn(agent)

    def owed() -> str:
        with agent.db.connection() as conn:
            return obligations.text(conn, agent.scope(), agent.clock.today())

    asked = (
        f"- {said} Work toward it first, or tell your owner once what it needs (name #{test['id']} in your message)."
    )
    assert asked in owed()
    with agent.db.transaction() as conn:  # a message naming another number tells nothing of this one
        store.insert_message(conn, agent.scope(), 1, f"Milestone #{test['id']}0 is met.", now(agent))
    assert asked in owed()
    agent.clock.advance(minutes=5)
    with agent.db.transaction() as conn:
        told = store.insert_message(
            conn,
            agent.scope(),
            1,
            f"First test #{test['id']} will likely miss: no poster passed my checks.",
            now(agent),
        )
    assert (
        f"- {said} You told your owner in message #{told}: work toward it; nothing more to tell them about it until it"
        " changes."
    ) in owed()


# --- 3. the ventures' steps ---


def idea_steps(agent: Agent) -> dict[int, int]:
    """The open triage step of each idea, by venture."""
    with agent.db.connection() as conn:
        found = plan.nodes(conn, agent.scope(), "level = 'step' AND status = 'open' AND template = 'triage@1'")
        return {int(plan.venture_of(conn, agent.scope(), int(s["id"]))): int(s["id"]) for s in found}


@pytest.mark.exploring
def test_a_triage_names_the_other_ideas_waiting_and_a_ventures_step_waits_for_its_day(data_dir: Path) -> None:
    """Live, six cycles in a row each parked one idea of a brainstorm, and an idea "paused until 10-20" came back the
    next day: nothing could make a venture's step wait."""
    agent = exploring_agent(data_dir)
    triages = idea_steps(agent)
    assert DROPSHIPPING in triages and len(triages) >= 4
    step = triages[DROPSHIPPING]
    with agent.db.transaction() as conn:  # the owner's Explore next: this cycle's step
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    steer = explored(agent)
    assert steer.kind == "venture" and steer.step is not None and steer.step.id == step

    def your_step() -> str:
        with agent.db.connection() as conn:
            return plan.step_text(conn, agent.scope(), steer, explore=True, today=agent.clock.today())

    others = [v for v in sorted(triages) if v != DROPSHIPPING]
    titles = {v["id"]: v["title"] for v in rows(agent, "SELECT id, title FROM ventures")}
    named = " · ".join(f'#{v} "{titles[v]}"' for v in others)
    assert (
        f"Other ideas wait for their triage: {named}: triage them in this cycle too (venture_update each: park the"
        " ones you would park, with why; research the one that deserves it most). A cycle for each costs more than"
        " its decision."
    ) in your_step()
    ctx = working(agent)
    ctx.venture, ctx.state.step_id = True, step  # a venture cycle: no product line
    later = triages[others[0]]
    day = (agent.clock.today() + timedelta(days=5)).isoformat()
    waited = call(
        ctx, "plan_step", {"action": "wait", "step_id": later, "on": "date", "until": day, "why": "after the freeze"}
    )
    assert waited.ok and waited.text == f"Step #{later} waits on {day} (1 of {plan.WAITS_A_DAY} waits today)."
    assert f"#{others[0]} " not in your_step().split("Other ideas wait for their triage: ")[1]
    assert {c.step.id: c.waiting for c in explored(agent).found}[later] == "date"
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    product = call(ctx, "plan_step", {"action": "wait", "step_id": demand, "on": "date", "until": day, "why": "x"})
    assert not product.ok and f"step #{demand} isn't a step of your ventures" in product.text
    done = call(ctx, "plan_step", {"action": "done", "why": "x"})
    assert not done.ok and "action must be one of wait" in done.text  # a venture cycle's plan_step waits only
    own = call(ctx, "plan_step", {"action": "wait", "on": "date", "until": day, "why": "after the freeze"})
    assert own.ok and own.text == f"Step #{step} waits on {day} (2 of {plan.WAITS_A_DAY} waits today)."  # YOUR STEP
    agent.clock.advance(days=5)
    lifted = keep(agent)
    assert f"Plan tree: step #{later} is ready again ({day} has come)." in lifted
    assert f"Plan tree: step #{step} is ready again ({day} has come)." in lifted


def brainstorm_steps(agent: Agent) -> list[dict[str, Any]]:
    return rows(
        agent, f"SELECT status, result FROM plan_nodes WHERE template = '{plan.BRAINSTORM_TEMPLATE}' ORDER BY id"
    )


@pytest.mark.exploring
def test_no_brainstorm_comes_within_a_week_of_one_whose_ideas_had_no_research(data_dir: Path) -> None:
    """Live, a brainstorm's six ideas were all parked within a day, and the funnel they left empty brought the next
    brainstorm at once: $1.76 in two days, nothing researched."""
    agent = exploring_agent(data_dir)
    for v in rows(agent, "SELECT id FROM ventures WHERE stage = 'idea'"):  # the owner parks the ideas: a thin funnel
        assert owner(agent).decide_venture(v["id"], {"action": "park"}, "Stefan").status == 200
    keep(agent)
    assert [s["status"] for s in brainstorm_steps(agent)] == ["open"]
    brainstormed = cycle(agent)
    with agent.db.transaction() as conn:  # a brainstorm's six ideas, each parked by a triage before the next keeper
        for n in range(6):
            made = ventures.create(
                conn,
                agent.scope(),
                title=f"Idea {n}",
                pitch="A guess.",
                stage="idea",
                now=now(agent),
                cycle_id=brainstormed,
            )
            ventures.update(conn, made, now(agent), stage="parked", parked_by="agent")
    today = agent.clock.today()
    ends = (today + timedelta(days=desk.BRAINSTORM_REST_DAYS)).isoformat()
    rest = (
        f"the last brainstorm's 6 ideas ({today.isoformat()}) had no research: the next brainstorm comes on {ends}, or"
        " once one of the ideas is researched"
    )
    keep(agent)
    assert brainstorm_steps(agent) == [{"status": "dropped", "result": rest}]
    agent.clock.advance(days=desk.BRAINSTORM_REST_DAYS - 1)
    keep(agent)
    assert len(brainstorm_steps(agent)) == 1  # not yet
    agent.clock.advance(days=1)
    keep(agent)
    assert [s["status"] for s in brainstorm_steps(agent)] == ["dropped", "open"]


def test_the_brainstorms_rest_reads_its_last_batch_of_ideas() -> None:
    def idea(n: int, cycle_id: int, stage: str = "parked", researched: int = 0, by: str = "agent") -> dict[str, Any]:
        return {
            "id": n,
            "created_by": by,
            "created_cycle_id": cycle_id,
            "created_at": "2026-10-12T21:40:00Z",
            "stage": stage,
            "researched": researched,
        }

    batch = [idea(n, 3711) for n in range(1, 7)]
    later = "2026-10-14T09:00:00Z"
    said = desk.brainstorm_rests(batch, later)
    assert said == (
        "the last brainstorm's 6 ideas (2026-10-12) had no research: the next brainstorm comes on 2026-10-19, or once"
        " one of the ideas is researched"
    )
    assert desk.brainstorm_rests(batch, "2026-10-19T00:00:00Z") == ""  # a week on
    assert desk.brainstorm_rests([*batch[1:], idea(1, 3711, researched=1)], later) == ""  # one was researched
    assert desk.brainstorm_rests([*batch[1:], idea(1, 3711, stage="researching")], later) == ""
    assert desk.brainstorm_rests([idea(1, 3711), idea(2, 3700)], later) == ""  # one idea a cycle: no brainstorm
    assert desk.brainstorm_rests([*batch, idea(9, 3720, by="owner"), idea(10, 3721)], later) == said  # not theirs


# --- 4. a channel's line ---


def test_a_channels_line_has_no_pins_or_posts_of_its_own(data_dir: Path) -> None:
    """Live, the Pinterest test's line got a Bluesky step the minute the channel was set up, refused: its line has no
    listing for a post to link."""
    agent, _ = lined(data_dir, titles=("Pinterest boards for the shop",))
    keep(agent)
    [line] = rows(agent, "SELECT id FROM projects")
    with agent.db.transaction() as conn:  # the channel is set up, and a pin step as 0.38.1 laid it out
        [maintain] = plan.nodes(
            conn, agent.scope(), "project_id = ? AND level = 'stage' AND stage = 'maintain'", (line["id"],)
        )
        for stage in plan.nodes(
            conn, agent.scope(), "project_id = ? AND level = 'stage' AND stage <> 'maintain'", (line["id"],)
        ):
            for step in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (stage["id"],)):
                plan._close(conn, step["id"], now(agent), "done", "set up", by="owner")
            plan._close(conn, stage["id"], now(agent), "done", "set up", by="owner")
        pin = plan._insert(
            conn,
            agent.scope(),
            now(agent),
            parent_id=maintain["id"],
            level="step",
            project_id=line["id"],
            template="recurring/pinterest",
            stage="maintain",
            kind="market",
            channel="pinterest",
            title="Pin it this week",
            source="code",
        )
    said = keep(agent)
    assert (
        f"Plan tree: line #{line['id']} is a channel: its steps #{pin} for pins and posts of its own dropped (its"
        " products' marketing is theirs)."
    ) in said
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {pin}") == [
        {"status": "dropped", "result": plan.CHANNEL_DUTY}
    ]
    keep(agent)
    assert rows(
        agent,
        f"SELECT COUNT(*) AS n FROM plan_nodes WHERE project_id = {line['id']} AND status = 'open' AND level = 'step'",
    ) == [{"n": 0}]


# --- 5. the review's stop ---


def test_a_reviews_stop_is_the_owners_decision(data_dir: Path) -> None:
    """Live, the review told the agent to close line #10 with project_update, which can't since 0.35.0; three cycles
    tried, and the next review said the stop wasn't carried out."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    judged(agent, (tracker, "stop", "demand"))
    day = agent.clock.today().isoformat()[5:]
    said = keep(agent)
    assert (
        f"Plan tree: line #{tracker} has a review's stop (your review of {day} says stop: the numbers say so): your"
        " owner decides on it."
    ) in said
    asked = f"SELECT id, kind, status, title FROM plan_nodes WHERE project_id = {tracker} AND template = 'decide/owner'"
    [decision] = rows(agent, asked)
    assert decision["kind"] == "owner" and decision["title"].startswith(f"You decide: keep line #{tracker} or drop it")
    assert keep(agent) == []  # asked once
    with agent.db.connection() as conn:
        assert plan.owner_decision(conn, agent.scope(), tracker, now(agent)) == "deciding"
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None
        assert "Act on it: a stop is your owner's decision (only they close a line)" in review.planner_text(conn, today)
    agent.clock.advance(days=1)
    with agent.db.connection() as conn:
        last = review._last_review(conn, agent.scope(), agent.clock.today())
    assert f"- #{tracker} stop: the numbers say so → your owner decides on it (their Plan tab)" in last
    with agent.db.transaction() as conn:  # the owner keeps it
        plan.owner_keep(conn, agent.scope(), decision["id"], "one more month", "Stefan", now(agent))
    with agent.db.connection() as conn:
        last = review._last_review(conn, agent.scope(), agent.clock.today())
    assert f"- #{tracker} stop: the numbers say so → your owner kept it" in last
    judged(agent, (tracker, "stop", "demand"))  # the next review says stop again
    assert keep(agent) == [] and len(rows(agent, asked)) == 1  # not within a week of their decision
    agent.clock.advance(days=plan.STOP_ASK_DAYS)
    judged(agent, (tracker, "stop", "demand"))
    assert any(f"line #{tracker} has a review's stop" in s for s in keep(agent))


# --- 6. plan_step's step ---


def test_plan_step_means_your_step_when_it_names_none(data_dir: Path) -> None:
    """Live, four calls were refused for naming no step (or the step in ref), each meaning the cycle's own."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    assert plan_step(
        agent, tracker, action="add", step_id=demand, steps=[{"title": "Count the listings", "kind": "create"}]
    ).ok
    mine = step_id(agent, tracker, "Count the listings")
    ctx = working(agent, tracker)
    ctx.state.step_id = mine  # YOUR STEP
    itself = call(ctx, "plan_step", {"action": "wait", "on": "step", "ref": mine, "why": "x"})
    assert not itself.ok and f"step #{mine} can't wait on itself: ref names the other step it waits on" in itself.text
    done = call(ctx, "plan_step", {"action": "done", "why": "counted 20 listings"})
    assert done.ok, done.text
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {mine}") == [
        {"status": "done", "result": "counted 20 listings"}
    ]
    ctx.state.step_id = None  # no step taken: one is named, as before
    nameless = call(ctx, "plan_step", {"action": "done", "why": "x"})
    assert not nameless.ok and "name the step (step_id)" in nameless.text


def test_the_loop_gives_plan_step_its_cycles_step(data_dir: Path) -> None:
    agent, fake = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    assert plan_step(
        agent, tracker, action="add", step_id=demand, steps=[{"title": "Count the listings", "kind": "create"}]
    ).ok
    mine = step_id(agent, tracker, "Count the listings")
    with agent.db.transaction() as conn:  # the owner's pin: the next cycle's step (the helper's cycle ended)
        plan.pin(conn, agent.scope(), mine, True, "Owner", now(agent))
        conn.execute("UPDATE cycles SET status = 'completed', ended_at = ? WHERE status = 'running'", (now(agent),))
    done = ToolCalls([("plan_step", {"action": "done", "why": "counted 20 listings"})])
    fake.script.extend([Plan({**WORK, "focus_project_id": tracker}), done, Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    assert rows(agent, f"SELECT status, closed_by, result FROM plan_nodes WHERE id = {mine}") == [
        {"status": "done", "closed_by": "agent", "result": "counted 20 listings"}
    ]


# --- 7. the critic's suggestions ---


def critic_step(agent: Agent, line: int) -> plan.Candidate:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), {})
    return next(c for c in found if c.step.product == line and c.step.title == CRITIC)


def seen(agent: Agent, listing: int, views: int) -> None:
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = ? WHERE listing_id = ?", (views, listing))


def test_the_critics_suggestions_weigh_as_a_chore_while_few_see_the_listing_or_its_checks_dont_pass(
    data_dir: Path,
) -> None:
    """Live, ten checks scored 5 or 6 of 10, each change brought new fixes, and "The critic passes it" took 8 of the 10
    cycles #257 to #266 while a listing had 0 to 4 views."""
    agent, line, listing = launched(data_dir)
    critic(agent, line, listing, 5)
    quiet = critic_step(agent, line)
    assert (quiet.step.kind, quiet.step.urgency) == ("chore", 0.0)  # no one sees it: its photo can't be judged yet
    seen(agent, listing, weights.CRITIC_VIEWS)
    heard = critic_step(agent, line)
    assert (heard.step.kind, heard.step.urgency) == ("fix", weights.IMPROVE)  # one check without a pass
    critic(agent, line, listing, 6)  # the second in a row
    stuck = critic_step(agent, line)
    assert (stuck.step.kind, stuck.step.urgency) == ("chore", 0.0)
    critic(agent, line, listing, 3)  # a defect comes first, seen or not
    defect = critic_step(agent, line)
    assert (defect.step.kind, defect.step.urgency) == ("fix", weights.DEFECT)
    seen(agent, listing, 0)
    assert critic_step(agent, line).step.urgency == weights.DEFECT


# --- 8. message_owner ---


@pytest.mark.parametrize(
    "commits", ["none", "None - informational only", "none — informational update only", "n/a", "N/A", "—", "nothing"]
)
def test_commits_that_say_none_promise_nothing(data_dir: Path, commits: str) -> None:
    """Live, three messages were refused as a promise without its due day."""
    agent, _ = lined(data_dir, titles=())
    sent = call(working(agent), "message_owner", {"text": "An update, nothing needed from you.", "commits": commits})
    assert sent.ok, sent.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations WHERE kind = 'promise'") == [{"n": 0}]


def test_a_promise_still_needs_its_day_and_a_text_too_long_says_how_much_to_cut(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    undated = call(
        ctx, "message_owner", {"text": "The book comes.", "commits": "None of the pins wait: the book first"}
    )
    assert not undated.ok and "a promise needs both" in undated.text  # "none" alone, or before a dash, is none
    long = call(ctx, "message_owner", {"text": "Soon.", "commits": "x" * 101, "due": agent.clock.today().isoformat()})
    assert not long.ok and "commits is too long: 101 of 100 characters (cut 1)" in long.text
