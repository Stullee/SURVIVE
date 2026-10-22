"""The owner's HTTP calls in the tests: the headers of a request the dashboard sends, a ledger grant, and (0.42.3) the
refusals of a write that only the owner's dashboard may send."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.security import NOT_OWNER
from tests.conftest import HA_CORE

CSRF = {"X-Ember-Request": "1", "X-Remote-User-Display-Name": "Stefan"}


def post(client: TestClient, path: str, body: dict | None = None, headers: dict | None = None):  # noqa: ANN201
    return client.post(path, json=body, headers=CSRF if headers is None else headers)


def grant(amount: str = "5", **extra: object) -> dict:
    return {"amount": amount, "idempotency_key": uuid.uuid4().hex, **extra}


def refused_but_to_the_owner(client: TestClient, path: str, body: dict | None, owner: dict, stranger: dict) -> None:
    """The owner's write to ``path`` is refused without the X-Ember-Request header (a cross-site form can't add it),
    from outside Ingress (Home Assistant's host network, to the same running app) and from a Home Assistant user who
    isn't in owner_user_ids. ``owner`` and ``stranger``: their requests' headers, the X-Ember-Request header in both."""
    missing = client.post(path, json=body, headers={k: v for k, v in owner.items() if k != "X-Ember-Request"})
    assert (missing.status_code, missing.text) == (403, "Missing X-Ember-Request header"), path
    outside = TestClient(client.app, client=HA_CORE)  # no startup of its own: ``client`` runs the app
    try:
        refused = outside.post(path, json=body, headers=owner)
    finally:
        outside.close()
    assert (refused.status_code, refused.text) == (403, "Forbidden"), path
    other = client.post(path, json=body, headers=stranger)
    assert (other.status_code, other.json()) == (403, {"code": "not_owner", "error": NOT_OWNER}), path
