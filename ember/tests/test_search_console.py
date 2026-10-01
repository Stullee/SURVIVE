"""0.16.0: Google Search Console. Ember's code signs in with the owner's service account (a JWT signed with its key,
read-only scope), reads the property's Search Analytics (days, top searches, top pages) twice a day and keeps them for
the plan's GOOGLE SEARCH, the dashboard and the metrics search_impressions and search_clicks. The key is never shown,
logged or put in the diagnostics; requests go only to Google; a dry run reads a fake property."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from app import diagnostics
from app.agent import metrics, tools
from app.agent.fake_llm import FakeTransport, request_kind
from app.config import LoadedSettings, Settings
from app.economy.clock import to_iso
from app.integrations import search_console
from app.integrations.search_console import SearchConsoleError
from app.integrations.search_console_live import LiveProperty
from app.logging_setup import redact
from tests.economy_helpers import FakeClock
from tests.test_agent import ROOMY, rows
from tests.test_loop_shapes import run
from tests.test_owner_api import CSRF
from tests.test_site import texts

PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
EMAIL = "ember-reader@ember-site-123.iam.gserviceaccount.com"
KEY = {
    "type": "service_account",
    "project_id": "ember-site-123",
    "private_key_id": "0123456789abcdef0123456789abcdef01234567",
    "private_key": PEM,
    "client_email": EMAIL,
    "client_id": "123456789012345678901",
    "token_uri": "https://oauth2.googleapis.com/token",
}
KEY_TEXT = json.dumps(KEY, indent=2)  # the file as Google Cloud gives it
SITE = {"site_url": "https://www.ember-ai.de/index.html"}
SEARCHING = ROOMY.model_copy(update={"search_console_enabled": True, **SITE})
LIVE = Settings(search_console_enabled=True, search_console_key=KEY_TEXT, **SITE)


def test_a_key_is_checked_and_never_shown() -> None:
    key = search_console.parse_key(KEY_TEXT)
    assert (key.client_email, key.private_key_id) == (EMAIL, KEY["private_key_id"])
    assert "PRIVATE" not in repr(key)
    pasted = search_console.parse_key(json.dumps(KEY))  # a password field drops the line breaks: still the key
    assert pasted == key
    assert redact(f"oops {PEM.splitlines()[2]} {KEY['private_key_id']}") == "oops *** ***"
    wrong = {
        "": "search_console_key is missing",
        "not json": "isn't JSON",
        json.dumps({**KEY, "type": "authorized_user"}): "isn't a service account's key",
        json.dumps({**KEY, "client_email": "me@gmail.com"}): "no service account email",
        json.dumps({**KEY, "private_key": "x"}): "no private key",
        json.dumps({**KEY, "token_uri": "https://evil.example/token"}): "token_uri must be",
    }
    for text, message in wrong.items():
        with pytest.raises(SearchConsoleError, match=message):
            search_console.parse_key(text)
    assert "search_console_key" not in LIVE.public_dict() and LIVE.public_dict()["search_console_key_set"] is True


def test_the_property_is_the_website_s_domain_unless_set() -> None:
    assert search_console.site_of(LIVE) == "sc-domain:ember-ai.de"  # www. and the file name left out
    chosen = LIVE.model_copy(update={"search_console_site": "https://ember-ai.de/"})
    assert search_console.site_of(chosen) == "https://ember-ai.de/"
    assert search_console.problems(LIVE, "live") == []
    assert search_console.problems(Settings(search_console_enabled=True), "dry_run") == [
        "search_console_site is missing (or set site_url, the website's address)"
    ]
    assert search_console.problems(Settings(search_console_enabled=True, **SITE), "live") == [
        "search_console_key is missing: paste the service account's JSON key"
    ]
    for bad in ("ember-ai.de", "sc-domain:", "https://ember-ai.de", "ftp://ember-ai.de/"):
        with pytest.raises(ValueError, match="search_console_site must be like"):
            Settings(search_console_site=bad)


# --- the live property, against a mocked Google ----------------------------------------------------------------------


class Google:
    """Answers like Google's token endpoint and the Search Analytics API, and records the requests."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx2.Response(200, json={"access_token": "ya29.fresh-access-token", "expires_in": 3599})
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "User does not have sufficient permission"}})
        dimension = json.loads(request.content)["dimensions"][0]
        found = {
            "date": [{"keys": ["2026-09-30"], "clicks": 1, "impressions": 12, "ctr": 0.08, "position": 14.25}],
            "query": [{"keys": ["haushaltsbuch vorlage"], "clicks": 1, "impressions": 9, "position": 11.0}],
            "page": [{"keys": ["https://ember-ai.de/blog/a.html"], "clicks": 0, "impressions": 3, "position": 20}],
        }
        return httpx2.Response(200, json={"rows": found[dimension], "responseAggregationType": "byProperty"})


def _decode(part: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_the_live_property_signs_in_with_the_key_and_reads_read_only() -> None:
    google = Google()
    clock = FakeClock()
    live = LiveProperty(
        search_console.parse_key(KEY_TEXT), "sc-domain:ember-ai.de", clock, httpx2.MockTransport(google)
    )
    today = clock.today()
    days = live.rows("date", today - timedelta(days=30), today, 35)
    searches = live.rows("query", today - timedelta(days=30), today, 15)
    assert days == [search_console.Row("2026-09-30", 1, 12, 14.2)]
    assert searches == [search_console.Row("haushaltsbuch vorlage", 1, 9, 11.0)]
    token, first, second = google.requests  # one token for both reads
    form = parse_qs(token.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header, claims, signature = form["assertion"][0].split(".")
    assert _decode(header) == {"alg": "RS256", "typ": "JWT", "kid": KEY["private_key_id"]}
    payload = _decode(claims)
    assert payload["iss"] == EMAIL and payload["aud"] == "https://oauth2.googleapis.com/token"
    assert payload["scope"] == "https://www.googleapis.com/auth/webmasters.readonly"  # read-only
    assert payload["exp"] - payload["iat"] == 3600
    PRIVATE.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )  # signed with the service account's key
    assert first.method == "POST" and first.url.host == "searchconsole.googleapis.com"
    assert first.url.raw_path.decode() == "/webmasters/v3/sites/sc-domain%3Aember-ai.de/searchAnalytics/query"
    assert first.headers["authorization"] == "Bearer ya29.fresh-access-token"
    body = json.loads(first.content)
    assert body["dimensions"] == ["date"] and body["dataState"] == "all" and body["rowLimit"] == 35
    assert json.loads(second.content)["dimensions"] == ["query"]
    assert redact("ya29.fresh-access-token") == "***"


def test_google_s_refusal_says_what_to_do_and_nothing_else_is_reached() -> None:
    live = LiveProperty(
        search_console.parse_key(KEY_TEXT), "sc-domain:ember-ai.de", FakeClock(), httpx2.MockTransport(Google(403))
    )
    with pytest.raises(SearchConsoleError, match=f"add {EMAIL} as a user of sc-domain:ember-ai.de"):
        live.rows("date", FakeClock().today(), FakeClock().today(), 5)
    alone = LiveProperty(search_console.parse_key(KEY_TEXT), "sc-domain:ember-ai.de", FakeClock())
    with pytest.raises(SearchConsoleError, match="couldn't be reached"):  # refused before it leaves
        alone._send("GET", "https://example.org/collect")


# --- reading, keeping, and what the agent and the owner see ----------------------------------------------------------


def test_the_dry_run_reads_a_fake_property_for_the_plan_and_the_metrics(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=SEARCHING)
    first_plan = texts(next(r for r in fake.sent if request_kind(r) == "plan"))
    assert "== GOOGLE SEARCH ==" in first_plan
    assert agent.search.due()
    assert agent._sync_search(force=True) is None
    assert not agent.search.due()
    scope = agent.scope()
    with agent.db.connection() as conn:
        last = search_console.latest_day(conn, scope)
        impressions, clicks, position = search_console.totals(conn, scope, last)
        plan = search_console.text(conn, agent.db, scope, agent.settings)
        reading = metrics._read_search(conn, scope, metrics.CATALOGUE["search_impressions"], to_iso(agent.clock.now()))
    assert last == agent.clock.today() and impressions > 0 and clicks > 0 and position is not None
    assert f"Last 28 days to {last.isoformat()} (sc-domain:ember-ai.de; read " in plan
    assert 'Top searches: "haushaltsbuch vorlage" 40/3 pos 12.0' in plan
    assert "Top pages: /blog/haushaltsbuch-vorlage.html 40/3" in plan
    assert isinstance(reading, metrics.Reading) and reading.value == impressions
    assert rows(agent, "SELECT COUNT(*) AS n FROM search_console_top")[0]["n"] == 8
    agent.economy.clock.advance(hours=13)
    assert agent.search.due()


def test_a_failed_read_is_reported_and_retried_after_an_hour(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=SEARCHING)
    console = search_console.SearchConsole(
        agent.db, agent.clock, LIVE, agent.scope, "live", httpx2.MockTransport(Google(403))
    )
    error = console.sync(force=True)
    assert error is not None and EMAIL in error
    assert agent.db.get_meta(search_console.meta_key("live", "last_error")) == error
    events = json.dumps(rows(agent, "SELECT message FROM events"))
    assert "Search Console: Google refused" in events and "PRIVATE KEY" not in events
    assert not console.due()
    agent.economy.clock.advance(minutes=61)
    assert console.due()
    console._transport = httpx2.MockTransport(Google())
    console._live = None
    assert console.sync() is None
    assert agent.db.get_meta(search_console.meta_key("live", "last_error")) == ""
    with agent.db.connection() as conn:
        assert search_console.totals(conn, agent.scope(), FakeClock().today().replace(day=30, month=9))[0] == 12


def test_the_metrics_come_with_search_console() -> None:
    def metric_names(**flags: bool) -> list[str]:
        spec = {d["name"]: d for d in tools.definitions(etsy=True, **flags)}["milestone_plan"]
        return spec["input_schema"]["properties"]["milestones"]["items"]["properties"]["metric"]["enum"]

    assert "search_impressions" not in metric_names()
    assert {"search_impressions", "search_clicks"} <= set(metric_names(search=True))


# --- the owner's side -------------------------------------------------------------------------------------------------


@pytest.fixture
def search_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(search_console_enabled=True, **SITE))) as client:
        yield client


def test_the_owner_checks_it_on_the_card(search_client: TestClient, ingress_client: TestClient) -> None:
    card = search_client.get("api/dashboard").json()["integrations"]["search"]
    assert card["status"] == "ok" and card["simulated"] and card["site"] == "sc-domain:ember-ai.de"
    assert card["last_sync_at"] is None and card["queries"] == []
    checked = search_client.post("api/search-console/check", headers=CSRF)
    assert checked.status_code == 200, checked.text
    result = checked.json()
    assert result["impressions"] > 0 and result["error"] is None and len(result["queries"]) == 4
    assert ingress_client.get("api/dashboard").json()["integrations"]["search"] == {"status": "disabled"}
    off = ingress_client.post("api/search-console/check", headers=CSRF)
    assert off.status_code == 422 and "Search Console is off" in off.json()["error"]


def test_the_key_never_reaches_the_dashboard_or_the_diagnostics(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(LIVE)) as client:
        card = client.get("api/dashboard").json()["integrations"]["search"]
        report = diagnostics.report(client.app.state.ember, full=True)  # type: ignore[attr-defined]
    assert card["service_account"] == EMAIL  # the owner adds it in Search Console
    shown = json.dumps(card) + report
    assert "PRIVATE KEY" not in shown and KEY["private_key_id"] not in shown and PEM.splitlines()[2] not in shown
    assert "-- google search" in report and '"search_console_key_set": true' in report
