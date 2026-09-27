"""The owner's HTTP endpoints: ledger entries, corrections, pause and resume, sensors."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import HA_CORE

CSRF = {"X-Ember-Request": "1", "X-Remote-User-Display-Name": "Stefan"}


def post(client: TestClient, path: str, body: dict | None = None, headers: dict | None = None):  # noqa: ANN201
    return client.post(path, json=body, headers=CSRF if headers is None else headers)


def grant(amount: str = "5", **extra: object) -> dict:
    return {"amount": amount, "idempotency_key": uuid.uuid4().hex, **extra}


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
