"""Ingress access rules, base-path handling, security headers and the JSON API."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import LoadedSettings, Settings, load_settings
from app.db import discover_migrations
from app.security import AccessPolicy, ingress_base_href, is_local_host_header
from app.version import read_version
from tests.conftest import HA_CORE, INGRESS

KEY = "sk-ant-api03-verysecretkeyvalue0987654321"


# --- base path -------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("/api/hassio_ingress/AbC-123_xyz", "/api/hassio_ingress/AbC-123_xyz/"),
        (None, "/"),
        ("", "/"),
        ("/api/hassio_ingress/", "/"),
        ('/api/hassio_ingress/abc"><script>alert(1)</script>', "/"),
        ("/api/hassio_ingress/abc/../../evil", "/"),
        ("https://evil.example/api/hassio_ingress/abc", "/"),
        ("//evil.example/x", "/"),
    ],
)
def test_ingress_base_href(header: str | None, expected: str) -> None:
    assert ingress_base_href(header) == expected


def test_index_uses_ingress_path(ingress_client: TestClient) -> None:
    response = ingress_client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/Tok_en-1"})
    assert response.status_code == 200
    assert '<base href="/api/hassio_ingress/Tok_en-1/">' in response.text
    assert "__BASE_HREF__" not in response.text and "__VERSION__" not in response.text


def test_index_ignores_malicious_ingress_header(ingress_client: TestClient) -> None:
    response = ingress_client.get("/", headers={"X-Ingress-Path": '"><script>alert(1)</script>'})
    assert '<base href="/">' in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_all_page_urls_are_relative(ingress_client: TestClient) -> None:
    """Absolute URLs would bypass the Ingress prefix."""
    html = ingress_client.get("/").text
    for url in re.findall(r'(?:src|href)="([^"]+)"', html):
        if url in ("/",):  # the base href itself outside Ingress
            continue
        assert not url.startswith(("/", "http:", "https:")), url
    js = ingress_client.get("/static/js/app.js").text
    assert "fetch(" in js
    # No string literal in the dashboard script may start with "/" or a scheme:
    # every request must stay under the Ingress base path.
    literals = re.findall(r"""["']([^"'\n]*)["']""", js)
    offending = [s for s in literals if s.startswith(("/api", "/static", "http:", "https:", "//"))]
    assert offending == []


def test_static_assets_served(ingress_client: TestClient) -> None:
    for path in (
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/js/theme.js",
        "/static/vendor/chart.umd.min.js",
    ):
        response = ingress_client.get(path)
        assert response.status_code == 200, path
    assert "Chart.js v4" in ingress_client.get("/static/vendor/chart.umd.min.js").text[:200]


# --- access policy -----------------------------------------------------------


def test_policy_ingress_only() -> None:
    policy = AccessPolicy()
    assert policy.allows("172.30.32.2", "/", "GET")
    assert policy.allows("172.30.32.2", "/api/anything", "POST")
    assert not policy.allows("172.30.33.5", "/", "GET")  # another app on the internal network
    assert not policy.allows("192.168.1.20", "/", "GET")  # the LAN
    assert not policy.allows("127.0.0.1", "/", "GET")
    assert not policy.allows(None, "/", "GET")
    assert not policy.allows("testclient", "/", "GET")


def test_policy_home_assistant_core_may_read_sensors_only() -> None:
    policy = AccessPolicy()
    assert policy.allows("172.30.32.1", "/api/sensors", "GET")
    assert not policy.allows("172.30.32.1", "/api/sensors", "POST")
    assert not policy.allows("172.30.32.1", "/", "GET")
    assert not policy.allows("172.30.32.1", "/api/dashboard", "GET")
    assert not policy.allows("172.30.33.7", "/api/sensors", "GET")


def test_policy_dev_mode_allows_direct_access() -> None:
    assert AccessPolicy(dev_mode=True).allows("172.17.0.1", "/", "GET")


def test_other_clients_are_refused(client_factory: Callable) -> None:
    with client_factory(client=("172.30.33.9", 1234)) as client:
        assert client.get("/").status_code == 403
        assert client.get("/api/dashboard").status_code == 403
        assert client.get("/api/sensors").status_code == 403
        assert client.get("/static/js/app.js").status_code == 403


def test_core_can_read_sensors(client_factory: Callable) -> None:
    with client_factory(client=HA_CORE) as client:
        response = client.get("/api/sensors")
        assert response.status_code == 200
        body = response.json()
        assert {"state", "balance_usd", "runway_days", "name", "dry_run", "updated_at"} <= body.keys()
        assert client.get("/").status_code == 403


def test_refused_requests_are_logged_once(client_factory: Callable) -> None:
    with client_factory(client=("172.30.33.9", 1234)) as client:
        client.get("/")
        client.get("/api/dashboard")
    with client_factory(client=INGRESS) as client:
        events = client.get("/api/events").json()
    refused = [e for e in events if "Refused request" in e["message"]]
    assert len(refused) == 1


def test_state_changing_requests_need_csrf_header(ingress_client: TestClient) -> None:
    # No POST routes exist yet: with the header the router answers 405, without it the middleware 403.
    assert ingress_client.post("/api/dashboard").status_code == 403
    assert ingress_client.post("/api/dashboard", headers={"X-Ember-Request": "1"}).status_code == 405


def test_security_headers(ingress_client: TestClient) -> None:
    response = ingress_client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'self'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_no_api_docs_exposed(ingress_client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert ingress_client.get(path).status_code == 404


@pytest.mark.parametrize("client", [INGRESS, ("172.30.33.9", 1234)])
def test_websockets_refused_by_the_middleware(client: tuple[str, int]) -> None:
    """Even with a websocket route behind it, the middleware refuses every websocket."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from app.security import SecurityMiddleware

    async def accept(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")

    inner = Starlette(routes=[WebSocketRoute("/ws", accept)])
    with (
        TestClient(SecurityMiddleware(inner, AccessPolicy()), client=client) as test_client,
        pytest.raises(WebSocketDisconnect),
        test_client.websocket_connect("/ws") as ws,
    ):
        ws.receive_text()


# --- API ---------------------------------------------------------------------


def test_health(ingress_client: TestClient) -> None:
    version = read_version(Path(__file__).resolve().parent.parent / "config.yaml")
    assert ingress_client.get("/api/health").json() == {"status": "ok", "version": version, "database": "ok"}


def test_dashboard_payload(ingress_client: TestClient) -> None:
    data = ingress_client.get("/api/dashboard").json()
    assert "mock" not in data
    for key in ("agent", "economy", "ledger", "memorial", "lives", "transitions", "system", "events"):
        assert key in data
    # No wake cycle has run yet; the agent's sections are real but empty.
    assert data["system"]["agent_error"] is None
    assert data["now"] is None
    for key in ("projects", "activity", "approvals", "inbox", "upgrades"):
        assert data[key] == []
    assert set(data["mind"]) == {"strategy", "identity", "lessons", "journal", "reviews", "lesson_pins"}
    assert data["mind"]["reviews"] == [] and data["mind"]["lesson_pins"] == []  # none before the second day, none yet
    assert data["mind"]["strategy"].startswith("# Strategy")
    assert data["coming_in_phase"] == {}
    assert data["badges"] == {
        "approvals_pending": 0,
        "approvals_todo": 0,
        "inbox_unread": 0,
        "upgrades_new": 0,
        "ventures_proposed": 0,
        "milestone_proposals": 0,
    }
    assert data["agent"]["cycles_enabled"] is False  # tests switch the scheduler off
    assert len(data["economy"]["days"]) == 30
    assert data["agent"]["state"] == "alive"
    assert data["agent"]["balance_usd"] == 20.0
    assert data["system"]["database"] == {"ok": True, "error": None, "schema_version": len(discover_migrations())}
    assert data["system"]["dry_run"] is True
    assert any(e["message"].startswith("Started version") for e in data["events"])


def test_scenario_parameter_is_ignored(ingress_client: TestClient) -> None:
    assert ingress_client.get("/api/dashboard", params={"scenario": "dead"}).json()["agent"]["state"] == "alive"


def test_api_key_never_in_responses(client_factory: Callable, write_options: Callable) -> None:
    write_options({"anthropic_api_key": KEY})
    with client_factory(load_settings()) as client:
        for path in ("/", "/api/dashboard", "/api/sensors", "/api/events", "/api/health"):
            assert KEY not in client.get(path).text
        assert client.get("/api/dashboard").json()["system"]["options"]["anthropic_api_key_set"] is True


def test_safe_mode_is_reported(client_factory: Callable, write_options: Callable) -> None:
    write_options({"daily_spend_cap_usd": 0.1, "cycle_spend_cap_usd": 0.5})
    with client_factory(load_settings()) as client:
        system = client.get("/api/dashboard").json()["system"]
        events = client.get("/api/events").json()
    assert system["safe_mode"] is True and system["dry_run"] is True
    assert system["config_errors"]
    assert any(e["kind"] == "config" and e["level"] == "error" for e in events)


def test_install_time_and_life_survive_restarts(client_factory: Callable) -> None:
    loaded = LoadedSettings(Settings())
    with client_factory(loaded) as client:
        first = client.get("/api/dashboard").json()
    with client_factory(loaded) as client:
        second = client.get("/api/dashboard").json()
    assert second["system"]["installed_at"] == first["system"]["installed_at"]
    assert second["agent"]["life_id"] == first["agent"]["life_id"]
    assert second["agent"]["balance_usd"] == 20.0  # the starting balance is recorded once


def test_broken_database_does_not_stop_the_dashboard(client_factory: Callable, data_dir) -> None:
    (data_dir / "ember.db").write_bytes(b"this is not a sqlite database" * 100)
    with client_factory() as client:
        assert client.get("/").status_code == 200
        data = client.get("/api/dashboard").json()
        assert data["system"]["database"]["ok"] is False
        assert data["system"]["database"]["error"]
        assert client.get("/api/health").json()["database"] == "error"


def test_unhandled_errors_are_logged_once_and_hidden(client_factory: Callable, monkeypatch) -> None:
    from app.economy.service import Economy

    def explode(*args, **kwargs):
        raise RuntimeError("kaboom " + KEY)

    monkeypatch.setattr(Economy, "dashboard", explode)
    # The TestClient re-raises unhandled errors by default: getting a response
    # at all shows the app handled the error itself.
    with client_factory() as client:
        response = client.get("/api/dashboard")
        assert response.status_code == 500
        assert response.json() == {"error": "internal error, see the system log"}
        # Error responses carry the same security headers as everything else.
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        events = client.get("/api/events").json()
    logged = [e for e in events if e["level"] == "error"]
    assert len(logged) == 1
    assert "Unhandled error on GET /api/dashboard" in logged[0]["message"]
    assert KEY not in json.dumps(logged)
    assert "kaboom" in json.dumps(logged)  # the traceback is kept for the owner, minus secrets


@pytest.mark.parametrize("path", ["/api/dashboard/", "/api/sensors/", "/static", "/api/health/"])
def test_no_redirects_out_of_ingress(ingress_client: TestClient, path: str) -> None:
    """A redirect's absolute Location would drop the /api/hassio_ingress/<token> prefix."""
    response = ingress_client.get(path, follow_redirects=False, headers={"Host": "homeassistant.local:8123"})
    assert response.status_code == 404
    assert "location" not in response.headers


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("localhost:8099", True),
        ("LOCALHOST:8099", True),
        ("127.0.0.1:8099", True),
        ("[::1]:8099", True),
        ("[::1]", True),
        ("evil.example:8099", False),
        ("localhost.evil.example", False),
        ("127.0.0.1.nip.io:8099", False),
        ("192.168.1.20:8099", False),
        ("", False),
        (None, False),
        ("localhost:8099:1", False),
    ],
)
def test_is_local_host_header(host: str | None, local: bool) -> None:
    assert is_local_host_header(host) is local


def test_dev_mode_answers_localhost_only(client_factory: Callable) -> None:
    """Blocks DNS rebinding: a web page that points its own hostname at 127.0.0.1."""
    with client_factory(dev_mode=True, client=("172.17.0.1", 40000)) as client:
        assert client.get("/api/dashboard", headers={"Host": "localhost:8099"}).status_code == 200
        assert client.get("/api/dashboard", headers={"Host": "127.0.0.1:8099"}).status_code == 200
        assert client.get("/api/dashboard", headers={"Host": "attacker.example:8099"}).status_code == 403
        response = client.post("/api/dashboard", headers={"Host": "attacker.example:8099", "X-Ember-Request": "1"})
        assert response.status_code == 403


def test_refused_request_cannot_forge_log_lines(client_factory: Callable, caplog) -> None:
    forged = "/x%0a2026-09-27 19:00:00 ERROR app.main: Anthropic API key rejected"
    with client_factory(client=("172.30.33.9", 1234)) as client:
        assert client.get(forged).status_code == 403
    refused = [r.getMessage() for r in caplog.records if "Refused request" in r.getMessage()]
    assert len(refused) == 1
    # The decoded newline is logged as the two characters backslash + n, never as a line break.
    assert "\n" not in refused[0]
    assert r"/x\n2026-09-27" in refused[0]


def test_database_failure_after_migrations_does_not_stop_the_dashboard(client_factory: Callable, monkeypatch) -> None:
    """A damaged page or a stuck lock can fail the first writes even though migrations succeeded."""
    import sqlite3

    from app.db import Database

    def damaged(self, key, value):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(Database, "set_meta_if_missing", damaged)
    with client_factory() as client:
        assert client.get("/").status_code == 200
        system = client.get("/api/dashboard").json()["system"]
    assert system["database"]["ok"] is False
    assert "malformed" in system["database"]["error"]


def test_secrets_never_reach_the_dashboard_log(client_factory: Callable, write_options: Callable) -> None:
    """A key that doesn't look like an Anthropic key is still redacted: create_app registers it."""
    custom_key = "my-proxy-key-0123456789"
    write_options({"anthropic_api_key": custom_key})
    import logging

    with client_factory(load_settings()) as client:
        logging.getLogger("some.library").warning("request headers %s", {"x-api-key": custom_key, "other": KEY})
        body = client.get("/api/events").text + client.get("/api/dashboard").text
    assert "some.library: request headers" in body
    assert custom_key not in body
    assert KEY not in body


def test_frontend_has_no_html_sinks() -> None:
    """Agent-written text will be shown here; an XSS in this page could read Home Assistant's login tokens,
    because the Ingress frame is same-origin with Home Assistant. Text must only ever go through textContent."""
    static = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
    sinks = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval)\b|new Function\(")
    offenders = []
    for path in static.rglob("*.js"):
        if "vendor" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0] if not line.strip().startswith("*") else ""
            if sinks.search(code):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


def test_page_and_api_carry_the_build_and_assets_are_revalidated(ingress_client: TestClient) -> None:
    from app.version import build_id

    page = ingress_client.get("/").text
    build = build_id()
    assert f'<meta name="ember-build" content="{build}">' in page
    assert f"static/js/app.js?v={build}" in page and "__BUILD__" not in page
    assert ingress_client.get("/api/dashboard").json()["system"]["build"] == build
    static = ingress_client.get(f"/static/js/app.js?v={build}")
    assert static.headers["cache-control"] == "no-cache"  # always checked with the server, never stale


def test_the_build_changes_with_the_static_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shutil

    from app import paths, version

    web = tmp_path / "web"
    shutil.copytree(paths.WEB_DIR, web)
    monkeypatch.setattr(paths, "WEB_DIR", web)
    version.build_id.cache_clear()
    try:
        first = version.build_id()
        (web / "static" / "js" / "app.js").write_text("// changed", encoding="utf-8")
        version.build_id.cache_clear()
        assert version.build_id() != first
    finally:
        version.build_id.cache_clear()
