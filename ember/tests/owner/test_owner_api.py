"""The owner's HTTP endpoints: ledger entries, corrections, pause and resume, sensors; and (0.42.3) taking back every
unlock and pinning a lesson, whose logic only was tested (test_audit, test_lessons)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import LoadedSettings, Settings
from tests.agent_helpers import rows
from tests.api_helpers import grant, post, refused_but_to_the_owner
from tests.conftest import HA_CORE
from tests.owner.test_owner_identity import OWNER, TABLET, as_user

LESSON = "Never hand my owner design work."


def test_grant_round_trip(ingress_client: TestClient) -> None:
    body = grant("7.50", note="for a domain")
    first = post(ingress_client, "api/ledger/grant", body)
    assert first.status_code == 201
    assert first.json()["entry"]["amount_usd"] == 7.5
    assert first.json()["entry"]["entered_by"] == "Stefan"
    assert first.json()["economy"]["balance_usd"] == 27.5
    assert post(ingress_client, "api/ledger/grant", body).status_code == 200
    changed = post(ingress_client, "api/ledger/grant", {**body, "amount": "8"})
    assert changed.status_code == 409 and changed.json()["code"] == "duplicate_key_mismatch"
    entries = ingress_client.get("api/ledger").json()["entries"]
    assert [e["type"] for e in entries] == ["owner_grant", "owner_grant"]
    dashboard = ingress_client.get("api/dashboard").json()
    assert dashboard["agent"]["balance_usd"] == 27.5
    assert any("Stefan recorded a grant of $7.50" in e["message"] for e in dashboard["events"])


def test_validation_errors_name_the_field(ingress_client: TestClient) -> None:
    response = post(ingress_client, "api/ledger/revenue", grant("5"))
    assert response.status_code == 422 and response.json()["field"] == "source"
    response = post(ingress_client, "api/ledger/grant", {"amount": 5, "idempotency_key": uuid.uuid4().hex})
    assert response.status_code == 422 and response.json()["field"] == "amount"
    assert post(ingress_client, "api/ledger/grant", None).json()["field"] == "body"
    assert post(ingress_client, "api/ledger/bonus", grant()).status_code == 404


def test_writes_need_the_csrf_header(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/ledger/grant", grant(), headers={}).status_code == 403
    assert post(ingress_client, "api/control/pause", headers={}).status_code == 403
    assert ingress_client.get("api/dashboard").json()["agent"]["balance_usd"] == 20.0


def test_home_assistant_core_can_only_read_sensors(client_factory: Callable) -> None:
    with client_factory(client=HA_CORE) as client:
        assert post(client, "api/ledger/grant", grant()).status_code == 403
        assert client.get("api/ledger").status_code == 403
        sensors = client.get("api/sensors").json()
    assert sensors["state"] == "alive"
    assert sensors["balance_usd"] == 20.0
    assert sensors["runway_days"] == 365 and sensors["runway_known"] is False
    assert sensors["mode"] == "dry_run" and sensors["dry_run"] is True


def test_confirmations(ingress_client: TestClient) -> None:
    large = post(ingress_client, "api/ledger/grant", grant("500"))
    assert large.status_code == 409 and large.json()["code"] == "unusually_large"
    body = grant("20", note="rent")
    body.pop("note")
    expense = post(ingress_client, "api/ledger/expense", {**body, "note": "everything"})
    # An agent that never spent API money doesn't die from an expense, but it is left with nothing: ask first.
    assert expense.status_code == 409 and expense.json()["state_after"] == "unfunded"
    confirmed = post(
        ingress_client, "api/ledger/expense", {**body, "note": "everything", "confirm_state_change": "unfunded"}
    )
    assert confirmed.status_code == 201
    assert ingress_client.get("api/dashboard").json()["agent"]["state"] == "unfunded"


def test_corrections_over_http(ingress_client: TestClient) -> None:
    entry = post(ingress_client, "api/ledger/grant", grant("5")).json()["entry"]
    body = {"amount": "2", "note": "typo", "idempotency_key": uuid.uuid4().hex}
    response = post(ingress_client, f"api/ledger/{entry['id']}/correct", body)
    assert response.status_code == 201 and response.json()["entry"]["amount_usd"] == -2.0
    assert (
        post(ingress_client, "api/ledger/999/correct", {**body, "idempotency_key": uuid.uuid4().hex}).status_code == 404
    )
    assert post(ingress_client, "api/ledger/0/correct", body).status_code == 422


def test_pause_and_resume(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/control/pause").json() == {"state": "paused", "paused": True}
    dashboard = ingress_client.get("api/dashboard").json()
    assert dashboard["agent"]["state"] == "paused" and dashboard["agent"]["paused"] is True
    assert dashboard["transitions"][0]["to_state"] == "paused"
    assert post(ingress_client, "api/control/resume").json()["state"] == "alive"


def test_dry_run_test_money(ingress_client: TestClient) -> None:
    response = post(ingress_client, "api/ledger/grant", grant("3", test_money=True))
    assert response.status_code == 201 and response.json()["entry"]["simulated"] is True
    agent = ingress_client.get("api/dashboard").json()["agent"]
    assert agent["balance_usd"] == 23.0 and agent["real_balance_usd"] == 20.0


def test_the_owner_takes_back_every_unlock_over_http_and_no_one_else_can(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(Settings(owner_user_ids=(OWNER,)))) as client:  # an unlock needs the option
        me = as_user(OWNER)
        due = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
        goal = post(client, "api/roadmap", {"title": "Ten sales", "measure": "10 orders", "due": due}, me).json()["id"]
        unlocked = post(client, f"api/milestones/{goal}/autonomy", {"rule": "email_reply", "level": "veto_window"}, me)
        assert unlocked.status_code == 200, unlocked.json()

        def unlocks() -> int:
            return int(client.get("api/dashboard", headers=me).json()["audit"]["unlocks"])

        assert unlocks() == 1
        path = "api/autonomy/take_back"
        refused_but_to_the_owner(client, path, {}, me, as_user(TABLET, "Wall tablet"))
        for body, field, error in (({"why": "x"}, "why", "unknown field 'why'"), (None, "body", "send a JSON object")):
            wrong = post(client, path, body, me)
            assert (wrong.status_code, wrong.json()) == (422, {"error": error, "field": field}), body
        assert unlocks() == 1  # nothing was taken back
        taken = post(client, path, {}, me)
        assert (taken.status_code, taken.json()["taken_back"]) == (200, 1)  # and its "wake": test_wake_switches
        assert unlocks() == 0
        agent = client.app.state.ember.agent  # type: ignore[attr-defined]
        assert rows(agent, "SELECT milestone_id, level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
            {"milestone_id": goal, "level": "manual", "by": f"Stefan ({OWNER})", "why": "you took back every unlock"}
        ]
        events = [e["message"] for e in client.get("api/dashboard", headers=me).json()["events"]]
        assert f"Stefan ({OWNER}) took back every unlock (1 in all)" in events
        assert post(client, path, {}, me).json()["taken_back"] == 0  # none stands


def test_the_owner_pins_a_lesson_over_http_and_no_one_else_can(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(Settings(owner_user_ids=(OWNER,)))) as client:
        agent = client.app.state.ember.agent  # type: ignore[attr-defined]
        agent.roots()[1].write("lessons.md", f"# Lessons\n\n- [#c1] {LESSON}\n- [#c2] Ask people first.\n")
        me = as_user(OWNER)
        path, body = "api/lessons/pins", {"text": f"- [#c1] {LESSON}"}  # a line of the Mind tab's lessons
        refused_but_to_the_owner(client, path, body, me, as_user(TABLET, "Wall tablet"))
        for wrong_body, status, field, error in (
            ({"lesson": LESSON}, 422, "lesson", "unknown field 'lesson'"),
            ({"text": ""}, 422, "text", "please fill in text"),
            ({"text": 5}, 422, "text", "text must be text"),
            (None, 422, "body", "send a JSON object"),
            ({"text": "Something it never learned"}, 409, "text", "that isn't one of the lessons now"),
        ):
            wrong = post(client, path, wrong_body, me)
            assert (wrong.status_code, wrong.json()) == (status, {"error": error, "field": field}), wrong_body
        assert rows(agent, "SELECT * FROM lesson_pins") == []
        pinned = post(client, path, body, me)
        [row] = rows(agent, "SELECT id, text, pinned_by, unpinned_at FROM lesson_pins")
        assert (pinned.status_code, pinned.json()) == (201, {"id": row["id"], "text": LESSON})
        assert (row["text"], row["pinned_by"], row["unpinned_at"]) == (LESSON, f"Stefan ({OWNER})", None)
        dashboard = client.get("api/dashboard", headers=me).json()
        assert [p["text"] for p in dashboard["mind"]["lesson_pins"]] == [LESSON]
        assert f"Stefan ({OWNER}) pinned a lesson: {LESSON}" in [e["message"] for e in dashboard["events"]]
        again = post(client, path, body, me)
        assert (again.status_code, again.json()) == (409, {"error": "that lesson is already pinned", "field": "text"})
