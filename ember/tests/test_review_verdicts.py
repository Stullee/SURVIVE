"""0.12.0: the daily review judges the roadmap, and Ember's code applies its verdicts. The review read the roadmap in
words only, and nothing followed from them. Now it gives each milestone that is overdue or due this week a verdict:
hit (its measure is met, with the evidence), miss (past its date and not met), extend (a new date) or park (it waits a
week). Ember's code applies each one with the rules of milestone_update, before the day's first plan, and keeps with
the review what came of it."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.agent import review, roadmap
from app.agent.fake_llm import Reply
from tests.test_agent import rows
from tests.test_review import next_day


def answer(milestones: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "verdicts": [],
            "working": "Drafts get finished.",
            "not_working": "Nothing sold yet.",
            "owner_feedback": "None yet.",
            "lesson": "Finish before starting.",
            "focus": "One listing today.",
            "ventures": "Research the heaviest idea.",
            "roadmap": "Two are overdue.",
            "milestones": milestones,
        }
    )


def test_the_review_reads_its_milestone_verdicts() -> None:
    text = answer(
        [
            {"milestone_id": 4, "verdict": "hit", "why": "Proposed as request #4.", "new_due": ""},
            {"milestone_id": 4, "verdict": "miss", "why": "Twice.", "new_due": ""},  # one verdict a milestone
            {"milestone_id": 5, "verdict": "maybe", "why": "?", "new_due": ""},
            {"milestone_id": "6", "verdict": "park", "why": "Not a number.", "new_due": ""},
            {"milestone_id": 7, "verdict": "extend", "why": "Photos take longer.", "new_due": "2026-09-20"},
        ]
    )
    parsed = review.parse(text, set())
    assert parsed is not None
    assert [(v.milestone_id, v.verdict, v.new_due) for v in parsed.milestones] == [
        (4, "hit", ""),
        (7, "extend", "2026-09-20"),
    ]


def test_code_applies_the_verdicts_with_the_milestone_rules(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    today = agent.clock.today()
    now = "2026-09-01T12:00:00Z"

    def milestone(title: str, due: int, **more: Any) -> int:
        with agent.db.transaction() as conn:
            return roadmap.create(
                conn,
                agent.scope(),
                title=title,
                measure="x",
                due=(today + timedelta(days=due)).isoformat(),
                now=now,
                **more,
            )

    listed = milestone("Listing proposed", -1)
    sale = milestone("First sale", -1)
    photos = milestone("Photos done", 2)
    pinterest = milestone("Pinterest test", 3)
    metric = milestone("Three live", 2, metric="listings_live", target=3)  # not past its date: code leaves it open
    new_due = (today + timedelta(days=9)).isoformat()
    fake.script.append(
        Reply(
            answer(
                [
                    {"milestone_id": listed, "verdict": "hit", "why": "Proposed as request #4.", "new_due": ""},
                    {"milestone_id": sale, "verdict": "miss", "why": "No buyer yet; smaller test next.", "new_due": ""},
                    {"milestone_id": photos, "verdict": "extend", "why": "Photos take longer.", "new_due": new_due},
                    {
                        "milestone_id": pinterest,
                        "verdict": "park",
                        "why": "Waits for my owner's account.",
                        "new_due": "",
                    },
                    {"milestone_id": metric, "verdict": "hit", "why": "3 listings live.", "new_due": ""},
                    {"milestone_id": 1, "verdict": "miss", "why": "Nothing earned.", "new_due": ""},
                    {"milestone_id": 99, "verdict": "hit", "why": "#99 done.", "new_due": ""},
                ]
            )
        )
    )
    agent.run_cycle("schedule")
    [saved] = rows(agent, "SELECT milestones FROM reviews WHERE status = 'ok'")
    outcomes = {v["milestone_id"]: v for v in json.loads(saved["milestones"])}
    assert [outcomes[i]["applied"] for i in (listed, sale, photos, pinterest, metric, 1, 99)] == [
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    ]
    assert "Ember's code closes milestone" in outcomes[metric]["outcome"]
    assert "Ember's code closes the money goal" in outcomes[1]["outcome"]
    assert outcomes[99]["outcome"] == "there is no milestone #99"
    state = {r["id"]: r for r in rows(agent, "SELECT * FROM milestones")}
    assert (state[listed]["status"], state[listed]["closed_by"]) == ("done", "agent")  # the agent's word, self-reported
    assert state[listed]["result"] == "Daily review: Proposed as request #4."
    assert state[sale]["status"] == "missed"
    assert (state[photos]["due"], state[photos]["moves"]) == (new_due, 1)
    assert state[pinterest]["wait_for"] == "the daily review's word: Waits for my owner's account."
    assert state[pinterest]["check_at"] == (today + timedelta(days=review.PARK_DAYS)).isoformat()
    assert state[metric]["status"] == "open"
    plan = next(r for r in fake.sent if "== TODAY'S REVIEW ==" in json.dumps(r))
    text = plan["messages"][0]["content"][0]["text"]
    assert f"- milestone #{listed}: hit: Proposed as request #4. (applied)" in text
    assert f"- milestone #{photos}: extend to {new_due}: Photos take longer. (applied)" in text
    assert "- milestone #99: hit: #99 done. (not applied: there is no milestone #99)" in text
    events = [e["message"] for e in agent.db.recent_events(limit=40)]
    assert f"The daily review: Milestone #{listed}: done." in events
