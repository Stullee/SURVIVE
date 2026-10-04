"""Every request the agent builds can be priced by the guard and fits the money reserved for it."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from app.agent import context, digest, library, loop, obligations, prompts, roadmap, tools, ventures
from app.agent.news import CHANGELOG_LIMIT, News
from app.config import Settings
from app.economy.estimate import plan_request
from app.economy.life import LifeStatus, Runway
from app.economy.metering import rough_token_count
from app.economy.pricing import LAST_WILL, PLANNER_OPENING, REFLECT, WORK

SETTINGS = Settings(agent_name="X" * 40)
HEADINGS = {
    "instructions": context.INSTRUCTIONS_HEADING,
    "news": "SINCE YOUR LAST WAKE",
    "research": context.RESEARCH_HEADING,
    "strategy": context.STRATEGY_HEADING,
    "identity": context.IDENTITY_HEADING,
    "lessons": context.LESSONS_HEADING,
}


def filler(budget: int) -> str:
    # Multibyte text is the worst case per byte for the guard's token count.
    return context.cut(("ä" * 40 + "\n") * (budget // 20), budget)


def biggest_planner_context() -> str:
    # Every section at its budget: the standing instructions, RECENT RESEARCH and MAIL included, and (0.12.0) the
    # OBLIGATIONS at their bound (never cut) and the lessons the owner pinned (on top of LESSONS' budget).
    parts = [f"== {HEADINGS.get(k, k.upper())} ==\n{filler(v)}" for k, v in context.PLANNER_BUDGETS.items()]
    owed = f"== {obligations.HEADING} ==\n{filler(obligations.MAX_BYTES)}"
    pinned = f"{context.PINS_HEADING}\n{filler(context.PINS_BUDGET)}"
    return "\n\n".join([owed, *parts, pinned, "== TASK ==\nPlan this wake cycle. Reply with the JSON plan only."])


def longest_undone() -> list[str]:
    """What the reflection is shown of its cycle's undone tool calls, at its longest (0.12.0)."""
    row = {"tool": "ä" * 64, "status": "interrupted", "summary": "ä" * 400, "input": json.dumps({"title": "ä" * 400})}
    return [digest.undone_line(row)] * digest.UNDONE_SHOWN + [f"and {10**6} more"]  # type: ignore[arg-type]


def first_step_and_reflection(brief: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The first work step, and the reflection after it with the room the loop keeps for one step's growth (with the
    most tools: an ordinary cycle's with a mailbox's, a shop's, the library's and, 0.13.0, Pinterest's, Printify's and
    the website's too, 0.14.0: the blog's, 0.19.0: Bluesky's, and the most undone calls; a venture cycle has fewer,
    0.12.0)."""
    grown = [{"role": "assistant", "content": [{"type": "text", "text": "x" * loop.STEP_GROWTH_BYTES}]}]
    longest = "ä" * prompts.ENDED_CHARS  # why the work ended, at its longest
    return (
        prompts.work_request(
            SETTINGS,
            brief,
            [],
            mail=True,
            etsy=True,
            library=True,
            pinterest=True,
            printify=True,
            site=True,
            blog=True,
            bluesky=True,
        ),
        prompts.reflect_request(
            SETTINGS,
            brief,
            grown,
            [],
            mail=True,
            etsy=True,
            ended=longest,
            library=True,
            undone=longest_undone(),
            pinterest=True,
            printify=True,
            site=True,
            blog=True,
            bluesky=True,
        ),
    )


def test_profiles_cover_the_biggest_requests_without_much_slack() -> None:
    planner = rough_token_count(prompts.plan_request(SETTINGS, biggest_planner_context(), venture=True))
    will = rough_token_count(prompts.will_request(SETTINGS, filler(context.WILL_BUDGET)))
    assert planner <= PLANNER_OPENING.input_tokens <= planner * 1.15
    assert will <= LAST_WILL.input_tokens <= will * 1.15
    assert PLANNER_OPENING.max_tokens >= prompts.PLAN_MAX_TOKENS
    assert LAST_WILL.max_tokens >= prompts.WILL_MAX_TOKENS
    for request, profile in zip(first_step_and_reflection(filler(context.BRIEF_MAX)), (WORK, REFLECT), strict=True):
        tokens = rough_token_count(request)
        assert tokens <= profile.input_tokens <= tokens * 1.15
        assert profile.max_tokens >= request["max_tokens"]
        assert plan_request(request, tokens).cache_ttls == profile.cache_ttls  # priced like the real request


def overflowing_snapshot() -> context.Snapshot:
    """Every section the planner and the brief show, far over its budget (rows as dicts)."""
    long = "\n".join(["ä" * 99] * 40)
    research = {
        "input": json.dumps({"question": "ä" * 500}),
        "result": f'<data src="research" id="0a1b2c">\n{"ä" * 2_000}\n</data id="0a1b2c">\n(cost $0.0100)',
    }
    decided = {
        "type": "publish",
        "title": "ä" * 120,
        "status": "approved_with_changes",
        "final_payload": "ä" * 8_000,
        "decision_comment": "ä" * 2_000,
    }
    project = {"status": "active", "title": "ä" * 80, "next_step": "ä" * 200, "hypothesis": "ä" * 400}
    venture = biggest_venture()
    milestone = biggest_milestone()
    today = date(2026, 9, 30)
    return context.Snapshot(
        status=LifeStatus(
            mode="live", life_id=1, state="critical", reason="", last_will_due=True, runway=Runway(1, None)
        ),
        local_time="Wednesday 2026-09-30 10:00 CEST",
        version="10.10.10",
        agent_name=SETTINGS.agent_name,
        today_spend=10**9,
        daily_cap=1_000,
        cycle_cap=1_000,
        projects=[{"id": 1_000 + i, **project} for i in range(8)],  # type: ignore[misc]
        owner_messages=[{"id": i, "created_at": "2026-09-30T08:00:00Z", "text": "ä" * 2_000} for i in range(8)],  # type: ignore[misc]
        pending=[  # type: ignore[misc]
            {"id": 1_000 + i, "type": "create_account", "title": "ä" * 120, "created_at": "2026-09-30T08:00:00Z"}
            for i in range(20)
        ],
        last_cycle={"id": 10_000, "status": "completed", "note": "ä" * 300, "plan": json.dumps({"goal": "ä" * 300})},  # type: ignore[arg-type]
        last_journal={"summary": "ä" * 240, "handoff": "ä" * 400},  # type: ignore[arg-type]
        memory={"strategy": long, "identity": long, "lessons": long},
        workspace=[f"{'ä' * 190}/{i}.md (65,536 B)" for i in range(40)],
        news=News(
            decided=[{"id": i, "version": 3, **decided} for i in range(10)],  # type: ignore[misc]
            changelog="ä" * CHANGELOG_LIMIT,
            running_version="10.10.10",
        ),
        research=[{"cycle_id": 10_000 + i, **research} for i in range(context.RESEARCH_CALLS)],  # type: ignore[misc]
        mail=context.MailView(
            "ä" * 60 + "@example.org", 10**6, tuple((10**9 + i, "ä" * 320, "ä" * 300) for i in range(3))
        ),
        instructions="😀" * 1_500,
        proven=[
            (f"workshop/scripts/{'ä' * 170}-{i}.py", f"its files are in approved request #{10**9 + i}") for i in (1, 2)
        ],
        review="ä" * 3_000,
        etsy="ä" * 3_000,
        ventures=[  # type: ignore[misc]
            {**venture, "id": 1_000 + i, "stage": ("researching", "idea", "proposed", "live")[i % 4]} for i in range(60)
        ],
        venture_money={1_000 + i: ventures.Money(10**12, 10**12) for i in range(60)},
        venture=True,
        venture_share=100,
        venture_day=(10**12, 10**12),
        decision_wakes=True,
        today=today,
        roadmap=[  # type: ignore[misc]
            {**milestone, "id": 1_000 + i, "due": (today + timedelta(days=i * 7 - 5)).isoformat()}
            for i in range(roadmap.MAX_OPEN)
        ],
        roadmap_closed=[{**milestone, "id": 2_000 + i, "status": "missed"} for i in range(12)],  # type: ignore[misc]
        obligations=filler(obligations.MAX_BYTES),  # 0.12.0: at its bound
        library=library.Shelf(  # 0.12.0: a full library, with its newly studied documents at their longest
            documents=library.MAX_DOCUMENTS,
            chars=library.LIBRARY_CHARS,
            learnings=10**6,
            studied=10**5,
            waiting=10**5,
            failed=10**5,
            new=[
                library.Studied(10**9 + i, "ä" * 200, "ä" * 600, ("ä" * 40,) * 10, tuple(range(60)))
                for i in range(library.NEW_SHOWN)
            ],
        ),
    )


def biggest_knowledge() -> str:
    """The brief's learnings from the library (0.12.0), as many as Ember's code picks, each at its longest."""
    row = {"topic": "ä" * 40, "text": "ä" * 300, "document_id": 10**9, "part": 10**5, "document_title": "ä" * 200}
    return "\n".join(library.learning_line(row) for _ in range(6))


def biggest_milestone() -> dict[str, Any]:
    """A milestone with every text at its limit, moved often, with the owner's longest note and a date proposed to
    them (0.12.0)."""
    return {
        "id": 10**9,
        "parent_id": 10**9 - 1,
        "venture_id": 10**9,
        "project_id": 10**9,
        "title": "ä" * 100,
        "measure": "ä" * 300,
        "first_due": "2026-09-01",
        "due": "2026-09-20",
        "moves": 99,
        "status": "open",
        "result": "ä" * 600,
        "closed_at": "2026-09-29T08:00:00Z",
        "notes": "ä" * 2_000,
        "created_by": "owner",
        "owner_action": "note",
        "owner_comment": "ä" * 1_000,
        "proposed_due": "2026-10-20",
    }


def biggest_venture() -> dict[str, Any]:
    """A venture with every text at its limit and the owner's longest comment."""
    return {
        "id": 10**9,
        "parent_id": 10**9 - 1,
        "stage": "researching",
        "title": "ä" * 80,
        "pitch": "ä" * 600,
        "next_question": "ä" * 300,
        "notes": "ä" * 2_000,
        **{name: "ä" * limit for name, _, limit in ventures.CASE},
        **dict.fromkeys(ventures.SCORE_FIELDS, 3),
        "scores_by": "research",
        "owner_action": "note",
        "owner_comment": "ä" * 1_000,
        "owner_at": "2026-09-30T08:00:00Z",
        "created_by": "owner",
        "created_at": "2026-09-30T08:00:00Z",
    }


def test_the_real_contexts_stay_within_what_the_profiles_measure() -> None:
    snap = overflowing_snapshot()
    planner, _ = context.planner_context(snap, dry_run=True)
    assert f"== {context.RESEARCH_HEADING} ==" in planner and "== FROM YOUR OWNER ==" not in planner
    assert "\n== MAIL ==\n" in planner and f"\n== {context.INSTRUCTIONS_HEADING} ==\n" in planner
    assert "\n== WORKSHOP ==\nWorkshop check: workshop/scripts/" in planner
    assert "\n== TODAY'S REVIEW ==\nää" in planner and "\n== ETSY SHOP ==\nää" in planner
    assert "\n== VENTURES ==\n#1000 [researching] ää" in planner
    assert "\n== ROADMAP ==\nToday: Wednesday 2026-09-30. 20 open milestones: 1 overdue, 1 this week," in planner
    assert "Roadmap check: 1 milestone is overdue (#1000)" in planner
    assert "\n== YOUR OWNER'S LIBRARY ==\n500 documents from your owner (5,000,000 characters)" in planner
    assert '\n== YOUR LAST CYCLE ==\nYour handoff to this cycle: "ää' in planner  # 0.12.0
    assert f"\n== WAITING FOR YOUR OWNER ==\n{context.WAITING_NOTE}\n#1000 create_account" in planner
    assert rough_token_count(prompts.plan_request(SETTINGS, planner, venture=True)) <= PLANNER_OPENING.input_tokens
    plan = {"goal": "ä" * 300, "steps": ["ä" * 200] * 6}
    focus = {"id": 1_000, "title": "ä" * 80, "status": "active", "hypothesis": "ä" * 400, "next_step": "ä" * 200}
    projects = [{"id": 10**9 + i, "title": "ä" * 80, "status": "active"} for i in range(8)]
    venture = ventures.focus_text(biggest_venture(), ventures.Money(10**12, 10**12), 10**9, projects)  # type: ignore[arg-type]
    goal = roadmap.focus_text(biggest_milestone(), date(2026, 9, 30), biggest_milestone())
    brief, _ = context.brief(  # type: ignore[arg-type]
        snap,
        True,
        plan,
        {**focus, "notes": "ä" * 2_000},
        100,
        venture_focus=venture,
        milestone_focus=goal,
        knowledge=biggest_knowledge(),
    )
    assert f"\n== {context.KNOWLEDGE_HEADING} ==\n[ää" in brief
    assert "\n== VENTURE CYCLE ==\n" in brief and "Focus venture: #1000000000 ää" in brief
    assert '\n== FOCUS ==\nFocus milestone: #1000000000 "ää' in brief
    assert "== FROM YOUR OWNER ==" in brief and "\n== MAIL ==\n" in brief and brief.endswith("bytes cut]")
    assert f"\n== {context.INSTRUCTIONS_HEADING} ==\n" in brief
    # The owner's and the research sections' room comes on top, even when the research itself is cut at the end.
    assert context.BRIEF_BUDGET < context.json_bytes(brief) <= context.BRIEF_MAX
    for request, profile in zip(first_step_and_reflection(brief), (WORK, REFLECT), strict=True):
        assert rough_token_count(request) <= profile.input_tokens


@pytest.mark.parametrize(
    "request_",
    [
        prompts.plan_request(SETTINGS, "context"),
        prompts.work_request(SETTINGS, "brief", []),
        prompts.work_request(SETTINGS, "brief", [], mail=True),
        prompts.work_request(SETTINGS, "brief", [], final=True),
        prompts.reflect_request(SETTINGS, "brief", [], []),
        prompts.reflect_request(SETTINGS, "brief", [], [], mail=True),
        prompts.will_request(SETTINGS, "context"),
        prompts.research_request(SETTINGS, "question", None),
        prompts.research_request(SETTINGS, "question", None, "reddit.com"),
        prompts.research_request(SETTINGS, "question", "https://example.com/page"),
    ],
)
def test_every_request_can_be_priced(request_: dict) -> None:
    plan = plan_request(request_, rough_token_count(request_))
    assert plan.model == "claude-sonnet-5"


def test_cache_breakpoints_stay_within_the_api_limit() -> None:
    def markers(node: object) -> int:
        if isinstance(node, dict):
            return ("cache_control" in node) + sum(markers(v) for v in node.values())
        if isinstance(node, list):
            return sum(markers(v) for v in node)
        return 0

    request = prompts.reflect_request(SETTINGS, "brief", [], [])
    assert markers(request) <= 4


def test_the_constitution_is_the_owners_text_with_the_name_filled_in() -> None:
    text = prompts.constitution(Settings(agent_name="Nova {x}"))
    assert text.startswith("You are Nova {x}, an autonomous AI agent")
    assert "MINDSET: SOLUTIONS, NOT OBSTACLES" in text


def test_tool_definitions_match_the_validation() -> None:
    assert {d["name"] for d in tools.definitions(mail=True)} - {d["name"] for d in tools.definitions()} == set(
        tools.MAIL_TOOLS
    )
    for definition in tools.definitions(mail=True):
        spec = tools.spec_of(definition["name"], venture=False)  # 0.15.0: as an ordinary cycle checks it
        assert spec is not None
        schema = definition["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(spec.fields)
        assert set(schema["required"]) == {n for n, f in spec.fields.items() if f.required}


def test_the_owners_knowledge_is_in_every_plan_and_work_step() -> None:
    known = prompts.knowledge()
    assert known.startswith("WHAT YOUR OWNER HAS LEARNED ABOUT THE OUTSIDE WORLD")
    for request in (prompts.plan_request(SETTINGS, "context"), prompts.work_request(SETTINGS, "brief", [])):
        assert any(block["text"] == known for block in request["system"])
    work = prompts.work_request(SETTINGS, "brief", [])["system"]
    assert "cache_control" in work[-1] and "cache_control" not in work[1]  # cached together with the rules
