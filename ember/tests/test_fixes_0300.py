"""0.30.0: a plan she keeps, and a playbook she grows.

READY put the line worked on longest ago first after what was owed and due, so every ordinary cycle took another line
(a dry run of 13 cycles on four lines switched lines 11 times), each handoff was written for a line the next cycle
didn't take, and the daily review's verdicts and the weekly look's choices were text no ranking read. Now the line in
progress comes first while it has work (3 cycles in a row at most), then the week's focus lines (the weekly look picks
them toward the goal, which its view now shows) and the review's changes; a bar of a listing test is no milestone due.
The playbook was written only by the weekly look, once a week, which came right after the first review of 0.18.0,
when hardly any case existed, and which was skipped at every cycle once its view outgrew its budget: now each
retrospective's lesson joins it as a hypothesis the day its case is kept, the look's view fits, the look curates, and
the owner sees the playbook under Mind → Playbook. 0.35.0: READY retired; the plan tree takes each cycle's step and
keeps a product going up to 3 cycles in a row (tests/test_fixes_0350.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import (  # noqa: E402
    context,
    learning,
    lines,
    loop,
    prompts,
    review,
    roadmap,
    store,
    views,
    weekly,
    weights,
)
from app.agent.service import Agent  # noqa: E402
from app.config import Settings  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import (  # noqa: E402
    call,
    shop_context,
    started,  # noqa: E402
)
from tests.test_fixes_0280 import lined, now  # noqa: E402


def ran(
    agent: Agent,
    project: int | None = None,
    *,
    status: str = "completed",
    trigger: str = "schedule",
    venture: int = 0,
    marketing: int = 0,
) -> int:
    """A cycle of the agent's that ended (``project``: the line it worked on), an hour after the last one."""
    agent.clock.advance(hours=1)
    scope, stamp = agent.scope(), now(agent)
    with agent.db.transaction() as conn:
        return int(
            conn.execute(
                "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
                " session, project_id, venture, marketing) VALUES (?, 'b', ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)",
                (scope.life_id, stamp, stamp, status, trigger, scope.session, project, venture, marketing),
            ).lastrowid
        )


def judged(agent: Agent, *verdicts: tuple[int, str, str]) -> None:
    """Today's review, with its verdicts (line, verdict, bottleneck), as Ember's code keeps it."""
    card = review.Scorecard("YOUR NUMBERS", {pid for pid, _, _ in verdicts})
    answer = review.Review(
        verdicts=[review.Verdict(pid, verdict, "the numbers say so", neck) for pid, verdict, neck in verdicts],
        working="",
        not_working="",
        owner_feedback="",
        lesson="",
        focus="",
    )
    with agent.db.transaction() as conn:
        review.save(conn, agent.scope(), ran(agent), now(agent), agent.clock.today(), card, answer)


def looked(agent: Agent, focus: list[Any], principles: list[dict[str, Any]] | None = None) -> list[str]:
    """This week's look, carried out and kept as Ember's code does (weekly.apply, weekly.save); what happened."""
    answer = weekly.parse(
        json.dumps(
            {"assessment": "x", "strategy": "Bring buyers to what is live.", "focus": focus, "principles": principles}
        )
    )
    assert answer is not None
    with agent.db.transaction() as conn:
        happened = weekly.apply(conn, agent.scope(), agent.memory(), answer, now(agent))
        weekly.save(conn, agent.scope(), ran(agent), now(agent), agent.clock.today(), "view", answer, happened, None)
    return happened


def journal(agent: Agent, cycle_id: int, handoff: str) -> None:
    with agent.db.transaction() as conn:
        store.write_journal(conn, agent.scope(), cycle_id, "agent", "Worked on it", "What I did.", now(agent), handoff)


# --- the week's focus, and handoffs that follow their line ---


def test_the_weekly_look_keeps_the_week_s_focus_lines(data_dir: Path) -> None:
    agent, _ = lined(data_dir, ("Planner", "Poster", "Checklist", "Bundle"))
    happened = looked(agent, [3, 99, 3, "2"])
    assert happened[1:3] == ["this week's focus: #3", "not a focus (no open line of yours): #99"]
    [row] = rows(agent, "SELECT answer FROM weekly_reviews")
    assert json.loads(row["answer"])["focus"] == [3]  # kept as Ember's code kept it
    with agent.db.connection() as conn:
        text = weekly.planner_text(weekly.latest(conn, agent.scope(), agent.clock.today()))
    assert "This week's focus lines: #3" in text


def test_each_line_keeps_the_next_step_its_last_cycle_left(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    worked = ran(agent, 2)
    journal(agent, worked, "Make the poster's cover photo and check it at 1000 px")
    elsewhere = ran(agent, None, venture=1)
    journal(agent, elsewhere, "Research the next venture's market")
    with agent.db.connection() as conn:
        said = lines.handoffs(conn, agent.scope())
        assert said[2] == (worked, "Make the poster's cover photo and check it at 1000 px")
        focus = lines.focus_text(conn, agent.scope(), 2, marketing=False)
        assert "left as next" not in lines.focus_text(conn, agent.scope(), 2, marketing=True)
    left = f'Your last cycle on it (#{worked}) left as next: "Make the poster\'s cover photo and check it at 1000 px"'
    assert left in focus
    planner = agent.planner_preview()  # the plan's YOUR LAST CYCLE says where its handoff was written
    assert 'Your handoff to this cycle (written in a venture cycle): "Research the next venture' in planner


def test_a_live_line_without_an_open_bet_is_asked_for_one(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.connection() as conn:
        assert "No open bet on it: say what you expect" in lines.focus_text(conn, agent.scope(), project, False)
        assert "No open bet" not in lines.focus_text(conn, agent.scope(), project, True)  # marketing asks itself
    placed = call(shop_context(agent), "project_update", {"project_id": project, "bet": "+5 views in 7 days: a pin"})
    assert placed.ok, placed.text
    with agent.db.connection() as conn:
        assert "No open bet" not in lines.focus_text(conn, agent.scope(), project, False)


# --- the weekly look plans toward the goal ---


def test_the_weekly_look_sees_the_goal_and_its_answer_needs_a_focus(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    with agent.db.connection() as conn:
        goal = roadmap.root(conn, agent.scope())
        text = weekly.goal_text(conn, agent.scope(), agent.clock.today(), None)
    assert goal is not None and text.startswith("THE GOAL (choose the week's focus toward it)\nThe goal (")
    assert "focus" in prompts.WEEKLY_SCHEMA["required"] and "Choose the week's focus" in prompts.WEEKLY_RULES
    assert weekly.parse(json.dumps({"assessment": "x", "focus": [4, 4, True, 7, 8, 9]}))["focus"] == [4, 7, 8]


def test_the_weekly_look_runs_with_the_goal_and_sets_the_week_s_focus(data_dir: Path) -> None:
    agent, project = started(data_dir)
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")  # the daily review, then the weekly look (the fake chooses the first open line)
    [row] = rows(agent, "SELECT * FROM weekly_reviews")
    assert "\nTHE GOAL (choose the week's focus toward it)\n" in row["view"]
    assert f"this week's focus: #{project}" in row["outcome"]
    with agent.db.transaction() as conn:
        assert weekly.focus(conn, agent.scope(), agent.clock.today()) == [project]
        store.update_project(conn, project, now(agent), status="abandoned")
        assert weekly.focus(conn, agent.scope(), agent.clock.today()) == []  # a closed line is no focus


def test_a_full_view_fits_the_weekly_look_s_budget() -> None:
    """The view was cut at 14,000 characters, but its request had to fit 12,000 tokens: any view over about 12,300
    characters didn't, and the look was skipped at every cycle with a line in the log only."""
    settings = Settings()
    full = ("Line of the view, as Ember's code writes it: numbers, titles and cases. " * 2 + "\n") * (
        weekly.VIEW_CHARS // 150
    )
    assert len(weekly.cut(full)) <= weekly.VIEW_CHARS
    assert context.fits(prompts.weekly_request(settings, weekly.cut(full)), loop.WEEKLY_INPUT_TOKENS)
    umlauts = ("ä" * 99 + "\n") * (weekly.VIEW_CHARS // 100)  # the worst case: two bytes a character
    fitting = [
        chars
        for chars in weekly.VIEW_STEPS
        if context.fits(prompts.weekly_request(settings, weekly.cut(umlauts, chars)), loop.WEEKLY_INPUT_TOKENS)
    ]
    assert fitting and fitting[0] < weekly.VIEW_CHARS  # cut further until it fits, never skipped


def test_a_big_business_still_gets_its_weekly_look_with_the_frame_first(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    with agent.db.transaction() as conn:
        for n in range(200):  # a view of about 20,000 characters
            store.create_project(
                conn,
                agent.scope(),
                cycle_id=1,
                title=f"Printable planner {n} for busy families, teachers and small shops",
                hypothesis="Someone pays 5 EUR for it " * 8,
                next_step="make it",
                status="active",
                now=now(agent),
            )
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")  # the daily review, then the weekly look
    [row] = rows(agent, "SELECT * FROM weekly_reviews")
    assert row["status"] == "ok", row["note"]
    view = row["view"]
    assert view.endswith("[view cut]") and len(view) <= weekly.VIEW_CHARS
    # 0.36.0: the owner's rulebook in place of their standing instructions
    assert view.index("YOUR OWNER'S RULEBOOK") < view.index("YOUR STRATEGY NOW") < view.index("PROJECTS")


# --- the playbook grows every day ---


def cases(agent: Agent, *retros: dict[str, str]) -> list[int]:
    """Cases as the daily review keeps them (learning.save_cases)."""
    base = {"subject": "bet #1", "expected": "+15 views", "happened": "+1", "why": "Nobody saw it.", "lesson": ""}
    with agent.db.transaction() as conn:
        return learning.save_cases(
            conn,
            agent.scope(),
            None,
            [{**base, "cause": "no_reach", "sure": "high", **retro} for retro in retros],
            [],
            now(agent),
        )


def adopt(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return learning.adopt(conn, agent.scope(), now(agent))


def test_a_retrospective_s_lesson_joins_the_playbook_the_day_it_is_kept(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    lesson = "Etsy listings without outside traffic stay unseen for two weeks."
    first, early, thin, bare = cases(
        agent,
        {"lesson": lesson},
        {"lesson": "Wait a week before judging.", "cause": "too_early"},
        {"lesson": "Maybe price matters.", "sure": "low"},
        {"lesson": ""},
    )
    assert adopt(agent) == [f"new principle #1 (hypothesis) from case #{first}: {lesson}"]
    assert adopt(agent) == []  # each case is weighed once
    [p] = rows(agent, "SELECT * FROM principles")
    assert (p["text"], json.loads(p["supports"]), p["confidence"]) == (lesson, [first], "hypothesis")
    # the same lesson from two more cases: one principle, established by three cases that agree
    again = cases(
        agent,
        {"lesson": "Etsy listings without outside traffic stay unseen for two weeks!"},
        {"lesson": "etsy listings without outside traffic stay unseen for two weeks"},
    )
    said = adopt(agent)
    assert said == [
        f"case #{again[0]} supports principle #1, which is hypothesis",
        f"case #{again[1]} supports principle #1, which is established",
    ]
    # its opposite (it denies what the principle says) is a hypothesis of its own, never a case for it
    [opposite] = cases(agent, {"lesson": "Etsy listings with outside traffic stay unseen for two weeks."})
    assert adopt(agent) == [
        f"new principle #2 (hypothesis) from case #{opposite}: Etsy listings with outside traffic stay unseen for two"
        " weeks."
    ]
    with agent.db.connection() as conn:
        found = {p["id"]: p["confidence"] for p in learning.principles(conn, agent.scope())}
    assert found == {1: "established", 2: "hypothesis"} and {early, thin, bare}.isdisjoint(
        {i for r in rows(agent, "SELECT supports FROM principles") for i in json.loads(r["supports"])}
    )


def test_a_case_the_weekly_look_cited_is_not_adopted_again_and_a_full_playbook_waits(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    [cited] = cases(agent, {"lesson": "Pins bring views within a week."})
    looked(agent, [], [{"id": None, "text": "Pins bring views.", "supports": [cited], "against": [], "retire": ""}])
    assert adopt(agent) == []  # the look weighed it
    with agent.db.transaction() as conn:
        for n in range(learning.MAX_PRINCIPLES - 1):
            conn.execute(
                "INSERT INTO principles (mode, session, text, confidence, created_at, confirmed_at)"
                " VALUES (?, ?, ?, 'hypothesis', ?, ?)",
                (agent.scope().mode, agent.scope().session, f"Rule number {n} of many.", now(agent), now(agent)),
            )
    [waiting] = cases(agent, {"lesson": "A fresh lesson about bundles and their prices."})
    assert adopt(agent) == [
        f"case #{waiting}'s lesson waits: the playbook holds {learning.MAX_PRINCIPLES} principles (the weekly look"
        " retires or merges some)"
    ]


def test_the_first_cycle_adopts_the_cases_kept_before_it(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    [kept] = cases(agent, {"lesson": "A listing photo that shows the product in use gets more favorites."})
    agent.run_cycle("schedule")
    [p] = rows(agent, "SELECT * FROM principles")
    assert json.loads(p["supports"]) == [kept] and p["confidence"] == "hypothesis"
    said = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE message LIKE 'new principle #%'")]
    assert said == [
        f"new principle #1 (hypothesis) from case #{kept}: A listing photo that shows the product in use gets more"
        " favorites."
    ]


def test_a_bug_in_the_playbook_s_keeper_never_ends_a_cycle(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = started(data_dir)

    def broken(*_: Any) -> list[str]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(learning, "adopt", broken)
    end = agent.run_cycle("schedule")
    assert end.status in ("completed", "idle"), end
    said = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE level = 'warning'")]
    assert "The playbook's keeper failed: RuntimeError" in said


def test_the_owner_sees_the_playbook_under_mind(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    empty = views.dashboard(agent)["mind"]["playbook"]
    assert (empty["principles"], empty["retired"], empty["cases"], empty["weekly"]) == ([], [], 0, None)
    first, early = cases(
        agent, {"lesson": "Unseen listings need pins first."}, {"lesson": "Too soon to tell.", "cause": "too_early"}
    )
    adopt(agent)
    looked(agent, [], [{"id": 1, "text": "", "supports": [], "against": [], "retire": "the pins brought nobody"}])
    playbook = views.dashboard(agent)["mind"]["playbook"]
    assert (playbook["cases"], playbook["cases_too_early"], playbook["principles"]) == (2, 1, [])
    [gone] = playbook["retired"]
    assert (gone["text"], gone["status"], gone["retired_why"]) == (
        "Unseen listings need pins first.",
        "retired",
        "the pins brought nobody",
    )
    assert gone["supports"] == [
        {"id": first, "subject": "bet #1", "cause": "no_reach", "why": "Nobody saw it.", "lesson": gone["text"]}
    ]
    assert playbook["weekly"]["focus"] == [] and playbook["weekly"]["outcome"].startswith("the strategy was rewritten")
    assert early not in {c["id"] for c in gone["supports"]}


def test_the_planner_s_rules_ask_to_finish_what_was_started() -> None:
    rules = " ".join(prompts.PLANNER_RULES.split())
    assert "Keep 2-3 lines going across your cycles" not in rules
    assert f"Finish what you start: it keeps you on that product up to {weights.STREAK_CAP} cycles in a row" in rules
    assert context.LINE_FOCUS_BUDGET >= 1_000  # room for the line's last handoff in FOCUS
