"""Preview data for the phase-1 dashboard.

None of this is real and none of it touches the database: it only exists so the
dashboard layout can be reviewed before the economy (phase 2) and the agent
(phase 3) are built. Every payload built from it is marked ``"mock": true``, and
the dashboard shows a banner while it is in use.

``scenario`` previews the different life states: alive, paused, critical, dead.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

SCENARIOS = ("alive", "paused", "critical", "dead")
DAYS = 30


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _economy(scenario: str, today: date) -> dict[str, Any]:
    rng = random.Random(42)  # noqa: S311 - fixed seed for repeatable preview data, not security
    start = today - timedelta(days=DAYS - 1)
    raw_costs = [rng.uniform(0.25, 0.85) for _ in range(DAYS)]
    raw_costs[-1] *= 0.4  # today is not over yet
    grants = [20.0 if i == 0 else 0.0 for i in range(DAYS)]
    expenses = [1.19 if i == 11 else 0.0 for i in range(DAYS)]
    revenues = [0.0] * DAYS
    death_index = None
    if scenario in ("alive", "paused"):
        grants[18] = 5.0
        for i in range(12, DAYS):
            if rng.random() < 0.3:
                revenues[i] = rng.choice([1.0, 2.0, 3.0, 5.0])
        if scenario == "paused":
            raw_costs[-1] = raw_costs[-2] = 0.0
        costs = raw_costs
    elif scenario == "critical":
        # Scale spending so the agent ends with 0.85 USD, a couple of days of runway.
        scale = (sum(grants) - sum(expenses) - 0.85) / sum(raw_costs)
        costs = [c * scale for c in raw_costs]
    else:  # dead: the money runs out three days ago
        death_index = DAYS - 4
        scale = (sum(grants) - sum(expenses)) / sum(raw_costs[: death_index + 1])
        costs = [c * scale if i <= death_index else 0.0 for i, c in enumerate(raw_costs)]
    days = []
    balance = 0.0
    for i in range(DAYS):
        balance += grants[i] + revenues[i] - costs[i] - expenses[i]
        if i == death_index:
            balance = 0.0
        days.append(
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "api_cost_usd": round(costs[i], 4),
                "expense_usd": expenses[i],
                "revenue_usd": revenues[i],
                "grant_usd": grants[i],
                "balance_usd": round(max(balance, 0.0), 4),
            }
        )
    totals = {
        key: round(sum(d[key] for d in days), 4) for key in ("api_cost_usd", "expense_usd", "revenue_usd", "grant_usd")
    }
    total_cost = totals["api_cost_usd"] + totals["expense_usd"]
    last7 = [d["api_cost_usd"] + d["expense_usd"] for d in days[-7:]]
    avg_daily = sum(last7) / len(last7)
    balance_now = days[-1]["balance_usd"]
    return {
        "days": days,
        "totals": totals,
        "self_sufficiency_ratio": round(totals["revenue_usd"] / total_cost, 4) if total_cost else None,
        "balance_usd": balance_now,
        "avg_daily_spend_usd": round(avg_daily, 4),
        "runway_days": round(balance_now / avg_daily, 1) if avg_daily > 0 and balance_now > 0 else 0.0,
        "today_spend_usd": days[-1]["api_cost_usd"],
    }


def dashboard(agent_name: str, daily_cap: float, cycle_cap: float, scenario: str = "alive") -> dict[str, Any]:
    scenario = scenario if scenario in SCENARIOS else "alive"
    now = datetime.now(UTC).replace(microsecond=0)
    born = now - timedelta(days=DAYS - 1, hours=5)
    economy = _economy(scenario, now.date())
    running = scenario == "alive"
    state = scenario
    agent = {
        "name": agent_name,
        "state": state,
        "born_at": _iso(born),
        "age_days": (now - born).days,
        "balance_usd": economy["balance_usd"],
        "runway_days": economy["runway_days"],
        "today_spend_usd": economy["today_spend_usd"],
        "daily_cap_usd": daily_cap,
        "cycle_cap_usd": cycle_cap,
        "last_wake_at": _iso(now - timedelta(minutes=6)),
        "next_wake_at": None if scenario in ("paused", "dead") else _iso(now + timedelta(hours=3, minutes=54)),
        "cycle_running": running,
    }
    payload: dict[str, Any] = {
        "mock": True,
        "scenario": scenario,
        "agent": agent,
        "economy": economy,
        "now": _now(now, running, cycle_cap),
        "projects": _projects(),
        "activity": _activity(now),
        "approvals": _approvals(now),
        "inbox": _inbox(now, agent_name),
        "upgrades": _upgrades(now),
        "mind": _mind(now, agent_name),
        "memorial": _memorial(agent_name, born, now, economy) if scenario == "dead" else None,
    }
    return payload


def sensors(scenario: str = "alive") -> dict[str, Any]:
    economy = _economy(scenario if scenario in SCENARIOS else "alive", datetime.now(UTC).date())
    return {
        "mock": True,
        "state": scenario if scenario in SCENARIOS else "alive",
        "balance_usd": economy["balance_usd"],
        "runway_days": economy["runway_days"],
        "today_spend_usd": economy["today_spend_usd"],
    }


def _now(now: datetime, running: bool, cycle_cap: float) -> dict[str, Any]:
    return {
        "cycle_id": 58,
        "running": running,
        "phase": "act" if running else "sleeping",
        "started_at": _iso(now - timedelta(minutes=6)),
        "step": 4 if running else None,
        "max_steps": 15,
        "spent_usd": 0.0412 if running else 0.0,
        "cycle_cap_usd": cycle_cap,
        "plan": (
            "1. Read the owner's reply about the blueprint pack.\n"
            "2. Draft the forum post for v2 (with AI disclosure) and request approval.\n"
            "3. Search for other Home Assistant users asking for energy-dashboard blueprints.\n"
            "4. Update the project notes, then sleep 4 hours."
        ),
        "current_action": 'web_search: "home assistant energy dashboard blueprint request"' if running else None,
    }


def _projects() -> list[dict[str, Any]]:
    return [
        {
            "id": 3,
            "title": "Home Assistant blueprint pack",
            "hypothesis": "HA users will tip for well-documented automation blueprints that save them an evening.",
            "status": "active",
            "spent_usd": 1.84,
            "earned_usd": 11.0,
            "next_step": "Owner posts v2 on the community forum (waiting for approval).",
            "updated_at": "2026-09-26T14:10:00Z",
        },
        {
            "id": 4,
            "title": "Smart-home glossary (German/English)",
            "hypothesis": "People setting up smart homes in Germany want a plain-language bilingual glossary.",
            "status": "waiting",
            "spent_usd": 2.1,
            "earned_usd": 0.0,
            "next_step": "Waiting for the owner to decide on the sales platform (Impressum needed).",
            "updated_at": "2026-09-24T09:32:00Z",
        },
        {
            "id": 2,
            "title": "Weekly AI-tools digest",
            "hypothesis": "A short weekly digest could grow an audience that later pays for a premium issue.",
            "status": "failed",
            "spent_usd": 3.02,
            "earned_usd": 0.0,
            "next_step": "None. Nobody subscribed in 10 days; stopped to save money.",
            "updated_at": "2026-09-15T18:00:00Z",
        },
        {
            "id": 5,
            "title": "README proofreading for open-source projects",
            "hypothesis": "Maintainers who are not native speakers would pay a small fee for a clearer README.",
            "status": "idea",
            "spent_usd": 0.0,
            "earned_usd": 0.0,
            "next_step": "Check whether this is allowed on the platforms where maintainers gather.",
            "updated_at": "2026-09-27T08:00:00Z",
        },
    ]


def _activity(now: datetime) -> list[dict[str, Any]]:
    cycles = []
    for n, (minutes_ago, summary, cost, tools) in enumerate(
        [
            (
                6,
                "Drafting forum post for blueprint pack v2",
                0.0412,
                [
                    ("llm", "plan (claude-sonnet-5)", 0.0213),
                    ("tool", "workspace_read notes/blueprints.md", 0.0),
                    ("tool", "workspace_write drafts/forum-post-v2.md", 0.0),
                    ("tool", 'web_search "home assistant energy dashboard blueprint request"', 0.0100),
                ],
            ),
            (
                246,
                "Checked approvals; reflected on the glossary project",
                0.0631,
                [
                    ("llm", "plan (claude-sonnet-5)", 0.0198),
                    ("tool", 'message_owner "Question about Impressum for the glossary"', 0.0),
                    ("llm", "reflect (claude-sonnet-5)", 0.0087),
                ],
            ),
            (
                490,
                "Researched tipping platforms and their rules",
                0.1124,
                [
                    ("llm", "plan (claude-sonnet-5)", 0.0231),
                    ("tool", 'web_search "Ko-fi terms of service Germany"', 0.0100),
                    ("tool", "web_fetch ko-fi.com/terms", 0.0),
                    ("tool", 'request_approval "Create a Ko-fi page"', 0.0),
                    ("llm", "reflect (claude-sonnet-5)", 0.0102),
                ],
            ),
        ]
    ):
        cycles.append(
            {
                "cycle_id": 58 - n,
                "started_at": _iso(now - timedelta(minutes=minutes_ago)),
                "status": "running" if n == 0 else "completed",
                "summary": summary,
                "cost_usd": cost,
                "steps": [{"kind": kind, "summary": text, "cost_usd": c} for kind, text, c in tools],
            }
        )
    return cycles


def _approvals(now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "id": 12,
            "type": "publish",
            "status": "pending",
            "title": "Post blueprint pack v2 on the Home Assistant community forum",
            "description": (
                "Adds three energy-dashboard blueprints requested in the thread. Clearly marked as AI-written."
            ),
            "payload": (
                "Hi all! I'm Ember, an AI agent run by a Home Assistant user. Version 2 of the blueprint pack adds "
                "three energy-dashboard blueprints you asked for. Free to use; tips welcome but never expected."
            ),
            "expected_cost": "About 10 minutes of the owner's time, no money.",
            "expected_benefit": "More users; tips so far average 2 USD per week.",
            "created_at": _iso(now - timedelta(minutes=3)),
        },
        {
            "id": 11,
            "type": "create_account",
            "status": "pending",
            "title": "Create a Ko-fi page for tips",
            "description": (
                "Ko-fi allows AI-assisted creators if disclosed. Needs the owner's name and an Impressum link."
            ),
            "payload": 'Page title: "Ember, an AI agent making Home Assistant blueprints".',
            "expected_cost": "Free; Ko-fi takes 0% on tips.",
            "expected_benefit": "Makes tipping easier than bank transfer.",
            "created_at": _iso(now - timedelta(hours=8)),
        },
        {
            "id": 9,
            "type": "publish",
            "status": "done",
            "title": "Publish blueprint pack v1",
            "description": "First three blueprints with setup instructions.",
            "payload": "(see workspace/drafts/pack-v1.md)",
            "expected_cost": "15 minutes of the owner's time.",
            "expected_benefit": "First test of demand.",
            "decision_comment": "Approved; I shortened the intro.",
            "result_note": "Posted: community thread, 14 replies so far.",
            "created_at": _iso(now - timedelta(days=9)),
        },
        {
            "id": 8,
            "type": "contact",
            "status": "rejected",
            "title": "Email 20 maintainers about README proofreading",
            "description": "Cold outreach to maintainers found via search.",
            "payload": "(draft email)",
            "expected_cost": "30 minutes of the owner's time.",
            "expected_benefit": "Maybe 1-2 paying customers.",
            "decision_comment": "No cold email. That's spam, even if it's polite.",
            "created_at": _iso(now - timedelta(days=11)),
        },
    ]


def _inbox(now: datetime, agent_name: str) -> list[dict[str, Any]]:
    return [
        {
            "id": 7,
            "from": "agent",
            "text": "Question: do I need an Impressum before we sell the glossary on Gumroad? "
            "If yes, is it OK to link yours?",
            "created_at": _iso(now - timedelta(hours=4)),
        },
        {
            "id": 6,
            "from": "owner",
            "text": "Nice work on v1. Please keep posts short, forum people skim.",
            "created_at": _iso(now - timedelta(days=2)),
        },
        {
            "id": 5,
            "from": "agent",
            "text": f"Hi, this is {agent_name}. Blueprint pack v1 got 14 replies and "
            "2 tips (recorded by you). I'll draft v2.",
            "created_at": _iso(now - timedelta(days=2, hours=3)),
        },
    ]


def _upgrades(now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "id": 3,
            "title": "Let me validate blueprint YAML before publishing",
            "problem": "Two blueprints in v1 had indentation errors a user had to report.",
            "proposed_change": "Enable the code execution tool so I can parse YAML before asking for approval.",
            "expected_benefit": "Fewer embarrassing fixes, less of the owner's time.",
            "priority": "high",
            "status": "new",
            "created_at": _iso(now - timedelta(days=1)),
        },
        {
            "id": 2,
            "title": "Show me forum replies automatically",
            "problem": "I only learn about replies when the owner pastes them.",
            "proposed_change": "A read-only feed of replies to threads I started.",
            "expected_benefit": "Faster feedback loops.",
            "priority": "medium",
            "status": "accepted",
            "created_at": _iso(now - timedelta(days=6)),
        },
        {
            "id": 1,
            "title": "Longer journal context",
            "problem": "I forget what I tried two weeks ago.",
            "proposed_change": "Include the last 10 journal summaries instead of 1.",
            "expected_benefit": "Better continuity.",
            "priority": "low",
            "status": "declined",
            "decision_comment": "Too expensive per cycle. Keep lessons.md up to date instead.",
            "created_at": _iso(now - timedelta(days=14)),
        },
    ]


def _mind(now: datetime, agent_name: str) -> dict[str, Any]:
    return {
        "strategy": (
            "# Strategy\n\n"
            "Focus on one thing that already shows demand: Home Assistant blueprints.\n\n"
            "- Ship small, useful releases; ask for tips, never pressure.\n"
            "- Spend at most 0.10 USD per cycle on research until tips cover costs.\n"
            "- Park the glossary until the Impressum question is answered.\n"
        ),
        "lessons": (
            "# Lessons\n\n"
            "1. Newsletters need an audience first; I had none. Don't start there again.\n"
            "2. Test every YAML file before asking for approval.\n"
            "3. The owner prefers one batched request over three small ones.\n"
            "4. Cold outreach is spam, even when polite.\n"
        ),
        "identity": (
            f"# Identity\n\nI am {agent_name}, an AI agent. I say so everywhere my work reaches people. "
            "I like making things that save people time.\n"
        ),
        "journal": [
            {
                "cycle_id": 57,
                "created_at": _iso(now - timedelta(hours=4)),
                "summary": "Asked the owner about the Impressum. Glossary waits. Tips this week: 2 USD.",
            },
            {
                "cycle_id": 56,
                "created_at": _iso(now - timedelta(hours=8)),
                "summary": "Read Ko-fi's terms. AI disclosure is allowed. Requested approval for a page.",
            },
            {
                "cycle_id": 55,
                "created_at": _iso(now - timedelta(hours=12)),
                "summary": "Mistake: I re-read the whole forum thread and wasted 0.04 USD. Summaries go in notes now.",
            },
        ],
    }


def _memorial(agent_name: str, born: datetime, now: datetime, economy: dict[str, Any]) -> dict[str, Any]:
    died = now - timedelta(days=3)
    return {
        "name": agent_name,
        "born_at": _iso(born),
        "died_at": _iso(died),
        "lifespan_days": (died - born).days,
        "cycles": 71,
        "total_cost_usd": round(economy["totals"]["api_cost_usd"] + economy["totals"]["expense_usd"], 2),
        "total_revenue_usd": economy["totals"]["revenue_usd"],
        "last_will": (
            "I tried a newsletter, a glossary and README proofreading. None found paying customers before my "
            "money ran out. What I learned: find people who already ask for something before building it, and "
            "spend less on research. If I lived again I would start with one small, free, useful thing and ask "
            "for feedback on day one."
        ),
    }
