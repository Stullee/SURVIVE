"""0.14.0: the unlock loopholes closed before any unlock is granted (FIX NOW 9), and the constitution's account of
what Ember's code carries out (FIX NOW 26e).

An automatic email's footer said the owner approved it before sending. Taking back every unlock, or Ember's own
revocation, stopped only what an unlock held, not what it had approved. The kill switch left the unlocks standing, so
its reset approved what a veto window held. Unlocks could be granted, and acted, while owner_user_ids named no one or
in safe mode. And an unlock approved a reply of 329 words without "Re:" or a listing with one photo."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("httpx2")

from app.agent import policy, prompts  # noqa: E402
from app.agent.owner import Owner, apply_kill_switch_reset, kill  # noqa: E402
from app.config import LoadedSettings, Settings  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import executor  # noqa: E402
from tests.test_agent import reply, rows, text  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_executor import REPLY  # noqa: E402
from tests.test_mail import JOURNAL, mail_cycle  # noqa: E402
from tests.test_never import a_use, approve_as_code, fits, request  # noqa: E402
from tests.test_owner_api import CSRF  # noqa: E402
from tests.test_owner_identity import OWNER, as_user  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone, change, price_of, work_on  # noqa: E402
from tests.test_ventures import tool_results  # noqa: E402

TRUE_FOOTER = "sent under rules they set, without their review of this email"
OWNERS_FOOTER = "approved by them before sending"


def unlock(agent: Any, goal: int, rule: str, level: str = "auto") -> None:
    assert owner(agent).set_autonomy(goal, {"rule": rule, "level": level}, "Stefan").status == 200


def a_small_cut(agent: Any, goal: int, listing_id: int, share: str = "0.95") -> dict[str, Any]:
    """The agent proposes a small price cut in a cycle for the milestone; the request made."""
    return work_on(agent, goal, change(listing_id, price=f"{price_of(agent, listing_id) * Decimal(share):.2f}"))[-1]


def status_of(agent: Any, approval_id: int) -> dict[str, Any]:
    found = rows(agent, f"SELECT status, decided_by, decision_comment FROM approvals WHERE id = {approval_id}")
    return found[0]


def answer(agent: Any, transport: Any, goal: int, **email: Any) -> dict[str, Any]:
    """A cycle for the milestone that answers the reader's email (email #1); the request made."""
    aimed = json.dumps(
        {"assessment": "A reader asked.", "goal": "Answer her", "steps": ["answer"], "sleep_minutes": 60}
        | {"focus_milestone_id": goal}
    )
    transport.outcomes.extend(
        [
            reply([{"type": "text", "text": aimed}], "end_turn"),
            calls(("propose_email", {**REPLY, **email})),
            text("Answered."),
            JOURNAL,
        ]
    )
    assert agent.run_cycle("schedule").status == "completed"
    return rows(agent, "SELECT id, status, decided_by FROM approvals ORDER BY id DESC LIMIT 1")[0]


# --- (a) the footer of an automatic email tells the truth ---


def test_an_automatic_reply_says_its_owner_did_not_review_it(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))  # the dry run's reader wrote (email #1)
    goal = a_milestone(agent)
    unlock(agent, goal, "email_reply")
    made = answer(agent, transport, goal)
    assert (made["status"], made["decided_by"]) == ("approved", policy.POLICY_BY)
    said = tool_results(agent, "propose_email")[-1]["result"]
    assert said.startswith(f"Approval request #{made['id']}: Ember's code approved it at once: your owner unlocked")
    assert "is waiting for your owner" not in said and "Nothing has been sent. If they approve it" not in said
    assert agent.execute_approved() == [(made["id"], "simulated")]
    [sent] = agent.mailbox.sent  # type: ignore[union-attr]
    body = sent.get_content()
    assert TRUE_FOOTER in body and OWNERS_FOOTER not in body
    assert OWNERS_FOOTER in executor.footer(agent.settings) and TRUE_FOOTER not in executor.footer(agent.settings)


# --- (b) taking back an unlock stops what it approved and Ember's code hasn't begun ---


def test_taking_back_every_unlock_stops_what_it_approved_before_it_ran(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    ran = a_small_cut(agent, goal, listing_id)
    assert agent.execute_approved() == [(ran["id"], "done")]  # carried out: it stays done
    cut = price_of(agent, listing_id)
    waiting = a_small_cut(agent, goal, listing_id)  # approved at once, not carried out yet (the shop's daily limit)
    assert status_of(agent, waiting["id"])["decided_by"] == policy.POLICY_BY
    assert owner(agent).take_back_unlocks({}, "Stefan").body == {"taken_back": 1}
    after = status_of(agent, waiting["id"])
    assert (after["status"], after["decided_by"]) == ("pending", None)
    assert after["decision_comment"] == (
        "Approved by your unlock, which was taken back (you took back every unlock) before Ember's code carried it "
        "out: it waits for you."
    )
    assert status_of(agent, ran["id"])["status"] == "done"
    assert agent.execute_approved() == [] and price_of(agent, listing_id) == cut
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == waiting["id"])
    assert (card["status"], card["veto_until"]) == ("pending", None)
    assert owner(agent).decide(waiting["id"], {"decision": "approve"}, "Stefan").status == 200  # the owner decides
    assert agent.execute_approved() == [(waiting["id"], "done")]


def test_a_rule_taken_back_or_revoked_by_ember_s_code_stops_what_it_approved(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    first = a_small_cut(agent, goal, listing_id)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "manual"}, "Stefan").status == 200
    assert status_of(agent, first["id"])["status"] == "pending"  # the owner's own take-back of the rule
    assert owner(agent).decide(first["id"], {"decision": "reject"}, "Stefan").status == 200
    unlock(agent, goal, "price_change")
    second = a_small_cut(agent, goal, listing_id)
    assert owner(agent).decide_milestone(goal, {"action": "drop"}, "Stefan").status == 200
    agent.run_policy()  # Ember's code revokes the unlock of a dropped milestone, and what it approved waits
    after = status_of(agent, second["id"])
    assert (after["status"], after["decided_by"]) == ("pending", None)
    assert f"(milestone #{goal} was dropped)" in after["decision_comment"]
    assert agent.execute_approved() == []


def test_a_spent_budget_ends_an_unlock_but_what_it_approved_runs(data_dir: Path) -> None:
    """A spent budget is a normal end, not a take-back. The scheduler's round revokes the unlock first, then carries
    out what is approved: the action that spent the budget runs."""
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    body = {"rule": "price_change", "level": "auto", "budget": 1}
    assert owner(agent).set_autonomy(goal, body, "Stefan").status == 200
    old = price_of(agent, listing_id)
    made = a_small_cut(agent, goal, listing_id)
    assert (made["status"], made["decided_by"]) == ("approved", policy.POLICY_BY)
    agent.run_policy()
    assert rows(agent, "SELECT level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "by": policy.REVOKED_BY, "why": "its budget of 1 actions is spent"}
    ]
    assert status_of(agent, made["id"])["status"] == "approved"
    assert agent.execute_approved() == [(made["id"], "done")]
    assert price_of(agent, listing_id) < old


def test_a_spent_budget_ends_a_veto_window_once_what_it_holds_is_approved(data_dir: Path) -> None:
    """The request that spent a veto window's budget is still approved at the time its card said."""
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    body = {"rule": "price_change", "level": "veto_window", "budget": 1}
    assert owner(agent).set_autonomy(goal, body, "Stefan").status == 200
    old = price_of(agent, listing_id)
    held = a_small_cut(agent, goal, listing_id)
    agent.run_policy()
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == held["id"])
    assert card["veto_until"] is not None and rows(agent, "SELECT COUNT(*) AS n FROM policy_grants") == [{"n": 1}]
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    assert status_of(agent, held["id"])["decided_by"] == policy.POLICY_BY
    assert agent.execute_approved() == [(held["id"], "done")] and price_of(agent, listing_id) < old
    agent.run_policy()  # now its budget ends it
    assert rows(agent, "SELECT level, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "why": "its budget of 1 actions is spent"}
    ]


@pytest.mark.parametrize("how", ["take back", "kill"])
def test_taking_back_every_unlock_also_stops_what_an_ended_unlock_approved(data_dir: Path, how: str) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    body = {"rule": "price_change", "level": "auto", "budget": 1}
    assert owner(agent).set_autonomy(goal, body, "Stefan").status == 200
    made = a_small_cut(agent, goal, listing_id)
    agent.run_policy()  # its budget is spent: the unlock ends, and its approval stands
    if how == "kill":
        assert kill(agent.db, agent.economy, "Ember", {"confirm_name": "Ember"}, "Stefan").status == 200
        why = "you used the kill switch"
    else:
        assert owner(agent).take_back_unlocks({}, "Stefan").body == {"taken_back": 0}
        why = "you took back every unlock"
    after = status_of(agent, made["id"])
    assert (after["status"], after["decided_by"]) == ("pending", None)
    assert f"({why})" in after["decision_comment"]


def test_ember_s_code_says_which_request_waits_and_which_it_closed(data_dir: Path) -> None:
    """Two requests the same unlock approved, alike (an older one, say): one waits for the owner again, the other is
    closed (one waits at a time), and the event log says which."""
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    first = a_small_cut(agent, goal, listing_id)["id"]
    columns = (
        "mode, session, life_id, cycle_id, created_at, type, title, description, payload, payload_sha256,"
        " expected_cost, expected_benefit, executor, action, milestone_id, venture_id"
    )
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        copied = f"INSERT INTO approvals ({columns}) SELECT {columns} FROM approvals WHERE id = ?"
        second = int(conn.execute(copied, (first,)).lastrowid)
        a_use(conn, second, policy.grant(conn, agent.scope(), goal, "price_change"), now)
        approve_as_code(conn, second, now)
    assert owner(agent).decide_milestone(goal, {"action": "drop"}, "Stefan").status == 200
    agent.run_policy()
    assert [status_of(agent, n)["status"] for n in (first, second)] == ["pending", "failed"]
    lines = "SELECT message FROM events WHERE message LIKE 'Request #% again:%' OR message LIKE 'Request #% closed:%'"
    assert [r["message"] for r in rows(agent, lines)] == [
        f"Request #{first} waits for you again: its unlock was taken back before it ran",
        f"Request #{second} closed: the same request waits for you as #{first}",
    ]


def test_the_owner_cancelling_an_automatic_action_is_a_veto(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    made = a_small_cut(agent, goal, listing_id)
    assert owner(agent).close(made["id"], {"outcome": "failed"}, "Stefan").status == 200
    agent.run_policy()
    assert rows(agent, "SELECT level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "by": policy.REVOKED_BY, "why": f"your owner cancelled request #{made['id']}"}
    ]


def test_what_ember_s_code_began_is_never_taken_back(data_dir: Path) -> None:
    """The executors commit their journal entry before they act: from then on the unlock's approval stands, whatever
    is taken back, and the database refuses to undo it (or any decision of the owner's)."""
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    made = a_small_cut(agent, goal, listing_id)
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO action_journal (mode, session, approval_id, class, started_at, status)"
            " VALUES (?, ?, ?, 'etsy.edit_listing', ?, 'running')",
            (scope.mode, scope.session, made["id"], now),
        )
    assert owner(agent).take_back_unlocks({}, "Stefan").body == {"taken_back": 1}
    assert status_of(agent, made["id"])["status"] == "approved"
    agent.run_policy()
    assert status_of(agent, made["id"])["status"] == "approved"
    reset = "UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL WHERE id = ?"
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="not an allowed status change"):
        conn.execute(reset, (made["id"],))
    owners = request(agent, goal)  # a request the owner approved stays theirs
    assert owner(agent).decide(owners, {"decision": "approve"}, "Stefan").status == 200
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="a decision is final|not an all"):
        conn.execute(reset, (owners,))


# --- (c) the kill switch takes back every unlock ---


def test_the_kill_switch_takes_back_every_unlock_and_its_reset_approves_nothing(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change", "veto_window")
    held = a_small_cut(agent, goal, listing_id)
    assert held["status"] == "pending"
    apply_kill_switch_reset(agent.db, agent.economy, 1)  # the option's value at the start
    stopped = kill(agent.db, agent.economy, agent.settings.agent_name, {"confirm_name": "Ember"}, "Stefan")
    assert stopped.body == {"state": "killed"}
    assert rows(agent, "SELECT level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "by": "Stefan", "why": "you used the kill switch"}
    ]
    note = rows(agent, f"SELECT owner_comment FROM milestones WHERE id = {goal}")[0]["owner_comment"]
    assert note.startswith("Used the kill switch, which took back every unlock")
    assert apply_kill_switch_reset(agent.db, agent.economy, 2) and agent.db.get_meta(KILLED_KEY) == "0"
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    assert status_of(agent, held["id"])["status"] == "pending"  # it waits for the owner
    assert agent.execute_approved() == []
    assert next(a for a in agent.dashboard()["approvals"] if a["id"] == held["id"])["veto_until"] is None


# --- (d) unlocks only while Ember knows its owner, and never in safe mode ---


def test_an_unlock_needs_owner_user_ids(client_factory: Callable[..., Iterator[TestClient]]) -> None:
    due = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    for settings, headers, granted in (
        (Settings(), {**CSRF}, False),  # everyone who opens the panel counts as the owner
        (Settings(owner_user_ids=(OWNER,)), as_user(OWNER), True),
    ):
        with client_factory(LoadedSettings(settings)) as client:
            milestone = {"title": f"Ten sales ({granted})", "measure": "10 orders", "due": due}
            goal = client.post("api/roadmap", json=milestone, headers=headers).json()["id"]
            path = f"api/milestones/{goal}/autonomy"
            said = client.post(path, json={"rule": "email_reply", "level": "auto"}, headers=headers)
            if granted:
                assert said.status_code == 200, said.json()
            else:
                assert said.status_code == 409
                assert "owner_user_ids" in said.json()["error"]
            assert (
                client.post(path, json={"rule": "email_reply", "level": "manual"}, headers=headers).status_code == 200
            )


def forget_the_owner(agent: Any, safe_mode: bool) -> str:
    """The app starts again without owner_user_ids, or in safe mode; why its unlocks are off now."""
    if safe_mode:  # the options are invalid: built-in defaults (dry run), whatever owner_user_ids said
        agent.loaded = LoadedSettings(agent.settings, errors=["daily_spend_cap_usd: too big"])
    else:
        agent.settings = agent.settings.model_copy(update={"owner_user_ids": ()})
        agent.loaded = LoadedSettings(agent.settings)
    return "the app runs in safe mode" if safe_mode else "owner_user_ids names no one"


@pytest.mark.parametrize("safe_mode", [False, True])
def test_standing_unlocks_act_only_while_ember_knows_its_owner(data_dir: Path, safe_mode: bool) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change", "veto_window")  # granted while Ember knew its owner
    held = a_small_cut(agent, goal, listing_id)
    assert held["status"] == "pending"
    reason = forget_the_owner(agent, safe_mode)
    assert agent.unlocks_off() == reason
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    assert status_of(agent, held["id"])["status"] == "pending"  # the veto window passed: it still waits
    assert agent.execute_approved() == []
    assert next(a for a in agent.dashboard()["approvals"] if a["id"] == held["id"])["veto_until"] is None
    actions = Owner(agent.db, agent.clock, agent.economy, agent.scope(), "Ember", unlocks_off=agent.unlocks_off())
    refused = actions.set_autonomy(goal, {"rule": "qa_fix", "level": "auto"}, "Stefan")
    assert refused.status == 409 and reason in refused.body["error"]
    assert actions.set_autonomy(goal, {"rule": "price_change", "level": "manual"}, "Stefan").status == 200


@pytest.mark.parametrize("safe_mode", [False, True])
def test_unlocks_off_are_taken_back_and_approve_nothing_when_back_on(data_dir: Path, safe_mode: bool) -> None:
    """While unlocks are off, Ember's code takes them back, as the kill switch does: a veto window that passed
    meanwhile approves nothing once owner_user_ids is back or safe mode is over. The dashboard says why."""
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change", "veto_window")
    held = a_small_cut(agent, goal, listing_id)
    settings, old = agent.settings, price_of(agent, listing_id)
    reason = forget_the_owner(agent, safe_mode)
    assert (agent.dashboard()["audit"]["unlocks_off"], agent.roadmap()["unlocks_off"]) == (reason, reason)
    agent.recover()  # the app starts again like this
    assert rows(agent, "SELECT level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "by": policy.REVOKED_BY, "why": f"unlocks are off while {reason}"}
    ]
    assert agent.dashboard()["audit"]["unlocks"] == 0
    agent.clock.advance(hours=3 * policy.VETO_HOURS)
    agent.settings, agent.loaded = settings, LoadedSettings(settings)  # the owner fixed the options and restarted
    agent.recover()
    assert agent.unlocks_off() == "" and agent.roadmap()["unlocks_off"] == ""
    agent.run_policy()
    assert status_of(agent, held["id"])["status"] == "pending"
    assert agent.execute_approved() == [] and price_of(agent, listing_id) == old


@pytest.mark.parametrize("safe_mode", [False, True])
def test_the_agent_hears_that_unlocks_are_off(data_dir: Path, safe_mode: bool) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "deactivate")  # granted while Ember knew its owner
    reason = forget_the_owner(agent, safe_mode)
    made = work_on(agent, goal, change(listing_id, state="deactivate"))[-1]
    assert (made["status"], made["decided_by"]) == ("pending", None)
    said = tool_results(agent, "propose_etsy_edit")[-1]["result"]
    assert said.endswith(f" It waits for your owner: unlocks are off while {reason}.")


# --- (e) an unlock carries only what passes its QA ---


def test_an_unlock_does_not_carry_a_reply_that_fails_its_qa(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))
    goal = a_milestone(agent)
    unlock(agent, goal, "email_reply")
    long = " ".join(["word"] * 329)
    made = answer(agent, transport, goal, subject="Planner in German", body=long)
    assert (made["status"], made["decided_by"]) == ("pending", None)
    said = tool_results(agent, "propose_email")[-1]["result"]
    assert said.endswith(
        " It waits for your owner: an unlock carries only what passes QA (its subject doesn't keep the thread "
        "(Re: ...); 329 words, more than 200: an answer is short)."
    )
    assert agent.execute_approved() == [] and agent.mailbox.sent == []  # type: ignore[union-attr]
    assert rows(agent, "SELECT COUNT(*) AS n FROM policy_uses") == [{"n": 0}]
    passing = answer(agent, transport, goal)  # the same unlock carries an answer that passes
    assert (passing["status"], passing["decided_by"]) == ("approved", policy.POLICY_BY)


def test_an_unlock_does_not_carry_a_listing_with_one_photo(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = listed(data_dir)  # a listing is live: a new one isn't Ember's first publication
    goal = a_milestone(agent)
    unlock(agent, goal, "listing_variant")
    action = json.loads(rows(agent, "SELECT action FROM approvals WHERE executor = 'etsy_listing'")[0]["action"])
    fits(monkeypatch, "listing_variant")
    one_photo = request(agent, goal, executor="etsy_listing", action={**action, "photos": action["photos"][:1]})
    with agent.db.transaction() as conn:
        said = policy.apply(conn, agent.scope(), one_photo, agent.clock)
    assert said == (
        " It waits for your owner: an unlock carries only what passes QA (1 photo, fewer than 5 (Etsy shows up to 10))."
    )
    assert status_of(agent, one_photo)["status"] == "pending"
    photos = (action["photos"] * 5)[:5]
    five = request(agent, goal, executor="etsy_listing", action={**action, "photos": photos})
    with agent.db.transaction() as conn:
        assert "approved it at once" in policy.apply(conn, agent.scope(), five, agent.clock)


def test_a_held_request_that_fails_its_qa_is_not_approved(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request held under older checks (QA moved on meanwhile) waits for the owner when its window passes."""
    agent, _ = listed(data_dir)
    goal = a_milestone(agent)
    unlock(agent, goal, "listing_variant", "veto_window")
    action = json.loads(rows(agent, "SELECT action FROM approvals WHERE executor = 'etsy_listing'")[0]["action"])
    fits(monkeypatch, "listing_variant")
    held = request(agent, goal, executor="etsy_listing", action={**action, "photos": (action["photos"] * 5)[:5]})
    with agent.db.transaction() as conn:
        assert "unless your owner decides first" in policy.apply(conn, agent.scope(), held, agent.clock)
    monkeypatch.setattr("app.integrations.qa.MIN_PHOTOS", 6)
    assert agent.dashboard()["audit"]["held"] == 0  # no longer promised: it can't be approved
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    agent.run_policy()
    after = status_of(agent, held)
    assert after["status"] == "pending"
    assert after["decision_comment"] == (
        "Held by your unlock, but not approved when its veto window passed: an unlock carries only what passes QA (5 "
        "photos, fewer than 6 (Etsy shows up to 10)). It waits for you."
    )
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == held)
    assert card["veto_until"] is None and card["decision_comment"] == after["decision_comment"]
    said = rows(agent, f"SELECT message FROM events WHERE message LIKE 'Request #{held} %'")
    assert [r["message"] for r in said] == [  # said once
        f"Request #{held} waits for you: an unlock carries only what passes QA (5 photos, fewer than 6 (Etsy shows up "
        "to 10))"
    ]


# --- the upgrade: every unlock of 0.13.0 is taken back once ---


def test_the_upgrade_takes_back_every_unlock_and_stops_what_it_approved(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    everything = discover_migrations()
    ours = next(m for m in everything if m.name == "unlock_safety")
    migrate(db_file, [m for m in everything if m.version < ours.version], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    then = "2026-09-29T10:00:00Z"
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', ?, 'born', 'alive')",
            (then,),
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', ?, 'completed', 'schedule', 0, 1)",
            (then,),
        )
        conn.execute(
            "INSERT INTO milestones (id, mode, session, life_id, title, measure, first_due, due, created_at,"
            " updated_at, created_by) VALUES (1, 'live', 0, 1, 'Ten sales', '10 orders', '2026-10-30', '2026-10-30', ?,"
            " ?, 'owner')",
            (then, then),
        )
        for rule, level in (("price_change", "auto"), ("deactivate", "veto_window"), ("qa_fix", "manual")):
            conn.execute(
                "INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, created_at)"
                " VALUES ('live', 0, 1, ?, ?, 3, 10, 'Stefan', ?)",
                (rule, level, then),
            )
        # 4 waits already as 5; 6 and 7 are alike; 8 began and 9 is alike. One of each waits at a time: approved first.
        made = ((1, 1, "auto"), (2, 2, "auto"), (3, 3, "veto_window"), (4, 4, "auto"), (5, 4, None), (6, 6, "auto"))
        for n, same, level in (*made, (7, 6, "auto"), (8, 8, "auto"), (9, 8, "auto")):
            conn.execute(
                "INSERT INTO approvals (id, mode, session, life_id, cycle_id, created_at, type, title, description,"
                " payload, payload_sha256, expected_cost, expected_benefit, executor, action, milestone_id)"
                " VALUES (?, 'live', 0, 1, 1, ?, 'sell', ?, 'Sells better.', ?, ?, 'none', 'more', 'etsy_edit', ?, 1)",
                (n, then, f"Cut {n}", f"Cut {same}", f"sha-{same}", json.dumps({"listing_id": same, "price": "4.05"})),
            )
            if level is None:
                continue
            grant = conn.execute("SELECT id FROM policy_grants WHERE level = ?", (level,)).fetchone()[0]
            veto = then if level == "veto_window" else None
            conn.execute(
                "INSERT INTO policy_uses (approval_id, grant_id, level, created_at, veto_until, approved_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (n, grant, level, then, veto, None if veto else then),
            )
            if level == "auto":
                conn.execute(
                    "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, version = 1"
                    " WHERE id = ?",
                    (then, policy.POLICY_BY, n),
                )
        for n in (2, 8):  # requests 2 and 8 are being carried out
            conn.execute(
                "INSERT INTO action_journal (mode, session, approval_id, class, started_at, status)"
                " VALUES ('live', 0, ?, 'etsy.edit_listing', ?, 'running')",
                (n, then),
            )
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [
        m.version for m in everything if m.version >= ours.version
    ]
    upgraded = Database(db_file)
    with upgraded.connection() as conn:
        newest = conn.execute(
            "SELECT g.rule, g.level, g.by FROM policy_grants g WHERE g.id = (SELECT MAX(h.id) FROM policy_grants h"
            " WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule) ORDER BY g.rule"
        ).fetchall()
        assert [tuple(r) for r in newest] == [
            ("deactivate", "manual", policy.REVOKED_BY),
            ("price_change", "manual", policy.REVOKED_BY),
            ("qa_fix", "manual", "Stefan"),
        ]
        got = conn.execute(
            "SELECT id, status, decided_by, closed_by, result_note FROM approvals ORDER BY id"
        ).fetchall()
        closed = "Approved by your unlock, which was taken back (the upgrade to 0.14.0) before Ember's code carried it"
        assert [tuple(r) for r in got] == [
            (1, "pending", None, None, None),  # approved at once, not begun: it waits for the owner
            (2, "approved", policy.POLICY_BY, None, None),  # begun: it runs on, exactly once
            (3, "pending", None, None, None),  # held: its unlock no longer stands
            (
                4,
                "failed",
                policy.POLICY_BY,
                policy.REVOKED_BY,
                f"{closed} out: it waits for you. The same request waits as #5.",
            ),
            (5, "pending", None, None, None),
            (6, "pending", None, None, None),
            (
                7,
                "failed",
                policy.POLICY_BY,
                policy.REVOKED_BY,
                f"{closed} out: it waits for you. The same request waits as #6.",
            ),
            (8, "approved", policy.POLICY_BY, None, None),
            (9, "pending", None, None, None),  # what began alike doesn't hold it back
        ]
    upgraded.close()


# --- FIX NOW 26e: the constitution says what Ember's code carries out now ---


def test_the_constitution_says_what_ember_s_code_carries_out() -> None:
    said = " ".join(prompts.constitution(Settings()).split())
    for done in ("emails", "Etsy listings and changes", "Pinterest pins", "Printify products", "Undo"):
        assert done in said, done
    assert "everything else your owner carries out" not in said
    assert "Revenue only counts when your owner records it" not in said
    assert "Revenue counts when your owner records it, or Ember's code from Etsy's numbers" in said
    assert "your owner carries out the rest" in said
    assert "must go through request_approval" in said
