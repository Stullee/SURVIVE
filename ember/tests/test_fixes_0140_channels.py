"""0.14.0: the channels' small defects. A library upload had the dashboard's 10-second timeout, so a large file over
remote access failed (perhaps after the server stored it); Pinterest's refresh-token lifetime was assumed, not read,
and nothing renewed an unused connection; an approved pin was made even when its listing had stopped being live while
it waited; the Impressum could list an email address as its only contact, and the privacy page didn't say how long
the server logs are kept; a channel switched on but not set up was invisible to the agent, and a channel venture's
first test ran while its channel couldn't be used."""

from __future__ import annotations

import base64
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from app.agent import library, stages, website  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.agent.owner import Owner  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import Clock, from_iso, to_iso  # noqa: E402
from app.integrations import pinterest_publisher  # noqa: E402
from app.integrations.pinterest import FakePinterest, TokenFile  # noqa: E402
from app.integrations.pinterest_connection import PinterestConnection  # noqa: E402
from app.integrations.pinterest_live import DEFAULT_REFRESH_DAYS, connect  # noqa: E402
from app.integrations.printify import PrintifyError  # noqa: E402
from app.products import site  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_library import GUIDE, a_pdf  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_api import post  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_pinterest import (  # noqa: E402
    LISTING,
    LIVE,
    PINNING,
    live_account,
    mock,
    pin_rows,
    proposed,
    some_tokens,
)
from tests.test_site import SITE, WHO, home  # noqa: E402
from tests.test_ventures import PINTEREST, VENTURING, plan, planner_texts, venture  # noqa: E402

APP_JS = Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js"
DAY = 86_400


# --- FIX 26j: a library upload's timeout fits its size -------------------------------------------------------------


def test_a_library_upload_has_a_timeout_that_fits_its_size() -> None:
    script = APP_JS.read_text(encoding="utf-8")
    assert 'request("POST", "api/library", body, { timeout: uploadTimeout(' in script
    formula = "Math.min(UPLOAD_TIMEOUT_MAX_MS, REQUEST_TIMEOUT_MS + Math.ceil(chars / UPLOAD_CHARS_PER_SECOND) * 1000)"
    assert f"return {formula};" in script
    value = {k: int(v) for k, v in re.findall(r"var (UPLOAD_\w+|REQUEST_TIMEOUT_MS) = (\d+);", script)}
    chars = -(-library.FILE_BYTES // 3) * 4  # the largest file, base64-encoded: about 10.7 million characters
    per_second, most = value["UPLOAD_CHARS_PER_SECOND"], value["UPLOAD_TIMEOUT_MAX_MS"]
    waits = min(most, value["REQUEST_TIMEOUT_MS"] + -(-chars // per_second) * 1000)
    assert chars * 8 / 1_000_000 < waits / 1000 <= 180  # it arrives at 1 Mbit/s upstream; bounded at three minutes
    # After a timeout the server may have stored it: the list is loaded again, and the owner told so.
    assert 'err.kind === "timeout"' in script and "it may have been added all the same" in script


def test_a_retried_upload_is_stored_once(ingress_client: Any) -> None:
    upload = {"file_name": "Tags.pdf", "file_data": base64.b64encode(a_pdf(GUIDE)).decode()}
    assert post(ingress_client, "api/library", upload).status_code == 201
    again = post(ingress_client, "api/library", upload)  # the owner's retry after a timeout
    assert again.status_code == 409 and again.json()["error"].startswith("this text is in the library already: #1")
    assert [d["id"] for d in ingress_client.get("api/library").json()["items"]] == [1]


# --- FIX 26k: Pinterest's refresh-token lifetime, read from its answer ------------------------------------------


def test_the_refresh_token_s_lifetime_is_read_from_pinterest_s_answer(tmp_path: Path) -> None:
    answer = {
        "access_token": "pina_fresh-access",
        "refresh_token": "pinr_fresh-refresh",
        "expires_in": 30 * DAY,
        "refresh_token_expires_in": 60 * DAY,
    }
    server, transport = mock({("POST", "/v5/oauth/token"): answer, ("GET", "/v5/user_account"): {"username": "p"}})
    now = from_iso("2026-10-01T09:00:00Z")
    clock = Clock(lambda: now)
    store = TokenFile(tmp_path / "tokens.json")
    connect(LIVE, clock, store, "the-code", "the-verifier", transport)
    saved = store.load()
    assert saved is not None
    assert from_iso(saved.expires_at) == now + timedelta(days=30)
    assert from_iso(saved.refresh_expires_at) == now + timedelta(days=60)
    # Without it, the documented default (not a year).
    del answer["refresh_token_expires_in"], answer["expires_in"]
    connect(LIVE, clock, store, "the-code", "the-verifier", transport)
    saved = store.load()
    assert saved is not None and DEFAULT_REFRESH_DAYS == 60
    assert from_iso(saved.refresh_expires_at) == now + timedelta(days=60)
    assert from_iso(saved.expires_at) == now + timedelta(hours=1)


def test_a_renewal_without_a_new_refresh_token_keeps_its_end(tmp_path: Path) -> None:
    ends = to_iso(Clock().now() + timedelta(days=20))
    renew = {"access_token": "pina_renewed", "expires_in": 30 * DAY}  # no new refresh token: the old one stays
    account, _ = live_account(
        tmp_path,
        {("POST", "/v5/oauth/token"): renew, ("GET", "/v5/user_account"): {"username": "plannershop"}},
        expires_at=to_iso(Clock().now()),
        refresh_expires_at=ends,
    )
    account.info()
    saved = account.tokens.load()
    assert saved is not None and saved.access_token == "pina_renewed" and saved.refresh_expires_at == ends


def test_an_unused_connection_is_renewed_before_it_lapses(data_dir: Path, tmp_path: Path) -> None:
    renew = {
        "access_token": "pina_renewed",
        "refresh_token": "pinr_renewed",
        "expires_in": 30 * DAY,
        "refresh_token_expires_in": 60 * DAY,
    }
    soon = to_iso(Clock().now() + timedelta(days=3))
    account, server = live_account(tmp_path, {("POST", "/v5/oauth/token"): renew}, refresh_expires_at=soon)
    agent, _ = run(data_dir, FakeTransport(), settings=PINNING)
    agent.pins.account = lambda: account  # no pins: the sync reads nothing, but keeps the connection
    assert agent.pins.sync(force=True) is None
    assert server.form(0) == {"grant_type": "refresh_token", "refresh_token": "pinr_r3fresh-t0ken-value"}
    saved = account.tokens.load()
    assert saved is not None and saved.refresh_token == "pinr_renewed"
    assert from_iso(saved.refresh_expires_at) > Clock().now() + timedelta(days=59)
    agent.pins.sync(force=True)
    assert len(server.requests) == 1  # renewed once: it lasts 60 days again
    assert FakePinterest(Clock(), None, lambda s: None).keep_alive() is None  # the dry run's account needs nothing


def test_a_connection_made_under_0130_learns_pinterest_s_lifetime(tmp_path: Path) -> None:
    renew = {
        "access_token": "pina_renewed",
        "refresh_token": "pinr_renewed",
        "expires_in": 30 * DAY,
        "refresh_token_expires_in": 60 * DAY,
    }
    # 0.13.0 stored a year for the refresh token; its access token expired while no pin was made.
    account, server = live_account(
        tmp_path, {("POST", "/v5/oauth/token"): renew}, expires_at=to_iso(Clock().now() - timedelta(days=1))
    )
    account.keep_alive()
    saved = account.tokens.load()
    assert saved is not None and saved.access_token == "pina_renewed"
    assert from_iso(saved.refresh_expires_at) < account.clock.now() + timedelta(days=61)
    account.keep_alive()  # fresh again: nothing to renew
    assert len(server.requests) == 1


def test_a_lapsed_connection_says_so(data_dir: Path, tmp_path: Path) -> None:
    agent, _ = run(data_dir, FakeTransport())
    store = TokenFile(tmp_path / "tokens.json")
    connection = PinterestConnection(agent.db, agent.clock, LIVE, "live", 0, store)
    tokens = some_tokens(agent.clock, expires=timedelta(days=30))
    tokens.refresh_expires_at = to_iso(agent.clock.now() + timedelta(days=60))
    store.save(tokens)
    agent.clock.advance(days=45)  # the access token expired, the refresh token still renews it
    assert connection.status() == ("ok", None) and connection.account() is not None
    agent.clock.advance(days=16)  # unused for 61 days (the app was off): the connection ended
    status, reason = connection.status()
    assert status == "not_connected" and reason == (
        "The connection expired: connect your account again (System, Pinterest)."
    )
    assert connection.account() is None


# --- FIX 26l: a pin's listing, checked again before the pin is made ---------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("UPDATE etsy_listings SET state = 'inactive'", f"#{LISTING} isn't live at Etsy any more (deactivated)"),
        ("UPDATE etsy_listings SET status = 'failed'", f"#{LISTING} isn't one of your live listings any more"),
        (
            "UPDATE etsy_listings SET auto_renew = 0, ends_at = '2000-01-31T00:00:00Z'",
            f"#{LISTING} isn't live at Etsy any more (it ended on 2000-01-31)",
        ),
    ],
)
def test_an_approved_pin_whose_listing_stopped_being_live_is_not_made(data_dir: Path, change: str, reason: str) -> None:
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    with agent.db.transaction() as conn:
        conn.execute(change)
    assert agent.execute_approved() == [(request, "failed")]
    [row] = pin_rows(agent)
    assert row["pin_id"] is None and row["error"] == reason
    closed = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {request}")[0]
    assert closed == {"status": "failed", "result_note": f"Not pinned: {reason}"}
    assert agent.pinterest.account().state["boards"] == {}  # nothing reached Pinterest
    assert rows(agent, f"SELECT status FROM action_journal WHERE approval_id = {request}") == [{"status": "failed"}]
    with agent.db.connection() as conn:  # a pin refused before sending doesn't use one of the day's pins
        assert pinterest_publisher.created_today(conn, agent.clock, agent.scope()) == 0


# --- FIX 26m: the Impressum ----------------------------------------------------------------------------------------


def test_an_impressum_with_email_as_its_only_contact_is_not_built() -> None:
    email_only = site.Owner(WHO.legal_name, (), WHO.email)
    with pytest.raises(site.SiteError, match="site_address needs the street and the postcode with the town"):
        site.build([home()], email_only)
    with pytest.raises(site.SiteError, match="site_owner_name is missing"):
        site.build([home()], site.Owner("", WHO.address, WHO.email))
    # The phone stays optional (name, postal address and email are what § 5 DDG asks), but if given it is one.
    assert "Telefon" not in site.build([home()], WHO)["impressum.html"].decode()
    with pytest.raises(site.SiteError, match="site_phone must be a phone number"):
        site.build([home()], site.Owner(**{**WHO.__dict__, "phone": "call me"}))
    imprint = site.build([home()], site.Owner(**{**WHO.__dict__, "phone": "+49 30 1234567"}))["impressum.html"]
    assert "Telefon: +49 30 1234567" in imprint.decode()


def test_an_impressum_without_a_phone_is_flagged(data_dir: Path) -> None:
    """The site has no contact form: without a phone the email is the Impressum's only contact, so the card and the
    plan say so (the phone stays optional)."""
    assert WHO.advice() == ["site_phone is empty: the Impressum's only contact is the email (add a phone)"]
    assert site.Owner(**{**WHO.__dict__, "phone": "+49 30 1234567"}).advice() == []
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=SITE)
    with agent.db.transaction() as conn:
        website.save(conn, agent.scope(), home(), None, "2026-09-30T10:00:00Z")
    with agent.db.connection() as conn:
        card = website.describe(conn, agent.scope(), SITE)
        text = website.planner_text(conn, agent.scope(), website.owner(SITE))
    assert card["status"] == "ok" and card["advice"] == WHO.advice()
    assert "Ask your owner: site_phone is empty: the Impressum's only contact is the email (add a phone)." in text
    script = APP_JS.read_text(encoding="utf-8")
    assert 'arr(s.advice).length ? [h("dt", { text: "Advice" })' in script
    with agent.db.connection() as conn:
        phoned = SITE.model_copy(update={"site_phone": "+49 30 1234567"})
        assert website.describe(conn, agent.scope(), phoned)["advice"] == []


def test_the_impressum_needs_a_postal_address() -> None:
    for lines, message in (
        (("Stefan Muster", "Berlin"), "site_address needs the street and the postcode with the town"),
        (("Musterstraße 1", "Berlin"), "site_address needs the street and the postcode with the town"),
        (("12345 Berlin", "Musterstraße 1"), "site_address needs the street and the postcode with the town"),
        (("c/o Studio", "12345 Berlin"), "site_address needs the street and the postcode with the town"),
        (("Firma 2000 GmbH", "12345 Berlin"), "site_address needs the street and the postcode with the town"),
        (
            ("Postfach 12 34", "12345 Berlin"),
            r"site_address must be where you can be found \(street, postcode and town",
        ),
        (("Packstation 123", "12345 Berlin"), r"site_address must be where you can be found"),
    ):
        with pytest.raises(site.SiteError, match=message):
            site.build([home()], site.Owner(**{**WHO.__dict__, "address": lines}))
    for lines in (
        ("Musterstraße 1", "12345 Berlin"),
        ("c/o Studio", "Hauptstr. 5a", "1010 Wien"),
        ("Am Markt 3-5", "12345 Berlin"),
        ("Musterstr.1", "12345 Berlin"),
    ):
        assert site.build([home()], site.Owner(**{**WHO.__dict__, "address": lines}))


def test_a_business_id_is_named_as_one() -> None:
    imprint = site.build([home()], site.Owner(**{**WHO.__dict__, "vat_id": "DE123456789-00001"}))["impressum.html"]
    text = imprint.decode()
    assert "Wirtschafts-Identifikationsnummer gemäß § 139c Abgabenordnung: DE123456789-00001" in text
    assert "Umsatzsteuer" not in text
    vat = site.build([home()], site.Owner(**{**WHO.__dict__, "vat_id": "DE123456789"}))["impressum.html"].decode()
    assert "Umsatzsteuer-Identifikationsnummer gemäß § 27a Umsatzsteuergesetz: DE123456789" in vat


def test_the_privacy_page_says_how_long_the_server_logs_are_kept() -> None:
    privacy = site.build([home()], WHO)["datenschutz.html"].decode()
    assert "Server-Protokollen" in privacy and "Die Protokolle werden gelöscht, sobald sie dafür nicht mehr" in privacy
    assert "die genaue Frist richtet sich nach den Vorgaben des Anbieters" in privacy


# --- X21: a channel switched on but not set up ------------------------------------------------------------------


def not_set_up(agent: Agent) -> FakePinterest | None:
    """Pinterest on as in the live report: no app ID, no secret (the fake account put aside)."""
    fake = agent.pinterest._fake
    agent.pinterest.mode, agent.pinterest._fake = "live", None
    return fake


def test_the_agent_hears_that_a_channel_waits_for_its_owner(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=PINNING.model_copy(update={"printify_enabled": True}))
    not_set_up(agent)
    agent.printify.mode, agent.printify._fake = "live", None  # Printify on, no token
    agent.run_cycle("schedule")
    plan_request = next(r for r in fake.sent if request_kind(r) == "plan")
    text = plan_request["messages"][0]["content"][0]["text"]
    line = (
        "\n== PINTEREST ==\nSwitched on, but it waits for your owner's setup (pinterest_app_id is missing; "
        "pinterest_app_secret is missing, then System, Pinterest, Connect): no Pinterest tools until then. A venture "
        "it serves starts its first test only then.\n"
    )
    assert line in text
    assert "\n== PRINTIFY ==\nSwitched on, but it waits for your owner's setup (printify_api_token is missing):" in text
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert not {t["name"] for t in work["tools"]} & {"propose_pin", "pinterest_boards"}
    agent.pinterest._fake = FakePinterest(agent.clock, None, lambda s: None)
    agent.pinterest.mode = "dry_run"  # set up: its section is the account's again
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    assert "== PINTEREST ==\nYour owner's account: ember-dry-run" in text and "setup (pinterest_app_id" not in text


# 0.14.0 (ventures): these ventures have no numbers yet, so the owner backs them with confirm ("Back it anyway").


def test_a_channel_venture_s_first_test_waits_for_its_channel(data_dir: Path) -> None:
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    transport = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, transport, settings=settings)
    fake = not_set_up(agent)
    assert owner(agent).decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    assert venture(agent, PINTEREST)["test_milestone_id"] is None  # its clock doesn't run yet
    agent.clock.advance(days=40)
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert (backed["stage"], backed["test_milestone_id"]) == ("building", None)  # not parked for a missed test
    # The agent isn't asked to plan a first test that Ember's code sets.
    assert "it is building now. Its first test starts once Pinterest is set up." in planner_texts(transport)[-1]
    agent.pinterest.mode, agent.pinterest._fake = "dry_run", fake  # the owner set it up
    agent.run_cycle("schedule")
    test = venture(agent, PINTEREST)["test_milestone_id"]
    assert test is not None
    [row] = rows(agent, f"SELECT metric, target, due FROM milestones WHERE id = {test}")
    assert (row["metric"], row["target"]) == ("pin_clicks", 10)
    assert row["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()


def test_a_venture_without_a_channel_still_gets_its_test_when_backed(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    back = {"action": "back", "confirm": True}
    assert owner(agent).decide_venture(3, back, "Stefan").status == 200  # dropshipping: no channel
    assert venture(agent, 3)["test_milestone_id"] is not None


def test_the_agent_hears_why_a_set_up_channel_s_tools_are_off(data_dir: Path, monkeypatch: Any) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=PINNING.model_copy(update={"printify_enabled": True}))

    def unknown() -> None:
        raise PrintifyError("no shop of yours is connected to Etsy at Printify: connect one, or set printify_shop_id")

    monkeypatch.setattr(agent.printify, "shop", unknown)  # a token, but Printify's shop isn't connected to Etsy
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    assert "== PRINTIFY ==\nSwitched on, but no Printify tools this cycle: Printify's shop isn't known: no shop" in text
    assert "starts its first test only then" not in text.split("== PRINTIFY ==")[1].split("==")[0]
    agent.etsy.mode, agent.etsy._fake = "live", None  # the Etsy shop isn't set up: neither channel can work
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    for name in ("PINTEREST", "PRINTIFY"):
        assert f"== {name} ==\nSwitched on, but it waits for your owner's setup (the Etsy shop isn't connected)" in text


def test_a_first_test_set_before_its_channel_was_set_up_starts_again(data_dir: Path) -> None:
    """The live case: venture #2 was backed under 0.13.0 (its first test set at once) while Pinterest wasn't set up."""
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    transport = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, transport, settings=settings)
    fake = not_set_up(agent)
    assert owner(agent).decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    with agent.db.transaction() as conn:  # as 0.13.0 did when the owner backed it
        old = stages.first_test(conn, agent.scope(), venture(agent, PINTEREST), agent.clock.today(), "2026-09-30")
    agent.clock.advance(days=30)
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert (backed["stage"], backed["test_milestone_id"]) == ("building", None)  # not parked for a missed test
    [row] = rows(agent, f"SELECT status, closed_by, result FROM milestones WHERE id = {old}")
    assert (row["status"], row["closed_by"]) == ("dropped", "code") and "Pinterest isn't set up" in row["result"]
    agent.pinterest.mode, agent.pinterest._fake = "dry_run", fake  # the owner set it up
    agent.run_cycle("schedule")
    test = venture(agent, PINTEREST)["test_milestone_id"]
    assert test is not None and test != old
    [row] = rows(agent, f"SELECT status, due FROM milestones WHERE id = {test}")
    assert row["status"] == "open"
    assert row["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()


def test_a_channel_venture_backed_while_its_channel_is_ready_gets_its_test_at_once(data_dir: Path) -> None:
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=settings)
    assert agent.channels_ready() == ["pinterest"]
    ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), "Ember", ready=agent.channels_ready())
    assert ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    assert venture(agent, PINTEREST)["test_milestone_id"] is not None
    not_set_up(agent)
    assert agent.channels_ready() == []


def test_a_first_test_running_when_the_channel_is_switched_off_starts_again(data_dir: Path, monkeypatch: Any) -> None:
    """Switched off mid-test, the channel's test isn't left to pass its date and be missed the moment it is back."""
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=settings)
    ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), "Ember", ready=agent.channels_ready())
    assert ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    old = venture(agent, PINTEREST)["test_milestone_id"]
    assert old is not None
    on = agent.pinterest.status, agent.pinterest.account
    monkeypatch.setattr(agent.pinterest, "status", lambda: ("disabled", None))  # the owner switched it off
    monkeypatch.setattr(agent.pinterest, "account", lambda: None)
    agent.clock.advance(days=40)
    agent.run_cycle("schedule")
    [row] = rows(agent, f"SELECT status, closed_by FROM milestones WHERE id = {old}")
    assert (row["status"], row["closed_by"]) == ("dropped", "code")
    assert (venture(agent, PINTEREST)["stage"], venture(agent, PINTEREST)["test_milestone_id"]) == ("building", None)
    monkeypatch.setattr(agent.pinterest, "status", on[0])  # and on again
    monkeypatch.setattr(agent.pinterest, "account", on[1])
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert backed["stage"] == "building" and backed["test_milestone_id"] not in (None, old)
