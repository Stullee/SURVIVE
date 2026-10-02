"""0.15.0 (caps-modes): every call obeys the burn mode and the event reserve; STATUS tells the real cap; refuse before
paying.

* Workshop runs, reviews and studies ignored the maintenance cap and the 20:00 event reserve: in a maintenance cycle a
  workshop run could still spend $1.50, six times a day. Now a maintenance cycle's cap bounds every call in it (and
  it offers no workshop), and until 20:00 a scheduled cycle's calls, all of them, leave the event reserve.
* The critic's and the consolidation's calls counted toward the cycle cap, although the notes said "daily cap only".
* STATUS said "This cycle may spend up to $1.00" in maintenance (the cap was $0.40), and a focus-mode venture cycle was
  still told to brainstorm and offered the tool, which then refused. STATUS now says the cap in force and why, and
  the brainstorm is offered only in explore.
* STATUS and the dashboard say when the burn mode moves down next at today's burn.
* A draft append past 64 KB was paid for, then thrown away; a workshop folder four levels deep likewise. Both are
  refused before the call now, and a paid draft that still doesn't fit is kept in a file of its own.
* A long library document went on being studied (and paid for) after its 60 learnings were full.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import context, library, loop, prompts, tools, ventures
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from app.config import Settings
from app.db import discover_migrations, migrate
from app.economy import burn, ledger, metering
from app.economy.metering import CallRefused, Completed
from tests.economy_helpers import START, ScriptedTransport, make_economy, message, metered, request
from tests.test_agent import ROOMY, rows
from tests.test_draft import DRAFT, notes
from tests.test_knowledge_parts import context_of
from tests.test_library import add
from tests.test_loop_shapes import JOURNAL, PLAN, run
from tests.test_owner_loop import owner
from tests.test_review import next_day
from tests.test_roadmap import planner_texts, section
from tests.test_ventures import DROPSHIPPING, VENTURING
from tests.test_ventures import plan as venture_plan

DAY = Settings(starting_balance_usd=50, daily_spend_cap_usd=7, cycle_spend_cap_usd=1, workshop_run_cap_usd=1.5)
SPENT = 5_000_000  # $5 of the $7 day spent before (so a scheduled cycle opens with $0.60 before 20:00)


def spent_before(economy: Any, base: int = SPENT) -> None:
    """The day's spending so far: ``base`` more than the books hold."""
    books = economy.books
    real = books.cap_spend_on
    books.cap_spend_on = lambda scope, day: base + real(scope, day)


def cap_of(economy: Any, cycle_id: int) -> int:
    with economy.db.connection() as conn:
        return int(conn.execute("SELECT cap_micros FROM cycles WHERE id = ?", (cycle_id,)).fetchone()[0])


def work_tools(fake: FakeTransport) -> list[set[str]]:
    return [{t["name"] for t in r["tools"]} for r in fake.sent if request_kind(r) in ("work", "reflect")]


def briefs(fake: FakeTransport) -> list[str]:
    return [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == "work"]


# --- X2: the critic and the consolidation count toward the daily cap only ---


@pytest.mark.parametrize("purpose", [metering.CRITIC, metering.CONSOLIDATE])
def test_a_critic_or_a_consolidation_leaves_the_cycle_cap_alone(data_dir: Path, purpose: str) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=0.5))
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 20_000))]))
    cycle = model.open_cycle("owner")
    assert model.rooms(cycle, "work")[0] == 500_000
    result = model.call(cycle, purpose, request(max_tokens=20_000))
    assert result.cost_micros == 2_000 + 200_000
    assert model.rooms(cycle, "work")[0] == 500_000  # before 0.15.0: $0.298 after a $0.20 critic
    assert economy.books.cycle_spend(cycle, outside_cap=False) == (0, 0)
    assert economy.books.cycle_spend(cycle)[0] == result.cost_micros  # the cycle's reports still show it
    assert model.rooms(cycle, purpose)[0] == 10_000_000  # its own cap is the daily cap, like the review's
    assert metering.OUTSIDE_CYCLE_CAP is ledger.OUTSIDE_CYCLE_CAP  # one list for the guard and the books
    assert set(ledger.OUTSIDE_CYCLE_CAP) == {
        metering.WORKSHOP,
        metering.REVIEW,
        metering.STUDY,
        metering.CONSOLIDATE,
        metering.CRITIC,
    }


# --- X1: the event reserve binds every call of a scheduled cycle until 20:00 ---


def test_a_scheduled_cycles_calls_outside_the_cycle_cap_leave_the_event_reserve(data_dir: Path) -> None:
    economy = make_economy(data_dir, DAY)
    spent_before(economy)
    model, transport = metered(economy)
    cycle = model.open_cycle("schedule")
    assert cap_of(economy, cycle) == 600_000  # $7 - $5 - the $1.40 kept for event wake-ups
    for purpose in (metering.WORKSHOP, metering.REVIEW, metering.STUDY, metering.CRITIC):
        assert model.rooms(cycle, purpose)[1] == 600_000, purpose  # before 0.15.0: $2.00
    for purpose in (metering.WORKSHOP, metering.REVIEW):
        with pytest.raises(CallRefused) as refused:  # $0.70 at most: it fits its own cap, not the day's rest
            model.call(cycle, purpose, request(max_tokens=70_000))
        assert refused.value.category == "cap"
        assert refused.value.reason.startswith("$1.40 of the daily cap is kept for event wake-ups until 20:00")
    assert transport.sent == []
    # A workshop run holds at least its cap per run (money-guard, FIX 1), so it waits for the evening here.
    model.call(cycle, metering.REVIEW, request(max_tokens=50_000))  # $0.50 at most fits
    # Another trigger's cycle keeps no reserve, and neither does a scheduled one after 20:00.
    model.close_cycle(cycle)
    owners = model.open_cycle("owner")
    assert model.rooms(owners, metering.WORKSHOP)[1] > 1_900_000
    model.close_cycle(owners)
    economy.clock.advance(hours=8, minutes=1)  # 20:01
    evening = model.open_cycle("schedule")
    assert model.rooms(evening, metering.WORKSHOP)[1] > 1_900_000


def test_what_a_call_outside_the_cap_spent_comes_out_of_a_held_cycles_room(data_dir: Path) -> None:
    economy = make_economy(data_dir, DAY)
    spent_before(economy)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 30_000))]))
    cycle = model.open_cycle("schedule")
    assert model.rooms(cycle, "plan")[0] == 600_000
    critic = model.call(cycle, metering.CRITIC, request(max_tokens=40_000))  # before the plan, outside the cap
    assert critic.cost_micros == 302_000
    # Its cost counts toward the day, so the cycle's own calls have only what the reserve leaves of the day's rest.
    assert model.rooms(cycle, "plan")[0] == 600_000 - 302_000
    with pytest.raises(CallRefused, match="kept for event wake-ups"):
        model.call(cycle, "plan", request(max_tokens=35_000))  # about $0.35: it fits the cycle cap, not the reserve


# --- X1: a maintenance cycle's cap bounds every call in it ---


def test_a_maintenance_cycles_cap_bounds_every_call_in_it(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    economy = make_economy(data_dir, DAY)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 10_000))]))
    cycle = model.open_cycle("owner")  # no event reserve: only the maintenance cap
    assert cap_of(economy, cycle) == 400_000
    with economy.db.connection() as conn:
        assert conn.execute("SELECT burn_mode FROM cycles WHERE id = ?", (cycle,)).fetchone()[0] == "maintenance"
    for purpose in (metering.WORKSHOP, metering.REVIEW, metering.STUDY):
        assert model.rooms(cycle, purpose)[0] == 400_000, purpose  # before 0.15.0: $1.50 and $7.00
    review = model.call(cycle, metering.REVIEW, request(max_tokens=15_000))
    assert review.cost_micros == 102_000
    assert economy.books.cycle_spend(cycle, outside_cap=False, every_purpose=True)[0] == 102_000
    assert model.rooms(cycle, "work")[0] == 400_000 - 102_000  # the review came out of the cycle's $0.40
    with pytest.raises(CallRefused) as refused:  # a $0.50 run fits its $1.50 cap, not the maintenance cycle
        model.call(cycle, metering.WORKSHOP, request(max_tokens=50_000))
    assert refused.value.reason.startswith("the cycle cap of $0.40 (maintenance: every call counts) would be exceeded")
    fixed = pytest.raises(sqlite3.IntegrityError, match="the burn mode a cycle opened in is fixed")
    with fixed, economy.db.transaction() as conn:
        conn.execute("UPDATE cycles SET burn_mode = 'explore' WHERE id = ?", (cycle,))


def test_a_maintenance_cycle_offers_no_workshop_and_says_its_cap(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", {"task": "Chart."})]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=ROOMY)
    assert prompts.workshop_on(ROOMY)
    assert all("workshop" not in names for names in work_tools(fake))  # the same list in every step and reflection
    [refused] = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'workshop'")
    assert "there is no tool called 'workshop'" in refused["result"]
    status = section(planner_texts(fake)[0], "STATUS")
    assert "This cycle may spend up to $0.40 (maintenance: $0.40 a cycle, every call counted)." in status
    assert "every call counted, no workshop runs or venture cycles" in status
    assert "This cycle may spend up to $0.40 (maintenance" in briefs(fake)[0]


def test_a_maintenance_cycles_review_leaves_what_the_cycle_needs_to_work(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, fake = next_day(data_dir)
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    # the cycle's work needs nearly all of the $0.40: a review would fit the cap, but not next to the work
    monkeypatch.setattr(loop, "working_cycle_cost", lambda *args: 390_000)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    [cycle] = rows(agent, "SELECT id, cap_micros FROM cycles ORDER BY id DESC LIMIT 1")
    assert cycle["cap_micros"] == 400_000
    kinds = [request_kind(r) for r in list(fake.sent)[before:]]
    assert "plan" in kinds and "review" not in kinds  # before 0.15.0 the review took the room the work needed
    assert rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'review'") == []


# --- X3: STATUS says the cap in force; the focus mode offers no brainstorm ---


def test_status_says_the_event_reserve_cut_the_cycles_cap(data_dir: Path) -> None:
    fake = FakeTransport(script=[Plan(PLAN), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, cycles=0, settings=DAY.model_copy(update={"venture_share": 0}))
    spent_before(agent.economy)
    agent.run_cycle("schedule")
    status = section(planner_texts(fake)[0], "STATUS")
    assert (
        "This cycle may spend up to $0.60 (until 20:00 a fifth of today's cap is kept for event wake-ups)." in status
    )  # before 0.15.0: "up to $1.00"
    assert rows(agent, "SELECT cap_micros FROM cycles") == [{"cap_micros": 600_000}]


def test_status_says_the_options_cap_when_nothing_cuts_it(data_dir: Path) -> None:
    fake = FakeTransport(script=[Plan(PLAN), Reply("Ok."), JOURNAL])
    run(data_dir, fake)
    status = section(planner_texts(fake)[0], "STATUS")
    assert f"This cycle may spend up to ${ROOMY.cycle_spend_cap_usd:.2f}.\n" in status + "\n"


def test_the_focus_mode_offers_no_brainstorm(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.FOCUS)
    fake = FakeTransport(script=[venture_plan(steps=["Research the venture"]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, cycles=0, settings=VENTURING)
    with agent.db.transaction() as conn:
        ventures.update(conn, DROPSHIPPING, "2026-09-01T12:00:00Z", first_test="Sell 3 stores' worth of samples")
    # 0.15.0 (ventures): a venture without numbers is backed only with the owner's confirmation
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "confirm": True}, "Stefan").status == 200
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT venture FROM cycles") == [{"venture": 1}]
    names = work_tools(fake)
    assert names and all("brainstorm" not in n and "evidence" in n for n in names)  # before 0.15.0: offered
    status = section(planner_texts(fake)[0], "STATUS")
    assert "A research call costs about" in status and "brainstorm" not in status.split("Burn mode", 1)[0]
    assert "or a brainstorm and" not in status
    venture = section(briefs(fake)[0], "VENTURE CYCLE")
    assert "brainstorm" not in venture and venture.startswith("This is a venture cycle: read guide 'ventures'")
    assert context.BRAINSTORM_BRIEF in context.VENTURE_BRIEF  # explore keeps it
    assert "(in the explore burn mode only)" in prompts.VENTURE_RULES


def test_the_room_line_prices_research_only_without_brainstorms() -> None:
    costs = {"research": 50_000, "brainstorm": 80_000}
    assert "or a brainstorm and" in ventures.room_text(1.0, costs)
    text = ventures.room_text(1.0, costs, brainstorms=False)
    assert "brainstorm" not in text and text.endswith("so about 7 research calls.")
    names = {d["name"] for d in tools.definitions(mail=True, etsy=True, venture=True, brainstorm=False)}
    assert "brainstorm" not in names and "evidence" in names


# --- X19: the projected date of the next burn-mode change ---


def kept(mode: str, days: float | None) -> burn.Burn:
    """0.18.0: a mode under the owner's conserve stance (the burn modes as they were)."""
    return burn.Burn(mode, days, stance=burn.CONSERVE)


def test_the_next_burn_mode_is_projected_at_todays_burn() -> None:
    now = datetime(2026, 9, 30, 12, 0, tzinfo=START.tzinfo)
    assert burn.projected(kept(burn.FOCUS, 18.3), now) == (
        burn.MAINTENANCE,
        now + timedelta(days=3.3),
    )
    assert burn.projected_text(kept(burn.FOCUS, 18.3), now) == "maintenance from about 10-03 at today's burn"
    assert burn.projected_text(kept(burn.EXPLORE, 40.0), now) == "focus from about 10-10 at today's burn"
    assert burn.projected_text(kept(burn.FOCUS, 33.0), now) == "maintenance from about 10-18 at today's burn"
    assert burn.projected_text(kept(burn.FOCUS, 14.0), now) == "maintenance from about 09-30 at today's burn"
    for mode, days in ((burn.EXPLORE, 61.0), (burn.EXPLORE, None), (burn.MAINTENANCE, 9.0), (burn.DORMANT, 1.0)):
        assert burn.projected(kept(mode, days), now) is None, (mode, days)


def test_status_and_the_dashboard_show_the_projection(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.FOCUS)
    fake = FakeTransport(script=[Plan(PLAN), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, cycles=0)
    real = agent.economy.life.evaluate

    def evaluate(*args: Any, **kwargs: Any) -> Any:
        status = real(*args, **kwargs)
        runway = dataclasses.replace(status.runway, net_days=18.3)
        return dataclasses.replace(status, runway=runway, stance=burn.CONSERVE)  # 0.18.0: the modes as they were

    monkeypatch.setattr(agent.economy.life, "evaluate", evaluate)
    agent.run_cycle("schedule")
    expected = (agent.clock.now() + timedelta(days=3.3)).strftime("%m-%d")
    status = section(planner_texts(fake)[0], "STATUS")
    assert f"no brainstorms, new ideas only your owner's; maintenance from about {expected} at today's burn." in status
    assert agent.economy.dashboard()["agent"]["burn_next"] == f"maintenance from about {expected} at today's burn"


# --- X5: refused before it is paid for; a paid draft is kept ---


def test_a_draft_append_that_may_not_fit_is_refused_before_it_is_paid_for(data_dir: Path) -> None:
    def long_file(agent: Agent) -> None:
        notes(agent)
        agent.roots()[0].write("drafts/guide.md", "Plan the week on Sunday evening.\n" * 1_250)  # about 40 KB

    fake = FakeTransport(
        script=[
            Plan(PLAN),
            ToolCalls([("draft", {**DRAFT, "mode": "append"})]),
            Reply("More of the guide."),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, before=long_file)
    [call] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'draft'")
    assert call["status"] == "error"
    assert "drafts/guide.md holds 40 KB and a draft may add 31 KB, more than the 64 KB a file holds" in call["result"]
    assert "draft the rest into a new file, with drafts/guide.md as a source" in call["result"]
    assert rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'draft'") == []  # before 0.15.0: paid, then refused


def test_a_paid_draft_that_doesnt_fit_is_kept_in_a_file_of_its_own(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "DRAFT_BYTES", 1_000)  # a draft longer than its room gets past the check
    text = "Plan the week on Sunday evening. " * 800  # about 26 KB

    def full_file(agent: Agent) -> None:
        notes(agent)
        agent.roots()[0].write("drafts/guide.md", "x" * 60_000)
        agent.roots()[0].write("drafts/guide-2.md", "taken")

    fake = FakeTransport(
        script=[Plan(PLAN), ToolCalls([("draft", {**DRAFT, "mode": "append"})]), Reply(text), Reply("Done."), JOURNAL]
    )
    agent, _ = run(data_dir, fake, before=full_file)
    [call] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'draft'")
    assert call["status"] == "ok"
    assert call["result"].startswith("Wrote drafts/guide-3.md: ")
    assert "It didn't fit in drafts/guide.md (64 KB)." in call["result"]
    jail = agent.roots()[0]
    assert jail.read("drafts/guide-3.md").startswith("Plan the week") and jail.size_of("drafts/guide.md") == 60_000
    assert [r["status"] for r in rows(agent, "SELECT status FROM llm_calls WHERE purpose = 'draft'")] == ["ok"]


def test_a_workshop_folder_too_deep_for_its_files_is_refused_before_the_run(data_dir: Path) -> None:
    args = {"task": "Make the posters.", "folder": "shop/posters/eu/a3"}
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", args)]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake)
    [answer] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'workshop'")
    assert answer["status"] == "error"
    assert "folder: at most 3 folder levels, so that its files fit inside it" in answer["result"]
    assert rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'workshop'") == []  # before 0.15.0: paid for


# --- X27: a document's study ends once its learnings are full ---

LONG = "\n\n".join(f"Tip {i}: use {i} photos in listing {i}. " + "Details follow here. " * 130 for i in range(100))


def test_a_long_document_stops_being_studied_once_its_learnings_are_full(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(seed=1), cycles=0)
    add(agent, text=LONG)
    parts = rows(agent, "SELECT parts FROM library_documents")[0]["parts"]
    for _ in range(10):
        if rows(agent, "SELECT study FROM library_documents")[0]["study"] != "waiting":
            break
        agent.run_cycle("schedule")
    document = rows(agent, "SELECT study, studied_parts, study_note, studied_at FROM library_documents")[0]
    assert document["study"] == "done" and document["studied_at"] is not None
    assert 0 < document["studied_parts"] < parts  # before 0.15.0: studied to the end, the last calls keeping nothing
    assert document["study_note"] == (
        f"Its {library.MAX_LEARNINGS} learnings are full, the most a document keeps: parts "
        f"{document['studied_parts'] + 1}-{parts} weren't studied (library_read reads them)."
    )
    assert rows(agent, "SELECT COUNT(*) AS n FROM learnings")[0]["n"] == library.MAX_LEARNINGS
    paid = rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"]
    kept = rows(agent, "SELECT COUNT(DISTINCT llm_call_id) AS n FROM learnings")[0]["n"]
    assert paid == kept  # every study call paid for kept something
    ctx = context_of(agent)
    ctx.library = True
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    listed = tools.run(ctx, "library_read", {}, "toolu_list", llm_call, "act")
    assert f"{library.MAX_LEARNINGS} learnings · {document['study_note']}" in listed.text  # and what it didn't study


def test_a_document_already_full_ends_without_a_call(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = run(data_dir, FakeTransport(seed=1), cycles=0)
    add(agent, text=LONG)
    agent.run_cycle("schedule")
    kept = rows(agent, "SELECT COUNT(*) AS n FROM learnings")[0]["n"]
    before = rows(agent, "SELECT study, studied_parts FROM library_documents")[0]
    assert before["study"] == "waiting" and kept > 0
    monkeypatch.setattr(library, "MAX_LEARNINGS", kept)  # as full as a document from before 0.15.0 could be
    paid = rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"]
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"] == paid
    after = rows(agent, "SELECT study, studied_parts, study_note FROM library_documents")[0]
    assert (after["study"], after["studied_parts"]) == ("done", before["studied_parts"])
    assert after["study_note"].startswith(f"Its {kept} learnings are full")


# --- the migration ---


def _migrated_before(path: Path) -> Callable[[], None]:
    """A database migrated to just before this package's migration, and how to apply the rest."""
    everything = discover_migrations()
    mine = next(m for m in everything if m.name == "caps_modes")
    migrate(path, [m for m in everything if m.version < mine.version])
    return lambda: migrate(path, everything) and None


def test_the_migration_keeps_the_cycles_and_the_library(data_dir: Path) -> None:
    path = data_dir / "old.db"
    rest = _migrated_before(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', '2026-09-01T00:00:00Z',"
        " 'first', 'alive')"
    )
    conn.execute(
        "INSERT INTO cycles (id, life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros)"
        " VALUES (7, 1, 'b', '2026-09-30T08:00:00Z', '2026-09-30T08:10:00Z', 'completed', 'schedule', 0, 1000000)"
    )
    documents = [
        (1, "waiting", 2, 5, None),
        (2, "done", 3, 3, None),
        (3, "failed", 1, 4, "the answer wasn't valid JSON"),
    ]
    for doc_id, study, studied, parts, note in documents:
        conn.execute(
            "INSERT INTO library_documents (id, mode, session, title, chars, parts, sha256, added_at, study,"
            " studied_parts, study_note, study_micros) VALUES (?, 'live', 0, ?, 100, ?, ?, '2026-09-01T00:00:00Z',"
            " ?, ?, ?, 1234)",
            (doc_id, f"Guide {doc_id}", parts, f"sha{doc_id}", study, studied, note),
        )
        for part in range(1, parts + 1):
            conn.execute(
                "INSERT INTO library_parts (document_id, part, text) VALUES (?, ?, ?)", (doc_id, part, f"Part {part}")
            )
    conn.execute(
        "INSERT INTO learnings (mode, session, document_id, part, topic, text, cycle_id, created_at)"
        " VALUES ('live', 0, 1, 1, 'tags', 'Use all 13 tags.', 7, '2026-09-30T08:05:00Z')"
    )
    before = conn.execute("SELECT * FROM library_documents ORDER BY id").fetchall()
    conn.commit()
    conn.close()

    rest()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert [tuple(r) for r in conn.execute("SELECT * FROM library_documents ORDER BY id")] == [tuple(r) for r in before]
    assert dict(conn.execute("SELECT id, cap_micros, burn_mode FROM cycles").fetchone()) == {
        "id": 7,
        "cap_micros": 1_000_000,
        "burn_mode": None,  # a cycle from before: judged as before
    }
    assert conn.execute("SELECT document_id FROM learnings").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'library_documents'")}
    assert {"library_one_copy", "library_by_scope"} <= indexes
    # A study may now end with parts left once its learnings are full, and only with a note that says so.
    conn.execute("UPDATE library_documents SET study = 'done', study_note = 'full' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE library_documents SET study = 'done', study_note = NULL WHERE id = 3")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE library_documents SET study = 'waiting' WHERE id = 2")  # all its parts were read
    with pytest.raises(sqlite3.IntegrityError, match="library_one_copy|UNIQUE"):
        conn.execute(
            "INSERT INTO library_documents (mode, session, title, chars, parts, sha256, added_at) VALUES"
            " ('live', 0, 'Again', 100, 1, 'sha2', '2026-09-30T00:00:00Z')"
        )
    conn.close()
