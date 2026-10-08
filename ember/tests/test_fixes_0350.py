"""0.35.0: the plan tree steers (Release 2b).

In 0.34.0 the tree ran in the shadow. Now Ember's code takes each cycle's step from it before the plan: the step
decides what the cycle is (a marketing step a marketing cycle, any other an ordinary one, unless it is the ventures'
turn) and which line its tools work on, and the plan sees it as YOUR STEP, under YOUR PLAN (the tree under the
owner's goal) in place of READY and the ROADMAP. The owner's decisions on a product's requests are steps taken first,
like a promise due. Ember changes her plan with plan_step, each change kept with its reason; she can hold a product to
work elsewhere, but only the owner closes or drops one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import context, obligations, plan, slack, store, tools, weights  # noqa: E402
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_fixes_0280 import lined, now, texts, working  # noqa: E402
from tests.test_fixes_0340 import ALL, BOOK, TRACKER, keep, project, request, steps_of, tree  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import JOURNAL, VENTURING  # noqa: E402

WORK = {
    "assessment": "ok",
    "goal": "Work on my step",
    "money_path": "Buyers pay for what the line sells",
    "focus_project_id": None,
    "focus_venture_id": None,
    "focus_milestone_id": None,
    "steps": ["work on the step"],
    "sleep_minutes": 600,
}


def steered(agent: Agent, venture_turn: bool = False, channels: dict[str, bool] = ALL) -> plan.Steer:
    with agent.db.connection() as conn:
        return plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), channels, venture_turn=venture_turn)


def step_id(agent: Agent, line: int, title: str) -> int:
    with agent.db.connection() as conn:
        [row] = conn.execute("SELECT id FROM plan_nodes WHERE project_id = ? AND title = ?", (line, title)).fetchall()
    return int(row["id"])


def changes(agent: Agent) -> list[tuple[int, str, str]]:
    return [(r["node_id"], r["actor"], r["action"]) for r in rows(agent, "SELECT * FROM plan_changes ORDER BY id")]


def plan_step(agent: Agent, line: int | None, **args: Any) -> tools.Outcome:
    return call(working(agent, line), "plan_step", {"why": "the research says so", **args})


# --- the tree takes each cycle's step ---


def test_the_tree_takes_each_cycles_step_its_line_and_kind_and_the_plan_sees_your_step(data_dir: Path) -> None:
    agent, fake = lined(data_dir)  # lines #1 Planner, #2 Poster, #3 Checklist, after an idle cycle
    fake.script.extend(
        [Plan({**WORK, "focus_project_id": 3}), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL]
    )
    end = agent.run_cycle("schedule")
    assert end.status == "completed"
    [first, pick] = rows(agent, "SELECT * FROM plan_picks ORDER BY id")
    assert (first["cycle_id"], first["decided"], first["node_id"]) == (1, "none", None)  # no product yet
    assert pick["cycle_id"] == 2 and pick["kind"] == "ordinary" and pick["decided"] == "weight"
    assert pick["line"] == pick["product"] and pick["node_id"] is not None
    # the plan named line #3 its focus: the tree's step decides the cycle's line
    assert rows(agent, "SELECT project_id FROM cycles WHERE id = 2") == [{"project_id": pick["line"]}]
    planner = texts(fake, "plan")[-1]
    assert f"\n== {context.STEP_HEADING} ==\nStep #{pick['node_id']} of product line #{pick['line']}" in planner
    assert f"\n== {context.PLAN_HEADING} ==\nToday: " in planner
    assert "== READY ==" not in planner and "== ROADMAP ==" not in planner
    assert "\nDone when: " in planner and "\nWhy: the heaviest step that is ready. Weight worth " in planner
    first_plan = texts(fake, "plan")[0]  # cycle 1, no product: a new one may start (the explore burn mode)
    assert "No step of your plan is ready." in first_plan and plan.NEW_PRODUCT in first_plan
    # its plan had steps ready: the 600 minutes it chose are cut (slack.py), not below the owner's interval
    assert end.sleep_minutes == max(slack.SLEEP_MINUTES, agent.settings.wake_interval_minutes)
    assert slack.WHY in (end.sleep_cut or "")
    assert "ready" not in json_schema(fake)["required"]  # an ordinary plan has no READY item to name


def json_schema(fake: Any) -> dict[str, Any]:
    return [r for r in fake.sent if request_kind(r) == "plan"][-1]["output_config"]["format"]["schema"]


def test_a_marketing_step_makes_a_marketing_cycle_and_the_ventures_turn_a_venture_cycle(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    poster = project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    keep(agent)
    with agent.db.transaction() as conn:  # launched: its stages before maintain done, its week's pin due
        for stage in plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'stage'", (poster,)):
            if stage["stage"] != "maintain":
                for step in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (stage["id"],)):
                    plan._close(conn, step["id"], now(agent), "done", "test", by="owner")
                plan._close(conn, stage["id"], now(agent), "done", "test", by="owner")
    keep(agent)
    marketing = steered(agent)
    assert marketing.kind == "marketing" and marketing.step is not None
    assert marketing.step.title == "This week's pin for Bauhaus posters" and marketing.line == poster
    venture = steered(agent, venture_turn=True)
    assert venture.kind == "venture" and venture.step is None and venture.pick.decided == "venture"
    off = steered(agent, channels={"pinterest": False, "bluesky": False, "blog": False})
    assert off.step is None and off.kind == "ordinary"  # the pin waits on a channel that isn't set up
    with agent.db.connection() as conn:
        text = plan.step_text(conn, agent.scope(), off, explore=False)
    assert text.startswith("No step of your plan is ready (2 on a channel that isn't set up).")  # a pin, a post
    assert "Nothing new starts in this burn mode" in text


def test_the_owners_waiting_messages_skip_the_ventures_turn(data_dir: Path) -> None:
    fake = FakeTransport(script=[Plan(WORK), Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake, cycles=0, settings=VENTURING)  # every cycle the ventures' turn
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO messages (mode, session, life_id, created_at, sender, text) VALUES (?, ?, ?, ?, 'owner', ?)",
            (agent.scope().mode, agent.scope().session, agent.scope().life_id, now(agent), "What is next?"),
        )
    agent.run_cycle("owner")
    assert rows(agent, "SELECT cycle_id, kind FROM plan_picks") == [{"cycle_id": 1, "kind": "ordinary"}]
    assert rows(agent, "SELECT venture FROM cycles WHERE id = 1") == [{"venture": 0}]


# --- the owner's decisions and promises ---


def decided(agent: Agent, line: int, status: str = "rejected") -> int:
    """A request of the line's the owner decided, and the obligation Ember's code keeps for it."""
    asked = request(agent, line, "etsy_listing")
    stamp = now(agent)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decision_comment = 'The photos are dark' WHERE id = ?",
            (status, stamp, asked),
        )
        obligations.keep(conn, agent.scope(), stamp, "2000-01-01T00:00:00+00:00")
    [owed] = rows(agent, f"SELECT id FROM obligations WHERE approval_id = {asked}")
    return int(owed["id"])


def test_an_owners_decision_on_a_products_request_is_a_step_taken_first_once_a_day(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    book = project(agent, *BOOK)
    keep(agent)
    owed = decided(agent, tracker)
    said = keep(agent)
    assert any(f"obligation #{owed} (a decision) is a step of product" in s for s in said)
    [step] = rows(agent, f"SELECT * FROM plan_nodes WHERE obligation_id = {owed}")
    assert step["kind"] == "fix" and step["stage"] == "research" and step["source"] == "code"
    assert step["title"].startswith(f"Obligation #{owed}: your owner rejected request #")
    assert changes(agent) == [(step["id"], "code", "add")]
    first = steered(agent)
    assert first.pick.decided == "promise" and first.step is not None and first.step.id == step["id"]
    with agent.db.transaction() as conn:  # taken by a cycle: the next ones weigh it like any step for a day
        plan.record(conn, agent.scope(), working(agent).cycle_id, now(agent), first)
    again = steered(agent)
    assert again.pick.decided == "weight" and {c.step.product for c in again.found} == {tracker, book}
    with agent.db.transaction() as conn:
        obligations.close_one(conn, owed, "redid the photos", "agent", None, now(agent))
    keep(agent)
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {step['id']}") == [
        {"status": "done", "result": "Ember's code: the obligation was closed"}
    ]


def test_a_promise_nothing_in_front_of_carries_is_a_step_of_its_own(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    keep(agent)
    request(agent, book, "kdp_package")  # proposed: release waits on the owner's publishing
    with agent.db.transaction() as conn:
        message = store.insert_message(conn, agent.scope(), 1, "The book's cover comes tomorrow.", now(agent))
        promise = obligations.promise(
            conn,
            agent.scope(),
            1,
            message,
            "A better front picture for the Haushaltsbuch",
            agent.clock.today().isoformat(),
            now(agent),
            project_id=book,
        )
    keep(agent)
    assert steps_of(agent, book)["You publish it at KDP and add its Amazon link"] == "open"
    pick = steered(agent)
    assert pick.pick.decided == "promise" and pick.step is not None
    assert pick.step.title.startswith(f"Keep promise #{promise}: ") and pick.line == book


# --- Ember's changes to her plan ---


def test_ember_adds_splits_and_replaces_her_steps_but_never_what_the_owner_or_the_code_put_there(
    data_dir: Path,
) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    photos = step_id(agent, tracker, "Make 5 photos and look at each")
    files = step_id(agent, tracker, "Make the product files, in the audience's languages, and check them")
    pins = step_id(agent, tracker, "Pin it twice, with different pictures")
    approve = step_id(agent, tracker, "You approve it, and it goes live")
    added = plan_step(
        agent, tracker, action="add", step_id=demand, steps=[{"title": "Count the top 20 listings", "kind": "create"}]
    )
    assert added.ok and added.text.endswith(f" to the research stage of line #{tracker}.")
    research = [
        r["title"]
        for r in rows(
            agent,
            f"SELECT title FROM plan_nodes WHERE project_id = {tracker}"
            " AND stage = 'research' AND level = 'step' ORDER BY seq, id",
        )
    ]
    assert research == ["Count the top 20 listings", "Write the demand note: searches, competitors, prices"]
    split = plan_step(
        agent,
        tracker,
        action="split",
        step_id=photos,
        steps=[{"title": "Photos 1-2: the cover", "kind": "create"}, {"title": "Photos 3-5: pages", "kind": "create"}],
    )
    assert split.ok and "stays after them" in split.text  # Ember's code still checks the five photos
    assert steps_of(agent, tracker)["Make 5 photos and look at each"] == "open"
    replaced = plan_step(
        agent, tracker, action="replace", step_id=files, steps=[{"title": "Reuse the files of #3", "kind": "create"}]
    )
    assert (
        replaced.ok
        and steps_of(agent, tracker)["Make the product files, in the audience's languages, and check them"] == "dropped"
    )  # the create stage has a check of its own: Ember can't loosen it
    loosen = plan_step(agent, tracker, action="replace", step_id=pins, steps=[{"title": "Pin it", "kind": "market"}])
    assert not loosen.ok and "split it (plan_step split) instead" in loosen.text
    owners = plan_step(agent, tracker, action="split", step_id=approve, steps=[{"title": "a", "kind": "ship"}] * 2)
    assert not owners.ok and "your owner's" in owners.text
    done = plan_step(agent, tracker, action="done", step_id=photos)
    assert not done.ok and "Ember's code checks step" in done.text
    [mine] = rows(agent, "SELECT id FROM plan_nodes WHERE title = 'Reuse the files of #3'")
    assert plan_step(agent, tracker, action="done", step_id=mine["id"], why="copied #3's files").ok
    assert rows(agent, f"SELECT status, closed_by, result FROM plan_nodes WHERE id = {mine['id']}") == [
        {"status": "done", "closed_by": "agent", "result": "copied #3's files"}
    ]
    other = project(agent, *BOOK)
    keep(agent)
    elsewhere = plan_step(agent, other, action="done", step_id=mine["id"])
    assert not elsewhere.ok and f"this cycle works on line #{other}" in elsewhere.text
    assert {a for _, actor, a in changes(agent) if actor == "agent"} == {"add", "split", "replace", "done"}


def test_a_step_waits_only_on_a_block_the_code_checks_and_is_taken_up_again_once_it_is_gone(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    demand = step_id(agent, tracker, "Write the demand note: searches, competitors, prices")
    asked = request(agent, tracker, "reddit_link")
    nowhere = plan_step(agent, tracker, action="wait", step_id=demand, on="owner", ref=asked + 1)
    assert not nowhere.ok and f"request #{asked + 1} isn't waiting for your owner" in nowhere.text
    far = plan_step(agent, tracker, action="wait", step_id=demand, on="date", until="2099-01-01")
    assert not far.ok and f"at most {plan.WAIT_DAYS} days ahead" in far.text
    waits = plan_step(agent, tracker, action="wait", step_id=demand, on="owner", ref=asked)
    assert waits.ok and waits.text.endswith(f"(1 of {plan.WAITS_A_DAY} waits today).")
    found = {c.step.id: c for c in steered(agent).found}
    assert found[demand].waiting == "owner" and found[demand].step.blocked
    day = (agent.clock.today() + timedelta(days=3)).isoformat()
    files = step_id(agent, tracker, "Make the product files, in the audience's languages, and check them")
    assert plan_step(agent, tracker, action="wait", step_id=files, on="date", until=day).ok
    third = plan_step(agent, tracker, action="wait", step_id=files, on="step", ref=demand)
    assert not third.ok and f"{plan.WAITS_A_DAY} steps were said to wait today already" in third.text
    with agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = 'rejected', decided_at = ? WHERE id = ?", (now(agent), asked))
    said = keep(agent)
    assert f"Plan tree: step #{demand} is ready again (request #{asked} is rejected)." in said
    assert rows(agent, f"SELECT waiting, wait_ref FROM plan_nodes WHERE id = {demand}") == [
        {"waiting": None, "wait_ref": None}
    ]
    assert rows(agent, f"SELECT waiting FROM plan_nodes WHERE id = {files}") == [{"waiting": "date"}]
    assert (demand, "code", "unwait") in changes(agent)


def test_ember_holds_a_product_to_work_elsewhere_but_only_the_owner_closes_one(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    book = project(agent, *BOOK)
    keep(agent)
    with agent.db.transaction() as conn:
        message = store.insert_message(conn, agent.scope(), 1, "The tracker comes Friday.", now(agent))
        promise = obligations.promise(
            conn,
            agent.scope(),
            1,
            message,
            "The tracker",
            agent.clock.today().isoformat(),
            now(agent),
            project_id=tracker,
        )
    keep(agent)
    promised = plan_step(agent, tracker, action="hold", why="the book comes first")
    assert not promised.ok and "has a promise open: no hold" in promised.text
    with agent.db.transaction() as conn:
        obligations.close_one(conn, promise, "sent it", "agent", None, now(agent))
    keep(agent)
    held = plan_step(agent, tracker, action="hold", why="the book comes first")
    assert held.ok and "on hold" in held.text
    found = [c for c in steered(agent).found if c.step.product == tracker]
    assert found and all(c.waiting == "hold" for c in found)
    resumed = plan_step(agent, book, action="resume", project_id=tracker, why="the book is proposed")
    assert resumed.ok and rows(
        agent, f"SELECT hold_reason FROM plan_nodes WHERE project_id = {tracker} AND level = 'product'"
    ) == [{"hold_reason": None}]
    closing = call(working(agent, tracker), "project_update", {"project_id": tracker, "status": "abandoned"})
    assert not closing.ok and "status must be one of idea, active, waiting" in closing.text
    made = call(working(agent, None), "project_create", {"title": "Stickers", "hypothesis": "h", "status": "idea"})
    assert made.ok and "lays it out in your plan" in made.text
    stale = call(
        working(agent, None), "project_create", {"title": "More", "hypothesis": "h", "next_step": "n", "status": "idea"}
    )
    assert not stale.ok and "unknown field 'next_step'" in stale.text
    assert tuple(a for _, actor, a in changes(agent) if actor == "agent") == ("hold", "resume")


def test_the_tool_is_an_ordinary_and_marketing_cycles_and_milestone_plan_is_gone() -> None:
    assert "plan_step" in tools.SPECS and "milestone_plan" not in tools.SPECS
    named = {d["name"] for d in tools.definitions(etsy=True)}
    assert "plan_step" in named and "milestone_plan" not in named
    assert "plan_step" not in {d["name"] for d in tools.definitions(etsy=True, venture=True)}
    assert "plan_step" in {d["name"] for d in tools.definitions(etsy=True, marketing=True)}
    assert weights.STREAK_CAP == 3  # the prompts' LINE_STREAK


def test_the_sleep_is_cut_while_the_plan_has_steps_ready() -> None:
    assert slack.sleep(600, True, 60, "explore") == slack.SLEEP_MINUTES
    assert slack.sleep(600, False, 60, "explore") == 600
    assert slack.sleep(600, True, 60, "maintenance") == 600
    assert slack.sleep(600, True, 240, "explore") == 240


# --- decide-by dates in place of the listing test's bars, and the records the tree takes the place of ---


def decide(agent: Agent, line: int, days_live: int, funnel: Any = None) -> list[str]:
    """The keeper's decide-by dates of line ``line``, live ``days_live`` days, with ``funnel`` as its numbers."""
    from app.agent import reach

    with agent.db.transaction() as conn:
        [product] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (line,))
        start = (agent.clock.today() - timedelta(days=days_live)).isoformat()
        conn.execute("UPDATE plan_nodes SET live_since = ? WHERE id = ?", (start, product["id"]))
        product = plan.node(conn, agent.scope(), product["id"])
        facts = plan.Facts(conn, agent.scope(), now(agent))
        facts._funnels = {line: funnel or reach.Funnel(listings=[1])}
        return plan._decide_by(facts, product, now(agent), agent.clock.today())


def product_row(agent: Agent, line: int) -> dict[str, Any]:
    [row] = rows(agent, f"SELECT * FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    return row


def test_a_listing_products_decide_by_dates_push_its_marketing_and_ask_the_owner(data_dir: Path) -> None:
    from app.agent import reach

    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    assert decide(agent, tracker, 6) == []  # not day 7 yet
    said = decide(agent, tracker, 7)
    assert said == [
        f"Plan tree: line #{tracker} has 0 views on day 7 (of {plan.DAY7_VIEWS}): its marketing comes first."
    ]
    pushed = product_row(agent, tracker)
    assert (
        json.loads(pushed["decide_by"]) == {"day7": "missed"}
        and pushed["pushed_until"] == (agent.clock.today() + timedelta(days=plan.PUSH_DAYS)).isoformat()
    )
    with agent.db.connection() as conn:
        assert plan._missed(conn, agent.scope(), agent.clock.today()) == {tracker}
    unseen = decide(agent, tracker, 14)  # nobody was brought to it: one more try, not a decision
    assert any("too little reach to judge it: one more try" in s for s in unseen)
    assert json.loads(product_row(agent, tracker)["decide_by"])["day14"] == "retry"
    late = decide(agent, tracker, 28, reach.Funnel(listings=[1], views=12, pins=3))
    assert any("your owner decides on it" in s for s in late)
    found = json.loads(product_row(agent, tracker)["decide_by"])
    assert (found["day28"], found["day21"]) == ("decide", "decide")  # one step for the owner, not two
    [ask] = rows(agent, f"SELECT * FROM plan_nodes WHERE template = 'decide/owner' AND project_id = {tracker}")
    assert ask["kind"] == "owner" and ask["stage"] == "maintain" and ask["title"].startswith("You decide: keep line #")
    sold = project(agent, "Nebenkosten-Vorlage", "Eine Vorlage für die Nebenkostenabrechnung")
    keep(agent)
    decide(agent, sold, 21, reach.Funnel(listings=[2], views=40, favorites=3, orders=1, pins=2))
    assert rows(agent, "SELECT title, check_kind FROM plan_nodes WHERE template = 'decide/scale'") == [
        {"title": plan.SCALE, "check_kind": "agent"}
    ]
    assert json.loads(product_row(agent, sold)["decide_by"]) == {"day7": "met", "day14": "met", "day21": "scale"}


def test_the_milestones_the_tree_takes_the_place_of_move_once_with_the_owners_unlocks(data_dir: Path) -> None:
    from app.agent import policy, roadmap

    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    stamp = now(agent)
    with agent.db.transaction() as conn:
        mine = roadmap.create(
            conn, agent.scope(), title="Tracker listed", measure="x", due="2026-12-01", now=stamp, project_id=tracker
        )
        loose = roadmap.create(conn, agent.scope(), title="A goal of mine", measure="x", due="2026-12-01", now=stamp)
        policy.set_grant(conn, agent.scope(), mine, "qa_fix", "auto", stamp, by="Felix", why="small fixes")
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (plan.MOVED_KEY, str(loose), stamp),
        )
        later = roadmap.create(conn, agent.scope(), title="After the upgrade", measure="x", due="2026-12-01", now=stamp)
    said = keep(agent)
    assert f"Plan tree: your unlock for QA fixes on milestone #{mine} is now on product line #{tracker}." in said
    milestones = {r["id"]: r for r in rows(agent, "SELECT id, status, closed_by, result, product_node FROM milestones")}
    assert (milestones[mine]["status"], milestones[mine]["closed_by"]) == ("dropped", "code")
    assert milestones[mine]["result"].startswith("0.35.0: a step of your plan now (#")
    assert milestones[loose]["result"] == "0.35.0: your plan's steps take the place of your own milestones."
    assert milestones[later]["status"] == "open"  # made after the upgrade: not the tree's to move
    [own] = [r for r in milestones.values() if r["product_node"] is not None]
    with agent.db.connection() as conn:
        assert [g["rule"] for g in policy.standing(conn, agent.scope()).get(own["id"], [])] == ["qa_fix"]
        assert own["id"] not in {m["id"] for m in roadmap.open_milestones(conn, agent.scope())}  # hidden
    assert steps_of(agent, tracker)[f"From your milestone #{mine}: Tracker listed"] == "open"
    assert not [s for s in keep(agent) if "closed:" in s]  # once


# --- a product's type from its records, a channel's own product, the owner's Plan tab ---


def test_a_plain_product_takes_its_type_once_its_records_name_it(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Hiking trail guides",))  # its words name no type
    keep(agent)
    [product] = rows(agent, "SELECT id, template FROM plan_nodes WHERE level = 'product'")
    assert product["template"].startswith("generic@")
    added = plan_step(agent, 1, action="add", stage="create", steps=[{"title": "Sketch the map", "kind": "create"}])
    assert added.ok, added.text
    request(agent, 1, "etsy_listing")  # its first listing request names it
    said = keep(agent)
    assert "Plan tree: line #1's records show its type, Etsy download: it takes that type's stages and steps." in said
    nodes = {n["title"]: n for n in tree(agent) if n["project_id"] == 1 and n["level"] == "step"}
    assert nodes["Make it"]["status"] == "dropped" and nodes["Propose the listing"]["status"] == "done"  # its check
    # her own step moved along, into the new create stage (done with it: the listing is proposed)
    assert [n["status"] for n in tree(agent) if n["title"] == "Sketch the map"] == ["dropped", "done"]
    with agent.db.connection() as conn:
        shown = plan.view(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    assert [(p["platform"], [q["template"] for q in p["products"]]) for p in shown["projects"]] == [
        ("etsy", ["Etsy download"])  # under its type's project, though its node stays where it was laid out
    ]
    assert not [s for s in keep(agent) if "records show its type" in s]  # once


def test_a_retyped_product_shows_and_works_only_its_types_stages(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Hiking trail guides",))
    keep(agent)
    request(agent, 1, "etsy_listing")
    keep(agent)
    [product] = [q for p in agent.plan()["projects"] for q in p["products"]]
    assert [s["stage"] for s in product["stages"]] == ["research", "create", "release", "launch", "maintain"]
    assert not [st for s in product["stages"] for st in s["steps"] if st["status"] == "dropped"]
    added = plan_step(agent, 1, action="add", stage="maintain", steps=[{"title": "Ask for a review", "kind": "ship"}])
    assert added.ok, added.text  # into the type's maintain stage, not the generic one it replaced
    with agent.db.transaction() as conn:  # launched: its maintain stage takes the recurring steps
        for stage in plan.nodes(conn, agent.scope(), "project_id = 1 AND level = 'stage' AND status = 'open'"):
            if stage["stage"] != "maintain":
                for step in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (stage["id"],)):
                    plan._close(conn, step["id"], now(agent), "done", "test", by="owner")
                plan._close(conn, stage["id"], now(agent), "done", "test", by="owner")
    keep(agent)
    maintain = [n for n in tree(agent) if n["project_id"] == 1 and n["stage"] == "maintain" and n["level"] == "stage"]
    assert [m["status"] for m in maintain] == ["dropped", "open"]
    under = rows(agent, f"SELECT title, template FROM plan_nodes WHERE parent_id = {maintain[1]['id']}")
    assert "Ask for a review" in [u["title"] for u in under]
    assert [u for u in under if (u["template"] or "").startswith("recurring/")]


def test_a_retyped_products_stage_its_type_lacks_hands_its_steps_on(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Hiking trail guides",))
    keep(agent)
    added = plan_step(agent, 1, action="add", stage="create", steps=[{"title": "Sketch the map", "kind": "create"}])
    assert added.ok, added.text
    request(agent, 1, "site_post")  # a site post has no create stage: the release stage takes her step
    keep(agent)
    stages = [n for n in tree(agent) if n["project_id"] == 1 and n["level"] == "stage" and n["status"] != "dropped"]
    assert [s["stage"] for s in stages] == ["research", "release", "maintain"]
    [moved] = [n for n in tree(agent) if n["title"] == "Sketch the map" and n["status"] == "open"]
    assert moved["stage"] == "release" and moved["parent_id"] == stages[1]["id"]


def test_a_channels_own_product_keeps_its_tools_in_an_ordinary_cycle(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Pinterest boards for the shop",))  # a channel's own product
    keep(agent)
    with agent.db.connection() as conn:
        steer = plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
        assert steer.kind == "ordinary" and plan.on_channel(conn, agent.scope(), steer.step)
        assert not plan.on_channel(conn, agent.scope(), None)


def test_the_plan_tab_has_each_products_unlocks_and_its_owner_actions(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    shown = agent.plan()
    [product] = [q for p in shown["projects"] for q in p["products"]]
    rules = {r["rule"]: r for r in product["autonomy"]}
    assert rules["qa_fix"]["level"] == "manual" and rules["qa_fix"]["fits"] is True
    assert rules["email_reply"]["fits"] is False  # a product's unlocks cover its listings, not email replies
    assert product["milestone"] is None and shown["unlocks_off"] is not None
    actions = owner(agent)
    assert actions.product_autonomy(product["id"], {"rule": "qa_fix", "level": "auto"}, "Stefan").status == 200
    again = {q["line"]: q for p in agent.plan()["projects"] for q in p["products"]}[tracker]
    assert again["milestone"] is not None and {r["rule"]: r["level"] for r in again["autonomy"]}["qa_fix"] == "auto"
    assert actions.end_product(product["id"], {"why": "No demand."}, "Stefan", done=False).status == 200
    assert rows(agent, f"SELECT status FROM projects WHERE id = {tracker}") == [{"status": "abandoned"}]
    assert agent.plan()["projects"][0]["products"][0]["status"] == "dropped"
