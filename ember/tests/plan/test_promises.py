"""0.35.1: a promise comes first, and no cycle sleeps hours while the plan has work.

Live, the first cycles on 0.35.0 were venture cycles that slept 6 hours, then 3, while the plan had 8 steps ready and a
KDP book was promised to the owner for 10-10: a venture cycle's sleep was never cut, a cut one kept 3 hours, and the
promise, made without naming its product, was no step of the plan at all. The owner: "a promise should alter the plan",
"I want it asap", "time is money". Now a promise without its product gets the one its words name (a listing's
number, KDP), it is a step of its own taken before the heaviest step and the ventures' turn until it is kept, and any
cycle whose plan had steps ready sleeps the owner's shortest sleep.

Tests from 0.24.0, 0.32.0, 0.33.0, 0.35.1 and 0.39.1 (formerly test_fixes_0240.py, test_fixes_0320.py,
test_fixes_0330.py, test_fixes_0351.py, test_fixes_0391.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

import sqlite3  # noqa: E402

from app.agent import obligations, plan, store, weights  # noqa: E402
from app.agent import plan as plan_tree  # noqa: E402
from app.agent.fake_llm import FakeTransport, Plan, Reply  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.channels.test_etsy import (  # noqa: E402
    call,
    shop_context,  # noqa: E402
    started,
)
from tests.cycle.test_agent import rows  # noqa: E402
from tests.cycle.test_loop_shapes import run  # noqa: E402
from tests.owner.test_owner_loop import owner  # noqa: E402
from tests.plan.test_cycle_line import cycle, lined, now, working  # noqa: E402
from tests.plan.test_plan_tree import ALL, BOOK, keep, project, request  # noqa: E402
from tests.ventures.test_ventures import JOURNAL, VENTURING  # noqa: E402


def promise(agent: Agent, what: str, days: int = 2, line: int | None = None) -> int:
    """A promise to the owner, due in ``days`` days, naming ``line`` (None: no product, as message_owner made them)."""
    with agent.db.transaction() as conn:
        message = store.insert_message(conn, agent.scope(), 1, "On it.", now(agent))
        due = (agent.clock.today() + timedelta(days=days)).isoformat()
        return obligations.promise(conn, agent.scope(), 1, message, what, due, now(agent), project_id=line)


def steered(agent: Agent, exploring: bool = False) -> plan.Steer:
    with agent.db.connection() as conn:
        return plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL, exploring=exploring)


def lines_of(agent: Agent) -> dict[int, Any]:
    return {r["id"]: r["project_id"] for r in rows(agent, "SELECT id, project_id FROM obligations")}


def test_a_promise_without_its_product_gets_the_one_its_words_name(data_dir: Path) -> None:
    agent, line = started(data_dir)  # a product with a live listing
    [listing] = rows(agent, "SELECT listing_id FROM etsy_listings WHERE listing_id IS NOT NULL")
    book = project(agent, *BOOK)
    keep(agent)
    pins = promise(agent, f"2 Pinterest pins (DE+EN) live for listing #{listing['listing_id']}")
    kdp = promise(agent, "Propose the Haushaltsbuch 2027 KDP book")
    report = promise(agent, "Report Bluesky reactions and Etsy view deltas for posts #43/#44")
    said = keep(agent)
    assert lines_of(agent)[pins] == line and lines_of(agent)[kdp] == book
    assert lines_of(agent)[report] is None  # its words name no product
    assert f"Plan tree: promise #{pins} names line #{line}: a step of that product now." in said
    steps = {r["obligation_id"]: r for r in rows(agent, "SELECT * FROM plan_nodes WHERE obligation_id IS NOT NULL")}
    assert steps[pins]["channel"] == "pinterest" and steps[pins]["project_id"] == line
    assert steps[kdp]["channel"] is None and steps[kdp]["project_id"] == book
    # 0.37.0: a step of the Owner project, weighed like any (until then no step of the plan at all)
    [top] = rows(agent, "SELECT id FROM plan_nodes WHERE level = 'project' AND platform = 'owner'")
    assert (steps[report]["parent_id"], steps[report]["project_id"]) == (top["id"], None)
    assert f"Plan tree: obligation #{report} (promise, of no product) is a step of your owner's." in said


def test_message_owner_names_the_line_a_promise_is_about_from_its_words(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    outcome = call(
        working(agent, None),
        "message_owner",
        {"text": "The book goes out today.", "commits": "Send the KDP book's package", "due": now(agent)[:10]},
    )
    assert outcome.ok and f"on line #{book}: close it with obligation_done" in outcome.text, outcome.text
    assert [r["project_id"] for r in rows(agent, "SELECT project_id FROM obligations WHERE kind = 'promise'")] == [book]


def urgency_of(steer: plan.Steer, step_id: int) -> float:
    [found] = [c.step.urgency for c in steer.found if c.step.id == step_id]
    return found


@pytest.mark.exploring  # 0.37.0: a promise is weighed like any step (0.35.1 to 0.36.0 it came first)
def test_a_promise_is_weighed_like_any_step_more_as_its_day_nears(data_dir: Path) -> None:
    agent, _ = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist, each with steps ready
    book = project(agent, *BOOK)
    kdp = promise(agent, "Propose the Haushaltsbuch 2027 KDP book", days=5)
    keep(agent)
    first = steered(agent, exploring=True)
    assert first.pick.decided == "weight" and first.kind == "ordinary" and first.line == book
    assert first.step is not None and first.step.title.startswith(f"Keep promise #{kdp}: ")
    # worth the owner's word (its product could earn nothing yet); 0.37.1: no urgency while its day is 5 days off
    # (0.37.0 kept the floor from the start)
    assert first.pick.parts is not None
    assert (first.pick.parts.worth, first.pick.parts.urgency) == (weights.PROMISE_WORTH, 0.0)
    with agent.db.connection() as conn:
        text = plan.step_text(conn, agent.scope(), first, explore=False)
    assert "Done when: you kept it and closed it with obligation_done." in text
    assert "Its product's open steps: #" in text and "Why: the heaviest step that is ready" in text
    agent.clock.advance(days=5)  # its day: more urgent
    due = steered(agent, exploring=True)
    assert due.step is not None and due.step.id == first.step.id
    assert urgency_of(due, first.step.id) == weights.promise_urgency(0.5)
    for _ in range(plan.PROMISE_TRIES):  # taken three times today without being kept: no urgency (0.37.1; 0.37.0 the
        with agent.db.transaction() as conn:  # floor), so a promise Ember can't keep yet doesn't take every cycle
            plan.record(conn, agent.scope(), cycle(agent, book), now(agent), due)
    assert urgency_of(steered(agent, exploring=True), first.step.id) == 0.0
    agent.clock.advance(hours=plan.OBLIGATION_HOURS + 1)  # a day later, overdue: urgent again, with its slip
    assert urgency_of(steered(agent, exploring=True), first.step.id) == weights.promise_urgency(0.5, slips=1)


def test_a_promise_of_pins_is_a_marketing_cycles_and_waits_while_its_request_does(data_dir: Path) -> None:
    agent, line = started(data_dir)
    [listing] = rows(agent, "SELECT listing_id FROM etsy_listings WHERE listing_id IS NOT NULL")
    pins = promise(agent, f"2 Pinterest pins live for listing #{listing['listing_id']}")
    keep(agent)
    marketing = steered(agent)
    assert marketing.kind == "marketing" and marketing.line == line
    assert marketing.step is not None and marketing.step.title.startswith(f"Keep promise #{pins}: ")
    agent.clock.advance(minutes=5)
    asked = request(agent, line, "pinterest_pin")  # the pins wait for the owner's decision
    waiting = steered(agent)
    assert waiting.step is None or not waiting.step.title.startswith(f"Keep promise #{pins}: ")
    assert [c.waiting for c in waiting.found if c.step.title.startswith(f"Keep promise #{pins}: ")] == ["owner"]
    assert owner(agent).decide(asked, {"decision": "reject", "comment": "Another picture."}, "Stefan").status == 200
    again = steered(agent).step
    assert again is not None and again.title.startswith(f"Keep promise #{pins}: ")  # decided: the promise again


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_venture_cycle_sleeps_the_owners_shortest_sleep_while_the_plan_has_steps_ready(data_dir: Path) -> None:
    fake = FakeTransport(script=[])
    agent, _ = run(data_dir, fake, cycles=0, settings=VENTURING)  # every cycle the ventures' turn
    cycle(agent)  # #1, for the project's records
    project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    venture = {
        "assessment": "ok",
        "goal": "Triage a venture",
        "money_path": "A business case my owner can back",
        "focus_project_id": None,
        "focus_venture_id": 1,
        "steps": ["research the venture"],
        "sleep_minutes": 360,
    }
    fake.script.extend([Plan(venture), Reply("Done."), JOURNAL])
    end = agent.run_cycle("schedule")
    assert end.status == "completed" and rows(agent, "SELECT kind FROM plan_picks") == [{"kind": "venture"}]
    assert (end.asked_minutes, end.sleep_minutes) == (360, agent.settings.min_sleep_minutes)
    reason = agent.db.get_meta(agent._key("next_wake_reason")) or ""
    assert "Ember chose 360 min" in reason and "cut it to 30 min: your plan has steps ready" in reason


# --- From 0.24.0 (formerly test_fixes_0240.py) ---


# --- promises, and an owner's message given to obligation_done -------------------------------------------------------


def test_a_promise_that_repeats_an_open_one_makes_no_second_obligation(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    # The next day: the fake's cycles may have written to the owner today (its choices follow the request's bytes), and
    # two messages that answer none of theirs are a day's.
    agent.clock.advance(days=1)
    ctx = shop_context(agent)
    due = (agent.clock.today() + timedelta(days=5)).isoformat()
    report = "Report Bluesky reactions and Etsy view deltas for posts #43/#44"
    first = call(ctx, "message_owner", {"text": "Report on 10-11.", "commits": report, "due": due})
    assert first.ok and "Your promise is obligation #" in first.text
    again = "Bluesky reaction + view delta report for posts #43/#44; full view re-check"
    second = call(ctx, "message_owner", {"text": "As promised, on 10-11.", "commits": again, "due": due})
    assert second.ok and "Its promise repeats your open promise #" in second.text and "no new obligation" in second.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations WHERE kind = 'promise'") == [{"n": 1}]
    with agent.db.connection() as conn:  # another promise, or one due days apart, is one of its own
        assert (
            obligations.repeated_promise(conn, agent.scope(), "Re-check all listing views and report again", due)
            is None
        )
        later = (agent.clock.today() + timedelta(days=8)).isoformat()
        assert obligations.repeated_promise(conn, agent.scope(), report, later) is None


def test_an_owner_s_message_given_to_obligation_done_is_named_as_such(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    assert owner(agent).send_message({"text": "Pinterest still waiting for approval"}, "Stefan").status == 201
    [message] = rows(agent, "SELECT MAX(id) AS id FROM messages")
    done = {"numbers": str(message["id"]), "result": "Answered via message #1."}
    refused = call(shop_context(agent), "obligation_done", done)
    assert not refused.ok and f"#{message['id']} is your owner's message, not an obligation" in refused.text


# --- From 0.32.0 (formerly test_fixes_0320.py) ---


# --- 7. small frictions: a promise said twice, an answered message, the caps, a cut-off call --------------------------


def test_a_promise_said_in_other_words_is_one_promise_and_another_listing_s_is_its_own(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    agent.clock.advance(days=1)  # the fake's cycles wrote to the owner yesterday: today's two messages are free
    ctx = shop_context(agent)
    due = (agent.clock.today() + timedelta(days=3)).isoformat()
    first = call(
        ctx, "message_owner", {"text": "KDP next.", "commits": "Propose the Haushaltsbuch 2027 KDP book", "due": due}
    )
    assert first.ok and "Your promise is obligation #" in first.text
    again = {"text": "Coming.", "commits": "Send the Haushaltsbuch 2027 KDP proposal", "due": due}  # live: #32, #33
    second = call(ctx, "message_owner", again)
    assert second.ok and "Its promise repeats your open promise #" in second.text, second.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations WHERE kind = 'promise'") == [{"n": 1}]
    [made] = rows(agent, "SELECT cycle_id, message_id, created_at FROM obligations WHERE kind = 'promise'")
    with agent.db.transaction() as conn:  # a reference stays whole: another listing's report is a promise of its own
        what = "Report views of listing #900000101"
        obligations.promise(conn, agent.scope(), made["cycle_id"], made["message_id"], what, due, made["created_at"])
        assert obligations.repeated_promise(conn, agent.scope(), "Report views of listing #900000102", due) is None
        assert obligations.repeated_promise(conn, agent.scope(), "Report the views of listing #900000101", due)
        assert obligations._promise_words("Report views of listing #900000101") == {
            "report",
            "view",
            "listin",
            "#900000101",
        }


def test_naming_an_answered_message_of_the_owner_s_in_obligation_done_is_no_error(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    agent.clock.advance(days=1)
    assert owner(agent).send_message({"text": "How do I claim the shop?"}, "Stefan").status == 201
    [message] = rows(agent, "SELECT MAX(id) AS id FROM messages")
    with agent.db.connection() as conn:
        listed = obligations.text(conn, agent.scope(), agent.clock.today())
    assert f"message #{message['id']} (FROM YOUR OWNER; waiting since" in listed
    assert "): message_owner naming it in answers closes it." in listed
    with agent.db.transaction() as conn:  # the plan showed it (an answer names a message the agent has seen)
        conn.execute("UPDATE messages SET seen_cycle_id = (SELECT MAX(id) FROM cycles) WHERE id = ?", (message["id"],))
    ctx = shop_context(agent)
    reply = call(ctx, "message_owner", {"text": "Settings > Claimed accounts > Etsy.", "answers": str(message["id"])})
    assert reply.ok, reply.text
    [answer] = rows(agent, "SELECT MAX(id) AS id FROM messages WHERE sender = 'agent'")
    done = call(
        ctx, "obligation_done", {"numbers": str(message["id"]), "result": f"Answered via message #{answer['id']}."}
    )
    assert done.ok and done.summary == "nothing to close", done.text  # live: refused in 4 of 12 cycles
    assert (
        f"#{message['id']} is your owner's message, which your message #{answer['id']} answered: nothing to close"
        in (done.text)
    )


# --- From 0.33.0 (formerly test_fixes_0330.py) ---


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
        owing = obligations.open_rows(conn, agent.scope())
        owed = [obligations.owed(conn, agent.scope(), r) for r in owing if obligations.presses(r, agent.clock.today())]
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


# --- From 0.39.1 (formerly test_fixes_0391.py) ---


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
    long = call(ctx, "message_owner", {"text": "x" * 2_001})
    assert not long.ok and "text is too long: 2,001 of 2,000 characters (cut 1)" in long.text
    # 0.42.0 (test_tool_limits.py): a promise's commits, a note OBLIGATIONS shows cut anyway, is cut, not refused
    cut = call(ctx, "message_owner", {"text": "Soon.", "commits": "x" * 101, "due": agent.clock.today().isoformat()})
    assert cut.ok and "commits was cut to 100 of its 101 characters" in cut.text
