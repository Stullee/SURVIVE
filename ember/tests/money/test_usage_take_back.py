"""0.42.2 (usage, analysis-0.40.1 audit): an accepted usage field can be taken back.

Since 0.42.0 the owner's "Treat usage.X = "Y" as standard price" refunds the uncertain calls down to their price once
and adds the field and value to economy.standard_usage for good (test_uncertain_bills.py). Nothing removed a pair: one
accepted by mistake, or a field that later came to cost more, priced every call carrying it as standard until someone
edited the database. Now the dashboard lists each accepted value with **Take back** (POST
api/economy/usage/standard/take_back). From then on its answers are charged at their worst case and offered again, as
before the acceptance; what is settled stays (the ledger only grows), and the owner is told so before and after: "past
refunds stay; calls from now on are charged at the worst case again". Accepting it again settles only the calls
charged since.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import LoadedSettings
from app.economy import metering
from app.economy.service import Economy
from tests.conftest import HA_CORE
from tests.economy_helpers import restart
from tests.money.test_uncertain_bills import (
    AGENT,
    CSRF,
    KNOWN,
    LIVE,
    STANDARD,
    accept,
    answers,
    live_economy,
    refunds,
)

TAKE_BACK = "api/economy/usage/standard/take_back"
SAID = "past refunds stay; calls from now on are charged at the worst case again"
SETTLED = ("economy.usage_settled.priority=standard", "economy.usage_netted.priority=standard")


def take_back(economy: Economy, **body: Any) -> Any:
    """The owner's Take back on the dashboard."""
    return economy.take_back_usage({**STANDARD, **body}, "Stefan")


def accepted(economy: Economy) -> list[int]:
    """Answers with usage.priority "standard" the owner then accepts (their refund shown); the calls' costs."""
    results = answers(economy, 2, priority="standard")
    [offer] = economy.uncertain_bills()
    assert accept(economy, offer).status == 200
    return [r.cost_micros for r in results]


def rows(economy: Economy, table: str) -> list[tuple[Any, ...]]:
    with economy.db.connection() as conn:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def messages(economy: Economy, limit: int = 5) -> list[str]:
    return [e["message"] for e in economy.db.recent_events(limit=limit)]


def test_a_value_taken_back_is_charged_at_its_worst_case_again(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    accepted(economy)
    [priced] = answers(economy, 1, priority="standard")
    assert not priced.billing_uncertain and priced.cost_micros == KNOWN

    reply = take_back(economy)
    assert reply.status == 200
    assert reply.body == {
        "field": "priority",
        "value": "standard",
        "taken_back": True,
        "note": f'Took back usage.priority "standard" as the standard price: {SAID}.',
        "standard_usage": {},
    }
    assert metering.standard_usage(economy.db) == {}
    assert f'Stefan took back usage.priority "standard" as the standard price: {SAID}' in messages(economy)

    # priced and flagged as before the owner accepted it: the worst case, the unknown field named, offered again
    [again] = answers(economy, 1, priority="standard")
    assert again.billing_uncertain and again.cost_micros == again.estimate_micros > KNOWN
    with economy.db.connection() as conn:
        error = conn.execute("SELECT error FROM llm_calls ORDER BY id DESC").fetchone()[0]
    assert error == "unknown usage field priority"
    [offer] = economy.dashboard()["agent"]["uncertain_bills"]
    assert (offer["value"], offer["accepted"], offer["calls"]) == ("standard", False, 1)
    assert offer["refund_micros"] == again.cost_micros - KNOWN
    assert economy.dashboard()["agent"]["standard_usage"] == {}

    # and after a restart: nothing of it is settled at startup, and it stays at its worst case
    before = refunds(economy)
    fresh = restart(economy)
    assert refunds(fresh) == before
    [later] = answers(fresh, 1, priority="standard")
    assert later.billing_uncertain and later.cost_micros == later.estimate_micros


def test_what_was_settled_stays_as_it_was(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    costs = accepted(economy)
    answers(economy, 1, priority="standard")  # at its price, while accepted
    scope = economy.life.scope()
    ledger, calls, balance = rows(economy, "ledger"), rows(economy, "llm_calls"), economy.books.balance(scope)
    marks = [economy.db.get_meta(key) for key in SETTLED]
    assert sum(amount for _, amount, _ in refunds(economy)) == -sum(cost - KNOWN for cost in costs)

    assert take_back(economy).body["taken_back"] is True
    # the refunds stay, no ledger row is added or changed, no call changes, and the balance is the same
    assert rows(economy, "ledger") == ledger and rows(economy, "llm_calls") == calls
    assert economy.books.balance(scope) == balance
    assert [economy.db.get_meta(key) for key in SETTLED] == marks  # where settle_usage got to stays too


def test_another_value_of_the_field_stays_accepted(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    accepted(economy)
    answers(economy, 1, priority="flex")
    [offer] = economy.uncertain_bills()
    assert accept(economy, offer, value="flex").status == 200
    assert metering.standard_usage(economy.db) == {"priority": ["standard", "flex"]}

    assert take_back(economy).body["standard_usage"] == {"priority": ["flex"]}
    [flex] = answers(economy, 1, priority="flex")
    assert not flex.billing_uncertain and flex.cost_micros == KNOWN
    [standard] = answers(economy, 1, priority="standard")
    assert standard.billing_uncertain


def test_a_value_not_accepted_is_answered_and_nothing_changes(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    accepted(economy)
    assert take_back(economy).body["taken_back"] is True
    held, ledger, events = economy.db.get_meta(metering.STANDARD_USAGE_KEY), rows(economy, "ledger"), messages(economy)

    for body in ({}, {"value": "fast"}, {"field": "lane"}):  # again, and a value or a field never accepted
        reply = take_back(economy, **body)
        assert reply.status == 200 and reply.body["taken_back"] is False
        assert reply.body["note"].startswith("Nothing to take back: usage.")
        assert reply.body["note"].endswith(" isn't treated as the standard price.")
    assert economy.db.get_meta(metering.STANDARD_USAGE_KEY) == held
    assert rows(economy, "ledger") == ledger and messages(economy) == events  # no line in the System log either


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"field": "speed"}, "field"),  # a field Ember's code prices itself
        ({"field": "Priority"}, "field"),
        ({"field": None}, "field"),
        ({"value": ""}, "value"),
        ({"value": "standard\n"}, "value"),
        ({"value": 1}, "value"),
    ],
)
def test_what_the_owner_may_take_back(data_dir: Path, body: dict[str, Any], field: str) -> None:
    economy = live_economy(data_dir)
    accepted(economy)
    reply = take_back(economy, **body)
    assert (reply.status, reply.body["field"]) == (422, field)
    assert metering.standard_usage(economy.db) == {"priority": ["standard"]}
    assert economy.take_back_usage(["priority", "standard"], "Stefan").status == 422  # not an object


def test_accepting_again_after_a_take_back_settles_only_the_calls_charged_since(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    accepted(economy)
    first = refunds(economy)
    take_back(economy)
    economy.clock.advance(days=1)
    since = answers(economy, 2, priority="standard")
    excess = sum(r.cost_micros - KNOWN for r in since)

    [offer] = economy.uncertain_bills()
    assert (offer["accepted"], offer["calls"], offer["refund_micros"]) == (False, 2, excess)
    reply = accept(economy, offer)
    assert reply.status == 200 and reply.body["accepted"]["refund_micros"] == excess
    assert refunds(economy)[: len(first)] == first  # the first episode is not refunded twice
    assert sum(amount for _, amount, _ in refunds(economy)[len(first) :]) == -excess
    assert metering.standard_usage(economy.db) == {"priority": ["standard"]}
    assert any('Stefan treats usage.priority "standard" as the standard price' in m for m in messages(economy))
    [priced] = answers(economy, 1, priority="standard")
    assert not priced.billing_uncertain and priced.cost_micros == KNOWN


def test_the_dashboard_lists_it_and_the_owner_takes_it_back_over_http(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(LIVE)) as client:
        economy = client.app.state.ember.economy
        accepted(economy)
        assert client.get("api/dashboard").json()["agent"]["standard_usage"] == {"priority": ["standard"]}
        assert '"standard_usage": {}' not in client.get("api/diagnostics").text

        reply = client.post(TAKE_BACK, json=STANDARD, headers=CSRF)
        assert reply.status_code == 200 and reply.json()["taken_back"] is True
        assert reply.json()["note"] == f'Took back usage.priority "standard" as the standard price: {SAID}.'
        assert client.get("api/dashboard").json()["agent"]["standard_usage"] == {}
        assert '"standard_usage": {}' in client.get("api/diagnostics").text  # the report says the same
        assert any(SAID in e["message"] for e in client.get("api/events").json())

        again = client.post(TAKE_BACK, json=STANDARD, headers=CSRF)  # a second click, or another tab
        assert again.status_code == 200 and again.json()["taken_back"] is False
        assert client.post(TAKE_BACK, json={"field": "speed", "value": "fast"}, headers=CSRF).status_code == 422


def test_the_route_refuses_without_the_request_header_and_outside_ingress(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    for path in AGENT.rglob("*.py"):  # the agent has no way to it
        assert "take_back_usage" not in path.read_text(encoding="utf-8"), path.name
    owner_id = "8f14e45fceea167a5a36dedd4bea2543"
    settings = LoadedSettings(LIVE.model_copy(update={"owner_user_ids": (owner_id,)}))
    with client_factory(settings) as client:
        economy = client.app.state.ember.economy
        accepted(economy)
        assert client.post(TAKE_BACK, json=STANDARD).status_code == 403  # no request header: not the dashboard
        stranger = {**CSRF, "X-Remote-User-Id": "0000000000000000000000000000beef"}
        assert client.post(TAKE_BACK, json=STANDARD, headers=stranger).status_code == 403  # not the owner
        assert metering.standard_usage(economy.db) == {"priority": ["standard"]}
    for host in (HA_CORE, ("172.30.33.9", 1234)):  # Home Assistant's host network, any other client: not Ingress
        with client_factory(settings, client=host) as client:
            owner = {**CSRF, "X-Remote-User-Id": owner_id}
            assert client.post(TAKE_BACK, json=STANDARD, headers=owner).status_code == 403
            assert metering.standard_usage(client.app.state.ember.db) == {"priority": ["standard"]}
    with client_factory(settings) as client:  # the owner, through Ingress, from the dashboard
        reply = client.post(TAKE_BACK, json=STANDARD, headers={**CSRF, "X-Remote-User-Id": owner_id})
        assert reply.status_code == 200 and reply.json()["taken_back"] is True
        assert metering.standard_usage(client.app.state.ember.db) == {}
