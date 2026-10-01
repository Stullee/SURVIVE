"""0.13.0: the audit. The owner sees what Ember's code did (before and after, on whose decision), undoes an action on a
listing with one click (a request of theirs, approved at once and carried out like any change), reads a daily digest
of the automatic actions, and can take back every unlock at once."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import audit, policy, store  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import connectors, etsy  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone, change, price_of, reject, work_on  # noqa: E402


def feed(agent: Any) -> list[dict[str, Any]]:
    return agent.dashboard()["audit"]["feed"]


def undo(agent: Any, journal_id: int) -> int:
    reply = owner(agent).undo(journal_id, "Stefan")
    assert reply.status == 200, reply.body
    return int(reply.body["approval_id"])


def shop_listing(agent: Any, listing_id: int) -> dict[str, Any]:
    return agent.etsy.shop().state["listings"][str(listing_id)]


def test_the_owner_sees_what_ember_s_code_did_and_undoes_a_change(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    old = price_of(agent, listing_id)
    new = (old * Decimal("0.95")).quantize(Decimal("0.01"))
    made = work_on(agent, goal, change(listing_id, price=f"{new}"))
    assert agent.execute_approved() == [(made[-1]["id"], "done")]
    changed, created = feed(agent)[:2]
    assert (changed["class"], changed["by"], changed["status"], changed["subject"]) == (
        "etsy.edit_listing",
        "unlock",
        "done",
        str(listing_id),
    )
    assert [(c["part"], c["before"].split()[0], c["after"].split()[0]) for c in changed["changes"]] == [
        ("Price", f"{old}", f"{new}")
    ]
    assert changed["undo"] == {"label": "Change it back", "why_not": None, "request": None}
    assert (created["class"], created["by"], created["undo"]["label"]) == (
        "etsy.create_listing",
        "owner",
        "Deactivate it",
    )
    assert created["undo"]["why_not"] == f"a later action changed this listing (#{changed['id']}): undo that one first"
    # The owner's Undo: a request of theirs, approved at once, carried out like any change
    request = undo(agent, changed["id"])
    assert rows(
        agent, f"SELECT status, decided_by, executor, title, milestone_id FROM approvals WHERE id = {request}"
    ) == [
        {
            "status": "approved",
            "decided_by": "Stefan",
            "executor": "etsy_edit",
            "title": f"Undo: change back Etsy listing #{listing_id}",
            "milestone_id": goal,  # it belongs where the action it undoes belonged
        }
    ]
    again = owner(agent).undo(changed["id"], "Stefan")
    assert (again.status, again.body["error"]) == (409, "this action can't be undone: your Undo of it is under way")
    assert agent.execute_approved() == [(request, "done")]
    assert price_of(agent, listing_id) == old
    undone, changed = feed(agent)[:2]
    assert (undone["by"], undone["undoes"], undone["by_text"]) == ("undo", changed["id"], "your Undo")
    assert [(c["before"].split()[0], c["after"].split()[0]) for c in undone["changes"]] == [(f"{new}", f"{old}")]
    assert changed["undo"]["request"]["status"] == "done" and changed["undo"]["why_not"] == "it is undone"
    assert undone["undo"]["label"] == "Change it back"  # an Undo can be undone in turn: the newest action on it
    assert owner(agent).undo(987_654, "Stefan").status == 404
    assert "Stefan undid action #" in " ".join(e["message"] for e in agent_events(agent))


def agent_events(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT message FROM events ORDER BY id")


def test_undo_deactivates_renews_and_turns_automatic_renewal_off(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    created = feed(agent)[0]
    request = undo(agent, created["id"])  # the creation: deactivate it
    assert agent.execute_approved() == [(request, "done")]
    assert shop_listing(agent, listing_id)["state"] == etsy.STATES["deactivate"]
    deactivated = feed(agent)[0]
    assert (deactivated["class"], deactivated["by"]) == ("etsy.deactivate", "undo")
    assert deactivated["undo"]["label"] == f"Renew it ({etsy.RENEWAL_FEE})"
    request = undo(agent, deactivated["id"])  # its deactivation: renew it (Etsy's fee)
    assert rows(agent, f"SELECT expected_cost FROM approvals WHERE id = {request}")[0]["expected_cost"].startswith(
        f"Etsy's listing fee for the renewal ({etsy.RENEWAL_FEE}"
    )
    assert agent.execute_approved() == [(request, "done")]
    assert shop_listing(agent, listing_id)["state"] == etsy.LIVE_STATE
    renewed = feed(agent)[0]
    assert (renewed["class"], renewed["undo"]["label"]) == ("etsy.renew", None)
    # Ember's code turned on a sold listing's automatic renewal on its own: the owner turns it off again
    now = to_iso(agent.clock.now())
    agent.etsy.shop().set_auto_renew(listing_id, True)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE etsy_listings SET auto_renew = 1, renew_set_at = ? WHERE listing_id = ?", (now, listing_id)
        )
        connectors.record(
            conn,
            agent.scope(),
            "etsy.auto_renew",
            str(listing_id),
            "done",
            now,
            before={"auto_renew": False},
            after={"auto_renew": True},
        )
    renewal = feed(agent)[0]
    assert (renewal["by"], renewal["changes"], renewal["undo"]["label"]) == (
        "code",
        [{"part": "Automatic renewal", "before": "off", "after": "on"}],
        "Turn automatic renewal off",
    )
    request = undo(agent, renewal["id"])
    payload = rows(agent, f"SELECT payload FROM approvals WHERE id = {request}")[0]["payload"]
    assert "Automatic renewal at Etsy: off" in payload
    assert agent.execute_approved() == [(request, "done")]
    assert shop_listing(agent, listing_id)["auto_renew"] is False
    assert rows(agent, f"SELECT auto_renew FROM etsy_listings WHERE listing_id = {listing_id}") == [{"auto_renew": 0}]
    off = feed(agent)[0]
    assert (off["class"], off["changes"], off["undo"]["label"]) == (
        "etsy.auto_renew_off",
        [{"part": "Automatic renewal", "before": "on", "after": "off"}],
        None,
    )


def test_what_the_owner_can_t_undo(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        connectors.record(conn, agent.scope(), "email.send", "ann@example.org", "done", now, after={"status": "sent"})
    sent, created = feed(agent)[:2]
    assert (sent["undo"]["label"], sent["undo"]["why_not"]) == (None, "an email can't be unsent")
    refused = owner(agent).undo(sent["id"], "Stefan")
    assert (refused.status, refused.body["error"]) == (409, "this action can't be undone: an email can't be unsent")
    # A change of the listing waits for the owner: decide it first
    goal = a_milestone(agent)
    made = work_on(agent, goal, change(listing_id, title="A better title"))
    waiting = made[-1]["id"]
    assert feed(agent)[1]["undo"]["why_not"] == f"change #{waiting} of this listing waits: decide it first"
    assert owner(agent).undo(created["id"], "Stefan").status == 409
    reject(agent, waiting)
    request = undo(agent, created["id"])
    # One Undo at a time, in the database too
    with pytest.raises(sqlite3.IntegrityError, match="undone or being undone"), agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO action_undos (journal_id, approval_id, by, created_at) VALUES (?, ?, 'Stefan', ?)",
            (created["id"], waiting, now),
        )
    # Cancelled before Ember's code made it: the owner may undo it again
    assert owner(agent).close(request, {"outcome": "failed"}, "Stefan").status == 200
    assert feed(agent)[1]["undo"]["why_not"] is None
    assert undo(agent, created["id"]) > request


def test_the_owner_takes_back_every_unlock(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal, other = a_milestone(agent), a_milestone(agent, "Twenty sales")
    for milestone, rule, level in (
        (goal, "price_change", "veto_window"),
        (goal, "deactivate", "auto"),
        (other, "qa_fix", "auto"),
    ):
        assert owner(agent).set_autonomy(milestone, {"rule": rule, "level": level}, "Stefan").status == 200
    old = price_of(agent, listing_id)
    held = work_on(agent, goal, change(listing_id, price=f"{old * Decimal('0.95'):.2f}"))[-1]["id"]
    shown = agent.dashboard()["audit"]
    assert (shown["unlocks"], shown["held"]) == (3, 1)
    assert owner(agent).take_back_unlocks({"why": "x"}, "Stefan").status == 422
    reply = owner(agent).take_back_unlocks({}, "Stefan")
    assert (reply.status, reply.body) == (200, {"taken_back": 3})
    with agent.db.connection() as conn:
        assert [g for g in policy.grants(conn, agent.scope()) if g["level"] != "manual"] == []
    assert (
        rows(agent, "SELECT by, why FROM policy_grants ORDER BY id DESC LIMIT 3")
        == [{"by": "Stefan", "why": "you took back every unlock"}] * 3
    )
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()  # what an unlock held waits for the owner now
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {held}") == [{"status": "pending"}]
    assert (agent.dashboard()["audit"]["unlocks"], agent.dashboard()["audit"]["held"]) == (0, 0)
    for milestone in (goal, other):
        comment = rows(agent, f"SELECT owner_comment FROM milestones WHERE id = {milestone}")[0]["owner_comment"]
        assert comment.startswith("Took back every unlock")
    assert owner(agent).take_back_unlocks({}, "Stefan").body == {"taken_back": 0}
    assert any("Stefan took back every unlock (3 in all)" in e["message"] for e in agent_events(agent))


def spending(agent: Any, listing_id: int, price: str) -> int:
    """A price change of a request that spends money, made as the tools make one: never automatic."""
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        made = store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            to_iso(agent.clock.now()),
            type="spend_money",
            title="Change the price",
            description="It pays for itself.",
            payload=f"Price: {price}",
            expected_cost="none",
            expected_benefit="more sales",
            executor="etsy_edit",
            action=store.canonical({"listing_id": listing_id, "currency": "EUR", "price": price}),
        )
        policy.apply(conn, agent.scope(), made, agent.clock)
    return made


def test_the_daily_digest(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)  # the owner approved a listing, Ember's code created it
    day = agent.clock.today()
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    old = price_of(agent, listing_id)
    made = work_on(agent, goal, change(listing_id, price=f"{old * Decimal('0.95'):.2f}"))  # carried by the unlock
    assert agent.execute_approved() == [(made[-1]["id"], "done")]
    waited = spending(agent, listing_id, f"{old:.2f}")  # 0.14.0: a reason's words are no act; money is NEVER
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {waited}") == [{"status": "pending"}]
    reject(agent, waited)
    missed = a_milestone(agent, "Thirty sales")
    assert owner(agent).set_autonomy(missed, {"rule": "qa_fix", "level": "auto"}, "Stefan").status == 200
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = 'missed', closed_at = ?, closed_by = 'code', result = 'x' WHERE id = ?",
            (to_iso(agent.clock.now()), missed),
        )
    agent.run_policy()  # takes the unlock back (and writes the digest of the day before, a quiet one)
    assert rows(agent, "SELECT day FROM owner_digests") == [{"day": (day - timedelta(days=1)).isoformat()}]
    agent.clock.current = agent.clock.day_start(day) + timedelta(hours=20)
    assert owner(agent).set_autonomy(goal, {"rule": "deactivate", "level": "veto_window"}, "Stefan").status == 200
    held = work_on(agent, goal, change(listing_id, state="deactivate"))[-1]
    assert held["status"] == "pending"
    agent.clock.current = agent.clock.day_start(day + timedelta(days=1)) + timedelta(minutes=5)
    agent.run_policy()
    agent.run_policy()  # once a day
    written = rows(agent, f"SELECT text, data FROM owner_digests WHERE day = '{day.isoformat()}'")
    assert len(written) == 1
    first = to_iso(agent.clock.day_start(day) + timedelta(hours=20 + policy.VETO_HOURS))[:16].replace("T", " ")
    assert written[0]["text"] == (
        f"{day.isoformat()}: Ember's code carried out 2 actions (1 on your unlocks, 1 you approved). Your unlocks"
        " approved 1 request(s) at once and 0 after their veto window. 1 held for your veto now (the first approved"
        f" {first}). Unlocks taken back: {policy.RULES['qa_fix'].label} for milestone #{missed} (milestone #{missed}"
        " was missed). 1 request(s) waited for you whatever you unlocked (never automatic)."
    )
    sensors = agent.sensor_fields()
    assert (sensors["digest_day"], sensors["digest"], sensors["unlocks"]) == (day.isoformat(), written[0]["text"], 2)
    assert agent.dashboard()["audit"]["digest"]["day"] == day.isoformat()
    assert any(e["message"].startswith(f"Daily digest: {day.isoformat()}: ") for e in agent_events(agent))
    with agent.db.connection() as conn:
        quiet, _ = audit.digest(conn, agent.scope(), agent.clock, day - timedelta(days=10))
    assert quiet.startswith(f"{(day - timedelta(days=10)).isoformat()}: Ember's code carried out nothing. 1 held")
