"""0.33.0: what the diagnostics of 2026-10-07 (0.31.0, cycles #124-#135) showed: twelve cycles on eight things.

A Haushaltsbuch KDP book was promised to the owner three times ("today's focus") and never proposed: a promise had no
line, so no cycle was taken for it, and the cycle two into the book went to a resume line's missed day-7 bar. Four more
misses of 2026-10-07 made every cycle an ordinary one for two days, the day Pinterest's Standard access came, and took
their lines one by one for edits of titles and tags their own review had just called fine; those edits counted as the
reach that would have parked the lines on day 14 though nobody had been brought to them. 0.35.0: READY, the listing
test's bars and the marketing share retired: the plan tree takes each cycle's step (tests/test_fixes_0350.py).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import obligations, reach, store, tools, ventures  # noqa: E402
from app.agent import plan as plan_tree  # noqa: E402
from app.agent.fake_llm import FakeTransport, Overrun, Reply, ToolCalls, request_kind  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.metering import WORKSHOP  # noqa: E402
from tests import test_fixes_0140_money_guard, test_fixes_0320  # noqa: E402
from tests.economy_helpers import FakeClock, make_economy  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, started  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, milestone, now, take, texts, working  # noqa: E402
from tests.test_fixes_0300 import judged  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_obligations import forcing  # noqa: E402
from tests.test_ventures import JOURNAL  # noqa: E402


def day(agent: Any, days: int) -> str:
    return (agent.clock.today() + timedelta(days=days)).isoformat()


# --- a promise names its line, which comes first ---


def test_a_promise_names_its_line_which_takes_the_cycle_when_due(data_dir: Path) -> None:
    agent, _ = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist
    cycle(agent, project=2)  # line #2 is in progress
    ctx = working(agent)
    refused = call(ctx, "message_owner", {"text": "About the checklist.", "project_id": 3})
    assert not refused.ok and "give commits and due with it" in refused.text
    with agent.db.transaction() as conn:
        store.update_project(conn, 1, now(agent), status="abandoned")
    closed = call(
        ctx, "message_owner", {"text": "x", "commits": "Send the planner", "due": day(agent, 1), "project_id": 1}
    )
    assert not closed.ok and "project #1 isn't one of your open projects" in closed.text
    made = call(
        ctx,
        "message_owner",
        {
            "text": "The book comes tomorrow.",
            "commits": "Propose the checklist book",
            "due": day(agent, 1),
            "project_id": 3,
        },
    )
    assert made.ok and f"due {day(agent, 1)} on line #3" in made.text
    [promise] = rows(agent, "SELECT id, project_id FROM obligations WHERE kind = 'promise'")
    assert promise["project_id"] == 3
    assert ctx.state.focus_project_id is None  # a promise is often about another line than the cycle's: no lock
    with agent.db.connection() as conn:
        owed = [o for _, o in obligations.pressing_owed(conn, agent.scope(), agent.clock.today())]
        listed = obligations.text(conn, agent.scope(), agent.clock.today())
    assert owed == [obligations.Owed(3, forces=True)]  # due tomorrow: it presses, on its line
    assert f"- [line #3] #{promise['id']} promise to your owner" in listed
    with agent.db.transaction() as conn:  # 0.35.0: a step of line #3's in the plan tree
        plan_tree.keep(conn, agent.scope(), now(agent), agent.clock.today(), {})
    assert rows(agent, f"SELECT project_id FROM plan_nodes WHERE obligation_id = {promise['id']}") == [
        {"project_id": 3}
    ]
    agent.clock.advance(days=1)  # due today: line #3's promise weighs most (0.37.0: by its urgency; first until then)
    with agent.db.transaction() as conn:
        steered = plan_tree.steer(conn, agent.scope(), now(agent), agent.clock.today(), {})
    assert steered.step is not None and steered.step.product == 3 and steered.pick.decided == "weight"
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="is fixed"):
        conn.execute(f"UPDATE obligations SET project_id = 2 WHERE id = {promise['id']}")


def test_a_repeated_promise_gives_the_earlier_one_its_line_once(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    first = {"text": "Soon.", "commits": "Propose the Haushaltsbuch 2027 KDP book", "due": day(agent, 3)}
    assert call(ctx, "message_owner", first).ok
    again = {"text": "Still.", "commits": "Send the Haushaltsbuch 2027 KDP proposal", "due": day(agent, 3)}
    said = call(ctx, "message_owner", {**again, "project_id": 1})
    assert said.ok and ", now on line #1): no new obligation" in said.text
    assert rows(agent, "SELECT project_id FROM obligations WHERE kind = 'promise'") == [{"project_id": 1}]
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="only a promise"):
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, approval_id, project_id)"
            " VALUES ('dry_run', 0, 'decision', 'x', '2026-01-01', 'x', NULL, 1)"
        )


# --- a miss is ranked, not forced ---


def test_a_miss_neither_decides_the_cycle_nor_takes_its_line(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    missed = milestone(agent, 1, -1, "Ten views of the planner")
    stamp = now(agent)
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, milestone_id)"
            " VALUES (?, ?, 'miss', 'decide', ?, ?, ?)",
            (agent.scope().mode, agent.scope().session, stamp[:10], stamp, missed),
        )
    cycle(agent, project=2)  # line #2 is in progress
    with agent.db.connection() as conn:
        owed = [o for _, o in obligations.pressing_owed(conn, agent.scope(), agent.clock.today())]
        assert forcing(conn, agent.scope(), agent.clock.today()) == []  # nothing of the owner's
    assert owed == [obligations.Owed(1)]  # pressing, on line #1, but not the owner's
    with agent.db.transaction() as conn:  # 0.35.0: the plan tree weighs its steps; nothing is taken first for it
        plan_tree.keep(conn, agent.scope(), now(agent), agent.clock.today(), {})
        steered = plan_tree.steer(conn, agent.scope(), now(agent), agent.clock.today(), {})
    assert steered.pick.decided in ("weight", "margin")


# --- reach that brings visitors ---


def test_listing_edits_are_reach_but_not_the_visitors_a_fair_test_needs(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, project = started(data_dir)
    real = reach.funnels

    def edited(conn: Any, scope: Any) -> dict[int, reach.Funnel]:
        found = real(conn, scope)
        found[project].edits = 5  # five edits of its titles and tags, nothing else
        found[project].views, found[project].favorites = 20, 0  # short of day 14's 30 views and 2 favorites
        return found

    monkeypatch.setattr(reach, "funnels", edited)
    with agent.db.connection() as conn:
        funnel = reach.funnels(conn, agent.scope())[project]
    assert (funnel.reach, funnel.traffic) == (5, 0)
    assert "less than the 3 a fair test needs (not edits)" in funnel.text()
    agent.clock.advance(days=14)
    with agent.db.transaction() as conn:  # 0.35.0: day 14's decide-by date (the listing test's bars until 0.34.0)
        happened = plan_tree.keep(conn, agent.scope(), now(agent), agent.clock.today(), {})
    said = "\n".join(happened)
    assert f"Plan tree: line #{project} on day 14: 20 views, 0 favorites, 0 orders after 0 reach actions" in said
    assert "too little reach to judge it: one more try" in said  # not the owner's decision
    [product] = rows(agent, f"SELECT decide_by FROM plan_nodes WHERE level = 'product' AND project_id = {project}")
    assert '"day14": "retry"' in product["decide_by"]


# --- the review hears which channels are ready ---


def test_the_daily_review_hears_which_channels_are_ready(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=test_fixes_0320.EVERYWHERE)
    agent.clock.advance(days=1)
    since = len(fake.sent)
    agent.run_cycle("schedule")
    [sent] = [r for r in list(fake.sent)[since:] if request_kind(r) == "review"]
    card = sent["messages"][0]["content"][0]["text"]
    settings = test_fixes_0320.EVERYWHERE
    assert (
        f"CHANNELS READY (their tools work now): Pinterest: ember-dry-run, at most {settings.pinterest_pins_per_day}"
        f" pins a day; Bluesky: at most {settings.bluesky_posts_per_day} posts a day; Printify: at most"
        f" {settings.printify_products_per_day} products a day." in card
    )
    assert "CHANNELS NOT READY" not in card


# --- what any cycle may keep of another line or venture ---


def test_any_cycle_keeps_another_line_s_records_and_parks_an_idea_its_work_stays_its_own(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    with agent.db.transaction() as conn:
        idea = ventures.create(conn, agent.scope(), title="RSS pins", pitch="p", stage="idea", now=now(agent))
    ctx = working(agent, line=1)
    learned = {"venture_id": idea, "stage": "parked", "note": "an Etsy shop can't be claimed on Pinterest"}
    assert call(ctx, "venture_update", learned).ok
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {idea}") == [{"stage": "parked"}]
    with agent.db.transaction() as conn:
        other = ventures.create(conn, agent.scope(), title="Coaches", pitch="p", stage="idea", now=now(agent))
    refused = call(ctx, "venture_update", {"venture_id": other, "stage": "researching"})
    assert not refused.ok and "a venture cycle's work: this cycle works on product line #1" in refused.text
    kept = call(ctx, "project_update", {"project_id": 2, "note": "Pinterest is set up: pin the poster"})
    assert kept.ok and ctx.state.focus_project_id == 1
    bet = call(ctx, "project_update", {"project_id": 2, "bet": "+5 views in 7 days: pins"})
    assert not bet.ok and "no update for project #2" in bet.text


# --- the work steps see why, the review and the strategy ---


def test_the_work_steps_see_why_the_plan_chose_its_line_the_review_of_it_and_the_strategy(data_dir: Path) -> None:
    agent, fake = lined(data_dir)
    judged(agent, (3, "change", "reach"))  # today's review: "the numbers say so"
    with agent.db.transaction() as conn:
        agent.memory().update(conn, "strategy", "replace", "Bring buyers to what is live first.", 1, now(agent))
    why = "Line #3 owes its book; its pins wait for a marketing cycle."
    with agent.db.transaction() as conn:  # 0.35.0: the plan tree takes the line: the owner's pin of a step of #3's
        plan_tree.keep(conn, agent.scope(), now(agent), agent.clock.today(), {})
        first = plan_tree.nodes(conn, agent.scope(), "level = 'step' AND project_id = 3 AND status = 'open'")[0]
        plan_tree.pin(conn, agent.scope(), first["id"], True, "Stefan", now(agent))
    fake.script.extend([take(3, assessment=why), ToolCalls([("project_list", {})]), Reply("Done.")])
    fake.script.append(JOURNAL)
    agent.run_cycle("schedule")
    brief = texts(fake, "work")[-1]
    assert f"== PLAN ==\nWhy: {why}\nGoal: Work on one line" in brief
    assert "Today's review of it: change (reach): the numbers say so" in brief
    strategy = agent.memory().read("strategy").strip()  # as the cycle's weekly look may have rewritten it
    assert f"== STRATEGY (written by you) ==\n{strategy}" in brief


# --- what the owner says stays ---


def test_an_owner_s_message_can_be_kept_as_a_standing_instruction() -> None:
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "parts.push(keepButton(m), removeButton(num(m.id)))" in script
    # 0.36.0: as a rule of the owner's rulebook (the Mind tab's), in place of a line of the standing instructions
    assert "openRule(null, asText(m.text))" in script and 'text: "Keep as rule"' in script
    assert 'else if (typeof add === "string") box.value = add.trim();' in script


def test_a_message_that_promises_in_words_only_goes_back_once(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    words = {"text": "Next cycle I'll publish the 5 finished pins."}
    back = call(ctx, "message_owner", words)
    assert not back.ok and '("Next cycle")' in back.text and "commits, with due and project_id" in back.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM messages WHERE sender = 'agent'") == [{"n": 0}]
    assert call(
        ctx, "message_owner", {"text": 'You wrote: "check it next cycle". Noted.'}
    ).ok  # a quote promises nothing
    assert call(ctx, "message_owner", words).ok  # sent again unchanged: its words promised nothing after all


# --- the file tools: an edit of every copy, a restore, the workshop's hold ---


WEEK = "| | | | |\n" * 15 + "\nWochensumme: ______\n"


def test_an_edit_changes_every_copy_of_a_passage_when_it_says_how_many(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    book = "# Haushaltsbuch\n\n" + "## Woche\n\n" + "\n## Woche\n\n".join([WEEK] * 3)
    assert call(ctx, "workspace_write", {"path": "books/b.md", "mode": "create", "content": book}).ok
    more = {"path": "books/b.md", "mode": "edit", "find": WEEK, "content": "| | | | |\n" * 22 + WEEK[-21:]}
    once = call(ctx, "workspace_write", more)
    assert not once.ok and "is in books/b.md 3 times: give more of it around" in once.text and "count 3" in once.text
    wrong = call(ctx, "workspace_write", {**more, "count": 4})
    assert not wrong.ok and "3 times, not 4: nothing was changed" in wrong.text
    done = call(ctx, "workspace_write", {**more, "count": 3})
    assert done.ok and "3 passages replaced" in done.text
    assert agent.roots()[0].read("books/b.md").count("| | | | |\n" * 22) == 3
    refused = call(ctx, "workspace_write", {"path": "books/b.md", "mode": "append", "content": "x", "count": 2})
    assert not refused.ok and "count is for mode edit" in refused.text


def test_an_overwrite_or_a_delete_can_be_restored(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    whole = "# Haushaltsbuch 2027\n\n" + "## Januar\n\n" * 200
    workspace = agent.roots()[0]
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "create", "content": whole}).ok
    header = "---\ntitle: Haushaltsbuch 2027\n---\n"
    gone = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "overwrite", "content": header})
    assert gone.ok and "Its text before is kept: restore brings it back." in gone.text  # live: cycle #134
    back = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"})
    assert back.ok and "as it was before the overwrite of " in back.text and workspace.read("books/i.md") == whole
    undone = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"})
    assert undone.ok and workspace.read("books/i.md") == header  # a second restore undoes the first
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "delete"}).ok
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"}).ok
    assert workspace.read("books/i.md") == header
    none = call(ctx, "workspace_write", {"path": "books/new.md", "mode": "restore"})
    assert not none.ok and "no earlier text of books/new.md is kept" in none.text
    kept = rows(agent, "SELECT COUNT(*) AS n FROM workspace_versions WHERE path = 'books/i.md'")[0]["n"]
    assert kept <= tools.VERSIONS_KEPT


def test_a_workshop_run_holds_what_the_day_has_left_never_less_than_its_worst_case(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=4, cycle_spend_cap_usd=1, workshop_run_cap_usd=1.5)
    economy = make_economy(data_dir, settings, clock=FakeClock())
    model = economy.metered(FakeTransport(script=[Overrun(), Overrun()]))
    first = model.open_cycle("test")
    model.call(first, WORKSHOP, test_fixes_0140_money_guard.workshop(settings))  # a run like #423
    model.close_cycle(first)
    cycle = model.open_cycle("test")
    asked = test_fixes_0140_money_guard.workshop(settings)
    full, quote, money = (
        model.reservation(asked, WORKSHOP),
        model.quote(asked, WORKSHOP),
        model.rooms(cycle, WORKSHOP)[1],
    )
    assert quote <= money < full  # 0.32.0 refused it: "the run holds $2.645 of the day, but only $2.237 is left"
    held = model.reservation(asked, WORKSHOP, room=money)
    assert held == money and model.reservation(asked, WORKSHOP, room=1) == quote
    model.call(cycle, WORKSHOP, asked, hold=held)  # admitted: the guard holds what the workshop held
    assert test_fixes_0140_money_guard.llm_calls(economy.db)[-1]["estimate_micros"] == held
