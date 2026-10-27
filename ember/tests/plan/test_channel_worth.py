"""A channel's worth in the plan: Bluesky and the blog by the listing views that followed their posts, a
channel at its floor takes no more cycles (its recurring steps wait unless the owner pins one), and a line's
pin waits while its listing has an approved one waiting for its slot.

Tests from 0.39.6 and 0.39.8 (formerly test_fixes_0396.py, test_fixes_0398.py)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, plan_keep, store, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.integrations import publish_queue  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.plan.test_cycle_line import lined, now  # noqa: E402
from tests.plan.test_marketing_order import (  # noqa: E402
    PINS,
    critic,
    launched,
)
from tests.plan.test_owner_holds import first_marketing_out, waits, weekly  # noqa: E402
from tests.plan.test_plan_tree import ALL, keep  # noqa: E402
from tests.plan.test_sleep_cuts import steer  # noqa: E402
from tests.plan.test_tree_steers import plan_step, steered  # noqa: E402

# --- From 0.39.6 (formerly test_fixes_0396.py) ---


# --- 5. Bluesky and the blog by the views that follow their posts ---


def views_on(agent: Agent, listing: int, day: str, value: int) -> None:
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO observations (mode, session, day, observed_at, subject, subject_id, metric, value)"
            " VALUES (?, ?, ?, ?, 'listing', ?, 'views', ?)",
            (scope.mode, scope.session, day, now(agent), listing, value),
        )


def request(agent: Agent, line: int, executor: str, payload: dict[str, Any], action: dict[str, Any]) -> int:
    """A request of line ``line`` for ``executor``, approved, with its payload and action (neither changes later)."""
    with agent.db.transaction() as conn:
        made = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        approval = store.insert_approval(
            conn,
            agent.scope(),
            1,
            now(agent),
            project_id=line,
            type="publish",
            title="A request",
            description="for the owner",
            payload=json.dumps({**payload, "n": made}),
            expected_cost="nothing",
            expected_benefit="buyers",
            executor=executor,
            action=json.dumps({**action, "n": made}),
        )
        conn.execute("UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?", (now(agent), approval))
    return approval


def post(agent: Agent, line: int, link: str, likes: int = 9) -> None:
    approval = request(agent, line, "bluesky_post", {}, {})
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bluesky_posts (mode, session, approval_id, text, status, sent, started_at, finished_at,"
            " likes, link) VALUES (?, ?, ?, 'A post', 'active', 1, ?, ?, ?, ?)",
            (scope.mode, scope.session, approval, now(agent), now(agent), likes, link),
        )


def blog_post(agent: Agent, line: int, slug: str, link: str) -> None:
    approval = request(agent, line, "site_post", {"product_url": link}, {})
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO blog_posts (mode, session, slug, title, description, day, position, approval_id, seen_at)"
            " VALUES (?, ?, ?, 'A post', 'About it', ?, 1, ?, ?)",
            (scope.mode, scope.session, slug, agent.clock.today().isoformat(), approval, now(agent)),
        )


def factors(agent: Agent) -> dict[str, float]:
    with agent.db.connection() as conn:
        return plan._channel_factors(conn, agent.scope(), now(agent))


def test_bluesky_and_the_blog_are_worth_the_listing_views_that_followed_their_posts(data_dir: Path) -> None:
    """Live, Bluesky was worth 0.76 for its reactions, and the blog 1.0 measured by nothing, while the listings its
    posts linked gained 0 to 2 views."""
    agent, line, listing = launched(data_dir)
    link = f"https://www.etsy.com/listing/{listing}"
    start = agent.clock.today()
    views_on(agent, listing, start.isoformat(), 2)
    for n in range(plan.FRESH_POSTS):
        post(agent, line, link)  # liked, but followed by no view
        blog_post(agent, line, f"post-{n}", link)
    assert (factors(agent)["bluesky"], factors(agent)["blog"]) == (1.0, 1.0)  # too new to judge
    agent.clock.advance(days=plan.RESULT_DAYS)
    views_on(agent, listing, agent.clock.today().isoformat(), 2)
    assert (factors(agent)["bluesky"], factors(agent)["blog"]) == (weights.CHANNEL_MIN, weights.CHANNEL_MIN)
    agent.clock.advance(days=1)
    views_on(agent, listing, agent.clock.today().isoformat(), 2 + 2 * plan.FRESH_POSTS)  # two views a post
    with agent.db.connection() as conn:
        assert plan._views_after(conn, agent.scope(), start.isoformat(), link) == 0  # its week ended before them
        assert plan._views_after(conn, agent.scope(), (start + timedelta(days=1)).isoformat(), link) == 8
        assert plan._views_after(conn, agent.scope(), start.isoformat(), "https://ember-ai.de/blog/") == 0


# --- 6. approved pins of one listing count once in the queue ---


def pin_request(agent: Agent, line: int, listing: int, status: str) -> int:
    link = f"https://www.etsy.com/listing/{listing}"
    image = {"path": "shop/pin.png", "sha256": "0" * 64, "bytes": 1}
    pin = {"board_id": "1", "title": "A pin", "description": "", "link": link, "image": image}
    approval = request(agent, line, "pinterest_pin", {}, pin)
    if status != "approved":
        with agent.db.transaction() as conn:
            conn.execute("UPDATE approvals SET status = ?, decided_at = NULL WHERE id = ?", (status, approval))
    return approval


def test_approved_pins_of_one_listing_count_once_in_the_queue(data_dir: Path) -> None:
    """Live, three approved pins of the Haushaltsbuch listing (slots 10-16, 10-18, 10-20) kept every other listing's
    pin out of the queue until 10-20."""
    agent, line, listing = launched(data_dir)
    same = [pin_request(agent, line, listing, "approved") for _ in range(3)]
    with agent.db.connection() as conn:
        waiting = publish_queue.waiting(conn, agent.scope(), "pinterest")
    assert waiting == same[:1] and len(waiting) < publish_queue.WAITING
    other = pin_request(agent, line, listing + 1, "approved")
    asked = pin_request(agent, line, listing, "pending")  # one waiting for the owner counts whatever it links
    with agent.db.connection() as conn:
        assert publish_queue.waiting(conn, agent.scope(), "pinterest") == [same[0], other, asked]


def test_a_lines_pin_waits_while_its_listing_has_one_waiting_for_its_slot(data_dir: Path) -> None:
    """The tool refuses another pin of a listing whose approved pin waits for its slot: the plan's pin step waits too,
    so no cycle is paid for the refusal."""
    agent, line, listing = launched(data_dir)
    assert pin_step(agent, line).waiting is None
    pin_request(agent, line, listing, "approved")
    assert pin_step(agent, line).waiting == "queue"


def pin_step(agent: Agent, line: int) -> plan.Candidate:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    return next(c for c in found if c.step.product == line and c.step.title == PINS)


# --- From 0.39.8 (formerly test_fixes_0398.py) ---


# --- 4. a channel at its floor takes no more cycles ---


FLOORED = {"pinterest": 0.5, "bluesky": 0.21, "blog": 0.2}  # live on 10-16


def weekly_out(data_dir: Path) -> tuple[Agent, int]:
    """A live product line with this period's weekly marketing laid out, one step for each channel its buyers read."""
    agent, line, listing = launched(data_dir)
    first_marketing_out(agent, line, ("pin", "post", "blog"))
    critic(agent, line, listing, 8)
    agent.clock.advance(days=1)
    keep(agent)
    assert weekly(agent, line)
    return agent, line


def by_channel(agent: Agent, line: int) -> dict[str, dict[str, Any]]:
    return {s["template"].removeprefix("recurring/"): s for s in weekly(agent, line) if s["status"] == "open"}


def test_a_step_of_a_channel_just_above_its_floor_leaves_the_agent_its_own_sleep(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    bluesky = weights.Step(901, 1, "This week's Bluesky post", "market", 2.0, channel=0.21)
    pins = weights.Step(902, 1, "This week's pin", "market", 2.0, channel=0.5)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steer(bluesky), now(agent))  # 0.39.6 counted it: 0.21 > 0.20
        assert plan.busy(conn, agent.scope(), steer(bluesky, pins), now(agent))


def test_a_weekly_step_of_a_channel_at_its_floor_waits_unless_the_owner_pins_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, line = weekly_out(data_dir)
    monkeypatch.setattr(plan, "_channel_factors", lambda conn, scope, now: dict(FLOORED))
    steps = by_channel(agent, line)
    waiting = waits(agent, line, ALL)
    assert waiting[steps["pinterest"]["title"]] is None
    assert {waiting[steps[c]["title"]] for c in ("bluesky", "blog") if c in steps} == {"floor"}
    post = steps.get("bluesky") or steps["blog"]
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), post["id"], True, "Owner", now(agent))
    assert waits(agent, line, ALL)[post["title"]] is None  # the owner wants it: it is taken first
    assert steered(agent).step.id == post["id"]


def test_a_recurring_step_is_skipped_for_its_period_outside_the_days_waits(data_dir: Path) -> None:
    agent, line = weekly_out(data_dir)
    steps = list(by_channel(agent, line).values())
    assert len(steps) >= 2
    first, other = steps[0], steps[1]
    skipped = plan_step(agent, line, action="skip", step_id=first["id"], why="the blog has no readers")
    assert skipped.ok, skipped.text
    assert skipped.text.startswith(f'Step #{first["id"]} ("')  # 0.42.6: with its title
    assert f'") is skipped for this period (it ends {first["due"]})' in skipped.text
    assert rows(agent, "SELECT actor, action FROM plan_changes WHERE action = 'drop'") == [
        {"actor": "agent", "action": "drop"}
    ]
    until = (agent.clock.today() + timedelta(days=3)).isoformat()
    waited = plan_step(agent, line, action="wait", step_id=other["id"], on="date", until=until)
    assert waited.ok and "(1 of 2 waits today)" in waited.text  # the skip was no wait
    keep(agent)
    assert first["template"] not in {s["template"] for s in weekly(agent, line) if s["status"] == "open"}
    agent.clock.advance(days=8)  # a week's period ended: the channel's next step for the line comes
    keep(agent)
    assert first["template"] in {s["template"] for s in weekly(agent, line) if s["status"] == "open"}


def test_only_a_recurring_step_of_ember_s_code_is_skipped_and_never_a_pinned_one(data_dir: Path) -> None:
    agent, line = weekly_out(data_dir)
    post = next(iter(by_channel(agent, line).values()))
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), post["id"], True, "Owner", now(agent))
    pinned = plan_step(agent, line, action="skip", step_id=post["id"])
    assert not pinned.ok and "your owner pinned" in pinned.text
    added = plan_step(
        agent, line, action="add", stage="maintain", steps=[{"title": "Ask for a review", "kind": "ship"}]
    )
    assert added.ok, added.text
    [own] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {line} AND title = 'Ask for a review'")
    refused = plan_step(agent, line, action="skip", step_id=own["id"])
    assert not refused.ok and "skip is for a recurring pin, post or blog post" in refused.text


def test_a_line_waiting_for_the_owners_keep_or_drop_takes_no_reach(data_dir: Path) -> None:
    """0.53.0 (evaluation 2026-10-27, C5): live, lines #3, #4 and #6 waited for the owner's keep-or-drop, and the
    daily reviews said "passive at zero cost, the owner decides"; four cycles ($1.01) still posted on Bluesky for them.
    Their weekly pins, posts and blog posts wait for the owner's decision ('deciding'), as their review's change does
    (0.42.6); the owner's pin brings one back, and their keep ends the wait."""
    agent, line = weekly_out(data_dir)
    steps = by_channel(agent, line)
    assert steps and set(waits(agent, line, ALL)[s["title"]] for s in steps.values()) <= {None, "floor"}
    with agent.db.transaction() as conn:
        [product] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (line,))
        assert plan_keep._owner_decides(conn, agent.scope(), product, now(agent), "day 14: 3 views")
    waiting = waits(agent, line, ALL)
    assert {waiting[s["title"]] for s in steps.values()} == {"deciding"}
    assert "keep-or-drop of its line" in plan.WAITS["deciding"]
    one = next(iter(steps.values()))
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), one["id"], True, "Owner", now(agent))
    assert waits(agent, line, ALL)[one["title"]] in (None, "floor")  # the owner wants it
    [decision] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {line} AND template = 'decide/owner'")
    with agent.db.transaction() as conn:
        plan.owner_keep(conn, agent.scope(), decision["id"], "one more month", "Stefan", now(agent))
    assert "deciding" not in waits(agent, line, ALL).values()
