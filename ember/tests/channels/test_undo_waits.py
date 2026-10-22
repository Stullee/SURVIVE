"""0.42.4 (analysis-0.40.1 codebase, M3): the owner's Undo of a pin, a post or a Printify product waits for its service
too.

Since 0.42.0 (Printify since 0.42.3) an approved request that its service answers with 429, or 503 with Retry-After,
before anything changed keeps its approval and is tried again when the service says (retry.py, test_approval_boundary).
The owner's Undo of a pin, a post or a product didn't: a 429 to its delete failed it, and the owner had to press Undo
again (Etsy's Undo is a change of the listing, which waited already). Now it waits as the request it undoes would: the
same retry.park and retry.due, the same card ("Pinterest asked to wait"), a wait in the daily digest, at most
retry.TRIES times. Its delete is one call, so a "come back later" to it changed nothing. The kill switch holds it, a
paused round carries it out, the owner can cancel it, and it never ages out (connectors.AGING leaves it out).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import audit  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import bluesky, connectors, pinterest, printify, retry  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.integrations.printify import FakePrintify  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.channels.test_approval_boundary import WAIT, journal, refuse, request_of  # noqa: E402
from tests.channels.test_bluesky import a_post, post_context, posted  # noqa: E402
from tests.channels.test_pinterest import BOARD, OTHER_WORDS, a_pin, pin_context, pinned  # noqa: E402
from tests.channels.test_printify import a_proposal, pod_context, read_catalog  # noqa: E402
from tests.channels.test_printify import proposed as product_proposed  # noqa: E402
from tests.etsy_helpers import views_approval  # noqa: E402
from tests.queue_helpers import slot_comes  # noqa: E402

ANSWER = f"HTTP 429{retry.said(429, WAIT)}"  # what the fake services answer (test_approval_boundary.refuse)


@dataclass(frozen=True)
class Service:
    name: str  # as its card and the notes say it
    made: str  # the journal class of what the Undo deletes
    deletes: str  # and of the Undo
    fake: type
    delete: str  # the fake account's call that deletes
    busy: type
    things: str  # what the fake account holds (its state's key)
    publisher: str  # the agent's executor of it


SERVICES = {
    "pinterest": Service(
        "Pinterest", "pinterest.create_pin", "pinterest.delete_pin", FakePinterest, "delete_pin", pinterest.Busy,
        "pins", "pins",
    ),
    "bluesky": Service(
        "Bluesky", "bluesky.create_post", "bluesky.delete_post", FakeBluesky, "delete_post", bluesky.Busy, "posts",
        "bluesky_posts",
    ),
    "printify": Service(
        "Printify", "printify.create_product", "printify.delete_product", FakePrintify, "delete", printify.Busy,
        "products", "pod",
    ),
}  # fmt: skip
KEYS = list(SERVICES)


def live(agent: Agent, key: str) -> dict[str, Any]:
    """The pins, posts or products the fake account holds."""
    held: dict[str, Any] = getattr(agent, key).account().state[SERVICES[key].things]
    return held


def entry_of(agent: Agent, journal_class: str) -> dict[str, Any]:
    """The audit feed's newest entry of this class."""
    return next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == journal_class)


def undo(agent: Agent, journal_id: int) -> int:
    """The owner's Undo of an action (approved at once, carried out in the next round)."""
    reply = owner(agent).undo(journal_id, "Stefan")
    assert reply.status == 200, reply.body
    return int(reply.body["approval_id"])


def one_live(data_dir: Path, key: str) -> Agent:
    """A dry-run agent with one live pin, post or Printify product of Ember's."""
    if key == "pinterest":
        agent, _, _ = pinned(data_dir)
    elif key == "bluesky":
        agent, _, _ = posted(data_dir)
    else:
        agent, _, request = product_proposed(data_dir)
        assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
        assert agent.execute_approved() == [(request, "active")]
    return agent


def made(data_dir: Path, key: str) -> tuple[Agent, int]:
    """``one_live``, and the owner's Undo of it."""
    agent = one_live(data_dir, key)
    return agent, undo(agent, entry_of(agent, SERVICES[key].made)["id"])


def another(agent: Agent, key: str) -> None:
    """A second live pin, post or product (in its slot of the publishing queue)."""
    if key == "pinterest":
        assert a_pin(agent, pin_context(agent), board_id=BOARD, image="shop/pin-2.png", **OTHER_WORDS).ok
    elif key == "bluesky":
        assert a_post(agent, post_context(agent), text="Plane die Woche an einem Sonntagabend. #Printable").ok
    else:
        ctx = pod_context(agent)
        read_catalog(ctx)
        assert a_proposal(agent, ctx, image="shop/poster-2.png", title="Minimalist lake poster, matte print").ok
    executor = {"pinterest": "pinterest_pin", "bluesky": "bluesky_post", "printify": "printify_product"}[key]
    request = rows(agent, f"SELECT MAX(id) AS id FROM approvals WHERE executor = '{executor}'")[0]["id"]
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    if key != "printify":
        slot_comes(agent, key)
    assert [r for r, _ in agent.execute_approved()] == [request]


def digest(agent: Agent) -> dict[str, Any]:
    with agent.db.connection() as conn:
        _, data = audit.digest(conn, agent.scope(), agent.clock, agent.clock.today())
    return data


@pytest.mark.parametrize("key", KEYS)
def test_an_undo_its_service_asks_to_wait_for_stays_approved_and_deletes_once_then(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    [thing] = list(live(agent, key))
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy)
    due = agent.clock.now() + timedelta(seconds=WAIT)
    assert agent.execute_approved() == [(request, retry.LATER)]  # before: failed, and the owner had to press it again
    assert request_of(agent, request)["status"] == "approved" and request_of(agent, request)["closed_at"] is None
    assert list(live(agent, key)) == [thing]
    [entry] = journal(agent, request)
    assert (entry["class"], entry["status"]) == (service.deletes, "failed")
    assert json.loads(entry["after"]) == {"retry_at": to_iso(due), "error": ANSWER}
    assert entry["note"] == f"{service.name} asked to wait: retrying at {due:%H:%M} (wait 1 of {retry.TRIES}: {ANSWER})"
    shown = views_approval(agent, request)["execution"]  # its card, as a pin's, a post's or a product's says it
    assert (shown["status"], shown["due_at"], shown["error"]) == ("retrying", to_iso(due), ANSWER)
    original = entry_of(agent, service.made)
    assert original["undo"]["why_not"] == "your Undo of it is under way"
    waited = next(e for e in agent.dashboard()["audit"]["feed"] if e["approval_id"] == request)
    assert (waited["status"], waited["by"]) == ("waited", "undo")
    events = [r["message"] for r in rows(agent, "SELECT message FROM events ORDER BY id")]
    assert f"Request #{request}: {service.name} asked to wait: retrying at {due:%H:%M}" in "\n".join(events)
    assert getattr(agent, service.publisher).recover() == 0  # a restart leaves it waiting: it isn't unclear
    agent.clock.advance(seconds=WAIT - 1)
    assert agent.execute_approved() == []  # not before the service's wait is over
    agent.clock.advance(seconds=1)
    assert agent.execute_approved() == [(request, "done")]
    assert len(calls) == 2 and live(agent, key) == {}
    assert [e["status"] for e in journal(agent, request)] == ["failed", "simulated"]
    assert request_of(agent, request)["status"] == "done"
    assert views_approval(agent, request)["execution"]["status"] == "deleted"
    assert entry_of(agent, service.made)["undo"]["why_not"] == "it is undone"
    assert agent.execute_approved() == []  # never twice
    data = digest(agent)  # the owner's digest: a wait, not a failed Undo
    assert data["trouble"] == {} and data["waited_for_service"] == 1 and data["actions"]["undo"] == 1


@pytest.mark.parametrize("key", KEYS)
def test_an_undo_fails_as_before_once_it_waited_three_times(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy, times=99)
    for _ in range(retry.TRIES):
        assert agent.execute_approved() == [(request, retry.LATER)]
        agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]
    assert len(calls) == retry.TRIES + 1 and len(live(agent, key)) == 1
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember")
    assert closed["result_note"] == f"Not deleted: {ANSWER}"
    entries = journal(agent, request)
    assert [json.loads(e["after"] or "{}").get("retry_at") is not None for e in entries] == [True] * 3 + [False]
    assert entry_of(agent, service.made)["undo"]["why_not"] is None  # nothing was deleted: the owner may press it again
    data = digest(agent)
    assert data["trouble"] == {"failed": 1} and data["waited_for_service"] == 1  # it fails once
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == [] and len(calls) == retry.TRIES + 1


@pytest.mark.parametrize("key", KEYS)
def test_the_kill_switch_holds_an_undo_that_waits(data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy)
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(seconds=WAIT)
    agent.db.set_meta(KILLED_KEY, "1")
    assert agent.execute_approved() == []  # the round: nothing while the switch is on
    executor = getattr(agent, service.publisher)
    assert executor.run() == [(request, connectors.HALTED)]  # its start transaction reads the switch again
    assert executor.run(undos=True) == [(request, connectors.HALTED)]  # as a paused round's does
    assert len(calls) == 1 and [e["status"] for e in journal(agent, request)] == ["failed"]  # nothing begun again
    assert views_approval(agent, request)["execution"]["status"] == "retrying"
    agent.db.set_meta(KILLED_KEY, "0")
    assert agent.execute_approved() == [(request, "done")]
    assert len(calls) == 2 and live(agent, key) == {}


@pytest.mark.parametrize("key", KEYS)
def test_a_paused_round_carries_out_an_undo_that_waits(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """While the agent is paused a round carries out only the owner's Undos (0.15.0, Agent.execute_approved): one that
    waits for its service too, once it is due."""
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy)
    agent.economy.set_paused(True)
    assert agent.executor_blocked() == "The agent is paused"
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(seconds=WAIT - 1)
    assert agent.execute_approved() == []
    agent.clock.advance(seconds=1)
    assert agent.execute_approved() == [(request, "done")]
    assert len(calls) == 2 and live(agent, key) == {}


@pytest.mark.parametrize("key", KEYS)
def test_an_undo_that_waits_never_ages_out(data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy, times=2)
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(days=connectors.APPROVED_DAYS + 1)  # the app was down, say
    assert agent.execute_approved() == [(request, retry.LATER)]  # tried again, not closed as too old
    assert request_of(agent, request)["status"] == "approved"
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "done")]
    assert len(calls) == 3 and live(agent, key) == {}


@pytest.mark.parametrize("key", KEYS)
def test_the_owner_cancels_an_undo_that_waits_and_may_press_it_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    service = SERVICES[key]
    agent, request = made(data_dir, key)
    refuse(monkeypatch, service.fake, service.delete, service.busy)
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert owner(agent).close(request, {"outcome": "failed"}, "Stefan").status == 200  # begun, but nothing deleted
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [] and len(live(agent, key)) == 1
    assert request_of(agent, request)["closed_by"] == "Stefan"
    original = entry_of(agent, service.made)
    assert original["undo"]["why_not"] is None
    again = undo(agent, original["id"])
    assert agent.execute_approved() == [(again, "done")] and live(agent, key) == {}


@pytest.mark.parametrize("key", KEYS)
def test_the_other_undos_of_the_round_wait_for_the_next_one(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A service that asks to come back later would answer the next delete the same: it isn't sent, and keeps its
    tries."""
    service = SERVICES[key]
    agent = one_live(data_dir, key)
    another(agent, key)
    both = [e for e in agent.dashboard()["audit"]["feed"] if e["class"] == service.made]
    first, second = (undo(agent, e["id"]) for e in reversed(both))  # the older first
    calls = refuse(monkeypatch, service.fake, service.delete, service.busy)
    assert agent.execute_approved() == [(first, retry.LATER)]
    assert journal(agent, second) == [] and views_approval(agent, second)["execution"]["status"] == "waiting"
    assert agent.execute_approved() == [(second, "done")]  # the next round; the first waits for its time
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(first, "done")]
    assert len(calls) == 3 and live(agent, key) == {}


def test_only_the_owner_s_undo_waits_in_its_journal_entry_alone(data_dir: Path) -> None:
    """A product keeps its row in printify_products, which a wait must leave 'failed' (a row left 'running' would be
    unclear at the next start): retry.park without a table is refused for it, and for an Undo not under way."""
    agent, request = made(data_dir, "printify")
    product = rows(agent, "SELECT approval_id FROM printify_products")[0]["approval_id"]
    busy = printify.Busy(ANSWER, WAIT)
    with agent.db.transaction() as conn:
        with pytest.raises(RuntimeError, match="waits in its executor's table"):
            retry.park(conn, None, product, busy, agent.clock, "Printify")
        with pytest.raises(RuntimeError, match="isn't running"):
            retry.park(conn, None, request, busy, agent.clock, "Printify")
    assert journal(agent, request) == []
