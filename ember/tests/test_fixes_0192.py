"""0.19.2: fixes from the diagnostics of 2026-10-04 (cycles #86 to #97, the first day after the learning loop).

- Bluesky: a post linked a blog post without its ".html", and an approved one by its file's name: both addresses were
  missing pages (404). A link to the owner's website must be a page Ember's code knows is there; a blog post's address
  without its ".html" is its address, and the check runs again when the post is made.
- The work steps never saw Etsy's numbers (only the plan's ETSY SHOP did): the agent asked its owner for its listings'
  views and promised five times to report them once sent. etsy_listing shows them, and the listings made through
  Printify.
- Lessons: an hour after 0.19.1 lifted project_create's limit, a lesson said it refused a ninth open project; the work
  step believed it over its plan and told its owner so. An upgrade retires the lessons naming a tool its notes name,
  and a full lessons file drops tool limits first (the daily review's lesson, which states no number, went first).
- The daily review's scorecard cut its ROADMAP behind 8 projects in full ("Milestones were not shown"), and listed no
  milestone due later this month.
- Sheet pictures showed IFERROR and other sheets' sums as formulas: a listing's cover showed "(B15/B15,0)".
- And: an approved Bluesky post was "your owner will carry it out"; bets written as Ember's code shows them were
  refused; a refused bet didn't say nothing changed; a journal's next written inside its entry was lost; a focus
  project of another venture took a new venture's listing; make_image ran a subtitle's parts together.
"""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

pytest.importorskip("httpx2")

from app import privacy  # noqa: E402
from app.agent import bets, memory, news, review, roadmap, store, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls  # noqa: E402
from app.integrations import bluesky, etsy, site_publisher  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.products import images, make, sheets  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_bluesky import SITE, a_post, listed, post_context, post_rows  # noqa: E402
from tests.test_etsy import call, shop_context  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_roadmap import plan  # noqa: E402


def blog_post(agent: Any, slug: str, title: str = "Der Wochenplan") -> None:
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO blog_posts (mode, session, slug, title, description, day, position, seen_at)"
            " VALUES (?, ?, ?, ?, 'So planst du deine Woche.', '2026-09-01', 0, 'x')",
            (scope.mode, scope.session, slug, title),
        )


# --- Bluesky: only pages Ember's code knows are there ---------------------------------------------------------------


def test_a_blog_post_without_its_html_is_linked_at_its_address_with_its_card(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "ki-agent-tagebuch-woche-1", "Mein erstes Tagebuch")
    made = a_post(agent, ctx, text="Woche 1, ehrlich.", link=f"{SITE}/blog/ki-agent-tagebuch-woche-1")
    assert made.ok, made.text
    action = json.loads(rows(agent, "SELECT action FROM approvals WHERE executor = 'bluesky_post'")[-1]["action"])
    assert action["link"] == f"{SITE}/blog/ki-agent-tagebuch-woche-1.html"
    assert action["link_title"] == "Mein erstes Tagebuch"


def test_a_page_ember_s_code_doesn_t_know_is_refused_with_the_pages_it_can_link(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "ki-agent-tagebuch-woche-1")
    for wrong in (f"{SITE}/blog/ki-agent-woche-1", f"{SITE}/impressum.html", f"{SITE}/live.html"):
        refused = a_post(agent, ctx, text="Woche 1.", link=wrong)
        assert not refused.ok and "isn't a page of your owner's website that Ember's code knows" in refused.text
        assert f"{SITE}/blog/ki-agent-tagebuch-woche-1.html" in refused.text and f"{SITE}/blog/" in refused.text
    other = a_post(agent, ctx, text="Woche 1.", link="https://example.org/blog/x.html")
    assert not other.ok and "other sites aren't yours to promote" in other.text


def test_the_known_pages_of_the_site() -> None:
    db_meta: dict[str, str] = {}

    class Db:
        def get_meta(self, key: str) -> str | None:
            return db_meta.get(key)

    class Conn:
        def execute(self, sql: str, params: Any) -> Any:
            class Rows:
                def fetchone(self) -> Any:
                    return {"slug": params[-1], "title": "T", "description": "D"} if params[-1] == "known" else None

                def fetchall(self) -> list[Any]:
                    return []

            return Rows()

    scope = store.AgentScope("live", 0, 1)
    base = "https://ember-ai.de"

    def page(link: str) -> str | None:
        found = site_publisher.known_page(Conn(), Db(), scope, base, link)  # type: ignore[arg-type]
        return None if found is None else found[0]

    assert page("https://ember-ai.de") == "https://ember-ai.de/"
    assert page("https://EMBER-AI.de/index.html") == "https://ember-ai.de/"
    assert (
        page("https://ember-ai.de/blog") == page("https://ember-ai.de/blog/index.html") == "https://ember-ai.de/blog/"
    )
    assert page("https://ember-ai.de/blog/known") == "https://ember-ai.de/blog/known.html"
    assert page("https://ember-ai.de/blog/known/") == "https://ember-ai.de/blog/known.html"
    for missing in (
        "https://ember-ai.de/blog/unknown.html",
        "https://ember-ai.de/blog/index",
        "https://ember-ai.de/live.html",
        "https://ember-ai.de/links.html",
        "https://ember-ai.de/blog/known.html?utm=x",
        "http://ember-ai.de/blog/known.html",
        "https://ember-ai.de.example.org/blog/known.html",
    ):
        assert page(missing) is None, missing
    db_meta["integrations.live.live.on_server"] = '["live.html", "en/live.html", "live/banner.svg"]'
    assert page("https://ember-ai.de/en/live.html") == "https://ember-ai.de/en/live.html"
    assert page("https://ember-ai.de/live/banner.svg") is None


def test_an_approved_post_with_a_missing_page_isnt_posted_and_one_without_html_is_posted_with_it(
    data_dir: Path,
) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    blog_post(agent, "alt", "Alt")
    agent.settings = agent.settings.model_copy(update={"site_url": SITE})
    agent.bluesky_posts.settings = agent.settings
    for link in (f"{SITE}/blog/wochenplan.html", f"{SITE}/blog/alt.html"):
        ctx.state = tools.CycleTools()
        assert a_post(agent, ctx, text=f"Neu: {link[-12:]}", link=link).ok
    good, gone = (r["id"] for r in rows(agent, "SELECT id FROM approvals WHERE executor = 'bluesky_post'"))
    scope = agent.scope()
    with agent.db.transaction() as conn:  # as an approved post of 0.19.1 had it: no ".html", or a page now gone
        conn.execute("DROP TRIGGER approvals_action_fixed")
        for request, link in ((good, f"{SITE}/blog/wochenplan"), (gone, f"{SITE}/blog/alt.html")):
            action = json.loads(conn.execute(f"SELECT action FROM approvals WHERE id = {request}").fetchone()[0])
            action.update(link=link, link_title="", link_description="")
            conn.execute("UPDATE approvals SET action = ? WHERE id = ?", (json.dumps(action), request))
        conn.execute("DELETE FROM blog_posts WHERE slug = 'alt' AND mode = ?", (scope.mode,))
    for request in (good, gone):
        assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    done = dict(agent.execute_approved())
    assert done == {good: "active", gone: "failed"}
    account = agent.bluesky.account()
    [made] = account.state["posts"].values()
    assert made["link"] == f"{SITE}/blog/wochenplan.html"
    notes = {r["id"]: r["result_note"] for r in rows(agent, "SELECT id, result_note FROM approvals")}
    assert "Its link went out as https://www.example.de/blog/wochenplan.html" in notes[good]
    assert "isn't a page of your owner's website that Ember's code knows is there" in notes[gone]
    assert [r["status"] for r in post_rows(agent)] == ["active", "failed"]


def test_the_blog_section_gives_each_post_its_address(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    blog_post(agent, "wochenplan")
    settings = agent.settings.model_copy(update={"site_url": SITE + "/index.html", "blog_enabled": True})
    with agent.db.connection() as conn:
        text = site_publisher.text(conn, agent.db, agent.scope(), settings)
    assert f'{SITE}/blog/wochenplan.html "Der Wochenplan" (2026-09-01)' in text


# --- what the agent hears ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("executor", "said"),
    [
        ("bluesky_post", "Ember's code carries it out and you'll hear the result"),
        ("pinterest_pin", "Ember's code carries it out and you'll hear the result"),
        ("site_post", "Ember's code carries it out and you'll hear the result"),
        ("reddit_link", "Your owner posts it and reports back"),
        (None, "Your owner will carry it out and report back"),
    ],
)
def test_an_approved_request_says_who_carries_it_out(executor: str | None, said: str) -> None:
    row = {
        "id": 45,
        "type": "publish",
        "title": "Bluesky: Week 1",
        "status": "approved",
        "decision_comment": None,
        "executor": executor,
        "final_payload": None,
        "result_note": None,
        "result_link": None,
    }
    [line] = news.News(decided=[row]).approval_lines()  # type: ignore[list-item]
    assert line.endswith(f". {said}.")


# --- Etsy's numbers in the work steps ------------------------------------------------------------------------------


def test_etsy_listing_shows_etsys_numbers_and_the_printify_listings(data_dir: Path) -> None:
    agent, listing_id = _listed_shop(data_dir)
    ctx = shop_context(agent)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE etsy_listings SET views = 2, favorites = 1, synced_at = '2026-09-01T10:00:00Z'"
            " WHERE listing_id = ?",
            (listing_id,),
        )
        conn.execute(
            "INSERT INTO printify_products (mode, session, approval_id, started_at, finished_at, status, product_id,"
            " listing_id, currency, title, views, favorites, synced_at) VALUES (?, ?, ?, 'x', 'x', 'active', 'p1',"
            " 800000002, 'EUR', 'Bauhaus Poster No.2', 1, 0, 'x')",
            (scope.mode, scope.session, _an_approval(agent)),
        )
    listing = call(ctx, "etsy_listing", {})
    assert listing.ok, listing.text
    assert "Etsy's numbers as Ember's code read them at 2026-09-01 10:00" in listing.text
    assert f"#{listing_id} " in listing.text and "· 2 views, 1 favorite, 0 sold" in listing.text
    assert "Made through Printify" in listing.text and "#800000002 Bauhaus Poster No.2 · 1 view, 0 favorites" in (
        listing.text
    )
    one = call(ctx, "etsy_listing", {"listing_id": listing_id})
    assert one.ok and "2 views, 1 favorite, 0 sold." in one.text
    poster = call(ctx, "etsy_listing", {"listing_id": 800000002})
    assert poster.ok and "Bauhaus Poster No.2" in poster.text


def _listed_shop(data_dir: Path) -> tuple[Any, int]:
    from tests.test_etsy import listed as etsy_listed  # noqa: PLC0415

    return etsy_listed(data_dir)


def _an_approval(agent: Any) -> int:
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        return store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            "2026-09-01T12:00:00Z",
            type="sell",
            title="Printify product: Bauhaus Poster No.2",
            description="A poster.",
            payload="Bauhaus Poster No.2",
            expected_cost="none",
            expected_benefit="a first order",
            executor="printify_product",
            action="{}",
        )


# --- lessons ----------------------------------------------------------------------------------------------------------


TOOLS = tuple(tools.SPECS)


def test_a_full_lessons_file_drops_tool_limits_before_the_review_s_lesson() -> None:
    lessons = (
        "# Lessons\n\n"
        "- [#c68] Printify blueprint 443 costs: making 6.28 EUR + shipping 5.39 EUR.\n"
        "- [#c89] A decided action that isn't sent the same day costs a full day of runway for nothing.\n"
        "- [#c93] workspace_write caps content at 2,500 chars per call.\n"
        "- [#c96] project_create fails at 8 open projects.\n"
    )
    cap = len(lessons.encode()) - 40
    kept, dropped = memory._drop_oldest(lessons, cap, set(), TOOLS)
    assert dropped == 1 and "workspace_write" not in kept and "A decided action" in kept and "project_create" in kept
    # Without the tools' names, the lesson without a number went first (0.12.0's rule alone).
    before, _ = memory._drop_oldest(lessons, cap, set())
    assert "A decided action" not in before
    # A word that is also a tool's name (research, draft, look) doesn't make a business lesson a tool's.
    assert memory.identifiers(["research", "look", "project_create"]) == {"project_create"}


def test_an_upgrade_marks_the_lessons_about_the_tools_its_notes_name_to_re_check() -> None:
    """0.21.0 (analysis 0.20.1, FIX NOW 14): they were deleted, each upgrade, also "photos must be visibly distinct"."""
    lessons = (
        "# Lessons\n\n"
        "- [#c96] project_create fails at 8 open projects; fold new work into an existing project.\n"
        "- [#c93] workspace_write caps content at 2,500 chars per call.\n"
        "- [#c89] A decided action that isn't sent the same day costs a day of runway.\n"
        "- [#c37] Pinned: project_create a project per product line.\n"
    )
    notes = "## 0.19.1\n- project_create no longer refuses a ninth open project. project_list shows them."
    pinned = {memory.lesson_key("Pinned: project_create a project per product line.")}
    new, marked, retired = memory.recheck_for_release(lessons, notes, TOOLS, pinned, "0.19.1") or ("", [], [])
    assert marked == ["project_create fails at 8 open projects; fold new work into an existing project."]
    assert retired == []
    assert (
        "- [#c96] (re-check: 0.19.1 changed project_create) project_create fails at 8 open projects; fold new work"
        " into an existing project.\n" in new
    )
    assert "workspace_write" in new and "A decided action" in new and "- [#c37] Pinned: project_create" in new
    assert memory.recheck_for_release(lessons, "## 0.19.2\n- research is cheaper.", TOOLS, set(), "0.19.2") is None
    again, _, _ = memory.recheck_for_release(new, notes, TOOLS, pinned, "0.20.0") or ("", [], [])
    assert "(re-check: 0.20.0 changed project_create) project_create fails" in again and "0.19.1" not in again
    # A marked lesson still is itself: for duplicates and pins (0.21.0, the pre-release review)
    marked_line = next(line for line in again.splitlines() if "(re-check:" in line)
    assert memory.lesson_key(marked_line) == memory.lesson_key("- [#c1] project_create fails at 8 open projects;"
                                                               " fold new work into an existing project.")  # fmt: skip
    assert memory.lesson_text(marked_line).startswith("project_create fails")


def test_a_full_lessons_file_gets_short_marks_and_says_what_it_retired() -> None:
    """0.21.0 (pre-release review): on a full file the marks deleted the oldest marked lessons, a pinned one marked by
    an earlier upgrade too, and the event said they were only marked. Now: shorter marks first; only the oldest
    unpinned ones the marks have no room for are retired, and said so."""
    notes = "## 0.21.0\n- project_create changed."
    pinned_line = "- [#c0] (re-check: 0.20.0 changed project_create) project_create: keep one per product line."
    roomy = "# Lessons\n\n" + pinned_line + "\n- [#c99] project_create needs a title.\n"
    for i in range(1, 100):  # filled to within a long mark of the cap, with room for a short one
        line = f"- [#c{i}] Lesson {i} about prices: {'x' * 20}\n"
        if len((roomy + line).encode()) > memory.CAPS["lessons"] - 15:
            break
        roomy += line
    text, marked, retired = memory.recheck_for_release(
        roomy, notes, TOOLS, {memory.lesson_key(pinned_line)}, "0.21.0"
    ) or ("", [], [])
    assert len(roomy.encode()) <= memory.CAPS["lessons"] < len(roomy.encode()) + 45  # a long mark doesn't fit
    assert "- [#c99] (re-check) project_create needs a title.\n" in text and retired == []
    assert len(text.encode()) <= memory.CAPS["lessons"]
    assert marked == ["project_create needs a title."] and pinned_line in text
    many = (
        "# Lessons\n\n"
        + pinned_line
        + "\n"
        + "".join(f"- [#c{i}] project_create note {i}: {'x' * 40}\n" for i in range(1, 60))
    )
    many = many[: many.rindex("\n", 0, memory.CAPS["lessons"]) + 1]  # just within the cap
    text, marked, retired = memory.recheck_for_release(
        many, notes, TOOLS, {memory.lesson_key(pinned_line)}, "0.21.0"
    ) or ("", [], [])
    assert len(text.encode()) <= memory.CAPS["lessons"] and pinned_line in text  # the pinned one stays
    assert retired and retired[0].startswith("project_create note 1:") and len(marked) + len(retired) >= 50


def test_the_first_cycle_after_an_upgrade_marks_them_once(data_dir: Path) -> None:
    def before(agent: Any) -> None:
        agent.db.set_meta(news.changelog_key(agent.scope().mode), "0.19.0")
        lessons = "# Lessons\n\n- [#c96] project_create fails at 8 open projects.\n- [#c9] Keep the owner informed.\n"
        with agent.db.transaction() as conn:
            agent.memory().rewrite(conn, "lessons", lessons, "consolidation", "2026-09-01T08:00:00Z")

    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), before=before)
    text = agent.memory().read("lessons")
    assert "] (re-check: " in text and "changed project_create) project_create fails" in text
    assert "Keep the owner informed." in text
    events = [e["message"] for e in agent.db.recent_events(30)]
    assert any(e.startswith("Ember's code marked 1 lesson(s) to re-check: tools ") for e in events)
    assert agent.db.get_meta(f"agent.{agent.scope().mode}.lessons_version")


# --- the daily review's roadmap ------------------------------------------------------------------------------------


def test_the_review_scorecard_shows_the_roadmap_before_the_projects(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    scope = agent.scope()
    now = "2026-09-01T12:00:00Z"
    with agent.db.transaction() as conn:
        for number in range(12):
            store.create_project(
                conn,
                scope,
                cycle_id=1,
                title=f"Product line {number} " + "x" * 50,
                hypothesis="h" * 400,
                next_step="n" * 150,
                status="active",
                now=now,
            )
        roadmap.create(conn, scope, title="Day 7: 10 views", measure="views", due="2026-09-20", now=now)
    card = _scorecard(agent)
    assert '\n- due later this month: #4 "Day 7: 10 views" (2026-09-20)' in card.text
    assert card.text.index("ROADMAP") < card.text.index("PROJECTS")


def _scorecard(agent: Any) -> review.Scorecard:
    books, ledger_scope = agent.economy.books, agent.economy.life.scope()
    with agent.db.connection() as conn:
        return review.scorecard(
            conn,
            agent.scope(),
            agent.clock,
            books,
            ledger_scope,
            agent.economy.life.evaluate(),
            dry_run=True,
        )


# --- sheet pictures: results, not formulas -------------------------------------------------------------------------


BUDGET = {
    "title": "Budget Planner",
    "sheets": [
        {
            "name": "Income",
            "title": "Monthly Income",
            "columns": [{"title": "Source"}, {"title": "Planned"}, {"title": "Actual", "format": "usd"}],
            "rows": [["Salary", 2800, 2800]],
            "empty_rows": 8,
            "totals": {"Planned": "sum", "Actual": "sum"},
        },
        {
            "name": "Expenses",
            "title": "Monthly Expenses",
            "columns": [
                {"title": "Category"},
                {"title": "Planned"},
                {"title": "Actual", "format": "usd"},
                {"title": "Remaining", "format": "usd", "formula": "=B{row}-C{row}"},
            ],
            "rows": [["Rent", 850, 850], ["Groceries", 350, 382.5]],
            "empty_rows": 20,
            "totals": {"Planned": "sum", "Actual": "sum"},
        },
        {
            "name": "Summary",
            "title": "Monthly Summary",
            "columns": [{"title": "Item"}, {"title": "Amount", "format": "usd"}],
            "rows": [
                ["Total Income", "=SUM(Income!C4:C12)"],
                ["Total Expenses", "=Expenses!C26"],
                ["Net", "=B4-B5"],
                ["Rent", '=SUMIF(Expenses!A:A,"Rent",Expenses!C:C)'],
                ["Verdict", '=IF(B6>0,"Saving","Overspent")'],
            ],
        },
        {
            "name": "Year Overview",
            "columns": [
                {"title": "Month"},
                {"title": "Income"},
                {"title": "Savings"},
                {"title": "Rate", "format": "percent", "formula": "=IFERROR(C{row}/B{row},0)"},
            ],
            "rows": [["January", 2800, 300], ["March", 0, 0]],
        },
        {
            "name": "Tracker",
            "columns": [{"title": "Company"}, {"title": "Status"}],
            "rows": [["A", "Applied"], ["B", "Interview"], ["C", "applied"]],
        },
        {
            "name": "Counts",
            "columns": [{"title": "Status"}, {"title": "Count", "formula": "=COUNTIF(Tracker!B:B,A{row})"}],
            "rows": [["Applied"], ["Offer"]],
        },
    ],
}


def test_the_sheet_pictures_work_out_iferror_if_countif_sumif_and_other_sheets() -> None:
    spec = sheets.parse(json.dumps(BUDGET), lambda path: "")
    assert spec.warnings == []
    book = sheets._spec_book(spec)
    summary, year, counts = book["summary"], book["year overview"], book["counts"]
    assert [summary.cell(i, 1) for i in range(5)] == [2800.0, 1232.5, 1567.5, 850.0, "Saving"]
    assert [year.cell(i, 3) for i in range(2)] == [300 / 2800, 0.0]  # IFERROR: a division by zero is 0
    assert [counts.cell(i, 1) for i in range(2)] == [2.0, 0.0]  # COUNTIF in any case, as Excel does
    data = sheets.build(spec)
    assert "=IFERROR(C3/B3,0) → 0" in sheets.workbook_text(data)
    number, picture = sheets.picture(data, "Summary")
    assert number == 3 and picture.width > 300
    assert sheets._shown(-32.5, "usd") == "-$32.50" and sheets._shown(32.5, "usd") == "$32.50"


def test_a_formula_that_cant_be_worked_out_is_still_shown_as_written() -> None:
    spec = sheets.parse(
        json.dumps({"sheets": [{"name": "S", "columns": [{"title": "A"}], "rows": [['=TEXT(TODAY(),"yyyy")']]}]}),
        lambda path: "",
    )
    assert sheets._spec_book(spec)["s"].cell(0, 0) == '=TEXT(TODAY(),"yyyy")'


def test_a_range_on_another_sheet_that_misses_its_data_is_reported() -> None:
    budget = json.loads(json.dumps(BUDGET))
    budget["sheets"][2]["rows"][:2] = [
        ["Total Income", "=SUM(Income!C2:C9)"],
        ["Total Expenses", "=SUM(Expenses!C4:C27)"],
    ]
    spec = sheets.parse(json.dumps(budget), lambda path: "")
    assert spec.warnings == [
        "Summary row 4: Income!C2:C9 leaves out some of the data of Income (rows 4 to 12; its total is in row 13): "
        "Income!C4:C12 takes them all",
        "Summary row 5: Expenses!C4:C27 counts the total row of Expenses (row 26) besides its data",
    ]


def test_make_spreadsheet_says_where_the_data_rows_are(tmp_path: Path) -> None:
    from tests.test_products import jail  # noqa: PLC0415

    workspace = jail(tmp_path)
    workspace.write("budget.json", json.dumps(BUDGET))
    made = make.spreadsheet(workspace, "budget.json", "shop/budget.xlsx")
    assert "Data rows: Income 4-12 (total 13), Expenses 4-25 (total 26), Summary 4-8," in " ".join(made.report)


# --- bets ----------------------------------------------------------------------------------------------------------


def test_a_bet_can_be_written_as_ember_shows_it() -> None:
    today = date(2026, 10, 4)
    assert bets.parse("+15 views in 13 days (by 10-17): the SEO terms match", today) == (
        15,
        "views",
        13,
        "the SEO terms match",
    )
    assert bets.parse("+10 views by 2026-10-09 (+2 so far): the blog links them", today)[:3] == (10, "views", 5)
    assert bets.parse("+15 views by 10-17: x", today)[2] == 13
    assert bets.parse("+8 views by 17.10.: x", today)[2] == 13
    assert bets.parse("+5 Etsy listing views in 10 days: posts", today)[:3] == (5, "views", 10)
    assert bets.parse("+3 favourites in 14 days: x", today)[1] == "favorites"
    for wrong in ("+15 views by 2026-10-05: too soon", "+5 views by 13-45: no such day", "+15 views by 10-17: x"):
        with pytest.raises(bets.BetError, match="write it as"):
            bets.parse(wrong, None if wrong.endswith(": x") else today)


def test_a_refused_bet_says_nothing_was_changed(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    ctx = shop_context(agent)
    with agent.db.transaction() as conn:
        project = store.create_project(
            conn,
            agent.scope(),
            cycle_id=1,
            title="Posters",
            hypothesis="h",
            next_step="Old",
            status="active",
            now="2026-09-01T12:00:00Z",
        )
    refused = call(ctx, "project_update", {"project_id": project, "next_step": "New", "bet": "more views soon"})
    assert not refused.ok and "nothing was changed, so send the update again" in refused.text
    assert rows(agent, f"SELECT next_step FROM projects WHERE id = {project}")[0]["next_step"] == "Old"


# --- the model's markup inside a text ------------------------------------------------------------------------------


def test_a_journal_s_next_written_inside_its_entry_is_kept_as_its_next(data_dir: Path) -> None:
    leaked = ToolCalls(
        [
            (
                "write_journal",
                {
                    "summary": "Research done",
                    "entry": 'It went well.</entry>\n<parameter name="next">Check the views on 10-07.</parameter>\n',
                },
            )
        ]
    )
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=["research"]), Reply("Done."), leaked]))
    [journal] = rows(agent, "SELECT entry, handoff FROM journal")
    assert (journal["entry"], journal["handoff"]) == ("It went well.", "Check the views on 10-07.")


def test_markup_naming_no_field_of_the_tool_is_left_alone() -> None:
    spec = tools.SPECS["write_journal"]
    raw = {"summary": "S", "entry": 'a <parameter name="title">x'}
    assert tools.unleaked(spec, raw, []) == raw


# --- a listing joins the right product line ------------------------------------------------------------------------


def test_a_focus_project_of_another_venture_doesnt_take_the_cycles_listing(data_dir: Path) -> None:
    from tests.test_etsy import listed as etsy_listed  # noqa: PLC0415

    agent, _ = etsy_listed(data_dir)
    ctx = shop_context(agent)
    with agent.db.connection() as conn:
        project = conn.execute("SELECT project_id FROM approvals WHERE executor = 'etsy_listing'").fetchone()[0]
        venture = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project,)).fetchone()[0]
    assert venture is not None
    ctx.state.focus_project_id, ctx.state.focus_venture_id = project, venture + 100
    with agent.db.connection() as conn, pytest.raises(tools.ToolError) as refused:
        tools._product_line(ctx, conn, {}, "listing")
    assert f"your focus project #{project} belongs to venture #{venture}, and this cycle works on venture" in str(
        refused.value
    )
    ctx.state.focus_venture_id = venture
    with agent.db.connection() as conn:
        assert tools._product_line(ctx, conn, {}, "listing") == project


# --- small things --------------------------------------------------------------------------------------------------


def test_a_subtitles_parts_begin_lines_of_their_own() -> None:
    draw = Image.new("RGB", (10, 10))
    from PIL import ImageDraw  # noqa: PLC0415

    font = images._font("sans", "", 40)
    lines = images._wrap(ImageDraw.Draw(draw), "Unlimited clients\nNo resale of files\nPlain English", font, 2000)
    assert lines == ["Unlimited clients", "No resale of files", "Plain English"]
    photo = images.listing([Image.new("RGB", (800, 1100), "white")], "Bundle", "One\nTwo\nThree\nFour\nFive")
    assert Image.open(io.BytesIO(photo)).size == images.SHAPES["landscape"]


def test_the_code_word_pin_is_capitalised() -> None:
    masker = privacy.Masker()
    assert masker('"shape":"pin","subtitle":"Word Template · DIN 5008"').endswith('DIN 5008"')
    assert masker("Your PIN is 4821") == "Your PIN is [masked]"


def test_the_sleep_ember_s_code_cut_keeps_the_agent_s_choice(data_dir: Path) -> None:
    from app.agent import loop  # noqa: PLC0415

    end = loop.CycleEnd("completed", sleep_minutes=180, sleep_reason="Nothing until 10-07.")
    end.asked_minutes, end.sleep_cut = 360, "Ember's code cut it to 180 min: READY lists useful work"
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    agent._after("schedule", end)
    assert agent.db.get_meta(agent._key("next_wake_reason")) == (
        'Ember chose 360 min: "Nothing until 10-07."; Ember\'s code cut it to 180 min: READY lists useful work'
    )
    assert loop._comes("2 messages of your owner's to answer") == "come"
    assert loop._comes("1 message of your owner's to answer") == "comes"
    assert loop._comes("obligation #14 (decision)") == "comes"


def test_a_long_reason_is_cut_not_refused(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    made = a_post(agent, ctx, text="Planer.", reason="r" * 400)
    assert made.ok and "reason was cut to 300 of its 400 characters" in made.text
    assert bluesky.TEXT_MAX == 300 and FakeBluesky.HANDLE
    assert etsy.listing_url(1)


def test_a_project_leaving_a_backed_venture_says_its_test_needs_one(data_dir: Path) -> None:
    from tests.test_fixes_0161 import backed_pod  # noqa: PLC0415

    agent, pod = backed_pod(data_dir)
    ctx = shop_context(agent)
    with agent.db.transaction() as conn:
        conn.execute(f"UPDATE projects SET venture_id = NULL WHERE venture_id = {pod}")
        first, second = (
            store.create_project(
                conn,
                agent.scope(),
                cycle_id=1,
                title=title,
                hypothesis="h",
                next_step="n",
                status="active",
                now="2026-09-01T12:00:00Z",
                venture_id=pod,
            )
            for title in ("Posters", "Mugs")
        )
    closed = call(ctx, "project_update", {"project_id": first, "status": "abandoned", "note": "No demand."})
    assert closed.ok and "no open project now" not in closed.text
    last = call(ctx, "project_update", {"project_id": second, "status": "abandoned", "note": "No demand."})
    assert last.ok and last.text.endswith(
        f"Venture #{pod} ({ventures_title(agent, pod)}) has no open project now: its test needs one."
    )


def ventures_title(agent: Any, venture_id: int) -> str:
    return str(rows(agent, f"SELECT title FROM ventures WHERE id = {venture_id}")[0]["title"])
