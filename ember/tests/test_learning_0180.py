"""0.18.0: Phase 1 of vision/learning.md. The diagnostics of 2026-10-02 showed an agent whose memory was full of tool
limits (12 of 17 lessons), whose daily review's lesson was never kept and whose verdicts read "0 views" as "no
demand", whose strategy still named a venture it had parked, and whose journal was refused for its length, which lost
the handoff to the next cycle. Now the review's lesson is kept by Ember's code, each verdict names a bottleneck, a
strategy that names a parked venture is an obligation, the lessons' consolidation may drop a tool's limit, and an
over-long journal is cut instead of refused."""

from __future__ import annotations

import json
from pathlib import Path

from app.agent import context, memory, obligations, prompts, review, tools, ventures
from app.config import Settings
from tests.test_agent import rows
from tests.test_lessons import lessons
from tests.test_review import next_day

LESSON = "A project without a finished listing after a few days teaches me nothing: finish or stop it."


def test_the_daily_review_s_lesson_is_kept_in_the_lessons(data_dir: Path) -> None:
    agent, _ = next_day(data_dir)
    agent.run_cycle("schedule")
    text = agent.memory().read("lessons")
    assert sum(LESSON in line for line in text.splitlines()) == 1
    events = [e["message"] for e in agent.db.recent_events(limit=60)]
    assert f"The daily review's lesson was kept: {LESSON}" in events
    [row] = rows(agent, "SELECT verdicts FROM reviews")
    assert all(v["bottleneck"] in prompts.BOTTLENECKS for v in json.loads(row["verdicts"]))


def test_a_verdict_names_its_bottleneck_and_the_plan_sees_it() -> None:
    answer = {
        "verdicts": [
            {"project_id": 3, "verdict": "change", "bottleneck": "reach", "why": "2 views, no pins or posts."},
            {"project_id": 4, "verdict": "continue", "bottleneck": "nonsense", "why": "Too early."},
        ],
        "lesson": "Nobody finds a listing by accident.",
    }
    parsed = review.parse(json.dumps(answer), {3, 4})
    assert parsed is not None
    assert [(v.project_id, v.bottleneck) for v in parsed.verdicts] == [(3, "reach"), (4, "")]
    item = prompts.REVIEW_SCHEMA["properties"]["verdicts"]["items"]
    assert "bottleneck" in item["required"] and item["properties"]["bottleneck"]["enum"] == list(prompts.BOTTLENECKS)


def test_the_review_judges_reach_before_demand() -> None:
    rules = " ".join(prompts.REVIEW_RULES.split())
    assert "few views with little reach is a reach problem, never proof of no demand (market it)" in rules
    assert "never set caps, budgets or sleep below them" in rules
    assert "Stop what has cost money for days without a sign of demand" not in rules


def test_a_strategy_naming_a_parked_venture_is_owed(data_dir: Path) -> None:
    agent, _ = next_day(data_dir)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        vid = ventures.create(
            conn, scope, title="Dropshipping store", pitch="Supplier products.", stage="parked", now="2026-09-01T12:00Z"
        )
        strategy = f"Current priority: dropshipping (#{vid}, owner's explicit wish) - research suppliers."
        line = obligations.stale_strategy(conn, scope, strategy)
        assert line.startswith(f"Your strategy names #{vid} Dropshipping store (parked): rewrite it")
        assert obligations.stale_strategy(conn, scope, "Etsy digital downloads, then Pinterest.") == ""
        assert obligations.stale_strategy(conn, scope, f"Request #{vid} waits for my owner.") == ""  # not the venture
        assert line in obligations.text(conn, scope, agent.clock.today(), strategy)


def test_the_consolidation_may_drop_a_tool_s_limit_but_never_a_no_backed_by_data() -> None:
    names = frozenset(tools.SPECS)
    text = lessons(
        "propose_printify_product's reason field caps at 300 chars.",
        "Dropshipping ruled out by data: margins under 5%.",
        "Owner wants finished products only.",
    )
    shown = memory.consolidation_input(text, set(), names)
    assert "1. propose_printify_product's reason field caps at 300 chars. (has numbers, tool)" in shown
    assert "2. Dropshipping ruled out by data: margins under 5%. (has numbers)" in shown
    answer = {"keep": [], "drop": [{"line": 1, "why": "a tool's limit"}, {"line": 2, "why": "old"}]}
    new, summary = memory.consolidate(text, answer, set(), 4_000, names) or ("", "")
    assert "propose_printify_product" not in new and "margins under 5%" in new
    assert summary.startswith("3 lessons became 2 (0 merged into others, 1 dropped)")
    assert memory.consolidate(text, answer, set(), 4_000) is None  # without the tools' names both have numbers: kept


def test_an_over_long_journal_is_cut_not_refused() -> None:
    spec = tools.SPECS["write_journal"]
    notes: list[str] = []
    args = tools._checked(spec.fields, {"summary": "Done.", "entry": "x" * 2_652, "next": "Poster #3."}, notes)
    assert len(args["entry"]) == 2_000 and args["entry"].endswith("…") and args["next"] == "Poster #3."
    assert notes == ["entry was cut to 2,000 of its 2,652 characters"]


def test_the_rules_say_to_invest_not_to_save() -> None:
    planner = prompts.PLANNER_RULES
    assert "Your daily cap is a limit, not a target" not in planner and "up to your owner's caps" in planner
    request = json.dumps(prompts.work_request(Settings(), "brief", []), ensure_ascii=False)
    assert "frugal founder" not in request and "sleeping longer to save money" not in request
    assert "Think like an investor: money is for bets on what blocks income most" in " ".join(request.split())
    assert "a tool's limits are no lessons" in " ".join(request.replace("\\n", " ").split())


def test_the_planner_shows_more_lessons() -> None:
    assert context.PLANNER_BUDGETS["lessons"] == 2_600
