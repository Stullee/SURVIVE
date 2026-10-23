"""0.47.0 (vision/generations.md): the System tab's Next generation card. It reads the estate and what the change waits
for, records the owner's verdicts, and begins the next generation only with CSRF and the agent's name and the next
generation's number typed as the kill switch asks for the name."""

from __future__ import annotations

from fastapi.testclient import TestClient

CSRF = {"X-Ember-Request": "1"}


def test_the_card_reads_the_estate_and_begins_the_next_generation(ingress_client: TestClient) -> None:
    status = ingress_client.get("api/generation")
    assert status.status_code == 200
    found = status.json()
    assert (found["generation"], found["next"], found["estate"], found["received"]) == (1, 2, [], [])
    assert found["agent_name"] == "Ember" and found["ready"] is True
    assert [c["name"] for c in found["checks"]] == [
        "No wake cycle is running",
        "No request waits",
        "No action is under way",
        "Every product has a verdict",
        "The retired products are down",
    ]
    assert ingress_client.post("api/generation", json={"confirm": "Ember 2"}).status_code == 403  # CSRF
    wrong = ingress_client.post("api/generation", json={"confirm": "Ember"}, headers=CSRF)
    assert wrong.status_code == 422 and wrong.json()["field"] == "confirm" and '"Ember 2"' in wrong.json()["error"]
    begun = ingress_client.post("api/generation", json={"confirm": " ember   2 "}, headers=CSRF)
    assert begun.status_code == 201, begun.text
    assert begun.json()["generation"] == 2 and begun.json()["backup"].startswith("ember-generation")
    again = ingress_client.get("api/generation").json()
    assert (again["generation"], again["next"]) == (2, 3)


def test_a_verdict_names_a_product_of_the_estate(ingress_client: TestClient) -> None:
    assert ingress_client.post("api/estate/etsy_listing/1", json={"verdict": "passive"}).status_code == 403
    missing = ingress_client.post("api/estate/etsy_listing/1", json={"verdict": "passive"}, headers=CSRF)
    assert missing.status_code == 404 and "no Etsy listing 1 in the estate" in missing.json()["error"]
    unknown = ingress_client.post("api/estate/etsy_listing/1", json={"verdict": "keep", "why": "x"}, headers=CSRF)
    assert unknown.status_code == 422 and unknown.json()["field"] == "why"


def test_the_page_has_the_card_and_its_dialog(ingress_client: TestClient) -> None:
    page = ingress_client.get("/").text
    for element in (
        "generation-card",
        "generation-estate",
        "generation-checks",
        "generation-begin",
        "generation-dialog",
        "generation-confirm",
        "generation-go",
    ):
        assert f'id="{element}"' in page
