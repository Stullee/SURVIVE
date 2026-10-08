"""0.19.3: venture cycles are for new ventures. The owner: "ventures for me was just what is something new we can try.
once we do it, it goes into an active project".

Live (2026-10-03 and 04), 2 of 12 cycles were venture cycles, and both tried to set up the first test of a venture the
owner had backed (the limit of 8 open projects refused it); 4 more that were the ventures' turn became ordinary cycles
because a message of the owner's waited; and READY's five places went to two builds, an appraisal and two triages, so
no brainstorm was offered while the ideas ran low.

- A backed venture is project work: Ember's code opens its project (once), ordinary cycles run its first test, READY
  has no "build" any more, a venture cycle isn't aimed at a backed venture, venture cycles run in explore only, and a
  backed venture takes none of the room of the 8 being researched (tests/test_desk.py, tests/test_burn_modes.py).
- A waiting message makes a venture cycle an ordinary one only when it woke the cycle; a scheduled venture cycle
  answers it first (tests/test_obligations.py).
- A due brainstorm keeps READY's last place (tests/test_desk.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import store, tools, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, shop_context  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import DROPSHIPPING, VENTURING, plan  # noqa: E402

FIRST_TEST = "Sell 3 sample boxes to shops in a week"


def backed(agent: Any, venture: int = DROPSHIPPING) -> None:
    with agent.db.transaction() as conn:
        ventures.update(conn, venture, to_iso(agent.clock.now()), first_test=FIRST_TEST)
    assert owner(agent).decide_venture(venture, {"action": "back", "confirm": True}, "Stefan").status == 200


def projects_of(agent: Any, venture: int) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT id, title, hypothesis, status, next_step FROM projects WHERE venture_id = {venture}")


def test_a_backed_venture_gets_its_project_once(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])] * 3), settings=VENTURING)
    assert projects_of(agent, DROPSHIPPING) == []
    backed(agent)
    agent.run_cycle("schedule")
    [project] = projects_of(agent, DROPSHIPPING)
    test = rows(agent, f"SELECT test_milestone_id FROM ventures WHERE id = {DROPSHIPPING}")[0]["test_milestone_id"]
    assert (project["title"], project["hypothesis"], project["status"]) == ("Dropshipping store", FIRST_TEST, "active")
    assert project["next_step"] == ""  # 0.35.0: its steps are the plan tree's
    events = [e["message"] for e in agent.db.recent_events(limit=40)]
    assert (
        f"Ember's code opened project #{project['id']} for venture #{DROPSHIPPING}, which your owner backed: its first "
        f"test (milestone #{test}) is the project's work, in ordinary cycles"
    ) in events
    with agent.db.transaction() as conn:  # the agent closed it: Ember's code doesn't open it again
        store.update_project(conn, project["id"], to_iso(agent.clock.now()), status="abandoned")
    agent.run_cycle("schedule")
    assert [p["status"] for p in projects_of(agent, DROPSHIPPING)] == ["abandoned"]


def test_a_venture_cycle_isnt_aimed_at_a_backed_venture(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=["Research it"], venture=DROPSHIPPING)])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    backed(agent)
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT venture, venture_id FROM cycles ORDER BY id")[-1] == {"venture": 1, "venture_id": None}
    work = [r for r in fake.sent if request_kind(r) == "work"]
    brief = work[0]["messages"][0]["content"][0]["text"]
    assert (
        f"Venture #{DROPSHIPPING} is building: your owner backed it, so its work is its project's, in ordinary cycles."
        in brief
    )


def test_backed_ventures_take_none_of_the_room_for_research(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    backed(agent)
    ctx = shop_context(agent)
    with agent.db.connection() as conn:
        room = ventures.MAX_ACTIVE - ventures.count(conn, agent.scope(), ventures.EXPLORED)
    for number in range(room):
        ctx.state = tools.CycleTools()  # venture_create: 3 a cycle
        made = call(ctx, "venture_create", {"title": f"New way {number}", "pitch": "p", "stage": "researching"})
        assert made.ok, made.text
    ctx.state = tools.CycleTools()
    refused = call(ctx, "venture_create", {"title": "One more", "pitch": "p", "stage": "researching"})
    assert not refused.ok and f"{ventures.MAX_ACTIVE} ventures are being researched already" in refused.text
    assert call(ctx, "venture_create", {"title": "An idea", "pitch": "p", "stage": "idea"}).ok
