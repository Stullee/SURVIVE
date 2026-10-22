"""0.42.1 (analysis-0.40.1 codebase, 6.2): Ember's model calls.

Caching across cycles. Every cycle's first step wrote the tools and the system prompt to the prompt cache again (9,500
to 12,300 tokens, about $0.025 to $0.03 on Sonnet 5): cycles are 30 minutes or more apart, and an entry lived 5
minutes. An entry that lives an hour costs 2 times the input price to write (5 minutes': 1.25 times) and pays only when
the next cycle reads it: half-hourly cycles on 10-21 would have, the 6-hour sleeps of 0.40.2 wouldn't. So a cycle asks
for an hour at the end of its system prompt (its plan's too) when the median of the last 3 gaps between cycle starts
is at most 50 minutes, else for 5 minutes as before (loop.cache_ttl). The money guard prices a 1-hour write at its own
rate, for at most the tokens of the tools and the system prompt (estimate.Plan.hour_tokens), and refuses a cache
lifetime it doesn't know. The fake model keeps a 1-hour entry for an hour of its clock, and refuses markers the API
refuses.

A refusal fallback for the planner. A refusal by the planner's model (Opus 5.5's classifier) ended the cycle as
stopped, and after the back-off the same context was likely refused again. Now the plan is made once more on the
worker's model, if that is another one, in the same cycle under the same guard, and the cycle's note says who made it.
Never more than once, and never for a work step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.agent import loop, prompts
from app.agent.fake_llm import FakeTransport, Plan, Raw, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from app.config import DEFAULT_PRICE_TABLE, LoadedSettings, Settings
from app.diagnostics import cache_line
from app.economy.estimate import Unpriceable, expected_micros, plan_request, prefix_request, worst_case_micros
from app.economy.metering import CallRefused, Completed, MeteredModel, Rejected
from tests.cycle.test_agent import ROOMY, rows
from tests.cycle.test_fake_llm import usage_of
from tests.cycle.test_loop_shapes import JOURNAL, PLAN
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message

SONNET = DEFAULT_PRICE_TABLE[0]  # claude-sonnet-5: input 2, output 10, cache write 2.5 (5 min) / 4 (1 h), read 0.2
OPUS_PLANS = ROOMY.model_copy(update={"planner_model": "claude-opus-5-5"})  # the owner's install: Opus 5.5 plans
HOUR = {"type": "ephemeral", "ttl": "1h"}
FIVE = {"type": "ephemeral"}


def agent_with(data_dir: Path, script: list[Any], settings: Settings = ROOMY) -> tuple[Agent, FakeTransport, FakeClock]:
    """An agent on the fake model, whose cache runs on the app's clock."""
    clock = FakeClock()
    economy = make_economy(data_dir, settings, clock)
    fake = FakeTransport(script=script, clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    return agent, fake, clock


def a_cycle() -> list[Any]:
    """A plan, one work step with a tool call, its report and the reflection's journal."""
    return [Plan(PLAN), ToolCalls([("workspace_list", {})]), Reply("Done."), JOURNAL]


def cycles(agent: Agent, clock: FakeClock, count: int, minutes: int) -> None:
    for number in range(count):
        if number:
            clock.advance(minutes=minutes)
        assert agent.run_cycle("schedule").status == "completed"


def calls(agent: Agent) -> list[dict[str, Any]]:
    return rows(
        agent,
        "SELECT cycle_id, purpose, model, stop_reason, cache_write_5m_tokens AS w5, cache_write_1h_tokens AS w1,"
        " cache_read_tokens AS r, input_tokens, cost_micros, estimate_micros FROM llm_calls ORDER BY id",
    )


def first_steps(agent: Agent) -> dict[int, dict[str, Any]]:
    """Each cycle's first work step."""
    found: dict[int, dict[str, Any]] = {}
    for call in calls(agent):
        if call["purpose"] == "work":
            found.setdefault(call["cycle_id"], call)
    return found


def refusal(model: str, text: str = "") -> Raw:
    """A classifier's refusal (HTTP 200, stop_reason "refusal"), before any output or after ``text``."""
    return Raw(
        {
            "id": f"msg_refused_{model}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": None},
            "usage": {"input_tokens": 4_000, "output_tokens": 1},
        }
    )


# --- caching across cycles: the lifetime, by the gaps between cycles ---


def started(data_dir: Path, gaps: list[int]) -> str:
    """The lifetime the newest of cycles started ``gaps`` minutes apart chooses."""
    clock = FakeClock()
    economy = make_economy(data_dir, ROOMY, clock)
    meter = economy.metered(ScriptedTransport(simulated=True))
    for gap in [0, *gaps]:
        clock.advance(minutes=gap)
        meter.close_cycle(meter.open_cycle("schedule"))
    with economy.db.connection() as conn:
        return loop.cache_ttl(conn, simulated=True)


@pytest.mark.parametrize(
    ("gaps", "ttl"),
    [
        ([], "5m"),  # the first cycle: no gap yet
        ([30], "1h"),
        ([30, 30, 30], "1h"),  # 10-21's half-hourly cycles
        ([360, 30, 30], "1h"),  # the median of the last 3
        ([30, 360, 360], "5m"),  # 0.40.2's sleeps of 6 hours
        ([360, 360, 360, 30, 30], "1h"),  # only the last 3 count
        ([50], "1h"),
        ([51], "5m"),
    ],
)
def test_the_cache_lifetime_follows_the_gaps_between_cycle_starts(data_dir: Path, gaps: list[int], ttl: str) -> None:
    assert loop.CACHE_GAPS == 3 and loop.CACHE_HOUR_GAP_MINUTES == 50
    assert started(data_dir, gaps) == ttl


def test_an_hour_goes_on_the_system_prompts_end_and_the_conversation_keeps_five_minutes() -> None:
    settings = Settings()
    work = prompts.work_request(settings, "brief", [], cache_ttl=prompts.HOUR)
    assert work["system"][-1]["cache_control"] == HOUR  # after the tools and the system prompt
    assert work["cache_control"] == FIVE  # the conversation's tail, cached automatically (a 1-hour entry comes first)
    assert [b.get("cache_control") for b in work["system"][:-1]] == [None, None]
    assert prompts.work_request(settings, "brief", [])["system"][-1]["cache_control"] == FIVE  # as before
    reflect = prompts.reflect_request(settings, "brief", [], [], cache_ttl=prompts.HOUR)
    assert reflect["system"] == work["system"] and reflect["cache_control"] == FIVE
    # the plan: an hour's entry for the next cycle's plan, and none for 5 minutes (a cycle plans once)
    plan = prompts.plan_request(settings, "STATUS", cache_ttl=prompts.HOUR)
    assert plan["system"][-1]["cache_control"] == HOUR and "cache_control" not in plan
    assert "cache_control" not in json.dumps(prompts.plan_request(settings, "STATUS"))
    venture = prompts.plan_request(settings, "STATUS", venture=True, cache_ttl=prompts.HOUR)
    assert venture["system"][-1]["text"] == prompts.VENTURE_RULES and venture["system"][-1]["cache_control"] == HOUR


def test_a_cycle_half_an_hour_later_reads_the_prefix_the_one_before_wrote(data_dir: Path) -> None:
    agent, fake, clock = agent_with(data_dir, a_cycle() * 3)
    cycles(agent, clock, 3, minutes=30)
    assert [t for t in fake.trace if t[1] == "invalid"] == []
    first, second, third = (first_steps(agent)[n] for n in (1, 2, 3))
    # the first cycle had no gap to go by: 5 minutes, as before; its entry is gone 30 minutes later
    assert first["w1"] == 0 and first["w5"] > 9_000 and first["r"] == 0
    # the second wrote the tools and the system prompt for an hour, the rest of its prompt for 5 minutes
    assert second["w1"] > 9_000 and second["r"] == 0 and 0 < second["w5"] < second["w1"]
    # the third read them: only its brief was written
    assert third["r"] == second["w1"] and third["w1"] == 0 and 0 < third["w5"] < third["r"]
    assert third["cost_micros"] < second["cost_micros"] / 2
    # the plan too: the second cycle's plan wrote its system prompt for an hour, the third's read it
    plans = [c for c in calls(agent) if c["purpose"] == "plan"]
    assert [p["w1"] > 0 for p in plans] == [False, True, False]
    assert plans[2]["r"] == plans[1]["w1"] and plans[0]["w5"] == plans[0]["r"] == 0
    # each step and the reflection after them still read all the step before them read and wrote
    sent = [r for r in fake.sent if request_kind(r) in ("work", "reflect")]
    assert [r["system"][-1]["cache_control"] for r in sent] == [FIVE] * 3 + [HOUR] * 6
    plans_of = "SELECT plan FROM llm_calls WHERE cycle_id = ?"
    with agent.db.connection() as conn:
        lines = [cache_line(conn.execute(plans_of, (n,)).fetchall()) for n in (1, 2, 3)]
    assert lines == [" cache=5m", " cache=1h", " cache=1h"]  # the diagnostics report's cycle line


def test_cycles_six_hours_apart_keep_five_minutes_and_pay_no_hour(data_dir: Path) -> None:
    agent, fake, clock = agent_with(data_dir, a_cycle() * 2)
    cycles(agent, clock, 2, minutes=360)
    assert all(c["w1"] == 0 for c in calls(agent))  # no 1-hour write is paid
    second = first_steps(agent)[2]
    assert second["r"] == 0 and second["w5"] > 9_000  # the prefix is written again, at the 5-minute rate
    assert all('"ttl"' not in json.dumps(r) for r in fake.sent)


def test_a_plan_that_wouldnt_fit_with_an_hour_goes_without(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan refused for money starves Ember (and once its will is written, ends its life): an entry it can't afford
    must never be why."""
    agent, fake, clock = agent_with(data_dir, a_cycle() * 2)
    monkeypatch.setattr(loop.CycleRunner, "_fits", lambda self, cycle_id, purpose, request: False)
    cycles(agent, clock, 2, minutes=30)
    plans = [r for r in fake.sent if request_kind(r) == "plan"]
    assert "cache_control" not in json.dumps(plans[1])
    work = [r for r in fake.sent if request_kind(r) == "work"]
    assert work[-1]["system"][-1]["cache_control"] == HOUR  # the work steps keep it: the guard checks each step


# --- the money guard: a 1-hour write is estimated and booked at its price ---


def test_an_hours_write_is_priced_for_the_tools_and_system_prompt_only() -> None:
    body = prompts.work_request(Settings(), "brief", [], cache_ttl=prompts.HOUR)
    prefix = prefix_request(body)
    assert prefix is not None and prefix["tools"] == body["tools"] and prefix["system"] == body["system"]
    assert prefix["messages"] == [{"role": "user", "content": "-"}]
    plan = plan_request(body, 30_000, prefix_tokens=12_000)
    assert plan.cache_ttls == ("1h", "5m") and plan.hour_part == 12_000
    out = body["max_tokens"] * 10
    assert worst_case_micros(plan, SONNET, 10) == 12_000 * 4 + 18_000 * 2.5 + out
    # expected: the first step writes both; a later one reads the start, and writes its new tail at 5 minutes' rate
    assert expected_micros(plan, SONNET, 0, output_tokens=0) == (12_000 * 4 + 18_000 * 2.5, 0)
    assert expected_micros(plan, SONNET, 25_000, output_tokens=0) == (
        25_000 * 0.2 + 5_000 * 2.5,
        12_000 * (4 - 0.2) + 13_000 * (2.5 - 0.2),  # a miss writes it all again
    )
    # uncounted, or a 1-hour marker after the system prompt: every token at the highest rate, as before
    assert worst_case_micros(plan_request(body, 30_000), SONNET, 10) == 30_000 * 4 + out
    late = {**body, "cache_control": HOUR, "system": [{**b, "cache_control": HOUR} for b in body["system"][-1:]]}
    assert worst_case_micros(plan_request(late, 30_000, prefix_tokens=12_000), SONNET, 10) == 30_000 * 4 + out
    # the plan: an hour for its system prompt, its context at the input rate (it has no 5-minute marker)
    opening = prompts.plan_request(Settings(), "STATUS", cache_ttl=prompts.HOUR)
    plan = plan_request(opening, 20_000, prefix_tokens=3_000)
    assert worst_case_micros(plan, SONNET, 10) == 3_000 * 4 + 17_000 * 2 + opening["max_tokens"] * 10
    assert prefix_request(prompts.work_request(Settings(), "brief", [])) is None  # 5 minutes: nothing to count


@pytest.mark.parametrize(
    "marker",
    [
        {"type": "ephemeral", "ttl": "24h"},
        {"type": "ephemeral", "ttl": None},
        {"type": "persistent"},
        {"type": "ephemeral", "ttl": "1h", "scope": "org"},
        "ephemeral",
    ],
)
def test_a_cache_lifetime_the_guard_doesnt_know_is_refused(marker: Any) -> None:
    body = {"model": "claude-sonnet-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    for where in ({"cache_control": marker}, {"system": [{"type": "text", "text": "x", "cache_control": marker}]}):
        with pytest.raises(Unpriceable, match="cache_control"):
            plan_request({**body, **where}, 100)
    for ttl in ("5m", "1h"):  # exactly these
        assert plan_request({**body, "cache_control": {"type": "ephemeral", "ttl": ttl}}, 100).cache_ttls == (ttl,)
    # the model's tool input is data, not a marker
    use = {"type": "tool_use", "id": "t1", "name": "x", "input": {"cache_control": marker}}
    turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [use]}]
    assert plan_request({**body, "messages": turns}, 100).cache_ttls == ()


class Counting(ScriptedTransport):
    """Counts the prefix of a 1-hour request apart from the whole prompt."""

    def count_tokens(self, request: Mapping[str, Any]) -> int:
        return 1_000 if request["messages"] == [{"role": "user", "content": "-"}] else 5_000


def test_a_request_with_an_hours_write_passes_the_guard_and_is_booked_at_its_price(data_dir: Path) -> None:
    economy = make_economy(data_dir, ROOMY)
    usage = {
        "cache_creation_input_tokens": 1_000 + 3_500,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000, "ephemeral_5m_input_tokens": 3_500},
        "cache_read_input_tokens": 0,
    }
    transport = Counting(outcomes=[Completed(message(input_tokens=500, output_tokens=200, **usage), "req_1")])
    meter: MeteredModel = economy.metered(transport)
    cycle_id = meter.open_cycle("schedule")
    request = prompts.work_request(ROOMY, "brief", [], cache_ttl=prompts.HOUR)
    worst = 1_000 * 4 + 4_000 * 2.5 + request["max_tokens"] * 10
    assert meter.quote(request, "work") == worst
    result = meter.call(cycle_id, "work", request)  # not refused
    assert result.status == "ok" and result.cost_micros == 500 * 2 + 1_000 * 4 + 3_500 * 2.5 + 200 * 10
    [row] = rows_of(economy, "SELECT cache_write_1h_tokens, cache_write_5m_tokens, estimate_micros FROM llm_calls")
    assert (row["cache_write_1h_tokens"], row["cache_write_5m_tokens"], row["estimate_micros"]) == (1_000, 3_500, worst)


def test_cache_writes_the_answer_doesnt_split_are_booked_at_an_hour(data_dir: Path) -> None:
    economy = make_economy(data_dir, ROOMY)
    answer = message(input_tokens=500, output_tokens=200, cache_creation_input_tokens=2_000)  # no split given
    meter = economy.metered(Counting(outcomes=[Completed(answer, "req_1")]))
    request = prompts.work_request(ROOMY, "brief", [], cache_ttl=prompts.HOUR)
    result = meter.call(meter.open_cycle("schedule"), "work", request)
    assert result.cost_micros == 500 * 2 + 2_000 * 4 + 200 * 10  # the longest lifetime the request asked for


def rows_of(economy: Any, sql: str) -> list[Any]:
    with economy.db.connection() as conn:
        return conn.execute(sql).fetchall()


# --- the fake model: an hour's entry, and the markers the API refuses ---


def test_the_fakes_hour_entry_lives_an_hour_of_its_clock() -> None:
    now = [1_000.0]
    fake = FakeTransport(clock=lambda: now[0])
    request = prompts.work_request(Settings(), "A brief. " * 20, [], cache_ttl=prompts.HOUR)
    first = usage_of(fake.send(request))
    hour = first["cache_creation"]["ephemeral_1h_input_tokens"]
    assert hour > 9_000 and first["cache_creation"]["ephemeral_5m_input_tokens"] > 0
    now[0] += 30 * 60  # the 5-minute entry is gone, the hour's isn't
    later = usage_of(fake.send({**request, "messages": [{"role": "user", "content": [{"type": "text", "text": "B"}]}]}))
    assert later["cache_read_input_tokens"] == hour and later["cache_creation"]["ephemeral_1h_input_tokens"] == 0
    now[0] += 3_599  # a read keeps it an hour from then
    assert usage_of(fake.send(request))["cache_read_input_tokens"] == hour
    now[0] += 3_601
    expired = usage_of(fake.send(request))
    assert expired["cache_read_input_tokens"] == 0 and expired["cache_creation"]["ephemeral_1h_input_tokens"] == hour


def test_the_fake_refuses_the_markers_the_api_refuses() -> None:
    fake = FakeTransport()
    body = prompts.work_request(Settings(), "brief", [], cache_ttl=prompts.HOUR)
    assert isinstance(fake.send(body), Completed)
    system = [*body["system"][:-1], {**body["system"][-1], "cache_control": FIVE}]
    for bad, why in (
        ({**body, "system": system, "cache_control": HOUR}, "must not come after"),  # an hour after 5 minutes
        ({**body, "cache_control": {"type": "ephemeral", "ttl": "24h"}}, "ttl"),
        ({**body, "cache_control": {"type": "persistent"}}, "type"),
    ):
        outcome = fake.send(bad)
        assert isinstance(outcome, Rejected) and why in outcome.error, outcome


# --- the planner's refusal: one plan on the worker's model ---


def test_a_planners_refusal_is_planned_once_on_the_worker_and_the_cycle_goes_on(data_dir: Path) -> None:
    agent, fake, _ = agent_with(data_dir, [refusal("claude-opus-5-5"), *a_cycle()], OPUS_PLANS)
    end = agent.run_cycle("schedule")
    assert end.status == "completed"
    made = calls(agent)
    assert [(c["purpose"], c["model"], c["stop_reason"]) for c in made[:2]] == [
        ("plan", "claude-opus-5-5", "refusal"),
        ("plan", "claude-sonnet-5", "end_turn"),
    ]
    assert [c["purpose"] for c in made[2:]] == ["work", "work", "reflect"]
    assert made[0]["cost_micros"] > 0  # the refusal was metered like any call, and so was the plan
    refused, retried = (r for r in fake.sent if request_kind(r) == "plan")
    assert retried["messages"] == refused["messages"] and retried["system"] == refused["system"]  # the same context
    assert retried["thinking"] == {"type": "disabled"} and retried["max_tokens"] == prompts.PLAN_MAX_TOKENS
    [cycle] = rows(agent, "SELECT status, note FROM cycles")
    assert cycle["note"] == "planned by claude-sonnet-5 after claude-opus-5-5 refused" == end.note
    said = rows(agent, "SELECT message FROM events WHERE message LIKE '%refused to plan%'")
    assert [e["message"] for e in said] == ["claude-opus-5-5 refused to plan cycle #1: claude-sonnet-5 plans it, once"]


def test_a_refused_retry_stops_the_cycle_as_a_refusal_did(data_dir: Path) -> None:
    agent, _, _ = agent_with(data_dir, [refusal("claude-opus-5-5"), refusal("claude-sonnet-5")], OPUS_PLANS)
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("stopped", "the model refused to plan")
    assert [(c["purpose"], c["model"]) for c in calls(agent)] == [
        ("plan", "claude-opus-5-5"),
        ("plan", "claude-sonnet-5"),
    ]  # never a third


def test_the_guards_refusal_of_the_retry_stops_the_cycle(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, _ = agent_with(data_dir, [refusal("claude-opus-5-5"), *a_cycle()], OPUS_PLANS)
    call = agent.meter.call

    def guarded(cycle_id: int, purpose: str, request: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
        if request["model"] == "claude-sonnet-5":
            raise CallRefused("the cycle cap of $1.00 would be exceeded", "cap")
        return call(cycle_id, purpose, request, *args, **kwargs)

    monkeypatch.setattr(agent.meter, "call", guarded)
    end = agent.run_cycle("schedule")
    assert end.status == "stopped"
    assert end.note == (
        "the model refused to plan, and the guard refused claude-sonnet-5's plan: the cycle cap of $1.00 would be"
        " exceeded"
    )
    assert [c["purpose"] for c in calls(agent)] == ["plan"]


def test_no_retry_when_the_worker_is_the_planner(data_dir: Path) -> None:
    agent, _, _ = agent_with(data_dir, [refusal("claude-sonnet-5"), *a_cycle()])
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("stopped", "the model refused to plan")
    assert [c["purpose"] for c in calls(agent)] == ["plan"]


def test_a_work_steps_refusal_still_ends_the_cycle(data_dir: Path) -> None:
    agent, _, _ = agent_with(data_dir, [Plan(PLAN), refusal("claude-sonnet-5", "I can't help with that.")], OPUS_PLANS)
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("stopped", "the model refused to continue")
    made = [(c["purpose"], c["model"]) for c in calls(agent)]
    assert made == [("plan", "claude-opus-5-5"), ("work", "claude-sonnet-5")]  # no retry on another model


def test_a_work_steps_refusal_before_any_output_ends_the_cycle_without_a_nudge(data_dir: Path) -> None:
    """0.42.1: an empty refused reply was nudged once (another paid call on the same refused context) and the work then
    ended as "done", with a reflection."""
    agent, _, _ = agent_with(data_dir, [Plan(PLAN), refusal("claude-sonnet-5")], OPUS_PLANS)
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("stopped", "the model refused to continue")
    assert [(c["purpose"], c["model"]) for c in calls(agent)] == [
        ("plan", "claude-opus-5-5"),
        ("work", "claude-sonnet-5"),
    ]


def test_a_cycle_line_says_which_lifetime_its_calls_asked_for() -> None:
    def call(*ttls: str) -> dict[str, Any]:
        return {"plan": json.dumps({"cache_ttls": list(ttls)})}

    assert cache_line([call(), call("1h", "5m")]) == " cache=1h"
    assert cache_line([call(), call("5m")]) == " cache=5m"
    assert cache_line([call(), {"plan": None}, {"plan": "not json"}]) == ""
