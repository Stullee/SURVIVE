"""The owner's HTTP calls in the tests: the headers of a request the dashboard sends, and a ledger grant."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

CSRF = {"X-Ember-Request": "1", "X-Remote-User-Display-Name": "Stefan"}


def post(client: TestClient, path: str, body: dict | None = None, headers: dict | None = None):  # noqa: ANN201
    return client.post(path, json=body, headers=CSRF if headers is None else headers)


def grant(amount: str = "5", **extra: object) -> dict:
    return {"amount": amount, "idempotency_key": uuid.uuid4().hex, **extra}
