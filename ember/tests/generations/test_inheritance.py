"""0.48.0 (vision/generations.md, "The inheritance"): what passes from one generation to the next besides the world and
the estate. An evaluation drafts an inheritance.json; Ember's code checks it (cases of hers that are evidence, no tool,
no id of hers, the limits); the owner previews it, strikes what shouldn't pass and gives it; the change writes it:
the principles as inherited hypotheses of the new mind's playbook, the facts of her work, her letter, the craft
files, a seed strategy and identity."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import inheritance, learning  # noqa: E402
from app.agent.generation import status_line  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.generation_helpers import decide_all, settled  # noqa: E402

CHECKLIST = "# KDP checklist\n\n- The cover's spine matches the page count.\n"


def case(agent: Any, cause: str = "worked", sure: str = "high", lesson: str = "") -> int:
    """A case of the mind that hands on (as a daily review keeps it)."""
    scope = agent.scope()
    with agent.db.transaction() as conn:
        return int(
            conn.execute(
                "INSERT INTO cases (mode, session, subject, expected, happened, why, cause, sure, lesson, created_at)"
                " VALUES (?, ?, 'bet #1', 'views', 'none', 'no reach', ?, ?, ?, ?)",
                (scope.mode, scope.session, cause, sure, lesson, to_iso(agent.clock.now())),
            ).lastrowid
        )


def drafted(agent: Any, **more: Any) -> dict[str, Any]:
    """An evaluation's inheritance.json: one principle on two cases of hers, a craft file and a note, a direction."""
    first, second = case(agent), case(agent, "no_reach", "medium")
    agent.roots()[0].write("kdp/checklist.md", CHECKLIST)
    body = {
        "format": 1,
        "evaluation": "2026-11-01 inheritance",
        "generation": 1,
        "principles": [
            {
                "text": "Bring outside reach to every launch: a listing nobody is sent to gets no views.",
                "for": [first, second],
                "why": "8 cases agree, none against",
            }
        ],
        "strategy": "Sell where buyers already search; finish one product before the next.",
        "craft": [
            {"path": "kdp/checklist.md", "why": "KDP's notices ask for it"},
            {"name": "quality-bar.md", "text": "Your owner rejects a book whose pages they can't check.", "why": ""},
        ],
    }
    return body | more


def test_the_inheritance_passes_at_the_change(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    body = drafted(agent)
    shown = inheritance.preview(agent, body)
    assert [p["text"] for p in shown["principles"]] == [body["principles"][0]["text"]]
    assert [c["cause"] for c in shown["principles"][0]["for"]] == ["worked", "no_reach"]
    assert "A seed from generation 1" in shown["strategy"] and "Sell where buyers already search" in shown["strategy"]
    assert "generation 2 of this agent" in shown["identity"] and "Money: spent" in shown["facts"]
    assert rows(agent, "SELECT id FROM inheritances") == []  # a preview writes nothing
    given = inheritance.give(agent, body, None, "Owner")
    assert (given["principles"], given["craft"]) == (1, 2)
    status, _ = agent.ask_letter("Owner")
    assert status == 202 and agent.decide().trigger == "letter"
    assert agent.run_cycle("letter").status == "completed"
    [letter] = rows(agent, "SELECT * FROM letters")
    assert letter["text"].startswith("To the next generation") and not agent.letter_asked()
    decide_all(agent, "passive")
    done = agent.begin_generation("Owner")
    assert len(done["inherited"]) == 1 and "inheritance/letter.md" in done["written"]
    scope = agent.scope()
    with agent.db.connection() as conn:
        [principle] = learning.principles(conn, scope)
        [passed] = conn.execute("SELECT * FROM inheritances").fetchall()
        line = status_line(conn, scope, agent.clock.today(), to_iso(agent.clock.now()))
    origin = learning.inherited(principle)
    assert principle["confidence"] == "hypothesis" and principle["supports"] == "[]"
    assert origin is not None and origin["generation"] == 1 and len(origin["for"]) == 2
    assert learning.principle_line(principle).startswith("[hypothesis, inherited: generation 1, 2 cases for, 0 against")
    assert passed["status"] == "passed" and passed["to_session"] == scope.session
    assert json.loads(passed["written"])["principles"] == [principle["id"]]
    workspace = agent.roots()[0]
    assert workspace.read("inheritance/craft/checklist.md") == CHECKLIST
    assert "can't check" in workspace.read("inheritance/craft/quality-bar.md")
    assert "her view, not evidence" in workspace.read("inheritance/letter.md")
    assert "What generation 1's work did" in workspace.read("inheritance/facts.md")
    memory = agent.memory().read_all()
    assert "Sell where buyers already search" in memory["strategy"] and "generation 2" in memory["identity"]
    assert "1 principle of hers in your playbook as inherited hypotheses" in line
    assert "her letter" in line and "inheritance/ in your workspace" in line


def test_the_owner_strikes_what_shouldnt_pass(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    body = drafted(agent)
    inheritance.give(agent, body, {"principles": [1], "craft": [2], "letter": True}, "Owner")
    replaced = inheritance.give(agent, body, {"strategy": True}, "Owner")  # a newer one replaces it
    with agent.db.connection() as conn:
        given = inheritance.summary(inheritance.given(conn, agent.scope()))
        statuses = [r["status"] for r in conn.execute("SELECT status FROM inheritances ORDER BY id")]
    assert statuses == ["replaced", "given"] and given is not None and given["id"] == replaced["id"]
    assert (len(given["principles"]), given["craft"], given["strategy"], given["letter"]) == (
        1,
        ["checklist.md", "quality-bar.md"],
        False,
        True,
    )
    with pytest.raises(inheritance.InheritanceError, match="numbers from 1 to 1"):
        inheritance.give(agent, body, {"principles": [2]}, "Owner")


def test_a_change_without_an_inheritance_still_seeds_the_facts(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    decide_all(agent, "passive")
    done = agent.begin_generation("Owner")
    assert done["inherited"] == [] and done["written"] == ["inheritance/facts.md"]
    memory = agent.memory().read_all()
    assert "Situation (facts Ember's code computed" in memory["strategy"] and "generation 2" in memory["identity"]
    assert "her letter" not in memory["identity"]


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"generation": 2}, "generation is 1"),
        ({"principles": [{"text": "Use propose_etsy_edit less.", "for": ["FIRST"]}]}, "names the tool"),
        ({"principles": [{"text": "Line #12 sold.", "for": ["FIRST"]}]}, "names an id of hers"),
        ({"principles": [{"text": "Reach sells.", "for": [999999]}]}, "no case #999999 of hers"),
        ({"principles": [{"text": "Reach sells.", "for": ["EARLY"]}]}, "too early, which is no evidence"),
        ({"principles": [{"text": "Reach sells.", "for": ["LOW"]}]}, "low-sure"),
        ({"principles": [{"text": "Reach sells.", "for": []}]}, "cites the cases of hers for it"),
        ({"principles": [{"text": "Reach sells.", "for": ["FIRST"], "against": ["FIRST"]}]}, "not both"),
        ({"principles": [{"text": "Reach sells.", "for": ["FIRST"]}] * 2}, "each principle once"),
        ({"principles": [{"text": "Reach sells.", "for": ["FIRST"]}] * 13}, "at most 12"),
        ({"craft": [{"path": "kdp/missing.md"}]}, "no kdp/missing.md in her workspace"),
        ({"craft": [{"name": "letter.md", "text": "x"}]}, "named twice, or taken"),
        ({"craft": [{"name": "Notes.MD", "text": "x"}]}, "a file name like quality-bar.md"),
        ({"strategy": "Keep line #3 going."}, "names an id of hers"),
        ({"score": 3}, "takes only"),
    ],
)
def test_ember_s_code_checks_the_draft(data_dir: Path, change: dict[str, Any], says: str) -> None:
    agent, _ = settled(data_dir)
    marks = {"FIRST": case(agent), "EARLY": case(agent, "too_early"), "LOW": case(agent, sure="low")}
    for item in change.get("principles", []):
        for key in ("for", "against"):
            item[key] = [marks.get(i, i) for i in item.get(key, [])]
    body = {"format": 1, "evaluation": "x", "generation": 1, "strategy": "Sell where buyers search."} | change
    with pytest.raises(inheritance.InheritanceError, match=says):
        inheritance.preview(agent, body)


def test_an_inherited_principle_is_a_prior_her_own_cases_test(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    origin = json.dumps({"generation": 1, "principle": None, "for": [1, 2], "against": [], "inheritance": 1})
    text = "Bring outside reach to every launch"
    with agent.db.transaction() as conn:
        pid = conn.execute(
            "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at, confirmed_at,"
            " inherited) VALUES (?, ?, ?, '[]', '[]', 'hypothesis', ?, ?, ?)",
            (scope.mode, scope.session, text, now, now, origin),
        ).lastrowid
    first, second = case(agent, lesson=text), case(agent, lesson=text)
    with agent.db.transaction() as conn:
        said = learning.adopt(conn, scope, now)
        [p] = learning.principles(conn, scope)
    assert said == [
        f"case #{first} supports principle #{pid}, which is hypothesis",
        f"case #{second} supports principle #{pid}, which is established",
    ]
    assert p["confidence"] == "established"  # two of hers and her predecessor's evidence as one
    assert learning.confidence([], [], inherited=True) == "hypothesis"
    assert learning.confidence([1], [2], inherited=True) == "disputed"


def test_an_inherited_principle_no_case_of_hers_tests_fades_after_60_days(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    scope = agent.scope()
    now = agent.clock.now()
    origin = json.dumps({"generation": 1, "principle": None, "for": [1], "against": [], "inheritance": 1})
    with agent.db.transaction() as conn:
        for text, supports in (("Untested", "[]"), ("Tested once", "[7]")):
            conn.execute(
                "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at,"
                " confirmed_at, inherited) VALUES (?, ?, ?, ?, '[]', 'hypothesis', ?, ?, ?)",
                (scope.mode, scope.session, text, supports, to_iso(now), to_iso(now), origin),
            )
        assert learning.fade(conn, scope, to_iso(now + timedelta(days=59))) != []  # the tested one: 42 days
        assert [p["text"] for p in learning.principles(conn, scope)] == ["Untested"]
        [said] = learning.fade(conn, scope, to_iso(now + timedelta(days=60)))
    assert "no case of yours confirmed it for 60 days" in said


def test_her_letter_is_one_call_the_owner_asks_for(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    wake = agent.db.get_meta("agent.dry_run.next_wake_at")
    assert agent.ask_letter("Owner")[0] == 202
    assert agent.ask_letter("Owner") == (409, {"error": "asked already: she writes it at her next wake"})
    end = agent.run_cycle("letter")
    assert end.status == "completed" and agent.db.get_meta("agent.dry_run.next_wake_at") == wake  # her wakes as before
    [cycle] = rows(agent, "SELECT trigger FROM cycles ORDER BY id DESC LIMIT 1")
    [call] = rows(agent, "SELECT purpose FROM llm_calls ORDER BY id DESC LIMIT 1")
    assert (cycle["trigger"], call["purpose"]) == ("letter", "letter")
    assert agent.decide().trigger != "letter"
    [journal] = rows(agent, "SELECT summary FROM journal ORDER BY id DESC LIMIT 1")
    assert journal["summary"] == "Wrote my letter to the next generation"
