"""0.37.9: the plan sees the owner's limits that only the tools enforced, and a promise to report weighs as a report.

The analysis of 0.37.8 (analysis-0.37.8/ember-analysis-0.37.8.md, 6.3 1 and 2, 6.4) found two limits that protect
the owner read by the tools alone: the publish requests that may wait for the owner (store.PENDING_CAPS, 3: pins,
Bluesky posts and blog posts; since 0.37.8 also 3 of a channel's pins or posts waiting for the owner or their slot,
publish_queue.WAITING) and the messages a day that answer none of the owner's (store.MESSAGES_PER_DAY, 2). plan.py,
weights.py and slack.py never read them: a pin, a post or a report looked ready, the sleep was cut to the owner's
shortest, a plan was paid for (about $0.15 before any work) and the tool refused the request. Of the 30 cycles #192 to
#221, 9 were refused by the full queue ($2.57) and 7 by the message limit ($1.55), five in a row (#193 to #197) trying
the same report. And a promise to report weighed as a promise to ship (1.0), not as a report (0.3).

These replay it: a step whose work a limit would refuse waits on the limit ('queue', 'messages'), isn't taken and cuts
no sleep, exactly while the tool would refuse it; the caps themselves stay as they are.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, quality, store, templates, tools, weights  # noqa: E402
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls  # noqa: E402
from app.agent.loop import CycleRunner  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import Clock, to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now, texts, working  # noqa: E402
from tests.test_fixes_0340 import ALL, TRACKER, keep, project  # noqa: E402
from tests.test_fixes_0350 import WORK, steered  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402
from tests.test_fixes_0353 import CRITIC, PINS  # noqa: E402
from tests.test_fixes_0376 import asked, cycles, decided, live, waits  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import JOURNAL  # noqa: E402

POSTS, BLOG = templates._POSTS.title, templates._BLOG.title
# English: its launch has a pin and a Bluesky post (the job application tracker, German, a pin and a blog post)
LETTERS = ("Cover letter templates", "Job seekers pay 5 EUR for the templates and a checklist")
REPORT = "Report the Bluesky reactions of the week"


def launched(data_dir: Path) -> tuple[Agent, FakeTransport, int, int]:
    """Two live products whose launch marketing is all that is ready (the critic passed both): (agent, its fake
    model, the job application tracker's line, the cover letters' line)."""
    agent, fake = lined(data_dir, titles=())
    tracker, letters = project(agent, *TRACKER), project(agent, *LETTERS)
    keep(agent)
    passed = {"score": 8, "verdict": "pass", "fixes": ""}
    for line in (tracker, letters):
        live(agent, line)  # its listing: 900000100 + line
        with agent.db.transaction() as conn:  # the critic passed it: no check due for 14 days
            quality.save(conn, agent.scope(), line, None, passed, None, now(agent), 900_000_100 + line)
    keep(agent)
    open_critics = f"SELECT COUNT(*) AS n FROM plan_nodes WHERE title = '{CRITIC}' AND status = 'open'"
    assert rows(agent, open_critics) == [{"n": 0}]  # its step closed on the critic's pass
    return agent, fake, tracker, letters


def full_queue(agent: Agent, tracker: int, letters: int) -> list[int]:
    """The three publish requests that may wait for the owner, waiting: the tracker's blog post and the cover letters'
    two Bluesky posts (all that their steps need: those wait on the owner)."""
    return [
        asked(agent, tracker, "site_post"),
        asked(agent, letters, "bluesky_post"),
        asked(agent, letters, "bluesky_post"),
    ]


def one_pin(agent: Agent, s: plan.Steer, cycle_id: int) -> None:
    """The cycle's work: one pin of the two its step needs, proposed (it waits for the owner)."""
    asked(agent, int(s.line or 0), "pinterest_pin", cycle_id)


def unprompted(agent: Agent, count: int) -> None:
    """``count`` messages of Ember's to the owner, now, that answer none of theirs."""
    with agent.db.transaction() as conn:
        for n in range(count):
            store.insert_message(conn, agent.scope(), 1, f"An update ({n + 1}).", now(agent))


def refused_by(check: Any, *args: Any) -> str | None:
    """What a tool's limit check says when it refuses (None: it lets the request through)."""
    try:
        check(*args)
    except tools.ToolError as exc:
        return str(exc)
    return None


# --- the publishing queue ---


def test_with_three_publish_requests_waiting_pin_it_twice_is_not_taken_and_cuts_no_sleep(data_dir: Path) -> None:
    """The replay of 9 of the 30 cycles: 0.37.8 took a "Pin it twice", cut the sleep to 30 minutes because the other
    one looked ready, took that one too, and propose_pin refused both (tools._room_for_request); the next day again."""
    agent, _, tracker, letters = launched(data_dir)
    assert waits(agent, PINS, tracker) is None and waits(agent, PINS, letters) is None
    waiting = full_queue(agent, tracker, letters)
    assert waits(agent, BLOG, tracker) == waits(agent, POSTS, letters) == "owner"  # their own requests (0.37.6)
    assert waits(agent, PINS, tracker) == waits(agent, PINS, letters) == "queue"  # a pin proposed now: refused
    taken = cycles(agent, 4)
    assert [(t.step, t.busy) for t in taken] == [(None, False)] * 4
    with agent.db.connection() as conn:
        said = plan.step_text(conn, agent.scope(), steered(agent), explore=False)
    assert said.startswith("No step of your plan is ready (") and f"2 on {plan.WAITS['queue']}" in said
    agent.clock.advance(days=1)  # the next day: the requests still wait for the owner (0.37.8: two more refusals)
    keep(agent)
    assert [(t.step, t.busy) for t in cycles(agent, 2)] == [(None, False)] * 2
    decided(agent, waiting[0], "approved")  # the owner approves the blog post: room for one more request
    assert waits(agent, PINS, tracker) is None and waits(agent, PINS, letters) is None
    [took] = cycles(agent, 1, work=one_pin)  # one pin of its two, proposed: three requests wait for the owner again
    assert took.title == PINS and took.moved
    assert not took.busy  # 0.37.8: cut, as its step moved and the other "Pin it twice" looked ready
    assert waits(agent, PINS, tracker) == waits(agent, PINS, letters) == "queue"


def test_a_channels_pins_waiting_for_their_slots_hold_back_its_steps_alone(data_dir: Path) -> None:
    """0.37.8's queue: while 3 of a channel's pins or posts wait for the owner or their slot, propose_pin (or
    propose_bluesky_post) refuses the next (tools._room_in_queue). The pins wait; a Bluesky post and a blog post,
    whose channels have room, are ready."""
    agent, _, tracker, letters = launched(data_dir)
    for line in (tracker, tracker, letters):  # approved: they wait for their slots, hours apart
        decided(agent, asked(agent, line, "pinterest_pin"), "approved")
    assert waits(agent, PINS, tracker) == "approved"  # its own two: being carried out (0.37.6)
    assert waits(agent, PINS, letters) == "queue"  # one of its two: the next would be the queue's fourth
    assert waits(agent, POSTS, letters) is None and waits(agent, BLOG, tracker) is None
    s = steered(agent)
    assert s.step is not None and s.step.title in (POSTS, BLOG) and s.kind == "marketing"
    [first] = rows(agent, "SELECT MIN(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")
    decided(agent, first["id"], "done")  # one went out in its slot: room for the next
    assert waits(agent, PINS, letters) is None


def test_the_plan_waits_on_the_queue_exactly_while_the_tools_would_refuse(data_dir: Path) -> None:
    agent, _, tracker, letters = launched(data_dir)
    ctx = working(agent, letters)

    def both() -> tuple[str | None, str | None, bool, bool]:
        with agent.db.connection() as conn:
            found = plan.limits(conn, agent.scope(), to_iso(agent.clock.day_start(agent.clock.today())))
            return (
                refused_by(tools._room_for_request, ctx, conn, "publish"),
                refused_by(tools._room_in_queue, ctx, conn, "pinterest", []),
                found.full(None),
                found.full("pinterest"),
            )

    assert both() == (None, None, False, False)
    asked(agent, tracker, "site_post")
    asked(agent, letters, "bluesky_post")
    assert both() == (None, None, False, False)  # two of three
    third = asked(agent, letters, "pinterest_pin")
    publish, queue, full, pins_full = both()
    assert publish is not None and "3 publish requests are already waiting for your owner" in publish
    assert queue is None and full and pins_full  # every channel's request waits: all three wait for the owner
    decided(agent, third, "approved")  # it waits for its slot: no longer for the owner
    assert both() == (None, None, False, False)
    for _ in range(2):
        decided(agent, asked(agent, letters, "pinterest_pin"), "approved")
    publish, queue, full, pins_full = both()
    assert publish is None and queue is not None and "3 pins of yours already wait" in queue
    assert (full, pins_full) == (False, True)  # the blog and Bluesky still take a request; Pinterest doesn't


def test_the_loop_keeps_the_sleep_while_the_queue_is_full_and_cuts_it_once_a_pin_can_go(data_dir: Path) -> None:
    """Through the loop: the steps a cycle can take are what cuts the sleep (slack.sleep, plan.busy)."""
    agent, fake, tracker, letters = launched(data_dir)
    waiting = full_queue(agent, tracker, letters)
    script = [Plan(WORK), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL]
    with mock.patch.object(CycleRunner, "_channels", lambda self: dict(ALL)):  # the channels set up and synced
        fake.script.extend(script)
        held = agent.run_cycle("schedule")
        decided(agent, waiting[0], "approved")
        fake.script.extend(script)
        free = agent.run_cycle("schedule")
    picks = rows(agent, "SELECT cycle_id, kind, node_id FROM plan_picks ORDER BY id")[-2:]
    assert (picks[0]["kind"], picks[0]["node_id"]) == ("ordinary", None)  # 0.37.8: a marketing cycle on the pins
    assert held.status == "completed" and (held.sleep_minutes, held.sleep_cut) == (600, None)
    planned = texts(fake, "plan")[-2]
    assert "No step of your plan is ready (" in planned and f"2 on {plan.WAITS['queue']}" in planned
    assert picks[1]["kind"] == "marketing" and picks[1]["node_id"] is not None  # room: a pin can go
    assert free.status == "completed" and free.sleep_minutes == agent.settings.min_sleep_minutes
    assert free.sleep_cut is not None  # the other product's "Pin it twice" is ready, and no cycle tried it


# --- the day's messages ---


def test_with_two_unprompted_messages_sent_today_a_report_promise_waits_until_tomorrow(data_dir: Path) -> None:
    """Live, five cycles in a row (#193 to #197) tried the same report, and message_owner refused each; 0.37.8 still
    took it once a day (the brake held it after that try) and paid for a plan the limit refused."""
    agent, _ = lined(data_dir, titles=())
    report = promise(agent, REPORT, days=1)  # promised yesterday, due today
    agent.clock.advance(days=1)
    keep(agent)
    title = f"Keep promise #{report}: {REPORT}"
    assert waits(agent, title) is None and steered(agent).step is not None
    unprompted(agent, store.MESSAGES_PER_DAY)  # today's two, sent since it was promised
    assert waits(agent, title) == "messages"
    taken = cycles(agent, 5)
    assert [(t.step, t.busy) for t in taken] == [(None, False)] * 5
    with agent.db.connection() as conn:
        said = plan.step_text(conn, agent.scope(), steered(agent), explore=False)
    assert said.startswith(f"No step of your plan is ready (1 on {plan.WAITS['messages']}).")
    text = f"This week on Bluesky: 14 likes and 2 reposts (promise #{report})."
    refused = call(working(agent), "message_owner", {"text": text})
    assert not refused.ok and "2 messages today that answer none of theirs" in refused.text  # as the plan said
    agent.clock.advance(days=1)  # tomorrow
    keep(agent)
    s = steered(agent)
    assert s.step is not None and s.step.title == title and s.kind == "ordinary"


def test_a_report_waits_not_while_a_message_of_the_owners_waits_for_the_answer_that_can_carry_it(
    data_dir: Path,
) -> None:
    agent, _ = lined(data_dir, titles=())
    report = promise(agent, REPORT, days=1)
    agent.clock.advance(days=1)
    unprompted(agent, store.MESSAGES_PER_DAY)
    keep(agent)
    title = f"Keep promise #{report}: {REPORT}"
    assert waits(agent, title) == "messages"
    assert owner(agent).send_message({"text": "How did the posts do?"}, "Stefan").status == 201
    assert waits(agent, title) is None  # the answer carries it: no message of the day's two
    [asking] = rows(agent, "SELECT MAX(id) AS id FROM messages WHERE sender = 'owner'")
    with agent.db.transaction() as conn:  # the cycle shows it (an answer names a message the agent has seen)
        conn.execute("UPDATE messages SET seen_cycle_id = 1 WHERE id = ?", (asking["id"],))
    text = f"14 likes and 2 reposts this week (promise #{report})."
    answer = call(working(agent), "message_owner", {"text": text, "answers": str(asking["id"])})
    assert answer.ok, answer.text


def test_a_report_naming_a_promise_the_owner_heard_nothing_of_since_goes_through_and_doesnt_wait(
    data_dir: Path,
) -> None:
    """tools._reports_promise: a message reporting on an open promise, naming it, goes through the limit while Ember
    hasn't written to the owner since the message that made it (obligation_done needs the owner told)."""
    agent, _ = lined(data_dir, titles=())
    unprompted(agent, 1)
    report = promise(agent, REPORT, days=1)  # today's second message made it
    keep(agent)
    title = f"Keep promise #{report}: {REPORT}"
    assert waits(agent, title) is None
    sent = call(working(agent), "message_owner", {"text": f"14 likes and 2 reposts this week (promise #{report})."})
    assert sent.ok, sent.text
    assert waits(agent, title) == "messages"  # told since: another report waits for tomorrow (it can be closed now)


def test_the_days_messages_count_from_the_start_of_the_owners_day_as_message_owner_counts_them(data_dir: Path) -> None:
    """tools._unasked_today counts from the start of the owner's day in their time zone, not from midnight UTC: in
    Berlin the 2nd of September begins at 22:00 UTC on the 1st. The loop, the Plan tab and the report give the plan
    their clock's day_start."""
    agent, _ = lined(data_dir, titles=())  # 2026-09-01 12:00 UTC
    report = promise(agent, REPORT, days=3)
    agent.clock.advance(hours=10, minutes=30)  # 22:30 UTC: 00:30 on the 2nd in Berlin
    unprompted(agent, store.MESSAGES_PER_DAY)
    keep(agent)
    title, the_2nd = f"Keep promise #{report}: {REPORT}", date(2026, 9, 2)

    def wait(today: date, day_start: str | None) -> str | None:
        with agent.db.connection() as conn:
            s = plan.steer(conn, agent.scope(), now(agent), today, ALL, day_start=day_start)
        return next(c.waiting for c in s.found if c.step.title == title)

    berlin = Clock(now=agent.clock.now, tz=ZoneInfo("Europe/Berlin"))
    assert (berlin.today(), to_iso(berlin.day_start(berlin.today()))) == (the_2nd, "2026-09-01T22:00:00Z")
    assert wait(the_2nd, "2026-09-01T22:00:00Z") == "messages"  # both were sent on the owner's 2nd
    with agent.db.connection() as conn:  # as the tool counts them with the owner's clock
        assert tools._unasked_today(dataclasses.replace(working(agent), clock=berlin), conn) == 2
    assert wait(the_2nd, None) is None  # from midnight UTC of the 2nd: none of them
    assert agent.next_steer().day_start == to_iso(agent.clock.day_start(agent.clock.today()))  # the loop's clock


def test_a_report_step_of_embers_waits_on_the_limit_too(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    [demand] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {tracker} AND title LIKE 'Write the demand%'")
    told = "Tell your owner what the demand note found"
    with agent.db.transaction() as conn:
        plan.add_steps(
            conn, agent.scope(), tracker, [{"title": told, "kind": "report"}], "they asked", None, now(agent),
            before=demand["id"],
        )  # fmt: skip
    assert waits(agent, told, tracker) is None
    unprompted(agent, store.MESSAGES_PER_DAY)
    assert waits(agent, told, tracker) == "messages"
    s = steered(agent)  # the demand note, which needs no message, is the step now
    assert s.step is not None and s.step.title.startswith("Write the demand note")


# --- a promise to report weighs as a report ---


def test_a_promise_to_report_weighs_as_a_report(data_dir: Path) -> None:
    """analysis-0.37.8 6.3 2: the kind promise isn't in weights.KIND, so plan._kind took every promise for shipping
    (1.0); 7 of the first 18 promises were reports, each worth 3, more than any Etsy product's 2."""
    agent, _ = lined(data_dir, titles=())
    files = promise(agent, "Send the owner the cover letters' files", days=9)
    report = promise(agent, REPORT, days=9)
    keep(agent)
    weighed = {s.title: p for s, p in steered(agent).pick.ranked}
    assert weighed[f"Keep promise #{report}: {REPORT}"].kind == weights.KIND["report"] == 0.3
    assert weighed[f"Keep promise #{files}: Send the owner the cover letters' files"].kind == weights.KIND["ship"]
    assert weighed[f"Keep promise #{report}: {REPORT}"].total == pytest.approx(weights.PROMISE_WORTH * 0.3)
    for kind, title, weighed_as in (
        ("promise", "Keep promise #7: Send the owner the pin report", "report"),
        ("promise", "Keep promise #8: Pin each live listing twice on Pinterest this week", "ship"),
        ("report", "Tell your owner what the demand note found", "report"),
        (templates.OWNER_KIND, "You approve it, and it goes live", "ship"),
        ("market", PINS, "market"),
    ):
        assert plan._kind({"kind": kind, "title": title}) == weighed_as, title


# --- what the report and the Plan tab say ---


def test_the_report_and_the_plan_tab_say_what_a_step_waits_on(ingress_client: Any) -> None:
    from tests.test_diagnostics import report, section  # noqa: PLC0415

    agent = ingress_client.app.state.ember.agent
    cycle(agent)  # the cycle the promise was made in
    promised = promise(agent, REPORT, days=1)
    unprompted(agent, store.MESSAGES_PER_DAY)  # sent since: a report would be today's third
    text = section(report(ingress_client), "AGENT RECORDS")
    assert (
        "0.37.9: 'queue', a pin, post or blog post the publishing queue would refuse, and 'messages', a report to the"
        " owner the day's message limit would refuse;"
    ) in text
    assert (  # 0.37.10: with the sell requests
        "Now: publish requests waiting for the owner 0 of 3; waiting for the owner or their slot: pins 0 of 3, posts 0"
        " of 3; sell requests waiting for the owner 0 of 6; messages today that answer none of the owner's 3 of 2)"
    ) in text
    # the preview lays out the Owner project's step and takes it back: its number is the report's alone
    assert re.search(rf"\n\d+ \| - \| messages \| Keep promise #{promised}: {re.escape(REPORT)}\n", text), text
    script = (Path(__file__).parent.parent / "app" / "web" / "static" / "js" / "app.js").read_text()
    assert 'queue: "the publishing queue is full: it waits until you decide a request, or one goes out",' in script
    assert 'messages: "today\'s messages to you are sent: it waits for tomorrow, or your next message",' in script
