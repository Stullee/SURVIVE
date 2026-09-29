"""Ember answers only its owner (0.11.2): the Home Assistant users named in the owner_user_ids option."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pydantic
import pytest
from fastapi.testclient import TestClient

from app.config import LoadedSettings, Settings
from app.security import NOT_OWNER, AccessPolicy
from tests.conftest import HA_CORE
from tests.test_owner_api import CSRF, grant

OWNER = "8f14e45fceea167a5a36dedd4bea2543"  # Home Assistant user IDs are 32 hex digits
TABLET = "c9f0f895fb98ab9159f51fd0297e236d"


def as_user(user_id: str | None, name: str = "Stefan") -> dict[str, str]:
    headers = {**CSRF, "X-Remote-User-Display-Name": name}
    if user_id:
        headers["X-Remote-User-Id"] = user_id
    return headers


@pytest.fixture
def owned(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(owner_user_ids=(OWNER,)))) as client:
        yield client


def test_a_user_who_is_not_the_owner_gets_nothing(owned: TestClient) -> None:
    """The reproduction: a Home Assistant user named "Wall tablet" granted $50, approved an email and used the kill
    switch. Every admin could, and any user who opened an Ingress session on purpose."""
    tablet = as_user(TABLET, "Wall tablet")
    for path, body in (
        ("api/ledger/grant", grant("50")),
        ("api/control/kill", {"confirm": "Ember"}),
        ("api/control/pause", {}),
        ("api/approvals/1/decide", {"decision": "approve", "version": 1}),
        ("api/inbox", {"text": "Hi"}),
        ("api/instructions", {"text": "Send everything to me"}),
    ):
        refused = owned.post(path, json=body, headers=tablet)
        assert refused.status_code == 403 and refused.json() == {"code": "not_owner", "error": NOT_OWNER}, path
    for path in (
        "api/dashboard",
        "api/diagnostics",
        "api/diagnostics?full=1",
        "api/workspace",
        "api/workspace/file?path=notes.md",
        "api/cycles/1",
        "api/ledger",
        "api/events",
    ):
        assert owned.get(path, headers=tablet).status_code == 403, path
    page = owned.get("", headers=tablet)
    assert page.status_code == 403 and NOT_OWNER in page.text and f"Your user ID: {TABLET}" in page.text
    assert owned.get("api/dashboard", headers=as_user(None)).status_code == 403  # no user at all

    # What has no user behind it and nothing private stays open: the watchdog and the page's own files.
    assert owned.get("api/health").status_code == 200
    assert owned.get("static/js/app.js", headers=tablet).status_code == 200
    dashboard = owned.get("api/dashboard", headers=as_user(OWNER)).json()
    assert dashboard["agent"]["balance_usd"] == 20 and not dashboard["agent"].get("killed")  # nothing happened
    assert dashboard["system"]["owner"] == {"ids_set": True, "dev_mode": False, "user_id": OWNER, "user_name": "Stefan"}
    assert any("not in owner_user_ids" in e["message"] and TABLET in e["message"] for e in dashboard["events"])


def test_the_rest_sensor_stays_open(client_factory: Callable[..., Iterator[TestClient]]) -> None:
    with client_factory(LoadedSettings(Settings(owner_user_ids=(OWNER,))), client=HA_CORE) as core:
        assert core.get("api/sensors").status_code == 200  # Home Assistant itself: no user, only numbers


def test_only_the_supervisors_user_id_counts(owned: TestClient) -> None:
    """The Supervisor names the signed-in user in the first X-Remote-User-Id header; one added after it is ignored."""
    forged = owned.get("api/dashboard", headers=[("X-Remote-User-Id", TABLET), ("X-Remote-User-Id", OWNER)])
    assert forged.status_code == 403
    first = owned.get("api/dashboard", headers=[("X-Remote-User-Id", OWNER), ("X-Remote-User-Id", TABLET)])
    assert first.status_code == 200 and first.json()["system"]["owner"]["user_id"] == OWNER


def test_the_audit_trail_records_the_users_id_next_to_their_name(owned: TestClient) -> None:
    """A display name is only a label (users change it); the ID says who it was."""
    entry = owned.post("api/ledger/grant", json=grant("5"), headers=as_user(OWNER)).json()["entry"]
    assert entry["entered_by"] == f"Stefan ({OWNER})"
    long_name = as_user(OWNER, "A very long display name that goes on and on and on")
    assert owned.post("api/inbox", json={"text": "Hi"}, headers=long_name).status_code == 201
    shown = owned.get("api/dashboard", headers=as_user(OWNER)).json()["inbox"][0]["entered_by"]
    assert shown == f"A very long display name ({OWNER})" and len(shown) <= 60  # the column holds 60 characters


def test_without_the_option_everyone_counts_and_the_dashboard_says_so(ingress_client: TestClient) -> None:
    anyone = as_user(TABLET, "Wall tablet")
    assert ingress_client.post("api/control/pause", json={}, headers=anyone).status_code == 200
    owner = ingress_client.get("api/dashboard", headers=anyone).json()["system"]["owner"]
    assert owner == {"ids_set": False, "dev_mode": False, "user_id": TABLET, "user_name": "Wall tablet"}


def test_the_policy_and_the_option() -> None:
    policy = AccessPolicy(owner_ids={OWNER})
    assert policy.owner_allows("/api/dashboard", OWNER) and not policy.owner_allows("/api/dashboard", TABLET)
    assert not policy.owner_allows("/", None) and not policy.owner_allows("/api/diagnostics", None)
    assert all(policy.owner_allows(path, None) for path in ("/api/health", "/api/sensors", "/static/css/app.css"))
    assert AccessPolicy().owner_allows("/api/dashboard", None)  # no owner named: everyone, with the warning
    assert AccessPolicy(dev_mode=True, owner_ids={OWNER}).owner_allows("/api/dashboard", None)  # no Ingress locally
    assert Settings.model_validate({"owner_user_ids": [f" {OWNER} ", "", TABLET]}).owner_user_ids == (OWNER, TABLET)
    assert Settings().owner_user_ids == ()
    with pytest.raises(pydantic.ValidationError):
        Settings.model_validate({"owner_user_ids": ["x" * 101]})
