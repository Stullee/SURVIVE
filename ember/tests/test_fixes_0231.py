"""0.23.1: fixes from the review of 0.22.0 (its pre-release review's confirmed findings, live in 0.22.1).

1. The daily review's limit counted every call of purpose 'review': the free retry of a call that never reached the
   API, and the weekly look's call, used up the day's second attempt.
2. An established principle that shared cases with an older one (a compatible one too) fell back to a hypothesis
   when a new case confirmed it.
3. The review's scorecard left projects out unnamed when even a line each didn't fit.
4. A project of a venture the owner parked could be moved to another venture and then made active again.
5. The owner's park overwrote a project's next step for good, and taking the venture up again didn't give it back.
6. Migration 0076 took back the "auto" unlocks of email replies but left the replies they had approved and Ember's
   code hadn't sent queued: they went out without the owner seeing them.
7. The projects the owner's park stopped took the plan's projects in full, ahead of the agent's working ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import learning, loop, policy, review, stages, store, ventures  # noqa: E402
from app.agent.fake_llm import Fail, FakeTransport, Reply  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.metering import NotSent  # noqa: E402
from app.integrations import executor  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_etsy import call, listed, shop_context  # noqa: E402
from tests.test_fixes_0140_unlock_safety import answer, status_of  # noqa: E402
from tests.test_listing_gates import started  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402
from tests.test_never import a_use, approve_as_code  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402
from tests.test_review import kinds, next_day  # noqa: E402

# --- 1. the daily review's attempts ---


def test_a_free_retry_of_the_review_call_doesn_t_use_up_the_day_s_second_review(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop, "RETRY_DELAY_SECONDS", 0)
    agent, fake = next_day(data_dir)
    # the first call never reached the API (free, tried again at once); the retry's answer isn't JSON
    fake.script.extend([Fail(NotSent("no connection")), Reply("Things are going fine.")])
    agent.run_cycle("schedule")
    assert [r["status"] for r in rows(agent, "SELECT status FROM llm_calls WHERE purpose = 'review'")] == [
        "failed",
        "ok",
    ]
    assert [r["status"] for r in rows(agent, "SELECT status FROM reviews")] == ["failed"]
    with agent.db.connection() as conn:
        assert review.sent_today(conn, agent.scope(), agent.clock.today()) == 1
        assert review.due(conn, agent.scope(), agent.clock)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert "review" in kinds(fake, before)  # the day's second attempt


def test_the_weekly_look_doesn_t_use_up_the_day_s_second_review(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    agent.run_cycle("schedule")  # the review and the weekly look
    agent.clock.advance(days=7)
    fake.script.append(Reply("Things are going fine."))  # today's review isn't JSON; the weekly look follows
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert kinds(fake, before)[:2] == ["review", "weekly"]
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert "review" in kinds(fake, before)


def test_answered_review_calls_count_whatever_happens_with_their_answer(data_dir: Path) -> None:
    """0.22.0's guard stays: a review call answered and paid for counts at once, though nothing saved its review."""
    agent, _ = next_day(data_dir)
    scope, today, now = agent.scope(), agent.clock.today(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        assert review.due(conn, scope, agent.clock)
        for _ in range(review.MAX_ATTEMPTS):
            review.note_sent(conn, scope, today, now)
        assert review.sent_today(conn, scope, today) == review.MAX_ATTEMPTS
        assert not review.due(conn, scope, agent.clock)
    agent.clock.advance(days=1)
    with agent.db.connection() as conn:
        assert review.sent_today(conn, scope, agent.clock.today()) == 0  # a new day


# --- 2. an established principle stays established ---


def test_an_established_principle_confirmed_again_stays_established(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    scope, stamp = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        ids: list[int] = []
        for i in range(5):
            retro = {"subject": f"bet #{i}", "expected": "", "happened": "", "why": "Few views.", "cause": "no_reach"}
            ids += learning.save_cases(conn, scope, None, [{**retro, "sure": "high", "lesson": ""}], [], stamp)
        # as 0.20.1 left them: two compatible principles citing the same cases, the newer one established
        for text, supports, level in (
            ("Listings need reach before they sell.", ids[:2], "hypothesis"),
            ("Unpromoted Etsy listings get no views.", ids[:3], "established"),
        ):
            conn.execute(
                "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at,"
                " confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (scope.mode, scope.session, text, json.dumps(supports), "[]", level, stamp, stamp),
            )
        said = learning.apply_principles(
            conn, scope, [{"id": 2, "text": "", "supports": [ids[3]], "against": [], "retire": ""}], set(ids), stamp
        )
        assert said == ["principle #2 is established"]
        # a newer hypothesis citing their cases still isn't promoted by them (0.22.0), only by its own
        conn.execute(
            "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at, confirmed_at)"
            " VALUES (?, ?, 'Reach first.', ?, '[]', 'hypothesis', ?, ?)",
            (scope.mode, scope.session, json.dumps(ids[:3]), stamp, stamp),
        )
        said = learning.apply_principles(
            conn, scope, [{"id": 3, "text": "", "supports": [ids[4]], "against": [], "retire": ""}], set(ids), stamp
        )
        assert said == ["principle #3 is hypothesis"]
        # and a case against an established one still disputes it
        said = learning.apply_principles(
            conn, scope, [{"id": 2, "text": "", "supports": [], "against": [ids[4]], "retire": ""}], set(ids), stamp
        )
        assert said == ["principle #2 is disputed"]


# --- 3. every project stays named in the review ---


def test_the_review_names_every_project_it_leaves_out() -> None:
    lines = []
    for pid in range(1, 41):
        lines.append(f"#{pid} [active] Product line {pid} with a long enough title to fill its line · spent $0.10")
        if pid % 3 == 0:
            lines.append(f"   hypothesis: People buy product {pid}.")
    text = "\n".join(lines)
    for room in (2_000, 1_000, 700):
        cut = review._cut_lines(text, room)
        assert len(cut) <= room, room
        for pid in range(1, 41):
            assert f"#{pid} " in cut or f"#{pid}," in cut or cut.endswith(f"#{pid}]"), (room, pid)
        assert "left out, for the scorecard's room: #" in cut
    assert review._cut_lines(text, len(text)) == text


# --- 4.-5. the owner's park ---


def parked_line(data_dir: Path) -> tuple[Any, Any, int, int, int]:
    """An agent whose venture A has an active and an idea project; A's owner parks it. (agent, ctx, A, B, project)"""
    agent, _ = listed(data_dir)
    ctx = shop_context(agent)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        first, other = (
            ventures.create(conn, agent.scope(), title=t, pitch="p.", stage="building", now=now) for t in ("A", "B")
        )
    made = call(
        ctx,
        "project_create",
        {"title": "Under A", "hypothesis": "h", "next_step": "Order the sample\nposter", "status": "active"}
        | {"venture_id": first},
    )
    idea = call(
        ctx,
        "project_create",
        {"title": "Also under A", "hypothesis": "h", "next_step": "Sketch it", "status": "idea", "venture_id": first},
    )
    assert made.ok and idea.ok
    assert owner(agent).decide_venture(first, {"action": "park", "comment": "Not now."}, "Stefan").status == 200
    return agent, ctx, first, other, made.project_id


def test_a_project_of_a_venture_the_owner_parked_stays_with_it(data_dir: Path) -> None:
    agent, ctx, parked, other, project = parked_line(data_dir)
    moved = call(ctx, "project_update", {"project_id": project, "venture_id": other})
    assert not moved.ok and moved.text.endswith(
        f"your owner parked venture #{parked}: its projects stay with it and wait until they take it up again. For "
        "another venture, open a project of its own."
    )
    again = call(ctx, "project_update", {"project_id": project, "venture_id": other, "status": "active"})
    assert not again.ok
    assert rows(agent, f"SELECT status, venture_id FROM projects WHERE id = {project}") == [
        {"status": "waiting", "venture_id": parked}
    ]
    with agent.db.transaction() as conn:  # a venture the agent parked itself: its projects move as before
        made = store.create_project(
            conn, agent.scope(), cycle_id=ctx.cycle_id, title="Under B", hypothesis="h", next_step="n",
            status="idea", now=to_iso(agent.clock.now()), venture_id=other,
        )  # fmt: skip
        ventures.update(conn, other, to_iso(agent.clock.now()), stage="parked", parked_by="agent")
        free = ventures.create(
            conn, agent.scope(), title="C", pitch="p.", stage="building", now=to_iso(agent.clock.now())
        )
    assert call(ctx, "project_update", {"project_id": made, "venture_id": free}).ok


def test_the_owner_s_park_keeps_a_project_s_next_step_and_taking_it_up_gives_it_back(data_dir: Path) -> None:
    agent, ctx, parked, _, project = parked_line(data_dir)
    found = rows(agent, f"SELECT status, next_step, notes FROM projects WHERE venture_id = {parked} ORDER BY id")
    assert [(r["status"], r["next_step"]) for r in found] == [("waiting", stages.PARKED_STEP)] * 2
    assert found[0]["notes"].endswith(
        f"[owner] Your owner parked venture #{parked}. It was active; its next step: Order the sample poster"
    )
    assert found[1]["notes"].endswith(f"Your owner parked venture #{parked}. It was idea; its next step: Sketch it")
    idea = rows(agent, "SELECT id FROM projects WHERE title = 'Also under A'")[0]["id"]
    assert call(ctx, "project_update", {"project_id": idea, "next_step": "Ask the owner about A"}).ok  # its own step
    assert owner(agent).decide_venture(parked, {"action": "back", "confirm": True}, "Stefan").status == 200
    found = rows(agent, f"SELECT status, next_step, notes FROM projects WHERE venture_id = {parked} ORDER BY id")
    assert [(r["status"], r["next_step"]) for r in found] == [
        ("active", "Order the sample poster"),
        ("waiting", "Ask the owner about A"),  # what the agent set meanwhile stays; it makes it active itself
    ]
    assert found[0]["notes"].endswith(f"[owner] Your owner took venture #{parked} up again.")
    assert call(ctx, "project_update", {"project_id": idea, "status": "active"}).ok


def test_a_project_stopped_at_the_upgrade_to_0_22_gets_a_true_next_step_when_taken_up(data_dir: Path) -> None:
    """Migration 0077 kept nothing of what it overwrote: the project says so when its venture is taken up again."""
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        venture = ventures.create(conn, agent.scope(), title="A", pitch="p.", stage="researching", now=now)
        project = store.create_project(
            conn, agent.scope(), cycle_id=1, title="Under A", hypothesis="h", next_step="n", status="active",
            now=now, venture_id=venture,
        )  # fmt: skip
    assert owner(agent).decide_venture(venture, {"action": "park"}, "Stefan").status == 200
    with agent.db.transaction() as conn:  # as 0077 left it
        conn.execute(
            "UPDATE projects SET notes = ? WHERE id = ?", (f"[owner] Your owner parked venture #{venture}.", project)
        )
    assert owner(agent).decide_venture(venture, {"action": "research"}, "Stefan").status == 200
    assert rows(agent, f"SELECT status, next_step FROM projects WHERE id = {project}") == [
        {
            "status": "waiting",
            "next_step": f"None yet: your owner took venture #{venture} up again; set one (project_update).",
        }
    ]


def test_a_note_on_a_parked_venture_doesn_t_take_its_projects_up(data_dir: Path) -> None:
    agent, _, parked, _, project = parked_line(data_dir)
    assert owner(agent).decide_venture(parked, {"action": "note", "comment": "Later."}, "Stefan").status == 200
    assert rows(agent, f"SELECT status, next_step FROM projects WHERE id = {project}") == [
        {"status": "waiting", "next_step": stages.PARKED_STEP}
    ]
    assert owner(agent).decide_venture(parked, {"action": "kill"}, "Stefan").status == 200
    assert rows(agent, f"SELECT status FROM projects WHERE id = {project}") == [{"status": "abandoned"}]


# --- 6. replies a taken-back "auto" unlock approved ---


def _copy(conn: Any, approval_id: int) -> int:
    names = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(approvals)") if r[1] != "id")
    sql = f"INSERT INTO approvals ({names}) SELECT {names} FROM approvals WHERE id = ?"
    return int(conn.execute(sql, (approval_id,)).lastrowid)


def _grant(conn: Any, agent: Any, goal: int, level: str, now: str) -> Any:
    cursor = conn.execute(
        "INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, created_at)"
        " VALUES (?, ?, ?, 'email_reply', ?, 3, 10, 'Stefan', ?)",
        (agent.scope().mode, agent.scope().session, goal, level, now),
    )
    return conn.execute("SELECT * FROM policy_grants WHERE id = ?", (cursor.lastrowid,)).fetchone()


def test_replies_an_auto_unlock_approved_wait_for_the_owner_after_the_upgrade(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))
    goal = a_milestone(agent)
    queued = answer(agent, transport, goal)["id"]  # approved by an "auto" unlock, not sent yet
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # as 0.20.1 left them
        conn.execute("DROP TRIGGER policy_grants_replies_veto")
        auto = _grant(conn, agent, goal, "auto", now)
        a_use(conn, queued, auto, now)
        approve_as_code(conn, queued, now)
        twin, begun = _copy(conn, queued), _copy(conn, queued)
        a_use(conn, twin, auto, now)  # the same reply approved twice
        a_use(conn, begun, auto, now)
        executor.Executor._start(conn, begun, now, None)  # Ember's code began sending it: it runs on
        upgrade = (paths.APP_DIR / "migrations" / "0076_reply_unlocks.sql").read_text(encoding="utf-8")
        conn.execute(upgrade[upgrade.index("INSERT INTO policy_grants") :])  # 0.22.0 took the unlock back
        vetoable = _copy(conn, queued)  # one the veto window the owner granted again carried: it stays approved
        a_use(conn, vetoable, _grant(conn, agent, goal, "veto_window", now), now)
        sql = (paths.APP_DIR / "migrations" / "0078_reply_unlocks_stop.sql").read_text(encoding="utf-8")
        for statement in sql.split(";\n"):
            if statement.strip():
                conn.execute(statement)
    assert status_of(agent, queued) == {
        "status": "pending",
        "decided_by": None,
        "decision_comment": "Approved by your unlock, which was taken back (email replies run at most with a veto "
        "window since 0.22.0) before Ember's code carried it out: it waits for you.",
    }
    closed = rows(agent, f"SELECT status, closed_by, result_note FROM approvals WHERE id = {twin}")[0]
    assert (closed["status"], closed["closed_by"]) == ("failed", policy.REVOKED_BY)
    assert closed["result_note"].endswith(f"The same request waits as #{queued}.")
    assert status_of(agent, begun)["status"] == status_of(agent, vetoable)["status"] == "approved"
    assert agent.execute_approved() == [(vetoable, "simulated")]  # not the one waiting for the owner
    assert owner(agent).decide(queued, {"decision": "approve"}, "Stefan").status == 200
    assert agent.execute_approved() == [(queued, "simulated")]


# --- 7. the plan's projects in full ---


def test_projects_the_owner_s_park_stopped_come_after_the_working_ones(data_dir: Path) -> None:
    agent, ctx, parked, _, _ = parked_line(data_dir)
    with agent.db.transaction() as conn:
        alone = store.create_project(  # waiting on something else, of no venture: in its place
            conn, agent.scope(), cycle_id=ctx.cycle_id, title="Waiting line", hypothesis="h", next_step="Ask",
            status="waiting", now=to_iso(agent.clock.now()),
        )  # fmt: skip
        for n in range(10):
            agent.clock.advance(minutes=1)
            store.create_project(
                conn, agent.scope(), cycle_id=ctx.cycle_id, title=f"Working line {n}", hypothesis="h",
                next_step=f"List product {n}", status="active", now=to_iso(agent.clock.now()),
            )  # fmt: skip
        # as migration 0077 left them: changed at the upgrade, after everything the agent did
        conn.execute(
            "UPDATE projects SET updated_at = ? WHERE venture_id = ?",
            (to_iso(agent.clock.now().replace(year=agent.clock.now().year + 1)), parked),
        )
        listed_ = store.open_projects(conn, agent.scope())
    stopped = [int(p["id"]) for p in listed_ if p["venture_id"] == parked]
    order = [int(p["id"]) for p in listed_]
    assert order[-len(stopped) :] == stopped and len(stopped) == 2
    working = [int(p["id"]) for p in listed_ if str(p["title"]).startswith("Working line")]
    assert order.index(alone) > max(order.index(pid) for pid in working)  # by when it changed
    assert order.index(alone) < order.index(stopped[0])
    plan = agent.planner_preview()
    projects = plan.split("== OPEN PROJECTS ==\n", 1)[1].split("\n== ", 1)[0]
    assert "(the ones waiting while your owner parks their venture come last)" in projects.split("\n", 1)[0]
    for pid in stopped:
        assert f"\n#{pid} [waiting]" not in projects  # named in the first line only
    assert "next: List product 9" in projects
    shown = call(ctx, "project_list", {})
    assert shown.text.split("\n")[-1].startswith(f"#{stopped[-1]} [waiting]")
