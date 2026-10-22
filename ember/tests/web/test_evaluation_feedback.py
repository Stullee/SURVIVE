"""0.44.0 (vision/evaluation.md): the feedback of an evaluation, given back to Ember.

1. A feedback.json's cases join her cases (their subjects begin with "evaluation <name>"), its corrections reach her
   in one message of the owner's, and what was given is kept: final, and never given twice.
2. Only its keys are taken: no score, grade or anything else rides along; each field has its limit.
3. The Diagnostics tab previews it (nothing written) and gives it, with CSRF, and lists what was given.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import feedback
from app.agent import learning
from tests.agent_helpers import make_agent, owner, rows
from tests.agent_helpers import plan as scripted_plan

CSRF = {"X-Ember-Request": "1"}
GOOD: dict[str, Any] = {
    "format": 1,
    "evaluation": "2026-10-26",
    "week": ["2026-10-19", "2026-10-25"],
    "cases": [
        {
            "subject": "triage of six ideas in six cycles",
            "expected": "each idea decided as its step came up",
            "happened": "six cycles parked six ideas for the same reason",
            "why": "one decision a cycle where one rule decided all six",
            "cause": "weak_execution",
            "sure": "high",
            "lesson": "when one rule decides several waiting ideas, decide them all in one cycle",
        },
        {"subject": "the Kassenbuch's checks", "why": "the page check caught each defect in turn", "cause": "worked",
         "sure": "medium"},
    ],
    "corrections": [
        {
            "right": "KDP's week of new titles starts every Sunday at 00:00 UTC",
            "wrong": "the week ends 7 days after a proposal",
            "source": "KDP's help",
        },
        {"right": "The English Haushaltsbuch is published as request #141"},
    ],
}  # fmt: skip


def started(data_dir: Path) -> Any:
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    return agent


# --- 1. given: cases, a message, kept once ---


def test_a_feedback_s_cases_join_hers_and_its_corrections_reach_her(data_dir: Path) -> None:
    agent = started(data_dir)
    side = owner(agent)
    preview = feedback.preview(side, GOOD)
    assert preview["given"] is False and rows(agent, "SELECT COUNT(*) AS n FROM cases") == [{"n": 0}]  # nothing written
    assert [c["subject"] for c in preview["cases"]] == [
        "evaluation 2026-10-26: triage of six ideas in six cycles",
        "evaluation 2026-10-26: the Kassenbuch's checks",
    ]
    given = feedback.give(side, GOOD, "Felix")
    assert len(given["cases"]) == 2
    with agent.db.connection() as conn:
        cases = learning.cases(conn, agent.scope())  # what her playbook and weekly look read
    assert {c["id"] for c in cases} == set(given["cases"])
    assert all(c["review_id"] is None and c["subject"].startswith("evaluation 2026-10-26: ") for c in cases)
    assert "(evaluation 2026-10-26: triage of six ideas in six cycles, weak execution, high)" in learning.case_line(
        next(c for c in cases if c["cause"] == "weak_execution")
    )
    [message] = rows(agent, "SELECT id, sender, text, entered_by FROM messages")
    assert message["id"] == given["message_id"] and message["sender"] == "owner" and message["entered_by"] == "Felix"
    text = message["text"]
    assert text.startswith('From the evaluation of your week 2026-10-19 to 2026-10-25 ("2026-10-26"):')
    assert "- KDP's week of new titles starts every Sunday at 00:00 UTC. You had: the week ends 7 days after a" in text
    assert "- The English Haushaltsbuch is published as request #141." in text
    assert f"2 cases of your own work joined your cases (#{given['cases'][0]} to #{given['cases'][1]}" in text
    [kept] = rows(agent, "SELECT * FROM evaluation_feedback")
    assert kept["evaluation"] == "2026-10-26" and json.loads(kept["cases"]) == given["cases"]
    assert json.loads(kept["payload"])["corrections"][1] == {
        "right": "The English Haushaltsbuch is published as request #141", "wrong": "", "source": "",
    }  # fmt: skip
    assert feedback.preview(side, GOOD)["given"] is True
    with pytest.raises(feedback.FeedbackError, match="given to Ember already") as twice:
        feedback.give(side, GOOD, "Felix")
    assert twice.value.status == 409
    with agent.db.connection() as conn:
        assert [g["cases"] for g in feedback.given(conn, agent.scope())] == [2]
        for sql in ("UPDATE evaluation_feedback SET evaluation = 'x'", "DELETE FROM evaluation_feedback"):
            with pytest.raises(sqlite3.IntegrityError, match="evaluation_feedback"):
                conn.execute(sql)


def test_corrections_alone_and_cases_alone(data_dir: Path) -> None:
    agent = started(data_dir)
    side = owner(agent)
    only = {"evaluation": "only facts", "corrections": GOOD["corrections"][:1]}
    assert feedback.give(side, only, None)["cases"] == []
    [text] = [r["text"] for r in rows(agent, "SELECT text FROM messages")]
    assert text.startswith('From the evaluation ("only facts"):') and "joined your cases" not in text
    cases = {"evaluation": "only cases", "cases": GOOD["cases"][1:]}
    assert len(feedback.give(side, cases, None)["cases"]) == 1


# --- 2. only its keys, within their limits ---


def wrong(change: Any) -> dict[str, Any]:
    body = copy.deepcopy(GOOD)
    change(body)
    return body


@pytest.mark.parametrize(
    ("body", "field", "says"),
    [
        (wrong(lambda b: b.update(scores={"capability": 52})), "feedback", "not scores"),
        (wrong(lambda b: b["cases"][0].update(score=40)), "case 1", "not score"),
        (wrong(lambda b: b["corrections"][0].update(grade="R1")), "correction 1", "not grade"),
        (wrong(lambda b: b.update(format=2)), "format", "format 1"),
        (wrong(lambda b: b.update(evaluation="")), "evaluation", "1 to 40"),
        (wrong(lambda b: b.update(evaluation="a/../b")), "evaluation", "1 to 40"),
        (wrong(lambda b: b.update(week=["2026-10-25", "2026-10-19"])), "week", "comes before"),
        (wrong(lambda b: b.update(week="last week")), "week", "YYYY-MM-DD"),
        (wrong(lambda b: b["cases"][0].update(cause="lazy")), "case 1", "cause is one of"),
        (wrong(lambda b: b["cases"][0].update(sure="certain")), "case 1", "sure is one of"),
        (wrong(lambda b: b["cases"][0].update(why="")), "case 1", "needs its why"),
        (wrong(lambda b: b["cases"][0].update(subject="x" * 57)), "case 1", "at most 56"),
        (wrong(lambda b: b["cases"][0].update(lesson="x" * 301)), "case 1", "at most 300"),
        (wrong(lambda b: b["cases"][0].update(project_id=999)), "case 1", "none of hers"),
        (wrong(lambda b: b["cases"][0].update(venture_id=True)), "case 1", "none of hers"),
        (wrong(lambda b: b.update(cases=GOOD["cases"] * 5)), "cases", "at most 8"),
        (wrong(lambda b: b.update(corrections=[{"right": "x"}] * 11)), "corrections", "at most 10"),
        (wrong(lambda b: b["corrections"][0].update(right="")), "correction 1", "needs its right"),
        (wrong(lambda b: b.update(cases=[], corrections=[])), "feedback", "at least one"),
        (
            wrong(lambda b: b.update(corrections=[{"right": "r" * 200, "wrong": "w" * 200}] * 10)),
            "corrections",
            "2,000",
        ),
        ([GOOD], "feedback", "JSON object"),
    ],
)
def test_only_its_keys_within_their_limits(body: Any, field: str, says: str, data_dir: Path) -> None:
    agent = started(data_dir)
    with pytest.raises(feedback.FeedbackError, match=says) as refused:
        feedback.give(owner(agent), body, None)
    assert refused.value.field == field and refused.value.status == 422
    assert rows(agent, "SELECT COUNT(*) AS n FROM cases") == [{"n": 0}]
    assert rows(agent, "SELECT COUNT(*) AS n FROM messages") == [{"n": 0}]


# --- 3. the Diagnostics tab ---


def test_the_tab_previews_and_gives_a_feedback(ingress_client: TestClient) -> None:
    assert ingress_client.post("api/evaluation-feedback/preview", json=GOOD).status_code == 403  # CSRF
    assert ingress_client.post("api/evaluation-feedback", json=GOOD).status_code == 403
    preview = ingress_client.post("api/evaluation-feedback/preview", json=GOOD, headers=CSRF)
    assert preview.status_code == 200 and "#… to #…" in preview.json()["message"]
    assert ingress_client.get("api/evaluation-export").json()["feedback"] == []
    refused = ingress_client.post("api/evaluation-feedback", json={**GOOD, "scores": {}}, headers=CSRF)
    assert refused.status_code == 422 and refused.json()["field"] == "feedback"
    given = ingress_client.post("api/evaluation-feedback", json=GOOD, headers=CSRF)
    assert given.status_code == 201 and len(given.json()["cases"]) == 2 and "wake" in given.json()
    assert ingress_client.post("api/evaluation-feedback", json=GOOD, headers=CSRF).status_code == 409
    [listed] = ingress_client.get("api/evaluation-export").json()["feedback"]
    assert listed["evaluation"] == "2026-10-26" and listed["cases"] == 2 and listed["corrections"] == 2
    inbox = ingress_client.get("api/dashboard").json()["inbox"]
    assert any(m["id"] == given.json()["message_id"] for m in inbox)
