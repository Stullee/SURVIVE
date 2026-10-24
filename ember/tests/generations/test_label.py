"""0.51.0 (vision/generations.md, "What the owner decides": the name): she is still Ember, with her generation named
on the dashboard, in the sensors and on the live page once there is more than one ("generation 2")."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.products import live

CSRF = {"X-Ember-Request": "1"}


def test_the_dashboard_and_the_sensors_name_her_generation(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/dashboard").json()["agent"]["generation"] == 1
    assert ingress_client.get("api/sensors").json()["generation"] == 1
    begun = ingress_client.post("api/generation", json={"confirm": "Ember 2"}, headers=CSRF)
    assert begun.status_code == 201, begun.text
    assert ingress_client.get("api/dashboard").json()["agent"]["generation"] == 2
    assert ingress_client.get("api/sensors").json()["generation"] == 2


def test_the_live_page_counts_her_days_in_her_generation() -> None:
    at = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    first = live.Snapshot(name="Ember", state="alive", at=at, life_no=2, age_days=35.2)
    second = live.Snapshot(name="Ember", state="alive", at=at, life_no=3, generation=2, age_days=2.5)
    assert live.English().age(first) == "Day 36 of life no. 2"  # as before a change
    assert live.English().age(second) == "Day 3 of generation 2"  # not "of life no. 3"
    assert live.Words().age(second) == "Tag 3 der 2. Generation"
