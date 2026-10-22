"""0.42.1 (plan holds): the owner's hold on files too, a rework Ember can decline, a paid test in the plan
(analysis-0.40.1 codebase, 6.2).

1. Your hold, on files too. The owner's hold of a line (and since 0.40.2 a review's stop) was kept only where a call
   names a project or a listing (tools._line): draft, the workshop, the make_ tools, workspace_write, a request without
   a project, a blog post recommending no listing and an email could still spend money on a held line. Now a call that
   writes a held line's file, makes one from it or pays a model to build on it is refused, and so is a request or an
   email naming one of its listings; reading is free, and so is a file of no line or of a line nothing holds. A hold
   Ember put on a line herself refuses nothing, as before.
2. A rework Ember can decline. A request the owner rejected stays urgent until it is proposed again (0.40.1), and a
   wait or a hold of the whole line were Ember's only ways out. plan_step decline closes the step with why (the owner
   sees it), no step proposes that request again, and the owner's "Rework it anyway" opens it again, pinned; a later
   request they reject gets a step again.
3. A paid test in the plan. A change of a listing in the owner's running paid test (the critic's fix, a listing edit)
   waits ('paid') until the test ends, as propose_etsy_edit refuses it (preconditions.PAID); the line's keep-or-drop
   dates are read once it ends.

Also tests from 0.39.8 (formerly test_fixes_0398.py)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import paid, plan, store, tools, weights, workfiles  # noqa: E402
from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.agent_helpers import JOURNAL, PLAN, rows  # noqa: E402
from tests.api_helpers import post  # noqa: E402
from tests.etsy_helpers import call, shop_context  # noqa: E402
from tests.learning.test_weekly_look import judged  # noqa: E402
from tests.plan.test_cycle_line import cycle, lined, now, working  # noqa: E402
from tests.plan.test_marketing_order import CRITIC, critic, launched  # noqa: E402
from tests.plan.test_owner_holds import LATER, hold, product_of  # noqa: E402
from tests.plan.test_plan_tree import BOOK, TRACKER, keep, project  # noqa: E402
from tests.plan.test_review_in_the_plan import held  # noqa: E402
from tests.plan.test_step_brake import asked, candidate, closed_without_proposing, decided  # noqa: E402
from tests.plan.test_tree_steers import plan_step, steered, step_id  # noqa: E402
from tests.tools.test_workshop import workshop_answer  # noqa: E402

# --- 1. your hold, on files too ---


BOOK_FILE = "books/haushaltsbuch.md"


def filed(agent: Agent, line: int | None, path: str, text: str = "Notes") -> None:
    """A text file written in a cycle on product line ``line`` (None: on none): filed under it (workfiles.py)."""
    made = call(working(agent, line), "workspace_write", {"path": path, "mode": "create", "content": text})
    assert made.ok, made.text


def test_the_owners_hold_refuses_what_writes_or_builds_on_its_lines_files_not_what_reads_them(data_dir: Path) -> None:
    """draft, the workshop, the make_ tools, workspace_write and a request without a project named no project, so the
    owner's hold of the book reached none of them: a draft or a workshop run paid for on its manuscript, a PDF made of
    it, a request to publish it."""
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    filed(agent, book, BOOK_FILE, "# Haushaltsbuch 2027\n\nJanuar")
    filed(agent, tracker, "shop/tracker.md")
    filed(agent, None, "notes/ideas.md")
    with agent.db.connection() as conn:
        found = workfiles.lines_of(conn, agent.scope(), [BOOK_FILE, "shop/tracker.md", "notes/ideas.md"])
    assert found == {BOOK_FILE: book, "shop/tracker.md": tracker}
    hold(agent, book)
    ctx = working(agent, tracker)
    ctx.draft = lambda *_: pytest.fail("a draft building on a held line's file was paid for")
    ctx.workshop = lambda *_: pytest.fail("a workshop run on a held line's file was paid for")
    request = {
        "type": "publish", "title": "Publish the book", "description": "The interior is done.",
        "payload": "The interior: books/haushaltsbuch.md.", "expected_cost": "nothing", "expected_benefit": "sales",
    }  # fmt: skip
    sources = "shop/tracker.md, books/haushaltsbuch.md"
    refused = {
        "workspace_write": ({"path": BOOK_FILE, "mode": "append", "content": "Feb"}, "change to its files"),
        "draft": ({"path": "drafts/more.md", "brief": "The months", "sources": sources}, "draft"),
        "workshop": ({"task": "A chart of the months", "files": f"shop/tracker.md,{BOOK_FILE}"}, "workshop run"),
        "make_document": ({"source": BOOK_FILE, "output": "shop/haushaltsbuch.pdf"}, "document"),
        "request_approval": (request, "request"),
    }
    for name, (args, what) in refused.items():
        said = call(ctx, name, args)
        assert not said.ok, name
        assert (
            f"{BOOK_FILE} is a file of product line #{book}, and your owner holds product line #{book}"
            f" ({LATER}): no {what} for it until they resume it" in said.text
        ), said.text
    assert not ctx.workspace.exists("shop/haushaltsbuch.pdf")
    assert not rows(agent, "SELECT id FROM approvals")
    read = call(ctx, "workspace_read", {"path": BOOK_FILE})
    assert read.ok and "Januar" in read.text  # reading is free
    for path in ("shop/tracker.md", "notes/ideas.md"):  # a line nothing holds, and none
        assert call(ctx, "workspace_write", {"path": path, "mode": "append", "content": "more"}).ok
    with agent.db.transaction() as conn:
        plan.owner_resume(conn, agent.scope(), product_of(agent, book), "Owner", now(agent))
    resumed = call(ctx, "workspace_write", {"path": BOOK_FILE, "mode": "append", "content": "Feb"})
    assert resumed.ok, resumed.text


def test_a_workshop_task_naming_a_held_lines_file_in_its_folder_is_refused_before_it_is_paid_for(
    data_dir: Path,
) -> None:
    """A file a workshop task names without a folder is made in the run's folder: "Summarise haushaltsbuch.md" with
    folder books would replace the held book's manuscript (0.40.3's T2 case), and the hold read only the bare name."""
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    filed(agent, book, BOOK_FILE)
    filed(agent, None, f"{tools.WORKSHOP_FOLDER}/chart.md")
    hold(agent, book)
    ctx = working(agent, tracker)
    ran: list[str | None] = []
    ctx.workshop = lambda _task, _files, _script, folder: ran.append(folder) or tools.Outcome(True, "Made.", "made")
    task = {"task": "Summarise haushaltsbuch.md for the blurb", "folder": "books/"}
    said = call(ctx, "workshop", task)
    assert not said.ok and f"{BOOK_FILE} is a file of product line #{book}, and your owner holds" in said.text
    assert ran == []
    assert call(ctx, "workshop", {**task, "folder": "drafts"}).ok  # its own file there, of no line
    assert call(ctx, "workshop", {"task": "Redo chart.md"}).ok  # workshop/out/chart.md is of no line
    assert ran == ["drafts", None]


def test_a_workshop_run_keeps_no_file_of_a_held_line_that_its_task_didnt_name(data_dir: Path) -> None:
    """The sandbox names what a run makes: a task that named no file could still bring back books/haushaltsbuch.md
    for the held book. The file isn't kept, and the run's report says why; its other files are."""
    agent, fake = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    filed(agent, book, BOOK_FILE, "# Haushaltsbuch 2027\n\nJanuar")
    hold(agent, book)
    with agent.db.transaction() as conn:  # the cycle filed() wrote in has ended
        conn.execute("UPDATE cycles SET status = 'completed', ended_at = ? WHERE status = 'running'", (now(agent),))
    ids = [fake._new_file(name, b"# Stub\n", True) for name in ("haushaltsbuch.md", "months.md")]
    run = ("workshop", {"task": "A table of the months", "folder": "books"})
    fake.script.extend([Plan(PLAN), ToolCalls([run]), workshop_answer(ids), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    [made] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'workshop'")
    assert "Kept: books/months.md" in made["result"], made["result"]
    assert (
        f"Not kept: haushaltsbuch.md: {BOOK_FILE} is a file of product line #{book}, and your owner holds product"
        f" line #{book} ({LATER}): no workshop run for it until they resume it" in made["result"]
    )
    assert agent.roots()[0].read(BOOK_FILE) == "# Haushaltsbuch 2027\n\nJanuar"


def test_a_reviews_stop_holds_its_lines_files_and_listings_too_but_embers_own_hold_doesnt(data_dir: Path) -> None:
    """0.40.2: a review's stop holds its line until the owner keeps it, and Ember can lift it (plan_step resume): the
    tools keep it like the owner's, and say how it lifts. A request for Etsy Ads naming the line's listing and no
    project spent the owner's money on it."""
    agent, line, listing = launched(data_dir)
    filed(agent, line, "shop/planner.md")
    judged(agent, (line, "stop", "demand"))
    keep(agent)
    reason = held(agent, line)["hold_reason"]
    assert reason.startswith(plan.REVIEW_HOLD)
    words = (
        f"product line #{line} is on hold ({reason}): no request for it until your owner keeps it; if its work can't"
        f" wait, lift the hold first (plan_step resume with project_id {line} and why)"
    )
    ctx = working(agent, None)
    spend = {
        "type": "spend_money", "title": "Etsy Ads for a week", "description": f"Please turn on Etsy Ads for #{listing}",
        "payload": "Etsy Ads, 1 EUR a day, 7 days", "expected_cost": "7 EUR", "expected_benefit": "views",
    }  # fmt: skip
    asked_for = call(ctx, "request_approval", spend)
    assert not asked_for.ok and f"listing #{listing} is product line #{line}'s, and {words}" in asked_for.text
    named = call(ctx, "request_approval", {**spend, "description": "Etsy Ads", "project_id": line})
    assert not named.ok and words in named.text  # tools._line, for the call naming it
    edit = call(ctx, "workspace_write", {"path": "shop/planner.md", "mode": "append", "content": "more"})
    assert not edit.ok and "no change to its files for it until your owner keeps it" in edit.text
    with agent.db.transaction() as conn:  # Ember lifts it, and then holds the line herself
        plan.resume(conn, agent.scope(), line, "The owner asked for this first", None, now(agent))
        plan.hold(conn, agent.scope(), line, "Other lines first this week", None, now(agent))
    assert held(agent, line)["hold_by"] == "agent"
    assert call(ctx, "workspace_write", {"path": "shop/planner.md", "mode": "append", "content": "more"}).ok
    made = call(ctx, "request_approval", spend)
    assert made.ok, made.text


# --- 2. a rework Ember can decline ---


def rejected_book(data_dir: Path) -> tuple[Agent, int, int, int]:
    """A KDP book whose package the owner rejected, its decision taken in: (agent, the book's line, the request, the
    step proposing it again)."""
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    package = asked(agent, book, "kdp_package")
    keep(agent)
    decided(agent, package, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    return agent, book, package, step_id(agent, book, f"Propose the book again: request #{package} rejected")


WHY = "Your owner wants a different book: this interior isn't what they want"


def test_ember_declines_a_rework_with_why_the_owner_sees_and_no_step_proposes_it_again(data_dir: Path) -> None:
    """A rejection that means "not this" kept "Propose the book again" urgent until it was proposed again: Ember could
    only wait 14 days at most, a wait of her two a day, or hold the whole line."""
    agent, book, package, step = rejected_book(data_dir)
    again = f"Propose the book again: request #{package} rejected"
    assert candidate(steered(agent), again, book).step.urgency == weights.REWORK
    with agent.db.connection() as conn:
        said = plan.step_text(conn, agent.scope(), steered(agent), explore=False)
    assert f"Your owner rejected request #{package}: propose it again as they asked" in said
    assert "plan_step decline" in said
    [other, *_] = rows(
        agent,
        f"SELECT id FROM plan_nodes WHERE project_id = {book} AND level = 'step' AND status = 'open'"
        f" AND id <> {step} ORDER BY id",
    )
    refused = plan_step(agent, book, action="decline", step_id=other["id"], why=WHY)
    assert not refused.ok and "decline is for a step proposing again a request your owner rejected" in refused.text
    short = plan_step(agent, book, action="decline", step_id=step, why="Not this book")
    assert not short.ok and f"in a sentence of {plan.DECLINE_WORDS} words or more" in short.text
    declined = plan_step(agent, book, action="decline", step_id=step, why=WHY)
    assert declined.ok, declined.text
    assert f"Step #{step} is closed: you won't propose request #{package} again" in declined.text
    result = f"declined request #{package}: {WHY}"
    assert rows(agent, f"SELECT status, closed_by, result FROM plan_nodes WHERE id = {step}") == [
        {"status": "dropped", "closed_by": "agent", "result": result}
    ]
    with agent.db.connection() as conn:  # the owner sees it: the step and its reason, Changes today, the System log
        tab = plan.view(conn, agent.scope(), now(agent), agent.clock.today(), {})
    shown = shown_step(tab, step)
    assert shown["reworkable"] is True and shown["result"] == result
    [change] = [c for c in tab["changes"] if c["id"] == step and c["actor"] == "agent"]
    assert change["action"] == "drop"
    assert change["why"] == f"declined: won't propose request #{package} again: {WHY}"
    assert any(f"Ember declined the rework of plan step #{step}" in e["message"] for e in agent.db.recent_events(5))
    agent.clock.advance(days=1)
    keep(agent)
    assert not rows(agent, "SELECT id FROM plan_nodes WHERE template = 'propose/kdp_package' AND status = 'open'")
    later = asked(agent, book, "kdp_package")  # a later request, which the owner rejects too: a step again
    keep(agent)
    decided(agent, later, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    found = candidate(steered(agent), f"Propose the book again: request #{later} rejected", book)
    assert found.waiting is None and found.step.urgency == weights.REWORK
    with agent.db.connection() as conn:  # the declined one is no longer the owner's to have made
        tab = plan.view(conn, agent.scope(), now(agent), agent.clock.today(), {})
    assert shown_step(tab, step)["reworkable"] is False


def test_only_a_rework_is_declined_and_the_owner_has_it_made_anyway(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    cycle(agent)
    book = project(agent, *BOOK)
    keep(agent)
    first = min(r["id"] for r in rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {book} AND level = 'step'"))
    with agent.db.transaction() as conn, pytest.raises(plan.PlanError, match="decline is for a step proposing again"):
        plan.decline_step(conn, agent.scope(), book, first, WHY, None, now(agent))
    package = asked(agent, book, "kdp_package")
    keep(agent)
    decided(agent, package, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    again = f"Propose the book again: request #{package} rejected"
    step = step_id(agent, book, again)
    assert post(ingress_client, f"api/plan/steps/{step}/rework").status_code == 409  # nothing declined yet
    with agent.db.transaction() as conn:
        plan.decline_step(conn, agent.scope(), book, step, WHY, None, now(agent))
    assert post(ingress_client, f"api/plan/steps/{step}/rework", headers={}).status_code == 403
    reworked = post(ingress_client, f"api/plan/steps/{step}/rework")
    assert reworked.status_code == 200, reworked.text
    new = reworked.json()["step"]
    assert reworked.json() == {"id": step, "line": book, "step": new, "pinned": True}
    [row] = rows(agent, f"SELECT title, status, pinned, source, check_spec FROM plan_nodes WHERE id = {new}")
    assert (row["title"], row["status"], row["pinned"], row["source"]) == (again, "open", 1, "owner")
    assert json.loads(row["check_spec"]) == {"executor": "kdp_package", "since": now(agent)}
    taken = steered(agent)
    assert taken.step is not None and taken.step.id == new  # pinned: the next cycle takes it
    assert candidate(taken, again, book).step.urgency == weights.REWORK
    with agent.db.transaction() as conn, pytest.raises(plan.PlanError, match=f"your owner pinned step #{new}"):
        plan.decline_step(conn, agent.scope(), book, new, WHY, None, now(agent))
    assert post(ingress_client, f"api/plan/steps/{step}/rework").status_code == 409  # once
    keep(agent)
    assert len(rows(agent, "SELECT id FROM plan_nodes WHERE template = 'propose/kdp_package' AND status = 'open'")) == 1
    with agent.db.connection() as conn:
        said = plan.owner_words(conn, agent.scope(), "2000-01-01T00:00:00Z")
    assert any(w.startswith(f'added #{new} "{again}": ') and "made anyway" in w for w in said), said
    assert post(ingress_client, "api/plan/steps/999999/rework").status_code == 404
    script = (Path(plan.__file__).parent.parent / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "planReworkButton(st)," in script and '"/rework"' in script


def shown_step(tab: dict[str, Any], step: int) -> dict[str, Any]:
    """A step as the owner's Plan tab shows it."""
    products = [q for p in tab["projects"] for q in p["products"]]
    return next(s for q in products for stage in q["stages"] for s in stage["steps"] if s["id"] == step)


# --- 3. a paid test in the plan ---


def test_a_change_of_a_listing_in_the_owners_paid_test_waits_for_its_end_as_the_tool_refuses_it(data_dir: Path) -> None:
    """Live, the owner's Etsy Ads test ran on the coach bundle from 10-16 to 10-23: STATUS said to leave it alone, but
    the critic's fix for it was ready, and a cycle spent one of Ember's two waits a day to put it off."""
    agent, line, listing = launched(data_dir)
    critic(agent, line, listing, 3)  # a defect: the critic's fix is urgent
    assert candidate(steered(agent), CRITIC, line).waiting is None
    request_id = ads(agent, listing)
    keep_paid(agent)
    ends = agent.clock.today() + timedelta(days=7)
    said = (
        f"listing #{listing} is in your owner's paid test (request #{request_id}): leave it as it is until the test"
        f" ends on {ends:%m-%d}, then judge it and change it"
    )
    fix = candidate(steered(agent), CRITIC, line)
    assert (fix.waiting, fix.refused) == ("paid", said)
    ctx = shop_context(agent)
    change = {"listing_id": listing, "title": "Bewerbungs-Tracker: alle Bewerbungen im Blick", "reason": "The critic"}
    refused = call(ctx, "propose_etsy_edit", change)
    assert not refused.ok and said in refused.text
    agent.clock.advance(days=8)
    keep_paid(agent)
    keep(agent)
    assert candidate(steered(agent), CRITIC, line).waiting is None
    made = call(shop_context(agent), "propose_etsy_edit", change)
    assert made.ok, made.text


def test_a_lines_keep_or_drop_dates_wait_for_the_end_of_the_owners_paid_test(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)  # live from today
    agent.clock.advance(days=5)
    ads(agent, listing)  # 7 days, to day 12
    keep_paid(agent)
    agent.clock.advance(days=3)  # day 8: past day 7
    assert not [s for s in keep(agent) if "on day 7" in s]
    assert "day7" not in decide_by(agent, line)
    agent.clock.advance(days=5)  # day 13: the test ended on day 12
    assert [s for s in keep(agent) if f"line #{line} has" in s and "on day 7" in s]
    assert decide_by(agent, line)["day7"] == "missed"


def decide_by(agent: Agent, line: int) -> dict[str, str]:
    [row] = rows(agent, f"SELECT decide_by FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    return dict(json.loads(row["decide_by"] or "{}"))


# --- From 0.39.8 (formerly test_fixes_0398.py) ---


# --- 5. the owner's paid test is tracked ---


def ads(agent: Agent, listing: int, status: str = "approved_with_changes", named: bool = True) -> int:
    """The owner's yes to a spend request naming ``listing`` (or none), as #128 was approved: "done starting from
    now"."""
    stamp = now(agent)
    words = f"#{listing} - Coach bundle (4 views)" if named else "the shop's best listings"
    with agent.db.transaction() as conn:
        found = store.insert_approval(
            conn,
            agent.scope(),
            1,
            stamp,
            type="spend_money",
            title="Enable Etsy Ads: €1/day for 7 days on top 3 listings",
            description=f"Please turn on Etsy Ads for these listings only, at a €1/day budget: {words}",
            payload="Etsy Ads, €1/day budget, 7 days, ~€7 total.",
            expected_cost="€7",
            expected_benefit="views",
        )
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decision_comment = 'done starting from now' WHERE id = ?",
            (status, stamp, found),
        )
    return found


def keep_paid(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return paid.keep(conn, agent.scope(), agent.clock)


def test_a_paid_tests_days_come_from_its_words() -> None:
    assert paid.days_named("Enable Etsy Ads: €1/day for 7 days on top 3 listings") == 7
    assert paid.days_named("a 14-day test at the minimum budget") == 14
    assert paid.days_named("Etsy Ads 10 Tage lang") == 10
    assert paid.days_named("Etsy Ads on the coach bundle") == paid.DEFAULT_DAYS


def test_the_owners_yes_to_a_spend_on_embers_listings_is_a_test_shown_in_status_until_it_is_judged(
    data_dir: Path,
) -> None:
    agent, _, listing = launched(data_dir)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 4 WHERE listing_id = ?", (listing,))
    request_id = ads(agent, listing)
    said = keep_paid(agent)
    start, ends = agent.clock.today(), agent.clock.today() + timedelta(days=7)
    assert said == [f"Request #{request_id} is a paid test of #{listing} from {start:%m-%d} to {ends:%m-%d}."]
    assert keep_paid(agent) == []  # once
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 9 WHERE listing_id = ?", (listing,))
    running = "+5 views (4 to 9) and +0 favorites (0 to 0) on its 1 listing so far"
    with agent.db.connection() as conn:
        assert running in paid.status_text(conn, agent.scope(), agent.clock.today())
    assert f"Paid test (your owner's spend, request #{request_id})" in agent.planner_preview()  # in STATUS
    agent.clock.advance(days=8)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 20 WHERE listing_id = ?", (listing,))
    [ended] = keep_paid(agent)
    gained = "+16 views (4 to 20) and +0 favorites (0 to 0) on its 1 listing"
    assert ended == f"The paid test of request #{request_id} ended: {gained}."
    with agent.db.connection() as conn:
        shown = paid.status_text(conn, agent.scope(), agent.clock.today())
    assert f"ended {ends:%m-%d}" in shown and "Judge it now" in shown and "in 7 days" in shown
    agent.clock.advance(days=paid.SHOWN_DAYS + 1)
    with agent.db.connection() as conn:
        assert paid.status_text(conn, agent.scope(), agent.clock.today()) == ""


def test_a_spend_that_names_no_listing_of_embers_is_no_paid_test(data_dir: Path) -> None:
    agent, _, listing = launched(data_dir)
    ads(agent, listing, named=False)
    with agent.db.transaction() as conn:
        store.insert_approval(
            conn, agent.scope(), 1, now(agent), type="spend_money", title="Ads", description="#123456789 is not ours",
            payload="x", expected_cost="€1", expected_benefit="views",
        )  # fmt: skip
    assert keep_paid(agent) == []
    assert not rows(agent, "SELECT id FROM paid_tests")
