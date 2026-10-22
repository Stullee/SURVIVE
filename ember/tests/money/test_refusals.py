"""Every refusal of the money guard, each tested (0.42.3, analysis-0.40.1 audit).

The audit noted that nobody had checked every refusal of the money guard against the tests. Each place the guard, and
the books it writes, refuses or raises was listed and its test found (coverage of app.economy showed the raise lines no
test reached); these are the refusals that had none. Each asserts the refusal, its reason as the owner or the agent
reads it, and that nothing was spent or written: the balance and the ledger as they were, no reservation left pending,
nothing sent. A refused call still leaves its row ("refused", nothing charged) and a line in the System log, which is
how the owner sees it; an answer whose bill can't be read is charged its worst case, once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.agent import prompts
from app.agent.service import select_transport
from app.config import LoadedSettings, Settings
from app.economy import metering
from app.economy.costs import micros_to_usd
from app.economy.estimate import Unpriceable
from app.economy.ledger import PreparedEntry
from app.economy.life import KILLED_KEY
from app.economy.metering import (
    WORKSHOP,
    CallFailed,
    CallRefused,
    Completed,
    FilesError,
    MeteredModel,
    ProcessLock,
    lock_path,
)
from app.economy.service import Economy, Recorded
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, owner, request, restart

GENEROUS = Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
LIVE = GENEROUS.model_copy(update={"dry_run": False})
WORST = 1_000 * 2 + 1_000 * 10  # request()'s worst case on Sonnet 5: 1,000 tokens in, up to 1,000 out
CODE = {"type": "code_execution_20250825", "name": "code_execution"}
# A paused turn's web_fetch, still to run when the next request is sent
FETCHING = [
    {"role": "user", "content": "Read the shop's page."},
    {
        "role": "assistant",
        "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_fetch", "input": {"url": "https://x.test"}}
        ],
    },
]


def money(economy: Economy) -> tuple[int, int, list[tuple[Any, ...]]]:
    """(the balance, what the calls in flight hold, every ledger row): what a refusal leaves as it was."""
    scope = economy.life.scope()
    with economy.db.connection() as conn:
        ledger = [tuple(row) for row in conn.execute("SELECT * FROM ledger ORDER BY id")]
    return economy.books.balance(scope), economy.books.pending(scope), ledger


def last_call(economy: Economy) -> dict[str, Any]:
    with economy.db.connection() as conn:
        row = conn.execute("SELECT status, cost_micros, guard_reason FROM llm_calls ORDER BY id DESC").fetchone()
    return dict(row)


def logged(economy: Economy) -> list[str]:
    return [event["message"] for event in economy.db.recent_events(limit=20)]


def refused(
    economy: Economy, model: MeteredModel, transport: ScriptedTransport, cycle: int, purpose: str, body: Any
) -> CallRefused:
    """``body`` as a ``purpose`` call in ``cycle``, refused: nothing sent, spent or held, and its refused row and the
    System log's line say why."""
    before, sent = money(economy), len(transport.sent)
    with pytest.raises(CallRefused) as caught:
        model.call(cycle, purpose, body)
    assert money(economy) == before and len(transport.sent) == sent
    assert last_call(economy) == {"status": "refused", "cost_micros": 0, "guard_reason": caught.value.reason}
    assert f"Refused a {purpose} call: {caught.value.reason}" in logged(economy)
    return caught.value


# --- the request's shape: what the guard can't bound is refused before it is sent ---


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        # a field the guard doesn't know could change the price: fallback models answer at another model's
        ({"fallbacks": [{"model": "claude-opus-5"}]}, "request fields the budget guard can't price: fallbacks"),
        # 0.42.1: a cache lifetime the guard doesn't know, or a marker with more than its type and lifetime
        (
            {"system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "24h"}}]},
            "a cache_control the budget guard can't price: {'type': 'ephemeral', 'ttl': '24h'}",
        ),
        (
            {"cache_control": {"type": "ephemeral", "ttl": "1h", "scope": "org"}},
            "a cache_control the budget guard can't price: {'type': 'ephemeral', 'ttl': '1h', 'scope': 'org'}",
        ),
        ({"tools": {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}}, "tools must be a list"),
        ({"tools": [CODE], "container": {"id": "c_1"}}, "container must be the id of an earlier call's container"),
        ({"messages": FETCHING}, "an unfinished web_fetch continues in this request, but no web_fetch tool bounds it"),
        ({"messages": ["Hi"]}, "could not size the request (AttributeError)"),  # sizing it broke: refused all the same
    ],
)
def test_a_request_the_guard_cant_bound_is_refused_and_says_why(data_dir: Path, extra: dict, problem: str) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    refusal = refused(economy, model, transport, cycle, "work", request(**extra))
    assert (refusal.reason, refusal.category) == (f"the request can't be priced: {problem}", "request")


@pytest.mark.parametrize("tokens", [-1, 20_000_000])
def test_a_prompt_count_the_guard_cant_believe_is_refused(data_dir: Path, tokens: int) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy, ScriptedTransport(tokens=tokens))
    cycle = model.open_cycle("test")
    refusal = refused(economy, model, transport, cycle, "plan", request())
    reason = "the request can't be priced: the prompt's token count is missing or implausible"
    assert (refusal.reason, refusal.category) == (reason, "request")


def test_the_pre_checks_refuse_a_model_without_a_price_and_write_nothing(data_dir: Path) -> None:
    # The guard's own refusal is test_metering's ("price table"); the agent's pre-checks raise the same, writing nothing
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    body = request(model="claude-opus-9")
    before = money(economy)
    checks: list[Callable[[], object]] = [
        lambda: model.quote(body),
        lambda: model.reservation(body, WORKSHOP),
        lambda: model.expected(body, "work", cycle),
        lambda: model.affordable(body, "work", cycle),
        lambda: model.prompt_tokens(body),
    ]
    for check in checks:
        with pytest.raises(Unpriceable, match=r"^model 'claude-opus-9' has no entry in the price table$"):
            check()
    assert money(economy) == before and transport.sent == []
    with economy.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_a_cycle_or_a_call_with_a_name_the_guard_doesnt_take_writes_nothing(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    with pytest.raises(ValueError, match="^bad trigger name$"):
        model.open_cycle("Wake now!")
    cycle = model.open_cycle("test")
    before = money(economy)
    with pytest.raises(ValueError, match="^bad purpose name$"):
        model.call(cycle, "work; DROP TABLE ledger", request())
    assert money(economy) == before and transport.sent == []
    with economy.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


# --- the system: one process spends, and only while its books are sound ---


def test_after_a_bookkeeping_error_nothing_is_spent_until_a_restart(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    held = model.reserve(cycle, "work", request())
    model.finalize(held, Completed(message()))
    before = money(economy)
    model.finalize(held, Completed(message()))  # settled twice: the books refuse the second, and spending stops
    assert economy.health.broken == f"call #{held.call_id} could not be recorded: RuntimeError"
    assert money(economy) == before  # nothing charged twice
    refusal = refused(economy, model, transport, cycle, "work", request())
    reason = f"spending is stopped after a bookkeeping error ({economy.health.broken}); restart the app"
    assert (refusal.reason, refusal.category) == (reason, "system")
    model.close_cycle(cycle)
    with pytest.raises(CallRefused) as opening:
        model.open_cycle("test")
    assert (opening.value.reason, opening.value.category) == (reason, "system")
    assert f"Spending is stopped after a bookkeeping error: {economy.health.broken}. Restart the app." in (
        economy.warnings()
    )
    assert transport.sent == [] and money(economy) == before
    fresh, _ = metered(restart(economy))  # a restart checks the books again
    assert fresh.call(fresh.open_cycle("test"), "work", request()).status == "ok"


def test_a_second_process_says_why_it_cant_spend_and_leaves_the_firsts_call_alone(data_dir: Path) -> None:
    first = make_economy(data_dir, GENEROUS)
    first.lock = ProcessLock(lock_path(data_dir))
    first.health.lock_held = first.lock.acquire()
    model, _ = metered(first)
    held = model.reserve(model.open_cycle("test"), "work", request())  # in flight
    second = Economy(first.db, LoadedSettings(GENEROUS), clock=first.clock, lock=ProcessLock(lock_path(data_dir)))
    try:
        second.start()
        assert second.health.lock_held is False
        expected = "Another Ember process is using the data folder; model calls are refused until it stops"
        assert any(e["message"] == expected and e["level"] == "error" for e in first.db.recent_events(limit=10))
        # It doesn't take the first's call for one a restart left: not charged at its worst case, still held
        scope = first.life.scope()
        assert first.books.pending(scope) == held.estimate == WORST
        assert [row for row in money(first)[2] if row[3] == "api_cost"] == []
        before = money(first)
        with pytest.raises(CallRefused) as opening:
            second.metered(ScriptedTransport()).open_cycle("test")
        reason = "another Ember process is using the data folder"
        assert (opening.value.reason, opening.value.category) == (reason, "system")
        assert money(first) == before
        assert model.finalize(held, Completed(message())).cost_micros == 1_000 * 2 + 200 * 10  # the first settles it
        assert first.books.pending(scope) == 0
    finally:
        first.lock.release()


def test_live_without_an_api_key_a_call_is_never_sent_and_costs_nothing(data_dir: Path) -> None:
    # The agent says why first (blocked_reason: the key); the transport it is given without one sends nothing
    economy = make_economy(data_dir, LIVE)
    transport = select_transport("live", 0, api_key="")
    model = economy.metered(transport)
    cycle = model.open_cycle("test")
    before = money(economy)
    with pytest.raises(CallFailed) as failed:
        model.call(cycle, "work", request())
    result, error = failed.value.result, "model calls need the Anthropic API key in the app's options"
    assert (result.status, result.cost_micros, result.billing_uncertain, result.error) == ("failed", 0, False, error)
    assert money(economy) == before  # nothing charged, nothing held
    assert f"Call #{result.call_id} (claude-sonnet-5) failed: {error}; nothing charged" in logged(economy)
    files: list[tuple[Callable[[], object], str]] = [
        (lambda: transport.upload_file("a.png", b"x", "image/png"), "files can't be sent without a model transport"),
        (lambda: transport.file_info("file_1"), "files can't be read without a model transport"),
        (lambda: transport.download_file("file_1", 1_000), "files can't be read without a model transport"),
        (lambda: transport.delete_file("file_1"), "files can't be deleted without a model transport"),
    ]
    for call, message_text in files:
        with pytest.raises(FilesError, match=f"^{message_text}$"):
            call()


def test_the_history_reads_only_a_cost_or_an_output_count(data_dir: Path) -> None:
    # Its column is written into the query, so the guard takes no other
    model, _ = metered(make_economy(data_dir, GENEROUS))
    for column in ("cost_micros", "output_tokens"):
        assert model._history("claude-sonnet-5", "work", column) is None  # noqa: SLF001 - no calls yet
    with pytest.raises(ValueError, match="^estimate_micros FROM llm_calls; --$"):
        model._history("claude-sonnet-5", "work", "estimate_micros FROM llm_calls; --")  # noqa: SLF001


# --- the agent's state, and the cycle a call is made in ---


def kill(economy: Economy) -> None:
    economy.life.set_switch(KILLED_KEY, True)


def pause(economy: Economy) -> None:
    economy.set_paused(True)


def ruin(economy: Economy) -> None:
    owner(economy, "expense", "50", note="the shop's rent")  # more than is left: the agent dies


@pytest.mark.parametrize(("stop", "state"), [(kill, "killed"), (pause, "paused"), (ruin, "dead")])
def test_a_call_after_the_agent_is_stopped_mid_cycle_is_refused(
    data_dir: Path, stop: Callable[[Economy], None], state: str
) -> None:
    # test_life refuses the cycle of a paused, dead or unfunded agent; this is the next call of a cycle already running
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request())
    stop(economy)
    refusal = refused(economy, model, transport, cycle, "work", request())
    assert (refusal.reason, refusal.category, refusal.state) == (f"the agent is {state}", "state", state)


def test_a_cycle_from_before_a_death_and_revival_spends_nothing_in_the_new_life(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request())
    born = economy.status().life_id
    ruin(economy)
    owner(economy, "grant", "20")  # the owner revives it while the old cycle is still marked running
    assert economy.status().state == "alive" and economy.status().life_id != born
    refusal = refused(economy, model, transport, cycle, "work", request())
    assert (refusal.reason, refusal.category) == ("the wake cycle belongs to an earlier life", "request")


def test_a_cycle_of_the_other_mode_spends_nothing(data_dir: Path) -> None:
    dry = make_economy(data_dir, GENEROUS)
    cycle = metered(dry)[0].open_cycle("test")  # a dry run's cycle (simulated)
    live = Economy(dry.db, LoadedSettings(LIVE), clock=dry.clock, boot_id=dry.boot_id)  # the same process, live
    live.start()
    model, transport = metered(live)
    refusal = refused(live, model, transport, cycle, "work", request())
    reason = "the wake cycle and the model transport are in different modes"
    assert (refusal.reason, refusal.category) == (reason, "system")


# --- the caps ---


def test_a_workshop_run_priced_over_its_cap_per_run_is_refused(data_dir: Path) -> None:
    settings = GENEROUS.model_copy(update={"workshop_run_cap_usd": 0.05})  # the least the options allow
    economy = make_economy(data_dir, settings)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    run = prompts.workshop_request(settings, "Render the three sheets of planner.xlsx to PNG.", [])
    priced = model.quote(run, WORKSHOP, scaled=False)  # the cap is checked against the run as priced
    assert priced > 50_000
    refusal = refused(economy, model, transport, cycle, WORKSHOP, run)
    reason = f"a workshop run may cost at most $0.05 (this call up to ${micros_to_usd(priced):.4f})"
    assert (refusal.reason, refusal.category) == (reason, "cap")


# --- settling: what an answer's bill can't say is charged at its worst case, once ---


def test_an_answer_whose_cost_cant_be_worked_out_is_charged_its_worst_case_once(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, -200))]))  # no bill counts so
    cycle = model.open_cycle("test")
    scope = economy.life.scope()
    balance = economy.books.balance(scope)
    with pytest.raises(CallFailed) as failed:
        model.call(cycle, "work", request())
    result, error = failed.value.result, "cost could not be computed (ValueError); charged the worst case"
    assert (result.status, result.cost_micros, result.billing_uncertain) == ("interrupted", WORST, True)
    assert result.error == str(failed.value) == error
    assert economy.books.balance(scope) == balance - WORST and economy.books.pending(scope) == 0
    with economy.db.connection() as conn:
        row = conn.execute("SELECT status, floor_micros, billing_uncertain, error FROM llm_calls").fetchone()
        costs = [r[0] for r in conn.execute("SELECT amount_micros FROM ledger WHERE type = 'api_cost'")]
    assert tuple(row) == ("interrupted", 0, 1, error) and costs == [WORST]
    assert f"Call #{result.call_id} (claude-sonnet-5) interrupted: {error}; charged $0.0120 (worst case)" in (
        logged(economy)
    )


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        ({**message(), "usage": {"output_tokens": 200}}, "usage missing or incomplete"),
        (message(iterations=["sampled twice"]), "unreadable usage iteration"),
        (
            message(iterations=[{"type": "compaction", "input_tokens": 10, "output_tokens": 5}]),
            "usage iteration of type 'compaction' on claude-sonnet-5",
        ),
        (message(brand_new_counter=5), "unknown usage field brand_new_counter"),
        (message(server_tool_use={"tool_search_requests": 2}), "unpriced server tool use tool_search_requests"),
        (message(service_tier="priority"), "service tier priority"),
        (message(speed="fast"), "speed fast"),
        (message(inference_geo="eu"), "inference region eu"),
        (message(model="claude-opus-5"), "answered by claude-opus-5 instead of claude-sonnet-5"),
    ],
)
def test_each_uncertain_bill_says_why_it_is_charged_at_its_worst_case(
    data_dir: Path, answer: dict[str, Any], note: str
) -> None:
    # test_metering checks that these are charged at the worst case; here, what the owner reads about each
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(answer)]))
    cycle = model.open_cycle("test")
    scope = economy.life.scope()
    balance = economy.books.balance(scope)
    result = model.call(cycle, "work", request())
    assert (result.billing_uncertain, result.cost_micros) == (True, WORST)
    assert economy.books.balance(scope) == balance - WORST and economy.books.pending(scope) == 0
    with economy.db.connection() as conn:
        assert tuple(conn.execute("SELECT error, cost_micros FROM llm_calls").fetchone()) == (note, WORST)
    assert f"Call #{result.call_id} was charged at its worst case because its bill is uncertain: {note}" in (
        logged(economy)
    )


def test_an_iteration_on_the_requested_models_dated_snapshot_is_an_ordinary_bill(data_dir: Path) -> None:
    """0.42.3: the answer's own model may be the alias's dated snapshot (_same_model), and an iteration's was compared
    exactly: one naming claude-sonnet-5-20260101 for a claude-sonnet-5 request charged its worst case, which neither
    the start's settlement nor the dashboard's offer refunds (its note is no unknown usage field)."""
    economy = make_economy(data_dir, GENEROUS)
    sampled = {"type": "message", "model": "claude-sonnet-5-20260101", "input_tokens": 1_000, "output_tokens": 100}
    other = {**sampled, "model": "claude-opus-5"}  # another model still makes the bill uncertain
    answers = [Completed(message(iterations=[sampled])), Completed(message(iterations=[other]))]
    model, _ = metered(economy, ScriptedTransport(outcomes=answers))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request())
    assert not result.billing_uncertain and result.cost_micros < WORST
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] is None
    assert model.call(cycle, "work", request()).billing_uncertain


@pytest.mark.parametrize("stored", ["{priority: standard", '["priority", "standard"]', '{"priority": "standard"}'])
def test_answers_stay_at_their_worst_case_while_the_owners_choice_cant_be_read(data_dir: Path, stored: str) -> None:
    # 0.42.0: an owner's standard usage that can't be read counts as none (fail-closed)
    economy = make_economy(data_dir, GENEROUS)
    economy.db.set_meta(metering.STANDARD_USAGE_KEY, stored)
    assert metering.standard_usage(economy.db) == {}
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(priority="standard"))]))
    result = model.call(model.open_cycle("test"), "work", request())
    assert (result.billing_uncertain, result.cost_micros) == (True, WORST)
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] == "unknown usage field priority"


# --- the books: an integration's entry ---


def test_an_integrations_entry_is_written_once_and_never_over_the_owners(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    economy = make_economy(data_dir, LIVE)
    sale = PreparedEntry(
        type="revenue",
        amount_micros=3_000_000,
        simulated=False,
        source="Etsy order 71",
        note=None,
        occurred_on="2026-09-01",
        day_given=True,
        idempotency_key="etsy-revenue-71",
        created_by="etsy",
    )
    caplog.set_level(logging.WARNING, logger="app.economy.service")
    assert economy.record_integration(sale).entry_id is not None
    before = money(economy)
    assert economy.record_integration(sale) == Recorded()  # its key was used: never twice
    assert money(economy) == before and caplog.text == ""  # known as written, not left for the database to refuse
    mine = owner(economy, "revenue", "2", source="a customer at the market")["entry"]
    before = money(economy)
    refund = PreparedEntry(
        type="revenue",
        amount_micros=-1_000_000,
        simulated=False,
        source=None,
        note="Etsy refunded part of an order",
        occurred_on="2026-09-01",
        day_given=True,
        idempotency_key="etsy-refund-77",
        corrects_id=mine["id"],
        created_by="etsy",
    )
    assert economy.record_integration(refund) == Recorded()  # the ledger refuses it: the entry is the owner's
    assert money(economy) == before
    assert "The ledger refused an entry from etsy: ledger: Etsy's numbers correct only the entries they made" in (
        caplog.text
    )
