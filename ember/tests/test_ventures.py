"""Ventures (0.10.0): the tree of ways to earn, venture cycles with the owner's share, and the owner's word on them."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import context, econ, prompts, store, tools, ventures
from app.agent.fake_llm import FakeTransport, Plan, Raw, Reply, ToolCalls, request_kind, validate_request
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.clock import to_iso
from tests.economy_helpers import make_economy
from tests.test_agent import ROOMY, rows
from tests.test_agent_requests import biggest_venture
from tests.test_loop_shapes import run
from tests.test_owner_api import post
from tests.test_owner_loop import owner

# Every cycle a venture cycle.
VENTURING = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=100)
JOURNAL = ToolCalls([("write_journal", {"summary": "Worked on ventures", "entry": "Researched and scored."})])
TITLES = [seed[2] for seed in ventures.SEEDS]
ETSY, PINTEREST, DROPSHIPPING, PRINT, WEBSITE, RECRUITING, COMPANION, FIVERR = range(1, 9)


def plan(steps: list[str] | None = None, venture: int | None = None, project: int | None = None) -> Plan:
    return Plan(
        {
            "assessment": "ok",
            "goal": "Work on my ventures",
            "money_path": "A business case my owner can back",
            "focus_project_id": project,
            "focus_venture_id": venture,
            "steps": ["research the venture"] if steps is None else steps,
            "sleep_minutes": 120,
        }
    )


def found(*urls: str) -> Raw:
    """A research call's answer that found these web pages (none: it found nothing)."""
    results = [{"type": "web_search_result", "url": url, "title": "A page"} for url in urls]
    return Raw(
        {
            "id": "msg_research",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": results},
                {"type": "text", "text": "Shops sell this for 25 EUR."},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 100, "server_tool_use": {"web_search_requests": 1}},
        }
    )


def tool_results(agent: Agent, tool: str) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT status, summary, input, result FROM tool_calls WHERE tool = '{tool}' ORDER BY id")


def venture(agent: Agent, venture_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM ventures WHERE id = {venture_id}")[0]


def planner_texts(fake: FakeTransport) -> list[str]:
    return [request["messages"][0]["content"][0]["text"] for request in fake.sent if request_kind(request) == "plan"]


# --- the tree ---


def test_the_tree_starts_once_with_the_ideas_so_far(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    tree = rows(agent, "SELECT id, parent_id, stage, title, created_by, owner_action, notes FROM ventures ORDER BY id")
    assert [v["title"] for v in tree] == TITLES
    by_title = {v["title"]: v for v in tree}
    assert by_title["Pinterest for the Etsy shop"]["parent_id"] == ETSY
    assert by_title["Print on demand in the Etsy shop"]["parent_id"] == DROPSHIPPING
    assert by_title["Etsy digital products"]["stage"] == "researching"  # no listing is live yet
    assert by_title["Services on Fiverr"]["stage"] == "parked"
    assert by_title["Services on Fiverr"]["notes"] == "Your owner put Fiverr on hold."
    assert {v["created_by"] for v in tree} == {"agent", "owner"}
    assert all(v["owner_action"] is None for v in tree)  # planted, not news
    agent.recover()
    with agent.db.transaction() as conn:
        assert ventures.seed(conn, agent.scope(), to_iso(agent.clock.now())) == 0
    assert len(rows(agent, "SELECT id FROM ventures")) == len(ventures.SEEDS)


def test_the_etsy_leg_is_live_with_the_projects_that_listed(data_dir: Path) -> None:
    economy = make_economy(data_dir, VENTURING)
    agent = Agent(economy.db, LoadedSettings(VENTURING), economy, transport=FakeTransport(), cycles_enabled=True)
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle_id = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros, session)"
            " VALUES (?, 'b', ?, 'running', 'schedule', 1, 0, ?)",
            (scope.life_id, now, scope.session),
        ).lastrowid
        project_id = conn.execute(
            "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'CV templates', 'Job seekers pay', 'active')",
            (scope.mode, scope.session, scope.life_id, cycle_id, now, now),
        ).lastrowid
        conn.execute("UPDATE cycles SET project_id = ? WHERE id = ?", (project_id, cycle_id))
        conn.execute("UPDATE cycles SET status = 'completed', ended_at = ? WHERE id = ?", (now, cycle_id))
        approval_id = conn.execute(
            "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
            " payload_sha256, expected_cost, expected_benefit, executor, action)"
            " VALUES (?, ?, ?, ?, ?, 'sell', 'Etsy listing: CV', 'd', 'p', 's', 'c', 'b', 'etsy_listing', '{}')",
            (scope.mode, scope.session, scope.life_id, cycle_id, now),
        ).lastrowid
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, title)"
            " VALUES (?, ?, ?, ?, ?, 'active', 'CV')",
            (scope.mode, scope.session, approval_id, now, now),
        )
        assert ventures.seed(conn, scope, now) == len(ventures.SEEDS)
    assert venture(agent, ETSY)["stage"] == "live"
    assert rows(agent, "SELECT venture_id FROM projects")[0]["venture_id"] == ETSY


def test_a_ventures_identity_and_history_are_kept(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    with agent.db.connection() as conn:
        for sql in (
            "UPDATE ventures SET title = 'Other' WHERE id = 1",
            "UPDATE ventures SET parent_id = 3 WHERE id = 2",
            "DELETE FROM ventures WHERE id = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ventures SET revenue = 6 WHERE id = 1")


def test_the_weight_counts_revenue_double_and_turns_the_bad_ones_around() -> None:
    assert ventures.weight({}) is None
    assert ventures.weight({"revenue": 5}) == 100
    assert ventures.weight({"revenue": 1, "risk": 5}) == 0
    best = {"revenue": 5, "doability": 5, "difficulty": 1, "risk": 1, "speed": 5, "cost": 1}
    assert ventures.weight(best) == 100
    assert ventures.weight(dict.fromkeys(ventures.SCORE_FIELDS, 3)) == 50
    assert ventures.weight({**best, "revenue": 1}) == 71  # 20 of 28 points: revenue counts double
    assert ventures.scores_text({**best, "scores_by": "brainstorm"}).startswith("weight 100 guessed (revenue 5, ")
    assert ventures.slug("Ärger-frei: Grants für Vereine!") == "arger-frei-grants-fur-vereine"
    assert ventures.file_of(12, "  ") == "ventures/12-venture.md"


# --- venture cycles ---


@pytest.mark.parametrize(
    ("share", "spent", "ventured", "turn"),
    [
        (25, 0, 0, False),  # the day's first cycle is an ordinary one
        (25, 400, 0, True),
        (25, 400, 100, False),  # exactly the share
        (25, 401, 100, True),
        (0, 400, 0, False),  # switched off
        (100, 0, 0, True),  # every cycle
    ],
)
def test_a_cycle_is_a_venture_cycle_while_ventures_are_below_their_share(
    share: int, spent: int, ventured: int, turn: bool
) -> None:
    assert ventures.venture_turn(share, spent, ventured) is turn


def test_venture_cycles_get_the_owners_share_of_the_days_spending(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=25)
    fake = FakeTransport(seed=5, scenario="founder")
    agent, ends = run(data_dir, fake, cycles=8, settings=settings)
    assert all(end.status == "completed" for end in ends)
    cycles = rows(agent, "SELECT id, venture FROM cycles ORDER BY id")
    spent = ventured = 0
    for cycle in cycles:  # each cycle followed the rule, with what the day had spent before it
        assert bool(cycle["venture"]) is ventures.venture_turn(25, spent, ventured), cycle
        cost = rows(agent, f"SELECT COALESCE(SUM(cost_micros), 0) AS c FROM llm_calls WHERE cycle_id = {cycle['id']}")
        spent += cost[0]["c"]
        ventured += cost[0]["c"] if cycle["venture"] else 0
    assert 2 <= sum(c["venture"] for c in cycles) <= 4
    assert 0.15 < ventured / spent < 0.35
    # The fake grows its tree in a venture cycle (brainstorm) and researches the venture it focused on.
    assert any(r["status"] == "ok" for r in tool_results(agent, "brainstorm"))
    assert rows(agent, "SELECT COUNT(*) AS n FROM ventures WHERE scores_by = 'brainstorm'")[0]["n"] >= 6
    focused = rows(agent, "SELECT venture_id FROM cycles WHERE venture = 1 AND venture_id IS NOT NULL")
    assert focused and agent.ventures()["items"][focused[0]["venture_id"] - 1]["spent_usd"] > 0
    texts = planner_texts(fake)
    assert any("This is a venture cycle." in t and "Plan this venture cycle." in t for t in texts)
    assert any("This is a venture cycle." not in t and "Plan this wake cycle." in t for t in texts)


def test_brainstorm_and_more_research_only_in_venture_cycles(data_dir: Path) -> None:
    research = [("research", {"question": f"Question {i}?"}) for i in range(4)]
    ordinary = FakeTransport(
        script=[
            plan(),
            ToolCalls([("brainstorm", {}), *research[:3]]),
            *[Reply("Found it.")] * 3,
            ToolCalls(research[3:]),  # refused by its limit before any model call
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, ordinary, settings=ROOMY)
    assert [r["status"] for r in tool_results(agent, "brainstorm")] == ["error"]
    assert "there is no tool called 'brainstorm'" in tool_results(agent, "brainstorm")[0]["result"]
    refused = tool_results(agent, "research")[-1]
    assert refused["status"] == "error" and "at most 3 times per cycle (8 in a venture cycle)" in refused["result"]
    work = [r for r in ordinary.sent if request_kind(r) == "work"]
    assert all("brainstorm" not in {t["name"] for t in r["tools"]} for r in work)


def test_a_venture_cycle_researches_up_to_eight_times(data_dir: Path) -> None:
    four = [("research", {"question": f"Question {i}?"}) for i in range(4)]
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls(four),
            *[Reply("Found it.")] * 4,
            ToolCalls(four),
            *[Reply("Found it.")] * 4,
            ToolCalls([("research", {"question": "One more?"})]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    statuses = [r["status"] for r in tool_results(agent, "research")]
    assert statuses == ["ok"] * 8 + ["error"]
    assert "at most 8 times per cycle" in tool_results(agent, "research")[-1]["result"]
    assert rows(agent, "SELECT venture, venture_id FROM cycles") == [{"venture": 1, "venture_id": DROPSHIPPING}]
    work = [r for r in fake.sent if request_kind(r) == "work"]
    assert all("brainstorm" in {t["name"] for t in r["tools"]} for r in work)
    brief = work[0]["messages"][0]["content"][0]["text"]
    assert "\n== VENTURE CYCLE ==\nThis is a venture cycle: answer your owner's waiting messages first" in brief
    assert "Focus venture: #3 Dropshipping store [idea]" in brief
    assert "Knowledge file: ventures/3-dropshipping-store.md (not written yet)" in brief


def test_the_venture_focus_keeps_what_matters_most_when_it_is_cut() -> None:
    # 0.12.0: the focus was cut from the end, and lost the owner's comment and the first test.
    row = {**biggest_venture(), "researched": 1}
    projects = [{"id": 10**9 + i, "title": "ä" * 80, "status": "active"} for i in range(8)]
    paid = ventures.Money(10**12, 10**12, 10**12, 10**12)  # 0.12.0: with refunds and expenses
    text = ventures.focus_text(row, paid, 10**9, projects)  # type: ignore[arg-type]
    assert all(len(line) <= 400 for line in text.split("\n"))  # every field at most 220 characters
    lines = context.cut(text, context.VENTURE_FOCUS_BUDGET).split("\n")
    assert lines[0].startswith("Focus venture: #1000000000 ää") and lines[0].endswith(
        "[researching] · spent $1000000.00 · earned $1000000.00 less $1000000.00 of expenses · net -$1000000.00"
    )
    assert lines[1] == f'Owner: your owner\'s note (2026-09-30): "{"ä" * 219}…"'
    assert lines[2:4] == [
        f"First test: {'ä' * 219}…",
        f"Next question: {'ä' * 219}…",
    ]  # 0.15.0: then the pitch, before the knowledge file (here no room is left for it)
    ordered = [line.split(":")[0] for line in text.split("\n")]
    assert ordered == [
        "Focus venture",
        "Owner",
        "First test",
        "Next question",
        "Pitch",
        "Knowledge file",
        "Scores",
        "Research for it",
        "Demand",
        "Economics",
        "Setup",
        "First euro",
        "Risks",
        "Projects",
        "Notes",
    ]


def test_the_planner_sees_the_tree_and_the_venture_rules(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    request = next(r for r in fake.sent if request_kind(r) == "plan")
    assert any(block["text"] == prompts.VENTURE_RULES for block in request["system"])
    text = request["messages"][0]["content"][0]["text"]
    tree = context_section(text, "VENTURES")
    assert tree.startswith("#1 [researching] Etsy digital products · not scored yet")
    assert "#3 [idea] Dropshipping store · not scored yet" in tree
    assert "#2 [idea] Pinterest for the Etsy shop (branch of #1) · not scored yet" in tree
    assert "Parked or killed (don't start them again): #8 Services on Fiverr." in tree
    assert (
        "Your owner gives ventures 100% of your spending: $0.00 of today's $0.00 so far. This is a venture cycle."
        in text
    )
    assert (
        "A research call costs about $0.05 and a brainstorm about $0.10 (lately): this cycle's $1.00 pays for about"
        " $0.35 of them after planning, the work steps and the reflection, so about 7 research calls, or a brainstorm"
        " and 5." in context_section(text, "STATUS")
    )

    ordinary = FakeTransport(script=[plan(steps=[])])
    economy_agent, _ = run(data_dir / "other", ordinary, settings=Settings(venture_share=25))
    text = next(r for r in ordinary.sent if request_kind(r) == "plan")["messages"][0]["content"][0]["text"]
    assert context_section(text, "VENTURES") == (
        "#1 [researching] Etsy digital products · not scored yet\n6 ideas in the tree."
    )
    status = context_section(text, "STATUS")
    assert "Plan this wake cycle." in text and "venture cycle" not in status and "research call" not in status


@pytest.mark.parametrize(
    ("cap", "research", "brainstorm", "calls", "after"),
    [
        (0.60, 50_000, 100_000, 4, 2),
        (1.00, 50_000, 100_000, 7, 5),
        (0.60, 150_000, 300_000, 1, 0),
        (5, 50_000, 0, 8, 8),
    ],
)
def test_a_venture_cycle_is_told_how_much_research_it_can_pay_for(
    cap: float, research: int, brainstorm: int, calls: int, after: int
) -> None:
    text = ventures.room_text(cap, {"research": research, "brainstorm": brainstorm})
    assert text.endswith(f"so about {calls} research calls, or a brainstorm and {after}.")
    assert f"this cycle's ${cap:.2f} pays for about ${cap * ventures.ROOM_SHARE:.2f} of them" in text


def test_research_costs_are_what_the_recent_calls_cost(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    with agent.db.connection() as conn:
        assert ventures.call_costs(conn, agent.scope()) == ventures.USUAL_COSTS
    fake = FakeTransport(
        script=[  # 0.12.0: a venture cycle's research is a venture's (its focus venture)
            plan(venture=DROPSHIPPING),
            ToolCalls([("research", {"question": "Who sells this?"})]),
            Reply("Found it."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    [paid] = rows(agent, "SELECT cost_micros FROM llm_calls WHERE purpose = 'research'")
    assert paid["cost_micros"] > 0
    with agent.db.connection() as conn:
        assert ventures.call_costs(conn, agent.scope()) == {
            "research": paid["cost_micros"],
            "brainstorm": ventures.USUAL_COSTS["brainstorm"],
        }


def context_section(text: str, title: str) -> str:
    return text.split(f"== {title} ==\n", 1)[1].split("\n\n== ", 1)[0]


# --- the tools ---


SCORES = {"revenue": 4, "doability": 3, "difficulty": 3, "risk": 3, "speed": 2, "cost": 2}
CASE = {
    "demand": "Shops like X sell 200 a month (source).",
    "economics": "25 EUR price, 12 EUR cost, 29 EUR a month for the shop: 3 sales to break even.",
    "setup": "Owner: Gewerbe, Shopify account, 3 hours; Ember: product pages.",
    "first_euro": "About 4 weeks: the store needs ads first.",
    "risks": "Slow shipping; GPSR duties: pick an EU supplier.",
    "first_test": "10 products, 50 EUR of ads: 3 sales in two weeks means go.",
}
RESEARCH = ("research", {"question": "What do dropshipping stores earn?"})
# 0.13.0: a business case's numbers (venture_case)
NUMBERS = {
    "channel": "other",
    "price_eur": "25",
    "unit_cost_eur": "12",
    "monthly_costs_eur": "29",
    "sales_low": 1,
    "sales_mid": 6,
    "sales_high": 20,
    "setup_eur": "10",
    "owner_hours": "10",
    "first_sale_days": 30,
    "api_usd": "3",
}


def test_ventures_grow_learn_and_make_a_business_case(data_dir: Path) -> None:
    learned = {"learned": "Shopify costs 29 EUR a month (src).", "next_question": "Which EU supplier ships in 3 days?"}
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls(
                [
                    ("venture_create", {"title": "Dropshipping store", "pitch": "Again.", "stage": "idea"}),
                    (
                        "venture_create",
                        {
                            "title": "EU supplier finder",
                            "pitch": "Find suppliers.",
                            "stage": "idea",
                            "parent_id": DROPSHIPPING,
                        },
                    ),
                    ("venture_update", {"venture_id": DROPSHIPPING, **learned, **SCORES}),  # no research yet
                    ("venture_update", {"venture_id": DROPSHIPPING, "stage": "proposed"}),
                ]
            ),
            # in a venture cycle, research counts for the focus venture (0.12.0: a question asked again is answered
            # from before and counts for none)
            ToolCalls(
                [
                    RESEARCH,
                    ("research", {"question": "Which EU suppliers ship in 3 days?"}),
                    ("venture_case", NUMBERS),  # 0.13.0: its numbers, for the focus venture
                    (  # 0.13.0: an independent page behind its demand (a knock-out without one)
                        "evidence",
                        {
                            "claim": "Stores sell 200 a month.",
                            "metric": "sales a month",
                            "low": "200",
                            "unit": "orders",
                            "region": "DE",
                            "url": "https://example.invalid/a",
                        },
                    ),
                ]
            ),
            found("https://example.invalid/a"),
            found("https://example.invalid/b"),
            ToolCalls(
                [
                    ("venture_update", {"venture_id": DROPSHIPPING, **learned, **SCORES, "stage": "researching"}),
                    ("venture_update", {"venture_id": DROPSHIPPING, "stage": "building"}),
                    ("venture_update", {"venture_id": DROPSHIPPING, "stage": "proposed", **CASE}),
                    ("venture_update", {"venture_id": FIVERR, "stage": "researching"}),
                ]
            ),
            ToolCalls(
                [
                    ("venture_update", {"venture_id": WEBSITE, "stage": "parked"}),
                    ("venture_update", {"venture_id": WEBSITE, "stage": "parked", "note": "Needs months of posts."}),
                    ("venture_update", {"venture_id": PRINT, "stage": "live"}),
                    ("venture_update", {"venture_id": DROPSHIPPING, "learned": "A second finding."}),
                ]
            ),
            ToolCalls([("venture_update", {"venture_id": WEBSITE, "stage": "researching"})]),  # its own park
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, ends = run(data_dir, fake, settings=VENTURING)
    assert ends[0].status == "completed"
    created = tool_results(agent, "venture_create")
    assert created[0]["status"] == "error" and "venture #3 (idea) already has this title" in created[0]["result"]
    assert created[1]["status"] == "ok" and "Venture #9 is in your tree (idea, a branch of #3)" in created[1]["result"]
    assert venture(agent, 9)["parent_id"] == DROPSHIPPING and venture(agent, 9)["created_cycle_id"] == 1
    updates = tool_results(agent, "venture_update")
    # 0.32.0: the scores are refused, and the rest of the update (its next question, what it learned) is made
    assert updates[0]["status"] == "ok" and updates[0]["result"].startswith("Venture #3: updated. What you learned")
    assert (
        "Not done (the rest is saved): scores come from research: research venture #3 first (research with "
        "venture_id 3" in updates[0]["result"]
    )
    assert updates[1]["status"] == "error" and updates[1]["result"] == (
        "Error: venture #3 can't be proposed yet: a business case needs 2 research calls for it that found something "
        "(it has 0); scores for revenue, doability, difficulty, risk, speed, cost; demand, economics, setup, "
        "first_euro, risks, first_test filled in; its numbers (venture_case)"  # 0.24.0: an idea needs no stage first
        # 0.32.0: with the knock-outs that stand
        ". Ember's code knocks it out too: no independent source for its demand (it has no evidence yet: save an "
        "independent page's demand numbers with evidence)."
    )
    research = tool_results(agent, "research")
    assert [r["status"] for r in research] == ["ok", "ok"]
    assert "\nResearch for venture #3: 2 calls that found something.\n" in research[1]["result"]
    assert updates[2]["status"] == "ok" and "Now weight 57 (revenue 4, doability 3" in updates[2]["result"]
    assert updates[3]["status"] == "error" and "must be one of" in updates[3]["result"]  # only the owner backs
    assert (
        updates[4]["status"] == "ok"
        and "Your owner sees its business case on the Ventures tab." in updates[4]["result"]
    )
    assert updates[5]["status"] == "error"  # 0.12.0: Fiverr is parked by the owner (put on hold before ventures)
    assert "your owner parked venture #8: only they take it up again" in updates[5]["result"]
    assert updates[6]["status"] == "error" and "say why in note" in updates[6]["result"]
    assert updates[7]["status"] == "ok" and venture(agent, WEBSITE)["notes"] == "[#c1] Needs months of posts."
    assert updates[8]["status"] == "error" and "goes live once your owner backed it" in updates[8]["result"]
    assert updates[9]["status"] == "ok"
    assert updates[10]["status"] == "ok"  # a venture the agent parked, it can research again
    assert (venture(agent, WEBSITE)["stage"], venture(agent, WEBSITE)["parked_by"]) == ("researching", None)
    assert (venture(agent, FIVERR)["stage"], venture(agent, FIVERR)["parked_by"]) == ("parked", "owner")
    dropshipping = venture(agent, DROPSHIPPING)
    assert dropshipping["stage"] == "proposed" and dropshipping["proposed_at"] is not None
    assert dropshipping["scores_by"] == "research" and ventures.weight(dropshipping) == 57
    assert {name: dropshipping[name] for name in ventures.CASE_FIELDS} == CASE
    recorded = rows(agent, "SELECT venture_id, cycle_id, question, url, sources FROM venture_research ORDER BY id")
    assert recorded == [
        {"venture_id": DROPSHIPPING, "cycle_id": 1, "question": question, "url": None, "sources": 1}
        for question in (RESEARCH[1]["question"], "Which EU suppliers ship in 3 days?")
    ]
    workspace, _ = agent.roots()
    knowledge = workspace.read("ventures/3-dropshipping-store.md")
    assert knowledge.startswith("# Venture #3: Dropshipping store\nPitch: A web shop selling physical products")
    assert "\n## What you learned (newest last)\n\n### 20" in knowledge
    assert "Shopify costs 29 EUR a month (src).\n" in knowledge and knowledge.endswith("A second finding.\n")
    assert agent.dashboard()["badges"]["ventures_proposed"] == 1
    shown = next(v for v in agent.ventures()["items"] if v["id"] == DROPSHIPPING)
    assert shown["researched"] == 2 and agent.ventures()["research_to_propose"] == 2


def test_research_counts_for_the_venture_it_names_once_it_finds_pages(data_dir: Path) -> None:
    def research(venture_id: int | None = None, question: str = RESEARCH[1]["question"]) -> tuple[str, dict[str, Any]]:
        return ("research", {"question": question, **({"venture_id": venture_id} if venture_id else {})})

    fake = FakeTransport(
        script=[
            plan(steps=["research"]),  # the day's first cycle is an ordinary one: no focus venture
            ToolCalls(
                [research(question="Who sells online?")]
            ),  # 0.12.0: a question asked again is answered from before
            found("https://example.invalid/any"),
            Reply("Done."),
            JOURNAL,
            plan(venture=DROPSHIPPING),  # a venture cycle
            ToolCalls([research(), research(PRINT), research(FIVERR), research(99)]),
            found(),  # found no web page
            found("https://example.invalid/print"),
            ToolCalls([("venture_update", {"venture_id": PRINT, **SCORES})]),  # scored from its research
            Reply("Done."),
            JOURNAL,
        ]
    )
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=50)
    agent, ends = run(data_dir, fake, settings=settings)
    assert owner(agent).decide_venture(FIVERR, {"action": "kill"}, "Stefan").status == 200
    ends.append(agent.run_cycle("schedule"))
    assert [e.status for e in ends] == ["completed", "completed"] and [t for t in fake.trace if t[1] == "invalid"] == []
    assert rows(agent, "SELECT venture, venture_id FROM cycles ORDER BY id") == [
        {"venture": 0, "venture_id": None},
        {"venture": 1, "venture_id": DROPSHIPPING},
    ]
    calls = tool_results(agent, "research")
    assert [r["status"] for r in calls] == ["ok", "ok", "ok", "error", "error"]
    assert "Research for venture" not in calls[0]["result"]  # it named none, and had no focus venture
    assert "\nIt found no web page, so it doesn't count as research for venture #3.\n(cost $" in calls[1]["result"]
    assert "\nResearch for venture #4: 1 call that found something.\n" in calls[2]["result"]
    assert "your owner killed venture #8" in calls[3]["result"]
    assert "there is no venture #99" in calls[4]["result"]
    counted = rows(agent, "SELECT venture_id, cycle_id, sources FROM venture_research ORDER BY id")
    assert counted == [
        {"venture_id": DROPSHIPPING, "cycle_id": 2, "sources": 0},
        {"venture_id": PRINT, "cycle_id": 2, "sources": 1},
    ]
    assert [u["status"] for u in tool_results(agent, "venture_update")] == ["ok"]
    assert venture(agent, PRINT)["scores_by"] == "research"
    with agent.db.connection() as conn:
        assert ventures.researched(ventures.get(conn, agent.scope(), PRINT)) == 1
        assert ventures.researched(ventures.get(conn, agent.scope(), DROPSHIPPING)) == 0
        assert [ventures.researched(v) for v in ventures.all_ventures(conn, agent.scope())][2:4] == [0, 1]


def test_only_the_owner_takes_a_venture_out_of_their_park(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=[]),
            plan(venture=FIVERR),
            ToolCalls(
                [
                    ("venture_update", {"venture_id": COMPANION, "stage": "researching"}),  # the owner parked it
                    ("venture_update", {"venture_id": COMPANION, "learned": "Still no niche.", "note": "Why?"}),
                    ("venture_update", {"venture_id": FIVERR, "stage": "proposed"}),  # taken up by the owner
                    ("venture_update", {"venture_id": RECRUITING, "stage": "parked", "note": "Needs a licence?"}),
                ]
            ),
            ToolCalls([("venture_update", {"venture_id": RECRUITING, "stage": "researching"})]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    assert venture(agent, FIVERR)["parked_by"] == "owner"  # planted parked: the owner put it on hold
    now = to_iso(agent.clock.now())
    with pytest.raises(sqlite3.IntegrityError, match="only the owner takes a venture out"), agent.db.transaction() as c:
        ventures.update(c, FIVERR, now, stage="researching")
    who = owner(agent)
    assert who.decide_venture(COMPANION, {"action": "park", "comment": "Not now."}, "Stefan").status == 200
    assert who.decide_venture(COMPANION, {"action": "note", "comment": "Really not now."}, "Stefan").status == 200
    assert who.decide_venture(FIVERR, {"action": "research"}, "Stefan").status == 200
    assert (venture(agent, COMPANION)["parked_by"], venture(agent, FIVERR)["parked_by"]) == ("owner", None)
    assert agent.run_cycle("schedule").status == "completed"
    updates = tool_results(agent, "venture_update")
    assert [u["status"] for u in updates] == ["error", "ok", "error", "ok", "ok"]
    assert "your owner parked venture #7: only they take it up again" in updates[0]["result"]
    assert "the researching stage first" not in updates[2]["result"]  # Fiverr is researching again: the owner's word
    assert venture(agent, COMPANION)["stage"] == "parked" and venture(agent, RECRUITING)["stage"] == "researching"
    assert agent.ventures()["items"][COMPANION - 1]["parked_by"] == "owner"


def test_a_venture_never_starts_live_on_the_agents_word(data_dir: Path) -> None:
    # 0.15.0: it could once Ember earned anywhere, skipping the owner's backing
    live = ("venture_create", {"title": "Etsy shop in English", "pitch": "The shop's English leg.", "stage": "live"})
    fake = FakeTransport(script=[plan(steps=["add a leg"]), ToolCalls([live]), Reply("Done."), JOURNAL])
    fake.script.extend([plan(steps=["add a leg"]), ToolCalls([live]), Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=ROOMY)
    sale = {"amount": "4.50", "source": "Etsy order 1", "idempotency_key": "a" * 32}
    assert agent.economy.record("revenue", sale, "Stefan").status == 201
    agent.run_cycle("schedule")
    assert [r["status"] for r in tool_results(agent, "venture_create")] == ["error", "error"]
    assert rows(agent, "SELECT stage FROM ventures WHERE title = 'Etsy shop in English'") == []


def test_a_business_case_needs_research_scores_and_a_source_or_euros() -> None:
    row = {**dict.fromkeys(ventures.CASE_FIELDS, "x"), **SCORES, "scores_by": "research", "researched": 2, "cases": 1}
    assert ventures.proposal_gaps(row, "researching") == ["a source link or an amount in euros in its business case"]
    for evidence in ("https://example.invalid/a", "25 EUR", "€25", "12,50 €", "EUR 9", "about 40 euros", "5 Euro"):
        assert ventures.proposal_gaps({**row, "economics": evidence}, "researching") == [], evidence
    for words in ("cheap", "25 USD", "Europe-wide", "a euro-zone shop"):
        assert ventures.proposal_gaps({**row, "economics": words}, "researching") != [], words
    guessed = {**row, "demand": "25 EUR", "scores_by": "brainstorm", "researched": 1, "cost": None}
    assert ventures.proposal_gaps(guessed, "idea") == [
        "the researching stage first",
        "2 research calls for it that found something (it has 1)",
        "scores for cost",
    ]
    assert "its scores from research (rescore it)" in ventures.proposal_gaps({**guessed, "cost": 2}, "researching")


def test_the_database_refuses_scores_and_cases_without_research(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    now = to_iso(agent.clock.now())
    with pytest.raises(sqlite3.IntegrityError, match="scores from research need research"), agent.db.transaction() as c:
        ventures.update(c, DROPSHIPPING, now, revenue=4, scores_by="research")
    with pytest.raises(sqlite3.IntegrityError, match="needs its numbers"), agent.db.transaction() as c:
        ventures.update(c, DROPSHIPPING, now, stage="proposed")  # 0.13.0
    case = econ.Case("etsy_digital", 4.9, 0, 0, (1, 5, 15), 0, 2, 30, 2)
    with agent.db.transaction() as c:
        ventures.add_case(c, DROPSHIPPING, None, case, econ.compute(case), now)
    with pytest.raises(sqlite3.IntegrityError, match="two research calls"), agent.db.transaction() as c:
        ventures.update(c, DROPSHIPPING, now, stage="proposed")
    with pytest.raises(sqlite3.IntegrityError, match="has no research yet"), agent.db.transaction() as c:
        ventures.create(c, agent.scope(), title="New", pitch="p", stage="proposed", now=now)
    with agent.db.transaction() as conn:
        ventures.update(conn, DROPSHIPPING, now, revenue=4, scores_by="brainstorm")  # a guess is fine
        for sources in (0, 3, 1):
            ventures.add_research(conn, DROPSHIPPING, 1, None, "q", None, sources, 10, now)
        ventures.update(conn, DROPSHIPPING, now, revenue=5, scores_by="research", stage="proposed")
    with pytest.raises(sqlite3.IntegrityError, match="never changed"), agent.db.transaction() as conn:
        conn.execute("UPDATE venture_research SET sources = 9")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"), agent.db.transaction() as conn:
        conn.execute("DELETE FROM venture_research")


def test_a_brainstorm_grows_the_tree_from_a_venture(data_dir: Path) -> None:
    ideas = [
        {
            "title": f"Idea {i}",
            "pitch": f"Pitch {i}.",
            "first_question": f"Question {i}?",
            "revenue": 4,
            "doability": 4,
            "difficulty": 2,
            "risk": 2,
            "speed": 4,
            "cost": 9 if i == 2 else 1,
        }
        for i in range(1, 6)
    ]
    ideas.append({**ideas[0], "title": "print on demand in the etsy shop"})  # already in the tree
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([("brainstorm", {"venture_id": DROPSHIPPING, "theme": "EU suppliers"})]),
            Reply(json.dumps({"ideas": ideas})),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    result = tool_results(agent, "brainstorm")[0]
    assert result["status"] == "ok", result
    assert result["result"].startswith("The brainstorm added 5 ideas to your tree as branches of #3 (cost $")
    assert "Already in the tree, so not added again: print on demand in the etsy shop." in result["result"]
    added = rows(agent, "SELECT id, parent_id, stage, scores_by, cost, next_question FROM ventures WHERE id > 8")
    assert [v["parent_id"] for v in added] == [DROPSHIPPING] * 5
    assert {v["stage"] for v in added} == {"idea"} and {v["scores_by"] for v in added} == {"brainstorm"}
    assert added[1]["cost"] is None  # a score out of range is left out
    assert added[0]["next_question"] == "Question 1?"
    call = next(r for r in fake.sent if request_kind(r) == "brainstorm")
    assert call["model"] == VENTURING.planner_model and call["system"][0]["text"] == prompts.BRAINSTORM_RULES
    asked = call["messages"][0]["content"][0]["text"]
    assert "- #3 Dropshipping store (idea)\n  - #4 Print on demand in the Etsy shop (idea)" in asked
    assert 'TASK\nGrow the tree from #3 "Dropshipping store" (idea)' in asked and 'Theme: "EU suppliers"' in asked
    workspace, _ = agent.roots()
    kept = workspace.read(ventures.IDEAS_FILE)
    assert "cycle #1 (branch of #3 Dropshipping store; theme: EU suppliers)" in kept
    assert "- #9 Idea 1: Pitch 1. First question: Question 1?" in kept
    assert "- (already in the tree) print on demand in the etsy shop" in kept
    assert validate_request(call) is None


# --- the owner's word ---


def test_the_owner_adds_ideas_and_decides_and_the_agent_hears_it(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake, cycles=0, settings=VENTURING)
    who = owner(agent)
    added = who.add_venture({"title": "Grant finder", "pitch": "Grants for clubs.", "parent_id": WEBSITE}, "Stefan")
    assert added.status == 201 and added.body == {"id": 9}
    assert who.add_venture({"title": "grant  FINDER", "pitch": "Again."}, None).status == 409
    assert who.add_venture({"title": "New", "pitch": "x", "parent_id": 99}, None).status == 404
    assert who.add_venture({"title": "Two\nlines", "pitch": "x"}, None).body["field"] == "title"
    assert who.add_venture({"title": "No pitch"}, None).body["field"] == "pitch"
    backed = who.decide_venture(
        DROPSHIPPING, {"action": "back", "comment": "Go, I make the accounts.", "confirm": True}, "Stefan"
    )
    assert backed.status == 200 and backed.body == {"id": DROPSHIPPING, "stage": "building"}
    assert who.decide_venture(DROPSHIPPING, {"action": "back"}, None).status == 409  # already building
    assert who.decide_venture(FIVERR, {"action": "kill"}, None).body["stage"] == "killed"
    assert (
        who.decide_venture(FIVERR, {"action": "research", "comment": "Look again"}, None).body["stage"] == "researching"
    )
    assert who.decide_venture(COMPANION, {"action": "note"}, None).body["field"] == "comment"
    assert who.decide_venture(COMPANION, {"action": "park", "expected_version": 5}, None).status == 409
    assert who.decide_venture(COMPANION, {"action": "dance"}, None).body["field"] == "action"
    assert who.decide_venture(99, {"action": "park"}, None).status == 404
    assert venture(agent, DROPSHIPPING)["owner_by"] == "Stefan" and venture(agent, FIVERR)["owner_version"] == 2

    agent.run_cycle("schedule")
    text = planner_texts(fake)[0]
    assert (
        'Your owner added a venture idea (a branch of #5), venture #9 "Grant finder": "Grants for clubs.". Research'
        " it; a venture cycle scores it." in text
    )
    assert (
        'Your owner backed venture #3 "Dropshipping store": it is building now. Its first test is milestone #1 on your'
        " roadmap: it goes live once Ember's code or your owner finds it met. Owner's comment: "
        '"Go, I make the accounts."' in text
    )
    assert 'Your owner wants venture #8 "Services on Fiverr" researched next (it is researching now)' in text
    assert (
        "#3 [building] Dropshipping store · not scored yet · first test: milestone #1; it goes live once that is met"
        " · your owner backed it (" in text
    )
    seen = rows(agent, "SELECT id, seen_cycle_id FROM ventures WHERE owner_action IS NOT NULL ORDER BY id")
    assert seen == [
        {"id": DROPSHIPPING, "seen_cycle_id": 1},
        {"id": FIVERR, "seen_cycle_id": 1},
        {"id": 9, "seen_cycle_id": 1},
    ]
    agent.run_cycle("schedule")
    assert "Your owner backed venture" not in planner_texts(fake)[1]  # news once


# --- the dashboard ---


def test_the_ventures_tab(ingress_client: TestClient) -> None:
    tree = ingress_client.get("api/ventures").json()
    assert tree["share"] == 25 and tree["mode"] == "dry_run" and tree["venture_cycles"] == 0
    assert [c["name"] for c in tree["criteria"]] == list(ventures.SCORE_FIELDS)
    assert [v["title"] for v in tree["items"]] == TITLES
    etsy = tree["items"][0]
    assert etsy["file"] == "ventures/1-etsy-digital-products.md" and etsy["file_bytes"] is None
    assert etsy["weight"] is None and etsy["missing"] == list(ventures.CASE_FIELDS) and etsy["projects"] == []
    stamp = ingress_client.get("api/dashboard").json()["ventures_stamp"]
    assert stamp == tree["stamp"]
    added = post(ingress_client, "api/ventures", {"title": "Grant finder", "pitch": "Grants for clubs."})
    assert added.status_code == 201
    decided = post(ingress_client, f"api/ventures/{added.json()['id']}/decide", {"action": "note", "comment": "Hi"})
    assert decided.status_code == 200
    data = ingress_client.get("api/dashboard").json()
    assert data["ventures_stamp"] != stamp and data["badges"]["ventures_proposed"] == 0
    item = ingress_client.get("api/ventures").json()["items"][-1]
    assert item["owner_action"] == "note" and item["owner_comment"] == "Hi" and item["created_by"] == "owner"
    assert post(ingress_client, "api/ventures/99/decide", {"action": "park"}).status_code == 404
    html = ingress_client.get("/").text
    assert 'id="tab-ventures"' in html and 'id="panel-ventures"' in html and 'id="vt-svg-ns"' in html


# --- the review and the rules ---


def test_the_review_reads_the_tree(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=25)
    agent, _ = run(data_dir, FakeTransport(seed=5, scenario="founder"), cycles=3, settings=settings)
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")
    review = rows(agent, "SELECT scorecard, ventures FROM reviews")[0]
    assert "\nVENTURES (" in review["scorecard"] and " of your spending)" in review["scorecard"]
    assert "Heaviest ideas: #" in review["scorecard"]
    assert review["ventures"].startswith("Research the heaviest idea next")
    with agent.db.connection() as conn:
        snap_review = context.review.planner_text(conn, conn.execute("SELECT * FROM reviews").fetchone())
    assert "\nVentures: Research the heaviest idea next" in snap_review


def test_the_rules_ask_for_a_path_never_a_no() -> None:
    assert "If you can't" not in prompts.OPERATING_RULES
    # 0.12.0: a no backed by data is a result, and research comes before building (the optimism and "research
    # sparingly" reversed a data-backed no live).
    rules = " ".join(prompts.OPERATING_RULES.split())
    venture_rules = " ".join(prompts.VENTURE_RULES.split())
    guide = " ".join(tools.guide_text("ventures").split())
    for text in (rules, venture_rules, guide):
        assert "a no backed by data, with the numbers and the closest test, is a result" in text.lower()
    assert "your hard rules are a no without a test, and then offer the closest variant" in rules
    assert "Research before you build: a research call costs about 5 cents" in rules
    for words in ("Never answer", "sparingly", "assumes it can be done", "Nothing is impossible", "real no"):
        assert words not in rules and words not in venture_rules and words not in guide, words
    assert "Don't limit ideas to your tools today" in prompts.VENTURE_RULES
    assert "focus_venture_id" in prompts.PLAN_SCHEMA["required"]
    assert "ventures" in prompts.REVIEW_SCHEMA["required"]
    assert len(tools.guide_text("ventures")) <= tools.MAX_RESULT_CHARS
    names = {d["name"] for d in tools.definitions(mail=True, etsy=True, venture=True)}
    assert {"venture_create", "venture_update", "brainstorm"} <= names
    assert "brainstorm" not in {d["name"] for d in tools.definitions(mail=True, etsy=True)}
    request = prompts.brainstorm_request(Settings(), "context")
    assert request_kind(request) == "brainstorm" and validate_request(request) is None


def test_a_project_with_listings_revenue_or_a_bet_keeps_its_venture(data_dir: Path) -> None:
    """0.22.0 (analysis 0.20.1, FIX NOW 11): moving the shop's project into a venture closed its "listings_live >= 1"
    as met, settled a forecast of 10% as a hit, and let an empty venture escape its park."""
    from tests.test_etsy import call, listed, shop_context  # noqa: PLC0415

    agent, _ = listed(data_dir)
    ctx = shop_context(agent)
    with agent.db.transaction() as conn:
        other = ventures.create(
            conn,
            agent.scope(),
            title="Wedding planners",
            pitch="Planners.",
            stage="building",
            now=to_iso(agent.clock.now()),
        )
    moved = call(ctx, "project_update", {"project_id": 1, "venture_id": other})
    assert not moved.ok and "project #1 has listings: what Ember's code counts" in moved.text
    made = call(
        ctx, "project_create", {"title": "Seating charts", "hypothesis": "h", "next_step": "n", "status": "idea"}
    )
    assert (
        made.ok and call(ctx, "project_update", {"project_id": made.project_id, "venture_id": other}).ok
    )  # nothing ties it
    assert rows(agent, f"SELECT venture_id FROM projects WHERE id = {made.project_id}") == [{"venture_id": other}]


def test_the_owner_s_park_or_kill_stops_its_projects(data_dir: Path) -> None:
    """0.22.0 (analysis 0.20.1, FIX NOW 12): the venture's projects stayed active after the owner parked or killed it,
    and the agent could open new ones under it."""
    from tests.test_etsy import call, listed, shop_context  # noqa: PLC0415

    agent, _ = listed(data_dir)
    ctx = shop_context(agent)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        parked, killed = (
            ventures.create(conn, agent.scope(), title=t, pitch="p.", stage="building", now=now) for t in ("A", "B")
        )
    project = {"hypothesis": "h", "next_step": "n", "status": "active"}
    first = call(ctx, "project_create", {"title": "Under A", "venture_id": parked, **project}).project_id
    with agent.db.transaction() as conn:
        second = store.create_project(
            conn, agent.scope(), cycle_id=ctx.cycle_id, title="Under B", now=now, **{**project, "venture_id": killed}
        )
    who = owner(agent)
    assert who.decide_venture(parked, {"action": "park", "comment": "Not now."}, "Stefan").status == 200
    assert who.decide_venture(killed, {"action": "kill", "comment": "No."}, "Stefan").status == 200
    found = rows(agent, f"SELECT id, status, next_step FROM projects WHERE id IN ({first}, {second}) ORDER BY id")
    assert found == [
        {"id": first, "status": "waiting", "next_step": "None until your owner takes the venture up again."},
        {"id": second, "status": "abandoned", "next_step": "None: closed with its venture."},
    ]
    again = call(ctx, "project_update", {"project_id": first, "status": "active"})
    assert not again.ok and f"your owner parked venture #{parked}: its projects wait until" in again.text
    new = call(ctx, "project_create", {"title": "Under A again", "venture_id": parked, **project})
    assert not new.ok and f"your owner parked venture #{parked}: no project goes into it" in new.text
    assert call(ctx, "project_update", {"project_id": first, "note": "Waiting for the owner."}).ok
    assert who.decide_venture(parked, {"action": "back", "confirm": True}, "Stefan").status == 200
    # 0.23.1: taken up again, it is what it was (stages.resume_projects)
    assert rows(agent, f"SELECT status, next_step FROM projects WHERE id = {first}") == [
        {"status": "active", "next_step": "n"}
    ]


def test_ventures_parked_or_killed_before_the_upgrade_stop_their_projects(data_dir: Path) -> None:
    """0.22.0: migration 0077 does for the ventures the owner parked or killed before what the owner's word does now."""
    from app import paths  # noqa: PLC0415

    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        ids = {
            t: ventures.create(conn, agent.scope(), title=t, pitch="p.", stage="researching", now=now) for t in "ABC"
        }
    who = owner(agent)
    assert who.decide_venture(ids["A"], {"action": "park"}, "Stefan").status == 200
    assert who.decide_venture(ids["B"], {"action": "kill"}, "Stefan").status == 200
    with agent.db.transaction() as conn:
        ventures.update(conn, ids["C"], now, stage="parked", parked_by="agent")
        for title, vid in ids.items():  # as projects stood under them before 0.22.0
            store.create_project(
                conn, agent.scope(), cycle_id=1, title=f"Under {title}", hypothesis="h", next_step="n",
                status="active", now=now, venture_id=vid,
            )  # fmt: skip
        sql = (paths.APP_DIR / "migrations" / "0077_owner_parks.sql").read_text(encoding="utf-8")
        conn.execute(sql[sql.index("UPDATE projects") :])
    found = {r["title"]: r for r in rows(agent, "SELECT title, status, notes FROM projects WHERE title LIKE 'Under %'")}
    assert {t: r["status"] for t, r in found.items()} == {
        "Under A": "waiting",
        "Under B": "abandoned",
        "Under C": "active",
    }
    assert found["Under B"]["notes"] == f"[owner] Your owner killed venture #{ids['B']}."
