"""0.38.1: the plan sees the sell requests that may wait for the owner, as 0.37.9 made it see the publishing queue.

At most 6 sell requests (store.PENDING_CAPS['sell']: a listing, a change of one, a Printify product, a KDP book) wait
for the owner at once: propose_etsy_listing, propose_etsy_edit, propose_printify_product and propose_kdp_book refuse
the next (tools._room_for_request). 0.37.9 taught the plan the publish and message limits (tests/test_fixes_0379.py),
but "Propose the listing", proposing one again and the critic's fixes still looked ready while 6 sell requests
waited: a cycle cut its sleep and paid for a plan (about $0.15 before any work) whose request the tool refused. Now
such a step waits ('sales') and cuts no sleep; the cap itself stays as it is.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, store, tools  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now, working  # noqa: E402
from tests.test_fixes_0340 import TRACKER, keep, project, request  # noqa: E402
from tests.test_fixes_0350 import steered  # noqa: E402
from tests.test_fixes_0353 import CRITIC, PINS, critic, launched  # noqa: E402
from tests.test_fixes_0376 import (  # noqa: E402
    MEALS,
    asked,
    closed_without_proposing,
    cycles,
    decided,
    made,
    noted,
    waits,
)
from tests.test_fixes_0379 import refused_by  # noqa: E402

PROPOSE = "Propose the listing"


def selling(agent: Agent, count: int, line: int | None = None, executor: str = "etsy_edit") -> list[int]:
    """``count`` sell requests waiting for the owner (changes of listings, by default), as the sell tools make them."""
    with agent.db.transaction() as conn:
        made_before = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        return [
            store.insert_approval(
                conn,
                agent.scope(),
                1,
                now(agent),
                project_id=line,
                type="sell",
                title=f"A {executor}",
                description="for the owner",
                payload=json.dumps({"request": made_before + n}),
                expected_cost="nothing",
                expected_benefit="sales",
                executor=executor,
                action="{}",
            )
            for n in range(count)
        ]


def to_the_cap(agent: Agent, line: int) -> list[int]:
    """As many changes of line ``line``'s listings as the sell requests already waiting leave room for."""
    with agent.db.connection() as conn:
        waiting = store.pending_of_type(conn, agent.scope(), "sell")
    return selling(agent, store.PENDING_CAPS["sell"] - waiting, line)


def ready_to_list(agent: Agent, line: int) -> None:
    """A product line whose demand note, files and photos are made: its create stage asks for the listing."""
    noted(agent, line)
    work = cycle(agent, line)
    made(agent, work, "make_spreadsheet")
    made(agent, work, "make_image", 5)
    keep(agent)


def list_it(agent: Agent, s: plan.Steer, cycle_id: int) -> None:
    """The cycle's work: the listing its step asks for, proposed (a sell request, waiting for the owner)."""
    selling(agent, 1, int(s.line or 0), "etsy_listing")


def proposals(agent: Agent) -> list[str | None]:
    """What the create stages' "Propose the listing" steps wait on, by product line."""
    return [c.waiting for c in steered(agent).found if c.step.title == PROPOSE and c.stage == "create"]


# --- the replay ---


def test_with_six_sell_requests_waiting_propose_the_listing_is_not_taken_and_cuts_no_sleep(data_dir: Path) -> None:
    """Two products ready to list: 0.37.9 took one's "Propose the listing", cut the sleep because the other's looked
    ready, took that one too, and propose_etsy_listing refused both."""
    agent, _ = lined(data_dir, titles=())
    tracker, meals = project(agent, *TRACKER), project(agent, *MEALS)
    keep(agent)
    for line in (tracker, meals):
        ready_to_list(agent, line)
    assert proposals(agent) == [None, None]
    changes = selling(agent, store.PENDING_CAPS["sell"])  # the owner hasn't decided six changes of listings
    assert proposals(agent) == ["sales", "sales"]
    taken = cycles(agent, 3)
    assert [(t.step, t.busy) for t in taken] == [(None, False)] * 3
    with agent.db.connection() as conn:  # (the tool refuses exactly then: the test after the next)
        said = plan.step_text(conn, agent.scope(), steered(agent), explore=False)
    assert said.startswith("No step of your plan is ready (") and f"2 on {plan.WAITS['sales']})." in said
    decided(agent, changes[0], "approved")  # the owner approves one: room for one more
    assert proposals(agent) == [None, None]
    [took] = cycles(agent, 1, work=list_it)  # its listing proposed: six sell requests wait again
    assert took.title == PROPOSE and took.moved
    assert not took.busy  # 0.37.9: cut, as its step moved and the other "Propose the listing" looked ready
    keep(agent)  # the proposed listing waits for the owner; the other product's proposal still waits for room
    assert waits(agent, "You approve it, and it goes live", took.line) == "owner" and proposals(agent) == ["sales"]


def test_the_critics_fixes_wait_on_the_sell_requests_and_the_pins_on_the_publishing_queue(data_dir: Path) -> None:
    """The critic's fixes are a change of the listing (etsy_edit, a sell request); the pins are publish requests: each
    waits on its own limit only."""
    agent, line, listing = launched(data_dir)
    critic(agent, line, listing, 5)  # the critic's suggestions: its fixes are a step
    assert waits(agent, CRITIC, line) is None and waits(agent, PINS, line) is None
    posters = project(agent, "Bauhaus posters", "People hang the posters in the living room and the office")
    changes = to_the_cap(agent, posters)  # changes of another product's listings (its own: it waits on them)
    assert waits(agent, CRITIC, line) == "sales" and waits(agent, PINS, line) is None
    for executor in ("bluesky_post", "site_post", "site_post"):  # and the publish requests that may wait, waiting
        asked(agent, line, executor)
    assert waits(agent, CRITIC, line) == "sales" and waits(agent, PINS, line) == "queue"
    decided(agent, changes[0], "rejected")  # a change the owner rejected: room for another
    assert waits(agent, CRITIC, line) is None and waits(agent, PINS, line) == "queue"


def test_a_listing_proposed_again_waits_on_the_sell_requests_too(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    first = request(agent, tracker, "etsy_listing")
    keep(agent)
    decided(agent, first, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    again = f"Propose the listing again: request #{first} rejected"
    assert waits(agent, again, tracker) is None
    selling(agent, store.PENDING_CAPS["sell"])
    assert waits(agent, again, tracker) == "sales"


def test_the_plan_waits_on_the_sell_requests_exactly_while_the_tool_would_refuse(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)

    def both() -> tuple[str | None, bool]:
        with agent.db.connection() as conn:
            found = plan.limits(conn, agent.scope(), to_iso(agent.clock.day_start(agent.clock.today())))
            return refused_by(tools._room_for_request, ctx, conn, "sell"), found.sales_full

    selling(agent, store.PENDING_CAPS["sell"] - 1, executor="printify_product")
    assert both() == (None, False)
    [book] = selling(agent, 1, executor="kdp_package")
    refused, full = both()
    assert refused is not None and "6 sell requests are already waiting for your owner" in refused and full
    decided(agent, book, "withdrawn")
    assert both() == (None, False)


def test_each_executors_request_type_is_the_one_its_tool_makes() -> None:
    """store.EXECUTOR_TYPES, which the plan reads, against the type= of every _new_request call in tools.py."""
    source = (Path(tools.__file__)).read_text(encoding="utf-8")
    made: set[tuple[str, str]] = set()
    for call in ast.walk(ast.parse(source)):
        if not isinstance(call, ast.Call) or getattr(call.func, "id", None) != "_new_request":
            continue
        given = {k.arg: k.value for k in call.keywords if k.arg}
        made.add((_value(given["executor"]), _value(given["type"])))
    assert len({executor for executor, _ in made}) == len(made), made  # each executor's requests are of one type
    assert dict(made) == store.EXECUTOR_TYPES
    assert set(store.EXECUTOR_TYPES.values()) <= set(store.PENDING_CAPS)


def _value(node: ast.expr) -> Any:
    """A literal, or a module's constant as tools.py names it (site_publisher.POST, kdp.EXECUTOR)."""
    if isinstance(node, ast.Constant):
        return node.value
    assert isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name), ast.unparse(node)
    return getattr(getattr(tools, node.value.id), node.attr)


# --- what the report and the Plan tab say ---


def test_the_report_and_the_plan_tab_say_a_step_waits_on_the_sell_requests(ingress_client: Any) -> None:
    from tests.test_diagnostics import report, section  # noqa: PLC0415

    agent = ingress_client.app.state.ember.agent
    cycle(agent)
    selling(agent, store.PENDING_CAPS["sell"])
    text = section(report(ingress_client), "AGENT RECORDS")
    assert (
        "0.38.1: 'sales', a listing, a change of one, a Printify product or a KDP book the sell requests waiting for"
        " the owner would refuse. Now: publish requests waiting for the owner 0 of 3; waiting for the owner or their"
        " slot: pins 0 of 3, posts 0 of 3; sell requests waiting for the owner 6 of 6;"
    ) in text
    script = (Path(__file__).parent.parent / "app" / "web" / "static" / "js" / "app.js").read_text()
    assert 'sales: "as many sell requests as may wait for you wait: it waits until you decide one",' in script
