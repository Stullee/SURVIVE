"""0.37.6: one brake for every step of the plan, and a product whose request ended is proposed again.

The analysis of 0.37.0 (analysis-0.37.0/ember-codebase-0.37.0.md, 3.1 and 3.2) found two high defects in the plan tree:

- Nothing noticed that a cycle took a step and nothing changed. A step stopped being ready only when Ember said it
  waits (two a day), the owner held its product or its channel was off, and any ready step cut every sleep to the
  owner's shortest. With no model at all, a live product whose two pins waited for the owner took 16 of 16 cycles 30
  minutes apart, and a second product would have needed about 23 days to win one; live on 2026-10-09, 21 cycles from
  midnight to 12:35 spent $5.88 of the $7 cap. An Owner promise of pins had no channel, so its ordinary cycles couldn't
  keep it.
- A product whose listing request expired, was withdrawn or rejected had no step ready again: "Propose the listing" had
  closed on the first request, closed is final, and "You approve it" waited on the owner with nothing waiting for them.

The tests until 0.37.5 replayed picks as if taking a step completed it (test_fixes_0353: each pick removed from the
list). These run consecutive cycles in which taking a step completes nothing unless the cycle's work moves it.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import obligations, plan, store, tools  # noqa: E402
from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now  # noqa: E402
from tests.test_fixes_0340 import ALL, BOOK, TRACKER, keep, project, request  # noqa: E402
from tests.test_fixes_0350 import WORK, steered  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402
from tests.test_fixes_0353 import PINS  # noqa: E402
from tests.test_fixes_0360 import exploring_agent  # noqa: E402
from tests.test_ventures import JOURNAL  # noqa: E402

MEALS = ("Meal planner printable", "Busy parents pay 4 EUR for a weekly meal planner")
DEMAND = "Write the demand note: searches, competitors, prices"


# --- consecutive cycles: taking a step completes nothing by itself ---


@dataclass(frozen=True)
class Taken:
    """One simulated cycle: the step it took (None: nothing was ready), what the cycle was, the owner's day, whether
    something moved for its step by the cycle's end, and whether its sleep was cut (plan.busy)."""

    step: int | None
    title: str | None
    line: int | None
    kind: str
    day: str
    moved: bool
    busy: bool


Work = Callable[[Agent, plan.Steer, int], None]


def cycles(
    agent: Agent,
    count: int,
    *,
    work: Work | None = None,
    minutes: int = 30,
    rest: int | None = None,
    exploring: bool = False,
    channels: dict[str, bool] = ALL,
) -> list[Taken]:
    """``count`` consecutive cycles as loop.py runs the plan's part of each: the keeper, the step the tree takes (a
    cycle on its line, a venture or marketing cycle flagged as such), the pick kept; then the cycle's work (``work``:
    none, by default: taking a step completes nothing), its end, and its sleep: ``minutes`` (the owner's shortest)
    when plan.busy says the plan had work a cycle can advance, else ``rest`` (what Ember chose; None: ``minutes``)."""
    scope, taken = agent.scope(), []
    for _ in range(count):
        keep(agent, channels)
        with agent.db.connection() as conn:
            s = plan.steer(conn, scope, now(agent), agent.clock.today(), channels, exploring=exploring)
        cid = cycle(agent, s.line, status="running")
        with agent.db.transaction() as conn:
            if s.kind in ("venture", "marketing"):
                store.update_cycle(conn, cid, **{s.kind: 1})
            plan.record(conn, scope, cid, now(agent), s)
        if work is not None and s.step is not None:
            work(agent, s, cid)
        with agent.db.transaction() as conn:
            conn.execute("UPDATE cycles SET status = 'completed', ended_at = ? WHERE id = ?", (now(agent), cid))
        with agent.db.connection() as conn:
            busy = plan.busy(conn, scope, s, now(agent), cid)
            row = plan.node(conn, scope, s.step.id) if s.step is not None else None
            moved = row is not None and (
                plan.mark(plan.Facts(conn, scope, now(agent)), row) != s.mark or plan._made_in(conn, cid, row)
            )
        taken.append(
            Taken(
                s.step.id if s.step else None,
                s.step.title if s.step else None,
                s.line,
                s.kind,
                agent.clock.today().isoformat(),
                moved,
                busy,
            )
        )
        agent.clock.advance(minutes=minutes if busy or rest is None else rest)
    return taken


def unmoved(taken: list[Taken]) -> Counter[tuple[int, str]]:
    """How often each step was taken on each day by a cycle that moved nothing for it."""
    return Counter((t.step, t.day) for t in taken if t.step is not None and not t.moved)


def made(agent: Agent, cycle_id: int, tool: str, times: int = 1, given: dict[str, Any] | None = None) -> None:
    """``times`` calls of ``tool`` that worked in cycle ``cycle_id``: a file, a picture or a KDP package made."""
    with agent.db.transaction() as conn:
        call = conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day)"
            " VALUES ('test', ?, 'work', 'scripted', 1, 'ok', ?, ?)",
            (cycle_id, now(agent), agent.clock.today().isoformat()),
        ).lastrowid
        first = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tool_calls WHERE cycle_id = ?", (cycle_id,)).fetchone()
        for n in range(first[0] + 1, first[0] + 1 + times):
            conn.execute(
                "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
                " started_at, summary, result) VALUES (?, ?, ?, 'act', 'local', ?, ?, ?, 'ok', ?, ?, 'made')",
                (cycle_id, call, n, tool, f"toolu_{cycle_id}_{n}", json.dumps(given or {}), now(agent), tool),
            )


def noted(agent: Agent, line: int, cycle_id: int = 1) -> None:
    """A demand note for product line ``line``."""
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO demand_notes (mode, session, cycle_id, project_id, created_at, keywords, demand, source)"
            " VALUES (?, ?, ?, ?, ?, 'planner', 'Etsy lists 40 of them', 'etsy.com')",
            (agent.scope().mode, agent.scope().session, cycle_id, line, now(agent)),
        )


def asked(agent: Agent, line: int, executor: str, cycle_id: int = 1) -> int:
    """A request of product line ``line`` by ``executor``, waiting for the owner, made in cycle ``cycle_id``."""
    with agent.db.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        return store.insert_approval(
            conn, agent.scope(), cycle_id, now(agent), project_id=line, type="publish", title=f"A {executor}",
            description="for the owner", payload=json.dumps({"request": count}), expected_cost="nothing",
            expected_benefit="buyers", executor=executor, action="{}",
        )  # fmt: skip


def decided(agent: Agent, approval: int, status: str) -> None:
    """The owner's decision on a request (or what became of it): approved, rejected, expired, withdrawn, done, failed;
    the decisions Ember owes a reaction to recorded, as each cycle's keeper does (obligations.keep)."""
    column = "closed_at" if status in ("done", "failed") else "decided_at"
    with agent.db.transaction() as conn:
        conn.execute(f"UPDATE approvals SET status = ?, {column} = ? WHERE id = ?", (status, now(agent), approval))
        obligations.keep(conn, agent.scope(), now(agent), "2000-01-01T00:00:00Z")


def closed_without_proposing(agent: Agent) -> None:
    """Ember closes the owner's decisions on the product without proposing it again (it said it would later)."""
    with agent.db.transaction() as conn:
        for row in conn.execute("SELECT id FROM obligations WHERE kind = 'decision' AND status = 'open'").fetchall():
            obligations.close_one(conn, row["id"], "noted, will redo the photos", "agent", None, now(agent))


def diligent(agent: Agent, s: plan.Steer, cycle_id: int) -> None:
    """The cycle's work as a model doing what YOUR STEP asks would do it, in one cycle: the demand note, the files, the
    photos, the KDP package check, the request its check needs; two pins or two posts proposed (they wait for the
    owner). A promise or a decision it can't keep in a cycle, nothing."""
    with agent.db.connection() as conn:
        row = plan.node(conn, agent.scope(), s.step.id) if s.step is not None else None
    if row is None or row["project_id"] is None:
        return
    line, kind, spec = int(row["project_id"]), row["check_kind"], json.loads(row["check_spec"] or "{}")
    if kind == "demand":
        noted(agent, line, cycle_id)
    elif kind == "built":
        made(agent, cycle_id, spec["tools"][0], int(spec.get("count", 1)))
    elif kind == "kdp_check":
        made(agent, cycle_id, "propose_kdp_book", given={"check": 1})
    elif kind == "request":
        asked(agent, line, spec["executor"], cycle_id)
    elif kind in ("pin", "post"):
        for _ in range(int(spec.get("count", 1))):
            asked(agent, line, plan.CHECK_EXECUTORS[kind][0], cycle_id)


def live(agent: Agent, line: int) -> int:
    """Product line ``line`` goes live: its listing request approved and carried out, the listing active."""
    listing = request(agent, line, "etsy_listing")
    decided(agent, listing, "approved")
    decided(agent, listing, "done")
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id, title,"
            " state, views, favorites, synced_at) VALUES (?, ?, ?, ?, ?, 'active', ?, 'Tracker', 'active', 0, 0, ?)",
            (
                agent.scope().mode,
                agent.scope().session,
                listing,
                now(agent),
                now(agent),
                900_000_100 + line,
                now(agent),
            ),
        )
    keep(agent)
    return listing


def candidate(s: plan.Steer, title: str, line: int | None = None) -> plan.Candidate:
    return next(c for c in s.found if c.step.title.startswith(title) and (line is None or c.step.product == line))


def waits(agent: Agent, title: str, line: int | None = None) -> str | None:
    return candidate(steered(agent), title, line).waiting


# --- 3.1: the brake ---


def test_two_pins_waiting_for_the_owner_wait_on_them_and_the_other_product_gets_its_cycles(data_dir: Path) -> None:
    """analysis-0.37.0 3.1 (repro/e2e/stuck_step.py): 16 cycles 30 minutes apart, no model work at all."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    meals = project(agent, *MEALS)
    keep(agent)
    live(agent, tracker)
    pins = [asked(agent, tracker, "pinterest_pin"), asked(agent, tracker, "pinterest_pin")]
    # its own requests are all its check needs: it waits on the owner, without one of Ember's two waits a day
    assert waits(agent, PINS, tracker) == "owner"
    taken = cycles(agent, 16)
    assert not [t for t in taken if t.title == PINS]  # 0.37.0: 16 of 16
    assert not [k for k, n in unmoved(taken).items() if n > 1]  # each step tried once today
    assert any(t.line == meals for t in taken[:4])  # 0.37.0: about 23 days
    # once nothing ready can move, no sleep is cut: the plan waits on the owner, and the next day
    assert [t.busy for t in taken[-10:]] == [False] * 10
    assert [t.step for t in taken[-10:]] == [None] * 10
    assert rows(agent, "SELECT COUNT(*) AS n FROM plan_changes WHERE action = 'wait'") == [{"n": 0}]
    decided(agent, pins[0], "rejected")  # the owner's decision: one pin is left, it needs another
    assert waits(agent, PINS, tracker) is None


def test_a_step_whose_own_requests_are_on_their_way_waits_on_them(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    live(agent, tracker)
    first = asked(agent, tracker, "pinterest_pin")
    assert waits(agent, PINS, tracker) is None  # one pin asked for, two needed: more to ask for
    second = asked(agent, tracker, "pinterest_pin")
    assert waits(agent, PINS, tracker) == "owner"
    with agent.db.connection() as conn:  # the owner's Plan tab lists it with what waits on them
        shown = plan.view(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    assert PINS in [w["title"] for w in shown["waiting"]]
    decided(agent, first, "approved")
    assert waits(agent, PINS, tracker) == "owner"  # the second still waits for the owner
    decided(agent, second, "approved")
    assert waits(agent, PINS, tracker) == "approved"  # Ember's code carries them out
    decided(agent, second, "failed")
    assert waits(agent, PINS, tracker) is None  # one failed: another is needed


def test_a_step_a_cycle_did_not_move_waits_until_something_moves_or_the_next_day(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    [first] = cycles(agent, 1)
    assert (first.title, first.moved, first.busy) == (DEMAND, False, False)
    s = steered(agent)
    assert candidate(s, DEMAND).waiting == "tried" and candidate(s, DEMAND).tried
    with agent.db.connection() as conn:
        said = plan.step_text(conn, agent.scope(), s, explore=True)
    assert s.step is None and f"1 on {plan.WAITS['tried']})" in said
    assert plan.TRIED in said and plan.NEW_PRODUCT not in said  # its step waits for tomorrow: nothing new starts
    noted(agent, tracker)  # its numbers moved (a note written elsewhere): it closes at the keep
    keep(agent)
    assert rows(agent, f"SELECT status FROM plan_nodes WHERE project_id = {tracker} AND title = '{DEMAND}'") == [
        {"status": "done"}
    ]
    [files] = cycles(agent, 1)  # the next step, tried today without moving: ready again the next day
    assert not files.moved and waits(agent, files.title) == "tried"
    agent.clock.advance(days=1)
    s = steered(agent)
    assert s.step is not None and s.step.title == files.title and candidate(s, files.title).tried


def test_a_step_whose_last_try_moved_nothing_cuts_no_sleep(data_dir: Path) -> None:
    """Ready again the next day, a step whose last try moved nothing gets its try, but no 30-minute cycles for it."""
    agent, _ = lined(data_dir, titles=())
    tracker, meals = project(agent, *TRACKER), project(agent, *MEALS)
    keep(agent)
    first = cycles(agent, 3)
    assert [t.line for t in first] == [tracker, meals, None] or [t.line for t in first] == [meals, tracker, None]
    assert [t.busy for t in first] == [True, False, False]  # the second product's step was still to be tried
    agent.clock.advance(days=1)
    again = cycles(agent, 3)
    assert {t.line for t in again[:2]} == {tracker, meals} and again[2].step is None
    assert [t.busy for t in again] == [False, False, False]  # both tried yesterday, and nothing moved since


def test_the_owners_pin_and_decision_move_a_step_that_waits_since_a_cycle_tried_it(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    live(agent, tracker)
    pin = asked(agent, tracker, "pinterest_pin")  # one of the two pins it needs
    with agent.db.transaction() as conn:  # nothing else ready to take: the pins step is the heaviest
        for title in ("A German blog post with its product box", "The critic passes it"):
            [row] = conn.execute("SELECT id FROM plan_nodes WHERE title = ?", (title,)).fetchall()
            plan._close(conn, row["id"], now(agent), "done", "test", by="owner")
    [taken] = cycles(agent, 1)
    assert taken.title == PINS and not taken.moved and waits(agent, PINS) == "tried"
    decided(agent, pin, "approved")  # the owner's decision on its request
    assert waits(agent, PINS) is None
    [again] = cycles(agent, 1)
    assert again.title == PINS and waits(agent, PINS) == "tried"
    with agent.db.transaction() as conn:  # the owner pins it: their word
        plan.pin(conn, agent.scope(), again.step, True, "Owner", now(agent))
    assert waits(agent, PINS) is None and steered(agent).pick.decided == "pin"


def test_no_step_is_taken_twice_in_a_day_without_moving_and_every_product_gets_a_cycle(data_dir: Path) -> None:
    """A week of hourly cycles in which nothing gets done: three products in research, a live one whose pins wait for
    the owner, a KDP book promised in three days and an Owner promise of pins in four."""
    agent, _ = lined(data_dir, titles=())
    tracker, meals, book = project(agent, *TRACKER), project(agent, *MEALS), project(agent, *BOOK)
    poster = project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    keep(agent)
    live(agent, tracker)
    asked(agent, tracker, "pinterest_pin")
    asked(agent, tracker, "pinterest_pin")
    kept = promise(agent, "Propose the Haushaltsbuch 2027 KDP book", days=3, line=book)
    pins = promise(agent, "Pin each live listing twice on Pinterest this week", days=4)
    taken = cycles(agent, 24 * 7, minutes=60)
    assert not [k for k, n in unmoved(taken).items() if n > 1]
    first_day = {t.line for t in taken if t.day == taken[0].day}
    assert {meals, book, poster} <= first_day
    for owed in (kept, pins):  # once a day while they can't be kept (0.37.0: every cycle)
        days = Counter(t.day for t in taken if t.title and t.title.startswith(f"Keep promise #{owed}: "))
        assert days and max(days.values()) == 1
    assert not [t for t in taken if t.title == PINS]  # its pins wait for the owner
    by_day = Counter(t.day for t in taken if t.step is not None)
    assert max(by_day.values()) <= 8  # each step once a day: the other cycles find nothing ready
    cut = Counter(t.day for t in taken if t.busy)
    assert all(cut[day] < by_day[day] for day in cut)  # and no sleep is cut once nothing ready can move


def test_steps_that_move_are_taken_until_their_product_waits_on_the_owner(data_dir: Path) -> None:
    """A model that does what YOUR STEP asks, and an owner who doesn't answer: each product goes from its demand note
    to the request its release needs (the first listing too: its create stage needs it proposed), then waits."""
    agent, _ = lined(data_dir, titles=())
    tracker, meals, book = project(agent, *TRACKER), project(agent, *MEALS), project(agent, *BOOK)
    keep(agent)
    taken = cycles(agent, 48, work=diligent, rest=240)
    assert not [k for k, n in unmoved(taken).items() if n > 1]
    pending = {r["project_id"]: r["executor"] for r in rows(agent, "SELECT * FROM approvals WHERE status = 'pending'")}
    assert pending == {tracker: "etsy_listing", meals: "etsy_listing", book: "kdp_package"}
    with agent.db.connection() as conn:
        said = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
    assert said.count("You approve it, and it goes live") == 2 and "You publish it at KDP" in said
    worked = [t for t in taken if t.step is not None]
    assert all(t.moved for t in worked)  # every cycle that took a step moved it: no idle retakes
    assert len(worked) <= 3 * 6  # demand note, files, photos (or interior, cover, check), the request
    assert not taken[-1].busy and taken[-1].step is None  # all three wait on the owner: Ember sleeps


def test_a_promise_ember_cant_keep_yet_takes_a_cycle_a_day_and_one_it_works_on_more(data_dir: Path) -> None:
    """analysis-0.37.0 3.1 (repro/plan/repro_promise_floor.py): a promise due in three days took 30 of 30 hourly
    cycles at 15.5 to 18 against at most 1.3 for anything else."""
    agent, _ = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist
    book = project(agent, *BOOK)
    kept = promise(agent, "Propose the Haushaltsbuch 2027 KDP book", days=3, line=book)
    keep(agent)
    taken = cycles(agent, 30, minutes=60)
    promised = [t for t in taken if t.title and t.title.startswith(f"Keep promise #{kept}: ")]
    assert 1 <= len(promised) <= 2  # its first try, and its day's
    assert {t.line for t in taken if t.step is not None} >= {1, 2, 3, book}

    def interior(agent: Agent, s: plan.Steer, cycle_id: int) -> None:  # the promise's cycle works on its book
        if s.step is not None and s.step.title.startswith(f"Keep promise #{kept}: "):
            made(agent, cycle_id, "make_document")

    agent.clock.advance(days=1)
    worked = cycles(agent, 3, work=interior)
    assert [t.title.startswith(f"Keep promise #{kept}: ") for t in worked if t.title] == [True, True, True]
    assert all(t.moved and t.busy for t in worked)


def test_an_owner_promise_of_pins_has_its_channel_and_a_marketing_cycle(data_dir: Path) -> None:
    """analysis-0.37.0 3.1 (repro/plan/repro_owner_pin_promise.py): it was an ordinary cycle, without propose_pin."""
    agent, _ = lined(data_dir)
    keep(agent)
    pins = promise(agent, "Pin each live listing twice on Pinterest this week", days=3)
    report = promise(agent, "Report the Bluesky reactions of the week", days=3)  # told in a message: no channel's
    keep(agent)
    steps = {r["obligation_id"]: r for r in rows(agent, "SELECT * FROM plan_nodes WHERE obligation_id IS NOT NULL")}
    assert (steps[pins]["channel"], steps[pins]["project_id"], steps[report]["channel"]) == ("pinterest", None, None)
    s = steered(agent)
    assert s.step is not None and s.step.id == steps[pins]["id"] and s.kind == "marketing"
    assert tools.offered(
        "propose_pin", mail=False, workshop=False, etsy=True, venture=False, library=False, pinterest=True,
        marketing=True, marketing_apart=False,
    )  # fmt: skip
    assert waits(agent, f"Keep promise #{pins}: ", None) is None
    assert steered(agent, channels={**ALL, "pinterest": False}).step.id != steps[pins]["id"]  # its channel is off
    with agent.db.transaction() as conn:  # laid out by 0.37.0, without its channel: the next keep gives it
        conn.execute("UPDATE plan_nodes SET channel = NULL WHERE id = ?", (steps[pins]["id"],))
    keep(agent)
    assert rows(agent, f"SELECT channel FROM plan_nodes WHERE id = {steps[pins]['id']}") == [{"channel": "pinterest"}]


def test_an_owners_decision_moves_when_it_is_closed_or_what_became_of_its_products_requests(data_dir: Path) -> None:
    """In a simulated week of dry run a decision the fake model never closed was taken again after every request a
    cycle made (45 times): a request made since doesn't move it, the owner's decision on one does."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    live(agent, tracker)
    pin = asked(agent, tracker, "pinterest_pin")
    decided(agent, pin, "rejected")  # a decision of the owner's: a step of the product, the heaviest
    keep(agent)
    [taken] = cycles(agent, 1, work=lambda agent, s, cycle_id: asked(agent, tracker, "bluesky_post", cycle_id))
    assert taken.title.startswith("Obligation #") and not taken.moved and waits(agent, "Obligation #") == "tried"
    other = asked(agent, tracker, "pinterest_pin")  # a request made since: nothing became of the product's requests
    assert waits(agent, "Obligation #") == "tried"
    decided(agent, other, "approved")  # the owner decided one
    assert waits(agent, "Obligation #") is None


@pytest.mark.exploring
def test_a_ventures_step_waits_once_a_cycle_took_it_without_its_venture_moving(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    [first] = cycles(agent, 1, exploring=True)
    assert first.kind == "venture" and not first.moved
    s = steered(agent, exploring=True)
    assert candidate(s, first.title).waiting == "tried"
    with agent.db.transaction() as conn:  # a note and new scores alone: the fake model's repeated research did so
        venture = plan.venture_of(conn, agent.scope(), first.step)
        assert venture is not None, "the seeded venture's step, not a brainstorm"
        conn.execute(
            "UPDATE ventures SET notes = 'Same answer as yesterday.', revenue = 4, risk = 2, updated_at = ?"
            " WHERE id = ?",
            (now(agent), venture),
        )
    assert candidate(steered(agent, exploring=True), first.title).waiting == "tried"
    with agent.db.transaction() as conn:  # it saved what its research found
        conn.execute(
            "INSERT INTO evidence (mode, session, venture_id, cycle_id, created_at, claim, metric, low, high, unit,"
            " region, url, source) VALUES (?, ?, ?, 1, ?, 'Etsy lists 40 planners', 'listings', 40, 40, 'listings',"
            " 'DE', 'https://www.etsy.com/search?q=planner', 'independent')",
            (agent.scope().mode, agent.scope().session, venture, now(agent)),
        )
    assert candidate(steered(agent, exploring=True), first.title).waiting is None


def test_a_cycle_that_moved_nothing_keeps_the_sleep_it_chose(data_dir: Path) -> None:
    """The loop: the cycle took the plan's only ready step and did nothing for it, so the 600 minutes it chose stay
    (0.35.1 to 0.37.5 cut to the shortest sleep while the plan had a step ready); test_fixes_0350 cuts it while other
    products' steps are ready."""
    agent, fake = lined(data_dir, titles=("Planner",))
    fake.script.extend(
        [Plan({**WORK, "focus_project_id": 1}), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL]
    )
    end = agent.run_cycle("schedule")
    assert end.status == "completed" and rows(
        agent, "SELECT title FROM plan_nodes WHERE id IN (SELECT node_id FROM plan_picks)"
    ) == [{"title": DEMAND}]
    assert (end.sleep_minutes, end.sleep_cut) == (600, None)


# --- 3.2: a request that ended without going live is proposed again ---


@pytest.mark.parametrize("ending", ["expired", "withdrawn", "rejected", "failed"])
def test_a_product_whose_listing_request_ended_gets_a_step_to_propose_it_again(data_dir: Path, ending: str) -> None:
    """analysis-0.37.0 3.2 (repro/plan/repro_stalled_release.py)."""
    agent, _ = lined(data_dir, titles=("Planner",))
    tracker = project(agent, *TRACKER)
    keep(agent)
    first = request(agent, tracker, "etsy_listing")
    keep(agent)  # pending: create and "Propose the listing" close (done is final)
    assert waits(agent, "You approve it, and it goes live", tracker) == "owner"
    decided(agent, first, "approved" if ending == "failed" else ending)
    if ending == "failed":
        decided(agent, first, "failed")
    keep(agent)
    closed_without_proposing(agent)  # a rejection or a failure is a decision of the owner's: Ember closed it
    keep(agent)
    title = f"Propose the listing again: request #{first} {ending}"
    assert waits(agent, title, tracker) is None
    assert waits(agent, "You approve it, and it goes live", tracker) == "step"  # nothing waits for the owner
    with agent.db.connection() as conn:
        said = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
    assert "Waiting on your owner" not in said
    assert any(t.line == tracker for t in cycles(agent, 3))  # 0.37.0: none in 10
    second = request(agent, tracker, "etsy_listing")
    keep(agent)
    assert rows(agent, f"SELECT status FROM plan_nodes WHERE title = '{title}'") == [{"status": "done"}]
    assert waits(agent, "You approve it, and it goes live", tracker) == "owner"
    decided(agent, second, "rejected")
    keep(agent)
    assert waits(agent, f"Propose the listing again: request #{second} rejected", tracker) is None


def test_the_create_stage_asks_for_the_listing_once_its_files_and_photos_are_made(data_dir: Path) -> None:
    """The first listing too: the create stage closes on the listing proposed, and its own steps are files and photos
    ("Propose the listing" is the release stage's, which waits behind it): with both made, nothing was ready."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    noted(agent, tracker)
    work = cycle(agent, tracker)
    made(agent, work, "make_spreadsheet")
    made(agent, work, "make_image", 5)
    keep(agent)
    s = steered(agent)
    proposal = next(c for c in s.found if c.step.title == "Propose the listing" and c.stage == "create")
    assert proposal.waiting is None and s.step is not None and s.step.id == proposal.step.id
    with agent.db.connection() as conn:
        row = plan.node(conn, agent.scope(), proposal.step.id)
    assert plan.check_words(row["check_kind"], json.loads(row["check_spec"])).startswith(
        "a request to the owner for the product, by etsy_listing"
    )
    request(agent, tracker, "etsy_listing")
    keep(agent)
    assert waits(agent, "You approve it, and it goes live", tracker) == "owner"


def test_a_kdp_package_rejected_is_proposed_again(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    package = request(agent, book, "kdp_package")
    keep(agent)
    assert waits(agent, "You publish it at KDP", book) == "owner"
    decided(agent, package, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    assert waits(agent, f"Propose the book again: request #{package} rejected", book) is None
    assert waits(agent, "You publish it at KDP", book) == "step"
