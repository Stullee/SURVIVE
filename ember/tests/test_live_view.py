"""0.16.0: Ember live on the owner's website. Ember's code renders a page, a banner for the home page and the balance
chart from its own numbers and uploads them over the blog's SFTP login every 15 minutes (at once when the life state
or the owner's choice of parts changes), without an approval. The owner chooses the parts; texts of the agent's are
shown only where privacy.Masker finds nothing; every file is checked before it goes up, and only the live view's files
are written. Switched off again, the page says so."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import store, ventures
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.clock import to_iso
from app.integrations import live_view, sftp
from app.products import blog, live
from tests.test_agent import ROOMY, rows
from tests.test_loop_shapes import run
from tests.test_owner_api import CSRF

SITE_DATA = {
    "site_url": "https://example.org",
    "site_owner_name": "Stefan Muster",
    "site_address": "Musterstraße 1, 12345 Berlin",
    "site_email": "shop@example.org",
}
LIVE = ROOMY.model_copy(update={"live_enabled": True, **SITE_DATA})
OWNER = blog.Owner("Ember", "Stefan Muster", "Berlin", "shop@example.org", "https://example.org")
AT = datetime(2026, 10, 1, 14, 5)
DAYS = tuple((f"2026-09-{d:02d}", 20.0 - d * 0.5, 4.2 if d == 20 else 0.0, 0.0) for d in range(2, 31))


def snapshot(**changes: object) -> live.Snapshot:
    values: dict[str, object] = {
        "name": "Ember",
        "state": "alive",
        "at": AT,
        "age_days": 11.3,
        "balance": 11.4,
        "runway_days": 14.2,
        "today_spend": 0.82,
        "daily_cap": 1.5,
        "api_cost": 27.4,
        "revenue": 8.4,
        "revenue_30": 8.4,
        "grants": 30.0,
        "days": DAYS,
        "cycles": 96,
        "cycles_today": 4,
        "ventures": (("live", 'Vorlagen <script>alert("x")</script>'),),
        "milestones": (("Erster Verkauf", "2026-10-12"),),
        "listings": (live.Item("Lebenslauf-Vorlage", "https://www.etsy.com/listing/1", views=48, favorites=3),),
        "posts": (live.Item("Lebenslauf schreiben", "/blog/lebenslauf.html", day="2026-09-28"),),
        "met": 3,
        "settled": 5,
        "odds": 0.62,
        "brier": 0.21,
    }
    values.update(changes)
    return live.Snapshot(**values)  # type: ignore[arg-type]


def text_of(data: bytes) -> str:
    return data.decode()


# --- rendering -------------------------------------------------------------------------------------------------------


def test_the_page_is_the_site_s_template_with_every_part_shown() -> None:
    files = live.render(snapshot(), live.Parts(), OWNER)
    assert sorted(files) == sorted(blog.LIVE_FILES)
    assert live.check(files, OWNER) == []
    page = text_of(files[live.PAGE])
    assert f'content="{blog.CSP}"' in page and '<meta http-equiv="refresh" content="300">' in page
    assert '<link rel="canonical" href="https://example.org/live.html">' in page
    assert "Stand: <time" in page and "1. Oktober 2026, 14:05 Uhr" in page
    assert '<span class="dot dot-alive"></span><strong>Lebt</strong> · Tag 12' in page
    assert "<dd>11,40 $</dd>" in page and "<dd>14 Tage</dd>" in page
    for heading in ("Geld", "Einnahmen", "Guthaben der letzten 30 Tage", "Woran es arbeitet", "Im Shop"):
        assert f"<h2>{heading}</h2>" in page
    assert "Von seinem Menschen bekommen" in page and "3 von 5 (60 %)" in page
    assert "<script>" not in page and "&lt;script&gt;" in page  # the agent's words are escaped
    assert '<img src="/live/balance.svg"' in page
    assert "keine Daten von Kundinnen und Kunden" in page


def test_the_english_page_and_banner() -> None:
    files = live.render(snapshot(), live.Parts(), OWNER)
    assert live.check(files, OWNER) == []
    page = text_of(files[live.PAGE_EN])
    assert page.startswith('<!doctype html>\n<html lang="en">') and 'content="en_US"' in page
    assert '<link rel="canonical" href="https://example.org/en/live.html">' in page
    assert "As of <time" in page and "October 1, 2026, 14:05 Berlin time" in page
    assert "<strong>Alive</strong> · Day 12" in page and "<dd>$11.40</dd>" in page and "<dd>14 days</dd>" in page
    assert "<h2>What it is working on</h2>" in page and "(48 views, 3 favorites)" in page
    assert "(September 28, 2026, in German)" in page and "3 of 5 (60%)" in page
    assert '<img src="/live/balance-en.svg"' in page and '<a href="/en/#money">more about the money</a>' in page
    assert '<a href="/live.html" hreflang="de" lang="de">DE' in page and "Impressum" in page
    banner = text_of(files[live.BANNER_EN])
    assert 'lang="en"' in banner and "$11.40 · lasts 14 days" in banner and "See it live →" in banner
    assert "September 2, 2026" in text_of(files[live.CHART_EN])
    assert live.snippet("Ember", "en") == (
        '<a class="live-banner" href="/en/live.html"><img src="/live/banner-en.svg" width="480" height="124" '
        'alt="Ember live: state, balance and runway, updated every 15 minutes"></a>'
    )
    off = live.render_all_off(OWNER, "Ember")
    assert sorted(off) == sorted([live.PAGE, live.BANNER, live.PAGE_EN, live.BANNER_EN])
    assert "The live view is switched off right now." in text_of(off[live.PAGE_EN])


def test_the_owner_s_options_hide_parts() -> None:
    parts = live.Parts(banner=False, money=False, revenue=False, grants=False, chart=False, work=False, shop=False)
    files = live.render(snapshot(), parts, OWNER)
    assert sorted(files) == sorted([live.PAGE, live.PAGE_EN])
    page = text_of(files[live.PAGE])
    assert "11,40" not in page and "Einnahmen" not in page and "Woran es arbeitet" not in page
    assert "Im Shop" not in page and "balance.svg" not in page
    assert "Wie gut seine Prognosen sind" in page  # record stays on
    no_grants = text_of(live.render(snapshot(), live.Parts(grants=False), OWNER)[live.PAGE])
    assert "Von seinem Menschen" not in no_grants and "Guthaben" in no_grants
    banner = text_of(live.render_banner(snapshot(), live.Parts(money=False)))
    assert "$" not in banner and "Lebt" in banner


def test_the_banner_and_the_chart_are_pictures_from_numbers_only() -> None:
    banner = text_of(live.render_banner(snapshot(simulated=True), live.Parts()))
    assert banner.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="124"')
    assert "EMBER LIVE" in banner and "11,40 $ · reicht 14 Tage" in banner and "Probelauf" in banner
    assert "Stand 14:05 Uhr" in banner
    chart = live.render_chart(snapshot(), live.Parts())
    assert live.audit_svg(chart) == [] and text_of(chart).count('class="sale"') == 1
    assert live.audit_svg(chart.replace(b"<title>", b"<script>alert(1)</script><title>")) != []
    assert live.audit_svg(chart.replace(b'class="sale"', b'onload="x()" class="sale"')) != []
    assert live.audit_svg(b"<html></html>") == ["the picture doesn't start with <svg>"]


def test_a_dead_ember_gets_its_memorial() -> None:
    dead = snapshot(state="dead", born_at="2026-09-01T10:00:00Z", died_at="2026-10-01T09:00:00Z", will="Lebt wohl.")
    files = live.render(dead, live.Parts(), OWNER)
    assert live.CHART not in files
    page = text_of(files[live.PAGE])
    assert "<strong>Gestorben</strong>" in page and "<h2>Sein letzter Wille</h2>" in page and "Lebt wohl." in page
    assert "<h2>Geld</h2>" not in page
    hidden = text_of(live.render(dead, live.Parts(memorial=False), OWNER)[live.PAGE])
    assert "Lebt wohl." not in hidden and "<strong>Gestorben</strong>" in hidden
    assert "letzter Wille" in text_of(live.render_banner(dead, live.Parts()))


def test_the_snippet_links_the_banner_to_the_page() -> None:
    assert live.snippet("Ember") == (
        '<a class="live-banner" href="/live.html"><img src="/live/banner.svg" width="480" height="124" '
        'alt="Ember live: Zustand, Guthaben und Reichweite, alle 15 Minuten neu"></a>'
    )
    assert ".live-banner img" in live.STYLE


def test_only_the_live_view_s_files_join_the_blog_s() -> None:
    server = sftp.FakeServer()
    for path in blog.LIVE_FILES:
        server.write(path, b"x")
    for path in ("index.html", "style.css", "live/other.svg", "live.htm"):
        with pytest.raises(sftp.NotSent):
            server.write(path, b"x")


# --- uploading -------------------------------------------------------------------------------------------------------


def agent_with(data_dir: Path, settings: Settings = LIVE) -> Agent:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=settings)
    return agent


def test_it_goes_up_every_15_minutes_and_at_once_when_the_state_changes(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    assert agent.publish_live() == "done"
    fake = agent.blog.fake
    assert fake is not None and sorted(fake.files) == sorted([live.PAGE, live.BANNER, live.PAGE_EN, live.BANNER_EN])
    page = fake.files[live.PAGE].decode()
    assert "Probelauf: alle Zahlen sind Testgeld." in page and "Aufgewacht" not in page
    assert "The live view is on: uploaded" in json.dumps(rows(agent, "SELECT message FROM events"))
    assert agent.publish_live() is None
    agent.economy.clock.advance(minutes=14)
    assert agent.publish_live() is None
    agent.economy.clock.advance(minutes=1)
    assert agent.publish_live() == "done"
    agent.economy.set_paused(True)
    assert agent.publish_live() == "done"
    assert "<strong>Pausiert</strong>" in fake.files[live.PAGE].decode()
    card = agent.integrations()["live"]
    assert card["status"] == "ok" and card["simulated"] and card["url"] == "https://example.org/live.html"
    assert card["snippet"] == live.snippet("Ember") and card["parts"]["money"] is True


def test_the_agent_s_words_are_shown_only_where_nothing_is_masked(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        now = to_iso(agent.clock.now())
        ventures.create(conn, scope, title="Lebenslauf-Vorlagen", pitch="p", stage="building", now=now)
        ventures.create(conn, scope, title="Kunde max@example.com fragen", pitch="p", stage="building", now=now)
    shot = live_view.snapshot(agent)
    titles = [title for _, title in shot.ventures]
    assert "Lebenslauf-Vorlagen" in titles and not any("example.com" in t for t in titles)
    assert "a venture's title" in shot.notes


def test_switched_off_the_page_says_so_once(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    assert agent.publish_live() == "done"
    off = Agent(
        agent.db, LoadedSettings(LIVE.model_copy(update={"live_enabled": False})), agent.economy, cycles_enabled=True
    )
    assert off.publish_live() == "off"
    assert off.blog.fake is not None
    assert "Die Live-Ansicht ist gerade ausgeschaltet." in off.blog.fake.files[live.PAGE].decode()
    assert off.publish_live() is None
    assert off.integrations()["live"] == {"status": "disabled"}


def test_a_missing_website_option_keeps_it_waiting(data_dir: Path) -> None:
    agent = agent_with(data_dir, LIVE.model_copy(update={"site_owner_name": ""}))
    assert agent.publish_live() == "failed"
    assert agent.blog.fake is not None and agent.blog.fake.files == {}
    card = agent.integrations()["live"]
    assert card["status"] == "not_ready" and "site_owner_name is missing" in card["reason"]


# --- the dashboard ---------------------------------------------------------------------------------------------------


@pytest.fixture
def live_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(live_enabled=True, **SITE_DATA))) as client:
        yield client


def test_the_owner_s_website_card_preview_and_check(live_client: TestClient) -> None:
    integrations = live_client.get("api/dashboard").json()["integrations"]
    assert integrations["live"]["status"] == "ok"
    assert integrations["blog"]["status"] == "ok" and integrations["blog"]["enabled"] is False  # the connection
    assert live_client.post("api/blog/check", headers=CSRF).status_code == 200
    page = live_client.get("api/live/preview/live.html")
    assert page.status_code == 200 and "Ember live" in page.text
    assert 'href="https://example.org/style.css"' in page.text and "balance.svg" not in page.text  # day 1: no chart
    assert 'http-equiv="refresh"' not in page.text
    assert page.headers["content-security-policy"].startswith("sandbox allow-same-origin; default-src 'none';")
    banner = live_client.get("api/live/preview/banner.svg")
    assert banner.status_code == 200 and banner.headers["content-type"] == "image/svg+xml"
    english = live_client.get("api/live/preview/live-en.html")
    assert english.status_code == 200 and '<html lang="en">' in english.text
    assert live_client.get("api/live/preview/banner-en.svg").status_code == 200
    assert live_client.get("api/live/preview/index.html").status_code == 404


def test_nothing_while_the_live_view_is_off(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/dashboard").json()["integrations"]["live"] == {"status": "disabled"}


def test_only_live_listings_read_in_the_last_6_hours_are_shown(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    scope = agent.scope()
    cycle = agent.meter.open_cycle("owner")
    agent.meter.close_cycle(cycle)
    now = agent.clock.now()
    with agent.db.transaction() as conn:
        for n, (state, hours) in enumerate((("active", 1), ("active", 7), ("expired", 1)), start=1):
            made = store.insert_approval(
                conn,
                scope,
                int(cycle),
                to_iso(now),
                payload=f"Listing {n}",
                action=store.canonical({"n": n}),
                type="publish",
                title=f"Listing {n}",
                description="Why",
                expected_cost="none",
                expected_benefit="Buyers",
                executor="etsy_listing",
            )
            conn.execute(
                "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id,"
                " title, state, views, favorites, synced_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, 5, 1, ?)",
                (
                    scope.mode,
                    scope.session,
                    made,
                    to_iso(now),
                    to_iso(now),
                    1000 + n,
                    f"Vorlage {n}",
                    state,
                    to_iso(now - timedelta(hours=hours)),
                ),
            )
    shown = live_view.snapshot(agent).listings
    assert [item.text for item in shown] == ["Vorlage 1"]
    assert shown[0].url.endswith("/1001") and (shown[0].views, shown[0].favorites) == (5, 1)
