"""Uncertain bills (0.42.0, analysis-0.40.1 codebase, 6.2 "Settling uncertain bills from settings", and E1).

An answer carrying a usage field Ember's code doesn't know is charged at its worst case. Until 0.42.0 only a release
ended such an episode: it taught the code the field and settled its calls once (usage.speed, 0.39.8: $15.94 booked
for $3.87 on 10-16), while the owner compared the Anthropic Console by hand and typed refunds. Now the dashboard offers
"Treat usage.X = "Y" as standard price: settles N calls, refunds $Z". The owner's yes prices the answers with exactly
that value at the price table's price from then on and settles the calls charged at their worst case for it, once (the
owner's own refunds count first). Another value of the field stays at its worst case, nothing is ever accepted by
Ember's code, and the agent has no way to it.

Also:
- ``burn.judged``, the net runway a move up of the burn mode is judged by, divides the balance the runway is judged on
  (answered uncertain calls at their floor), not the booked one: 0.40.3 left it on the booked balance;
- the System warning on calls charged at their worst case has **Keep as charged** (the owner's report: $0.10 shown from
  10-18 on, as nobody looks a few cents up in the Console). It names only what is charged so after the calls kept, and
  no money moves.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import LoadedSettings, Settings
from app.economy import burn, metering
from app.economy.life import LifeStatus, Runway
from app.economy.metering import CallFailed, CallResult, Completed, Interrupted, Outcome
from app.economy.service import KEPT_KEY, Economy
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, owner, request, restart

KNOWN = 1_000 * 2 + 200 * 10  # what each answer below costs at the standard price: 1,000 in and 200 out on Sonnet 5
LIVE = Settings(dry_run=False, starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
CSRF = {"X-Ember-Request": "1", "X-Remote-User-Display-Name": "Stefan"}
STANDARD = {"field": "priority", "value": "standard"}
AGENT = Path(__file__).resolve().parents[2] / "app" / "agent"


def live_economy(data_dir: Path) -> Economy:
    return make_economy(data_dir, LIVE, clock=FakeClock())


def calls(economy: Economy, outcomes: list[Outcome], transport: ScriptedTransport | None = None) -> list[CallResult]:
    """One live cycle with a work call for each outcome (a call that broke off too)."""
    model = economy.metered(transport or ScriptedTransport(simulated=False, outcomes=outcomes))
    cycle = model.open_cycle("test")
    results = []
    for _ in outcomes:
        try:
            results.append(model.call(cycle, "work", request()))
        except CallFailed as failed:
            results.append(failed.result)
    model.close_cycle(cycle)
    return results


def answers(economy: Economy, count: int, **usage: Any) -> list[CallResult]:
    """``count`` answers of 1,000 tokens in and 200 out, with ``usage``'s fields besides."""
    return calls(economy, [Completed(message(1_000, 200, **usage)) for _ in range(count)])


def refunds(economy: Economy) -> list[tuple[str, int, str]]:
    with economy.db.connection() as conn:
        return [
            (r["occurred_on"], r["amount_micros"], r["created_by"])
            for r in conn.execute(
                "SELECT occurred_on, amount_micros, created_by FROM ledger WHERE type = 'api_cost_correction'"
                " ORDER BY id"
            )
        ]


def accept(economy: Economy, offer: dict[str, Any] | None = None, **body: Any) -> Any:
    """The owner's yes to an offer on the dashboard (the refund it showed)."""
    shown = offer["refund_micros"] if offer is not None else 0
    return economy.accept_usage({**STANDARD, "refund_micros": shown, **body}, "Stefan")


# --- an episode settled by the owner ---


def test_the_owner_accepts_a_usage_field_and_its_calls_are_settled_once(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    first = answers(economy, 2, priority="standard")
    economy.clock.advance(days=1)
    second = answers(economy, 1, priority="standard")
    assert all(r.billing_uncertain and r.cost_micros == r.estimate_micros > KNOWN for r in first + second)
    excess = sum(r.cost_micros - KNOWN for r in first + second)
    scope = economy.life.scope()
    balance, cap = economy.books.balance(scope), economy.books.cap_spend_on(scope, economy.clock.today())

    # the owner sees what it would settle before saying yes
    [offer] = economy.dashboard()["agent"]["uncertain_bills"]
    assert offer == {
        "field": "priority",
        "value": "standard",
        "accepted": False,
        "calls": 3,
        "excess_usd": excess / 1e6,
        "owner_usd": 0.0,
        "refund_usd": excess / 1e6,
        "refund_micros": excess,
    }
    assert refunds(economy) == []  # an offer changes nothing

    reply = accept(economy, offer)
    assert reply.status == 200 and reply.body["accepted"]["refund_micros"] == excess
    assert len(refunds(economy)) == 2  # one refund of Ember's code for each day the calls were made on
    assert sum(amount for _, amount, _ in refunds(economy)) == -excess
    assert {who for _, _, who in refunds(economy)} == {"system"}
    assert economy.books.balance(scope) == balance + excess
    assert economy.books.provisional_excess(scope) == 0
    assert economy.books.cap_spend_on(scope, economy.clock.today()) == cap  # the cap counted what they cost
    assert economy.dashboard()["agent"]["uncertain_bills"] == []
    messages = [e["message"] for e in economy.db.recent_events(limit=5)]
    assert any('Stefan treats usage.priority "standard" as the standard price' in m for m in messages)
    assert any("3 call(s) charged at their worst case for usage.priority" in m for m in messages)

    # once: again, and after a restart
    again = accept(economy)
    assert again.status == 200 and again.body["accepted"]["calls"] == 0
    fresh = restart(economy)
    assert len(refunds(fresh)) == 2 and fresh.books.balance(fresh.life.scope()) == balance + excess
    assert metering.standard_usage(fresh.db) == {"priority": ["standard"]}

    # answers with it cost their price from then on; another value stays at its worst case
    [priced] = answers(fresh, 1, priority="standard")
    assert not priced.billing_uncertain and priced.cost_micros == KNOWN
    [fast] = answers(fresh, 1, priority="fast")
    assert fast.billing_uncertain and fast.cost_micros == fast.estimate_micros
    [other] = fresh.dashboard()["agent"]["uncertain_bills"]
    assert (other["value"], other["accepted"], other["calls"]) == ("fast", False, 1)
    assert len(refunds(restart(fresh))) == 2  # not accepted: nothing settled at startup either


def test_the_owners_own_refunds_since_then_count_first(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    results = answers(economy, 3, priority="standard")
    excess = sum(r.cost_micros - KNOWN for r in results)
    owner(economy, "api-correction", "0.01", direction="decrease")  # typed from the Console meanwhile
    [offer] = economy.uncertain_bills()
    assert (offer["owner_usd"], offer["refund_micros"]) == (0.01, excess - 10_000)
    assert accept(economy, offer).status == 200
    assert sum(amount for _, amount, who in refunds(economy) if who == "system") == -(excess - 10_000)
    assert economy.books.provisional_excess(economy.life.scope()) == 0


def test_only_an_answer_whose_one_unknown_is_that_value_is_settled(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    answers(economy, 1, priority="standard")
    answers(economy, 1, priority="standard", lane="slow")  # a second unknown field
    answers(economy, 1, priority={"tier": "standard"})  # not a text value
    [offer] = economy.uncertain_bills()  # neither of the others is offered
    assert (offer["field"], offer["calls"]) == ("priority", 1)
    assert accept(economy, offer).status == 200
    [mixed] = answers(economy, 1, priority="standard", lane="slow")
    assert mixed.billing_uncertain  # usage.lane is still unknown
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls ORDER BY id DESC").fetchone()[0] == "unknown usage field lane"


def test_the_refund_the_owner_saw_is_the_most_it_refunds(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    answers(economy, 1, priority="standard")
    [offer] = economy.uncertain_bills()
    answers(economy, 1, priority="standard")  # answered after the dashboard showed the offer
    changed = accept(economy, offer)
    assert changed.status == 409 and changed.body["code"] == "refund_changed"
    assert changed.body["offer"]["calls"] == 2
    assert refunds(economy) == [] and metering.standard_usage(economy.db) == {}  # nothing changed
    assert accept(economy, changed.body["offer"]).status == 200


@pytest.mark.parametrize(
    ("body", "status", "field"),
    [
        ({"value": "fast"}, 404, "value"),  # nothing charged for it: nothing to accept
        ({"field": "speed"}, 422, "field"),  # a field Ember's code prices itself
        ({"field": "Priority"}, 422, "field"),
        ({"value": ""}, 422, "value"),
        ({"value": "standard\n"}, 422, "value"),
        ({"value": 1}, 422, "value"),
        ({"refund_micros": None}, 422, "refund_micros"),
        ({"refund_micros": True}, 422, "refund_micros"),
    ],
)
def test_what_the_owner_may_accept(data_dir: Path, body: dict[str, Any], status: int, field: str) -> None:
    economy = live_economy(data_dir)
    answers(economy, 1, priority="standard")
    reply = accept(economy, {"refund_micros": 10**9}, **body)
    assert (reply.status, reply.body["field"]) == (status, field)
    assert refunds(economy) == [] and metering.standard_usage(economy.db) == {}


class OwnerMidCall(ScriptedTransport):
    """The owner says yes while a call is in flight (between its request and its answer)."""

    def __init__(self, economy: Economy, outcomes: list[Outcome]) -> None:
        super().__init__(simulated=False, outcomes=outcomes)
        self.economy = economy
        self.replies: list[Any] = []

    def send(self, request: Any) -> Outcome:
        if not self.replies:
            [offer] = self.economy.uncertain_bills()
            self.replies.append(accept(self.economy, offer))
        return super().send(request)


def test_a_call_in_flight_when_the_owner_accepts_costs_its_price(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    [before] = answers(economy, 1, priority="standard")
    transport = OwnerMidCall(economy, [Completed(message(1_000, 200, priority="standard"))])
    [during] = calls(economy, transport.outcomes[:], transport)
    assert transport.replies[0].status == 200 and transport.replies[0].body["accepted"]["calls"] == 1
    assert not during.billing_uncertain and during.cost_micros == KNOWN
    assert sum(amount for _, amount, _ in refunds(economy)) == -(before.cost_micros - KNOWN)


def test_the_agent_has_no_way_to_accept_or_keep(client_factory: Callable[..., Iterator[TestClient]]) -> None:
    for path in AGENT.rglob("*.py"):  # no tool, nor anything else of the agent's, names them
        text = path.read_text(encoding="utf-8")
        for name in ("accept_usage", "standard_usage", "economy/usage", "keep_as_charged", "kept_as_charged"):
            assert name not in text, (path.name, name)
    settings = LIVE.model_copy(update={"owner_user_ids": ("8f14e45fceea167a5a36dedd4bea2543",)})
    with client_factory(LoadedSettings(settings)) as client:
        for path in ("api/economy/usage/standard", "api/economy/uncertain/keep"):
            body = {**STANDARD, "refund_micros": 0, "held_micros": 0}
            assert client.post(path, json=body).status_code == 403  # no request header: not the dashboard
            stranger = {**CSRF, "X-Remote-User-Id": "0000000000000000000000000000beef"}
            assert client.post(path, json=body, headers=stranger).status_code == 403  # not the owner


def test_the_dashboard_offers_it_and_the_owner_accepts_over_http(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(LIVE)) as client:
        economy = client.app.state.ember.economy
        results = answers(economy, 2, priority="standard")
        excess = sum(r.cost_micros - KNOWN for r in results)
        agent = client.get("api/dashboard").json()["agent"]
        [offer] = agent["uncertain_bills"]
        assert offer["refund_micros"] == excess
        assert agent["held_warning"]["held_micros"] == excess  # the System warning names them too
        body = {"field": offer["field"], "value": offer["value"], "refund_micros": offer["refund_micros"]}
        reply = client.post("api/economy/usage/standard", json=body, headers=CSRF)
        assert reply.status_code == 200 and reply.json()["accepted"]["refund_usd"] == excess / 1e6
        agent = client.get("api/dashboard").json()["agent"]
        assert agent["uncertain_bills"] == [] and agent["held_warning"] is None
        assert agent["balance_usd"] == pytest.approx(50 - 2 * KNOWN / 1e6)


# --- the burn mode's move up, on the balance the runway is judged on ---


def judged_status(**runway: Any) -> LifeStatus:
    return LifeStatus(
        mode="live",
        life_id=1,
        state="alive",
        reason="",
        runway=Runway(200.0, None, window_spend=2_800_000, active_days=7.0, net_days=200.0, **runway),
        money_in_id=2,
        balance=80_000_000,  # booked: answered uncertain calls at their worst case
        stance="conserve",
    )


def test_a_move_up_of_the_burn_mode_is_judged_on_the_balance_the_runway_is_judged_on() -> None:
    since = burn.Since(money_in=1, spend=5_500_000)  # $5.50 a day before maintenance
    settled = judged_status(balance=110_000_000)  # $30 of it answered uncertain calls' worst case
    assert burn.judged(settled, since) == pytest.approx(110 / 5.5)  # 20 days: until 0.42.0, 80 / 5.5 = 14.5
    assert burn.settle("maintenance", settled, since) == "focus"
    assert burn.judged(judged_status(), since) == pytest.approx(80 / 5.5)  # a runway built without one: the booked
    assert burn.settle("maintenance", judged_status(), since) == "maintenance"


# --- Keep as charged ---


def broken_off(economy: Economy) -> int:
    """A call whose answer broke off (charged at its worst case); what it may have been overcharged."""
    [result] = calls(economy, [Interrupted("stream broke", partial_usage={"input_tokens": 1_000})])
    assert result.status == "interrupted"
    return result.cost_micros - 1_000 * 2


def held(economy: Economy) -> list[str]:
    return [w for w in economy.warnings() if "charged at the worst case" in w]


def test_the_owner_keeps_the_worst_case_as_charged(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    first = broken_off(economy)
    [warning] = held(economy)
    assert warning.startswith(f"${first / 1e6:.2f} of interrupted calls") and "or keep it as charged" in warning
    assert economy.dashboard()["agent"]["held_warning"] == {
        "text": warning,
        "held_usd": first / 1e6,
        "held_micros": first,
    }
    scope = economy.life.scope()
    balance, excess, entries = economy.books.balance(scope), economy.books.provisional_excess(scope), refunds(economy)

    changed = economy.keep_as_charged({"held_micros": first - 1}, "Stefan")  # less than it names now
    assert changed.status == 409 and changed.body["held_micros"] == first and held(economy)
    assert economy.keep_as_charged({}, "Stefan").status == 422

    reply = economy.keep_as_charged({"held_micros": first}, "Stefan")
    assert reply.status == 200 and reply.body["kept_usd"] == first / 1e6
    assert held(economy) == [] and economy.dashboard()["agent"]["held_warning"] is None
    # only the advice: no money moves, and a correction can still be recorded
    assert economy.books.balance(scope) == balance and economy.books.provisional_excess(scope) == excess
    assert refunds(economy) == entries
    assert any("Stefan kept" in e["message"] for e in economy.db.recent_events(limit=3))
    assert economy.keep_as_charged({"held_micros": 0}, "Stefan").status == 200  # again: nothing left to keep

    fresh = restart(economy)
    assert held(fresh) == [] and fresh.db.get_meta(KEPT_KEY) is not None

    second = broken_off(fresh)  # a new one brings it back, with its amount alone
    [warning] = held(fresh)
    assert warning.startswith(f"${second / 1e6:.2f} of interrupted calls")
    assert fresh.dashboard()["agent"]["held_warning"]["held_micros"] == second
    # recording the correction it asks for still works, and settles it
    owner(fresh, "api-correction", f"{math.ceil(second / 10_000) / 100:.2f}", direction="decrease")
    assert held(fresh) == []
