"""0.12.0: models by purpose. Every plan ran on the planner model and all research on the worker model, so a stronger
model for the few decisions that set the course cost as much as using it for every routine plan. Now the venture
cycles' plans and the daily review run on the strategy model (strategy_model, the planner model if none is set), and
research runs on the owner's research model (claude-haiku-4-5, say), but only once a check on 10 of the agent's
research questions, asked of both models, shows it found web pages for as many, less one."""

from __future__ import annotations

from pathlib import Path

from app.agent import prompts, research_check
from app.agent.fake_llm import FakeTransport, Raw, Reply, ToolCalls
from app.agent.service import Agent
from app.config import Settings
from app.economy import pricing
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_ventures import JOURNAL, found, plan

HAIKU = "claude-haiku-4-5"
ROUTED = Settings(strategy_model="claude-opus-5-5", research_model=HAIKU)
CHECKING = Settings(starting_balance_usd=100, daily_spend_cap_usd=50, cycle_spend_cap_usd=2, research_model=HAIKU)


def test_venture_plans_and_reviews_run_on_the_strategy_model() -> None:
    assert prompts.plan_request(ROUTED, "context")["model"] == "claude-sonnet-5"  # a routine plan
    assert prompts.plan_request(ROUTED, "context", venture=True)["model"] == "claude-opus-5-5"
    assert prompts.review_request(ROUTED, "YOUR NUMBERS")["model"] == "claude-opus-5-5"
    plain = Settings()
    assert prompts.plan_request(plain, "context", venture=True)["model"] == plain.planner_model  # none set
    assert prompts.research_request(ROUTED, "q", None)["model"] == ROUTED.worker_model  # until the check passes
    assert prompts.research_request(ROUTED, "q", None, model=HAIKU)["model"] == HAIKU


def test_a_cycle_can_start_only_if_its_costlier_plan_fits(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[]), cycles=0)
    routine = pricing.opening_cost(Settings(), agent.db)
    assert pricing.opening_cost(ROUTED, agent.db) > routine  # type: ignore[operator]


def state_of(agent: Agent) -> research_check.Check:
    with agent.db.connection() as conn:
        return research_check.check(conn, agent.scope(), HAIKU)


def researched(agent: Agent, fake: FakeTransport, question: str, *answers: Raw) -> None:
    """A cycle that asks one research question; ``answers``: its research calls' answers (in the check, the worker
    model's, then the research model's). Scripted: the fake researches when its dice say so, and the dice are seeded
    by the request, which holds the cycle's random fence nonce (tools.wrap), so 12 cycles without research came by
    chance, about once in 30 runs (CI, 10-01)."""
    fake.script.extend(
        [plan(steps=["research"]), ToolCalls([("research", {"question": question})]), *answers, Reply("Done."), JOURNAL]
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert [t for t in fake.trace if t[1] == "invalid"] == []


def compared(agent: Agent, times: int, found: int) -> None:
    """``times`` comparisons already made, the research model finding pages in ``found`` of them."""
    with agent.db.transaction() as conn:
        for i in range(times):
            research_check.record(
                conn,
                agent.scope(),
                HAIKU,
                None,
                f"Question {i}?",
                (None, 3, 900),
                (None, 3 if i < found else 0, 500, True),
                "now",
            )


def test_the_research_model_takes_over_once_its_check_passed(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=CHECKING, before=lambda a: compared(a, 9, 9))
    page = found("https://example.invalid/planners")
    researched(agent, fake, "Who sells weekly meal planners in German?", page, page)
    state = state_of(agent)
    assert state.passed and agent.dashboard()["models"]["research"] == HAIKU
    events = [e["message"] for e in agent.db.recent_events(limit=100)]
    assert any(m.startswith(f"The research model's check passed: {HAIKU} answered 10 of 10") for m in events)
    before = rows(agent, "SELECT COUNT(*) AS n FROM llm_calls")[0]["n"]
    researched(agent, fake, "What do German meal planners cost on Etsy?", page)
    [call] = rows(agent, f"SELECT model FROM llm_calls WHERE purpose = 'research' AND id > {before}")
    assert call["model"] == HAIKU  # research runs on it now
    assert rows(agent, "SELECT COUNT(*) AS n FROM research_checks")[0]["n"] == 10  # and nothing is compared any more


def test_a_research_model_that_finds_less_stays_out(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=CHECKING, before=lambda a: compared(a, 9, 6))
    page = found("https://example.invalid/planners")
    researched(agent, fake, "Who sells weekly meal planners in German?", page, page)
    state = state_of(agent)
    assert not state.passed and state.text().startswith(f"failed: {HAIKU} answered 10 of 10 and found web pages for 7")
    models = agent.dashboard()["models"]
    assert models["research"] == CHECKING.worker_model and "failed" in models["research_check"]["text"]
    [paid] = rows(agent, "SELECT model, overhead FROM llm_calls WHERE purpose = 'research_check'")
    assert paid == {"model": HAIKU, "overhead": 1}  # the check is overhead, not the venture's research


def test_the_check_counts_answers_and_pages() -> None:
    def state(answered: int, found: int, worker: int) -> research_check.Check:
        return research_check.Check(HAIKU, 10, answered, found, worker)

    assert state(10, 9, 10).passed and state(9, 8, 9).passed
    assert not state(8, 8, 8).passed  # two unanswered
    assert not state(10, 7, 9).passed  # two fewer questions with pages
    assert not research_check.Check(HAIKU, 9, 9, 9, 9).passed  # not done yet
