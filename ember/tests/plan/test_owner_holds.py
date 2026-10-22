"""0.39.3: the two plan defects of the analysis of 0.39.1 (analysis-0.39.1, 4.4 and 4.5).

1. The owner's holds leaked (4.4). "Hold it" holds a product: its steps wait until the owner resumes it. Its promises
   and the owner's decisions on it went on: the owner rejected a listing and held its product ("I look at it next
   week"), and on the rejection's day it weighed most, so the next cycle worked on the held line; a promise did the
   same each day as its day neared (repro/plan/r3_owner_hold_bypass.py). No tool read a hold either. Now the owner's
   hold holds them as their park does, no cycle is aimed at the line and no tool takes it, and the hold names what is
   owed on it. And "Hold new things" ("nothing new") let a new product through when it named a venture, an idea an
   ordinary cycle could make with venture_create (repro/plan/r6_hold_new_things.py): both are refused now.
2. A live product got no weekly pin, post or blog step until the critic passed it (4.5): the weekly steps came once no
   stage but maintain was open, and the launch stage holds "The critic passes it", which passed no listing in 13
   checks. After their first pins and posts the live products had no marketing step at all, and the day-7 and day-14
   pushes pushed nothing (repro/plan/r1_launch_never_closes.py); a launch step waiting on a channel that is off did
   the same to the channels that are on (repro/plan/r1b_channel_off.py). Now a launch counts once its first marketing
   is out.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, weights  # noqa: E402
from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.channels.test_etsy import call  # noqa: E402
from tests.cycle.test_agent import rows  # noqa: E402
from tests.owner.test_owner_loop import owner  # noqa: E402
from tests.plan.test_cycle_line import lined, now, texts, working  # noqa: E402
from tests.plan.test_exploring import exploring_agent  # noqa: E402
from tests.plan.test_marketing_order import CRITIC, PINS, critic, launched, urgencies  # noqa: E402
from tests.plan.test_plan_tree import ALL, BOOK, TRACKER, keep, project  # noqa: E402
from tests.plan.test_promises import promise  # noqa: E402
from tests.plan.test_tree_steers import WORK, decided, steered  # noqa: E402
from tests.ventures.test_ventures import JOURNAL, PINTEREST  # noqa: E402

# --- 1. the owner's holds ---

LATER = "I look at it next week"
AGAIN = {
    "type": "other",
    "title": "Publish the book again",
    "description": "The interior is redone: please publish it.",
    "payload": "books/haushaltsbuch-2027.pdf",
    "expected_cost": "nothing",
    "expected_benefit": "the book on sale",
}


def product_of(agent: Agent, line: int) -> int:
    [row] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    return int(row["id"])


def hold(agent: Agent, line: int) -> tuple[int | None, list[str]]:
    """The owner holds product line ``line`` on their Plan tab."""
    product = product_of(agent, line)
    with agent.db.transaction() as conn:
        return plan.owner_hold(conn, agent.scope(), product, LATER, "Owner", now(agent))


def owed_waits(agent: Agent) -> dict[int, str | None]:
    """What each promise's and decision's step waits on (None: ready), by its obligation."""
    taken = steered(agent)
    found = {c.step.id: c.waiting for c in taken.found}
    steps = rows(agent, "SELECT id, obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL AND status = 'open'")
    return {int(s["obligation_id"]): found.get(int(s["id"]), "gone") for s in steps}


def test_the_owners_hold_holds_a_products_promises_and_their_decisions_and_names_them(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    promised = promise(agent, "Redo the interior and resubmit via propose_kdp_book", days=1, line=book)
    owed = decided(agent, tracker)  # the owner rejected the tracker's listing
    [asked] = rows(agent, f"SELECT approval_id FROM obligations WHERE id = {owed}")
    keep(agent)
    assert owed_waits(agent) == {promised: None, owed: None}  # both ready
    assert steered(agent).line in (book, tracker)
    due = (agent.clock.today() + timedelta(days=1)).isoformat()
    named = f'promise #{promised} "Redo the interior and resubmit via propose_kdp_book" (due {due})'
    assert hold(agent, book) == (book, [named])  # the hold names what is owed on the line, which waits with it
    reply = owner(agent).hold_product(product_of(agent, tracker), {"why": LATER}, "Stefan")
    decision = f"your decision on request #{asked['approval_id']}"
    assert reply.status == 200 and reply.body == {
        "id": product_of(agent, tracker),
        "line": tracker,
        "hold": "owner",
        "waits": [decision],
    }
    [said] = rows(agent, "SELECT message FROM events WHERE kind = 'owner' ORDER BY id DESC LIMIT 1")
    assert said["message"] == f"Stefan held product line #{tracker} in the plan; waiting with it: {decision}"
    for _ in range(3):  # until the owner's next week: no step of either line, what is owed on them included
        keep(agent)
        assert owed_waits(agent) == {promised: "hold", owed: "hold"}
        taken = steered(agent)
        assert taken.step is None and {c.waiting for c in taken.found} == {"hold"}
        agent.clock.advance(days=1)
    resumed = product_of(agent, tracker)
    with agent.db.transaction() as conn:  # resumed: what is owed is ready again; Ember's own hold doesn't hold it
        plan.owner_resume(conn, agent.scope(), resumed, "Owner", now(agent))
        plan.hold(conn, agent.scope(), tracker, "the book comes first", None, now(agent))
        assert plan.line_held(conn, agent.scope(), tracker) is None
        assert plan.line_held(conn, agent.scope(), book) == LATER
    assert owed_waits(agent)[owed] is None
    assert steered(agent).line == tracker  # the owner's word comes before Ember's hold, as before


def test_no_tool_takes_a_line_the_owner_holds(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    hold(agent, book)
    ctx = working(agent)  # a cycle without a line yet: it took the first line it worked on, a held one too
    refused = call(ctx, "request_approval", {**AGAIN, "project_id": book})
    assert not refused.ok and f"your owner holds product line #{book} ({LATER}): no request for it" in refused.text
    assert ctx.state.focus_project_id is None and not rows(agent, "SELECT id FROM approvals")
    noted = call(ctx, "project_update", {"project_id": book, "note": "Waits for my owner's word"})
    assert noted.ok and ctx.state.focus_project_id is None  # its record is kept, as another line's: no line taken
    reopened = call(ctx, "project_update", {"project_id": book, "status": "active"})
    assert not reopened.ok and f"your owner holds product line #{book}" in reopened.text
    assert call(ctx, "project_update", {"project_id": tracker, "note": "Next: the demand note"}).ok
    assert ctx.state.focus_project_id == tracker  # a line nobody holds is taken as before


def test_a_plan_aimed_at_a_line_the_owner_holds_loses_its_focus(data_dir: Path) -> None:
    agent, fake = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    hold(agent, book)
    again = ToolCalls([("request_approval", {**AGAIN, "project_id": book})])
    fake.script.extend([Plan({**WORK, "focus_project_id": book}), again, Reply("Done."), JOURNAL])
    agent.run_cycle("event")  # a reactive cycle: its plan names its focus
    [ran] = rows(agent, "SELECT id, project_id FROM cycles ORDER BY id DESC LIMIT 1")
    assert ran["project_id"] is None  # until 0.39.3 the loop dropped one only for a closed line or a stopped venture
    assert (
        f"No focus project: your plan's #{book} waits, because your owner holds it ({LATER}). Work on what doesn't"
        " need it, until they resume it."
    ) in texts(fake, "work")[0]
    [asked] = rows(
        agent, f"SELECT status, result FROM tool_calls WHERE cycle_id = {ran['id']} AND tool = 'request_approval'"
    )
    assert asked["status"] == "error" and f"your owner holds product line #{book}" in asked["result"]
    assert not rows(agent, "SELECT id FROM approvals")


@pytest.mark.exploring
def test_hold_new_things_refuses_a_new_venture_and_a_new_product_of_one(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    with agent.db.transaction() as conn:
        top = plan.ventures_node(conn, agent.scope())
        given = plan.owner_hold(conn, agent.scope(), top["id"], "nothing new this week", "Owner", now(agent))
    assert given == (None, [])  # new things: no line, so nothing owed on one
    idea = {"title": "Wedding planner", "pitch": "Couples pay 9 EUR for a planner", "stage": "idea"}
    refused = call(working(agent), "venture_create", idea)  # an ordinary cycle has it too
    assert not refused.ok and "your owner holds new things (nothing new this week): no new venture" in refused.text
    started = {"title": "Wedding planner", "hypothesis": "Couples pay 9 EUR", "status": "active"}
    for venture in (None, PINTEREST):  # whatever venture it names (PINTEREST: an idea)
        refused = call(working(agent), "project_create", {**started, "venture_id": venture})
        assert not refused.ok and "your owner holds new things (nothing new this week): no new product" in refused.text
    assert not rows(agent, "SELECT id FROM projects") and len(rows(agent, "SELECT id FROM ventures")) == 8
    with agent.db.transaction() as conn:
        plan.owner_resume(conn, agent.scope(), top["id"], "Owner", now(agent))
    assert call(working(agent), "venture_create", idea).ok
    assert call(working(agent), "project_create", {**started, "venture_id": PINTEREST}).ok


# --- 2. a live product's weekly marketing ---


def first_marketing_out(agent: Agent, line: int, checks: tuple[str, ...]) -> None:
    """The launch's marketing steps of ``checks`` closed, as their checks close them once the pins, posts or blog post
    are live."""
    marks = ", ".join("?" for _ in checks)
    with agent.db.transaction() as conn:
        for step in plan.nodes(
            conn,
            agent.scope(),
            f"project_id = ? AND level = 'step' AND stage = 'launch' AND status = 'open' AND check_kind IN ({marks})",
            (line, *checks),
        ):
            plan._close(conn, step["id"], now(agent), "done", "Ember's code: its check passed")


def weekly(agent: Agent, line: int) -> list[dict[str, Any]]:
    """The recurring marketing steps of line ``line`` (the critic's fixes aside), oldest first."""
    return rows(
        agent,
        f"SELECT id, title, template, status, due FROM plan_nodes WHERE project_id = {line}"
        " AND template LIKE 'recurring/%' AND template <> 'recurring/critic' ORDER BY id",
    )


def stages(agent: Agent, line: int) -> dict[str, str]:
    found = rows(agent, f"SELECT stage, status FROM plan_nodes WHERE level = 'stage' AND project_id = {line}")
    return {r["stage"]: r["status"] for r in found}


def waits(agent: Agent, line: int, channels: dict[str, bool]) -> dict[str, str | None]:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), channels)
    return {c.step.title: c.waiting for c in found if c.step.product == line}


def duties(agent: Agent, line: int, channels: dict[str, bool]) -> list[str]:
    """The recurring steps a launched product of line ``line`` gets: one for each channel its buyers read that is on
    (the fake's product, and so its audience, follows the fake model's choices)."""
    [product] = rows(agent, f"SELECT audience FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    return [f"recurring/{c}" for c in plan._audience_channels(product) if channels.get(c)]


def test_a_live_products_weekly_marketing_comes_once_its_first_marketing_is_out_though_the_critic_never_passes(
    data_dir: Path,
) -> None:
    agent, line, listing = launched(data_dir)
    keep(agent)
    assert not weekly(agent, line)  # its first marketing isn't out: its launch's steps come first
    first_marketing_out(agent, line, ("pin", "post", "blog"))
    critic(agent, line, listing, 5)  # improve: live, 13 checks in a row without a pass
    agent.clock.advance(days=2)
    said = keep(agent)
    assert stages(agent, line)["launch"] == "open"  # its launch stays open until the critic passes it
    assert [s["template"] for s in weekly(agent, line)] == duties(agent, line, ALL)
    assert any("pinterest step for this period" in s for s in said)
    agent.clock.advance(days=5)  # day 7, no view: the push makes its weekly marketing urgent (it pushed nothing)
    assert any("its marketing comes first" in s for s in keep(agent))
    found = urgencies(agent, line)
    # 0.39.6: unseen, the critic's suggestions wait for views (0.39.1: a chore of no urgency)
    assert CRITIC not in found and {found[s["title"]] for s in weekly(agent, line)} == {weights.MISSED_BAR}
    taken = steered(agent)
    assert taken.kind == "marketing" and taken.step is not None
    assert taken.step.id in {s["id"] for s in weekly(agent, line)}


def test_a_launch_step_waiting_on_a_channel_that_is_off_keeps_no_weekly_step_from_the_channels_that_are_on(
    data_dir: Path,
) -> None:
    agent, line, listing = launched(data_dir)
    no_pinterest = {"pinterest": False, "bluesky": True, "blog": True}  # every audience's launch pins
    first_marketing_out(agent, line, ("post", "blog"))  # out in the channels that are on
    critic(agent, line, listing, 8)  # it passes
    agent.clock.advance(days=1)
    keep(agent, no_pinterest)
    week = weekly(agent, line)
    assert week and [s["template"] for s in week] == duties(agent, line, no_pinterest)
    assert waits(agent, line, no_pinterest) == {PINS: "channel", **{s["title"]: None for s in week}}
    with agent.db.transaction() as conn:  # this period's steps are out, and Pinterest is set up meanwhile
        for step in week:
            plan._close(conn, step["id"], now(agent), "done", "Ember's code: its check passed")
    agent.clock.advance(days=31)  # past every period (a blog post's lasts 30 days)
    keep(agent)  # once launched, a product stays launched: its launch's pins, ready now, hold back no weekly step
    assert sorted(s["template"] for s in weekly(agent, line) if s["status"] == "open") == sorted(
        duties(agent, line, ALL)
    )
    assert waits(agent, line, ALL)[PINS] is None
