"""0.13.0 (Phase E3): the owner's website. The agent writes pages in the products' markdown; Ember's code builds a
static site from them in one audited template (every text escaped, no script, nothing from elsewhere), with the
Impressum and the privacy page made from the owner's options only; the owner previews it, downloads it and publishes
it. Ember never does."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import diagnostics
from app.agent import tools, website
from app.agent.fake_llm import FakeTransport, request_kind
from app.config import LoadedSettings, Settings
from app.products import site
from tests.test_agent import ROOMY, rows
from tests.test_etsy import call, shop_context
from tests.test_loop_shapes import run

OWNER = {
    "site_enabled": True,
    "site_owner_name": "Stefan Muster",
    "site_address": "Musterstraße 1, 12345 Berlin",
    "site_email": "shop@example.org",
}
SITE = ROOMY.model_copy(update=OWNER)
WHO = site.Owner("Stefan Muster", ("Musterstraße 1", "12345 Berlin"), "shop@example.org")
HOME = "# Planners that work\n\nA <script>alert(1)</script> **weekly** planner. [Shop](https://www.etsy.com/shop/x)\n"


def texts(request: dict[str, Any]) -> str:
    """The text blocks of a request's messages, joined."""
    return "\n".join(
        block["text"]
        for message in request["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    )


def home(**changes: Any) -> site.Page:
    fields = {
        "slug": "index",
        "title": "Printable planners",
        "description": "Planners to print at home.",
        "source": HOME,
    }
    fields.update(changes)
    return site.check(**fields)


def test_a_page_is_checked() -> None:
    assert home(slug=" Index ").slug == "index"  # lower-cased
    for changes, message in (
        ({"slug": "impressum"}, "and not datenschutz, impressum"),  # Ember's code's own pages
        ({"slug": "a b"}, "lower-case letters"),
        ({"slug": "-a"}, "lower-case letters"),
        ({"title": " "}, "1 to 80"),
        ({"description": "x" * 161}, "1 to 160"),
        ({"menu": "x" * 25}, "at most 24"),
        ({"source": ""}, "the page's text: the document is empty"),
        ({"source": "---\ntheme: nope\n---\nText\n"}, "theme must be one of"),
        ({"source": "x" * 20_001}, "at most 20,000"),
    ):
        with pytest.raises(site.SiteError, match=message):
            home(**changes)
    printed = home(source="Text.\n\n:::photo 50x50 Me\n\n:::lines 3\n")
    assert printed.notes == (
        "2 print-only blocks (space, photo frames, writing lines, page breaks) show nothing on a web page",
    )


def test_the_site_is_escaped_scriptless_and_self_contained() -> None:
    about = site.check("about", "About <us> and the planners we make at home", "Who makes them.", "Made by hand.\n")
    files = site.build([about, home()], WHO)
    assert set(files) == {"index.html", "about.html", "impressum.html", "datenschutz.html", "robots.txt"}
    page = files["index.html"].decode()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<strong>weekly</strong>" in page
    assert '<a href="https://www.etsy.com/shop/x" rel="nofollow noopener">Shop</a>' in page
    assert (
        '<a href="index.html" aria-current="page">Start</a><a href="about.html">About &lt;us&gt; and the…</a>' in page
    )
    # The stylesheet is inline and the only thing the policy allows: its hash is the style's.
    style = re.search(r"<style>(.*)</style>", page, re.DOTALL)
    assert style is not None
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in site.CSP and f'content="{site.CSP}"' in page
    assert site.CSP.startswith("default-src 'none';")
    for name, data in files.items():
        text = data.decode().lower()
        assert "<script" not in text and "http://" not in text and "<img" not in text and "<iframe" not in text, name
        assert " style=" not in text and '<link rel="stylesheet"' not in text, name
    assert 'rel="canonical"' not in page and "sitemap" not in files["robots.txt"].decode().lower()  # no address
    imprint = files["impressum.html"].decode()
    assert "Angaben gemäß § 5 DDG" in imprint and "Stefan Muster<br>Musterstraße 1<br>12345 Berlin" in imprint
    assert "mailto:shop@example.org" in imprint and "Umsatzsteuer" not in imprint and "Telefon" not in imprint
    assert '<meta name="robots" content="noindex">' in imprint and '<html lang="de">' in imprint
    privacy = files["datenschutz.html"].decode()
    assert "keine Cookies" in privacy and "Stefan Muster" in privacy and "der Anbieter, bei dem" in privacy
    english = site.build([home()], site.Owner(**{**WHO.__dict__, "language": "en", "name": "Planner Studio"}))
    page = english["index.html"].decode()
    assert (
        '<html lang="en">' in page and ">Home</a>" in page and ">Imprint</a>" in page and ">Planner Studio</a>" in page
    )
    assert '<html lang="de">' in english["impressum.html"].decode()  # German law's pages stay German


def test_the_owner_s_data_and_a_home_page_are_needed() -> None:
    with pytest.raises(site.SiteError, match="site_owner_name is missing; site_address needs .*; site_email"):
        site.build([home()], site.Owner("", ("One line",), "nobody"))
    with pytest.raises(site.SiteError, match="no home page"):
        site.build([site.check("about", "About", "Who.", "Text.\n")], WHO)
    full = site.Owner(**{**WHO.__dict__, "phone": "+49 30 1234", "vat_id": "DE123456789", "host": "Strato AG, Berlin"})
    files = site.build(
        [home(), site.check("about", "About", "Who.", "Text.\n")],
        site.Owner(**{**full.__dict__, "url": "https://planner.example.org/"}),
    )
    assert b"Sitemap: https://planner.example.org/sitemap.xml" in files["robots.txt"]
    assert files["sitemap.xml"].decode().count("<loc>") == 2  # the pages, never the legal ones
    assert b"<loc>https://planner.example.org/</loc>" in files["sitemap.xml"]
    assert b'<link rel="canonical" href="https://planner.example.org/about.html">' in files["about.html"]
    assert b"Telefon: +49 30 1234" in files["impressum.html"] and b"DE123456789" in files["impressum.html"]
    assert "bei Strato AG, Berlin gespeichert" in files["datenschutz.html"].decode()
    first, second = site.archive(files), site.archive(files)
    assert first == second  # the same site is the same download
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert sorted(archive.namelist()) == sorted(files)
        assert archive.read("index.html") == files["index.html"]
    settings = Settings(
        **{**OWNER, "site_phone": " +49 30 1234 ", "site_address": "c/o Studio, Musterstraße 1,12345 Berlin"}
    )
    assert website.owner(settings).address == ("c/o Studio", "Musterstraße 1", "12345 Berlin")
    assert website.owner(settings).phone == "+49 30 1234"


def test_what_changed_since_a_download() -> None:
    files = site.build([home()], WHO)
    before = site.fingerprints(files)
    assert site.changes(files, before) == []
    later = site.build([home(title="Planners"), site.check("about", "About", "Who.", "Text.\n")], WHO)
    changed = site.changes(later, before)
    assert changed[0] == "about.html (new)" and "index.html" in changed and "impressum.html" in changed  # the menu
    back = site.changes(files, site.fingerprints(later))
    assert back == ["datenschutz.html", "impressum.html", "index.html", "about.html (gone)"]


def test_the_options_are_checked() -> None:
    with pytest.raises(ValueError, match="site_url must be an https address"):
        Settings(site_url="http://example.org")
    with pytest.raises(ValueError, match="site_email must be an email address"):
        Settings(site_email="shop at example.org")
    assert Settings(site_url=" https://example.org/shop ", site_email="a@b.de").site_url == "https://example.org/shop"


# --- the agent's tool and the plan ---------------------------------------------------------------------------------


def test_the_tool_and_its_manual_come_with_the_website() -> None:
    off = {d["name"]: d for d in tools.definitions(etsy=True)}
    on = {d["name"]: d for d in tools.definitions(etsy=True, site=True)}
    assert "site_page" not in off and "site_page" in on
    assert "website" not in off["guide"]["input_schema"]["properties"]["topic"]["enum"]
    assert "website" in on["guide"]["input_schema"]["properties"]["topic"]["enum"]
    assert "site_page" not in {d["name"] for d in tools.definitions(etsy=True, site=True, venture=True)}
    guide = tools.guide_text("website")
    assert "At most 8 pages" in guide and "{" not in guide


def test_the_agent_writes_and_removes_pages(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=SITE)
    plan = texts(next(r for r in fake.sent if request_kind(r) == "plan"))
    assert (
        "== WEBSITE ==\nIt can't be built yet: the site has no home page yet (the page 'index').\nPages (0 of 8)"
        in plan
    )
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert "site_page" in {t["name"] for t in work["tools"]}
    ctx = shop_context(agent)
    ctx.site = website.owner(SITE)
    agent.roots()[0].write("site/index.md", HOME)
    made = call(
        ctx,
        "site_page",
        {"name": "index", "title": "Printable planners", "description": "Planners.", "source": "site/index.md"},
    )
    assert made.ok and "Page 'index' is on the site (1 of 8 pages)" in made.text, made.text
    assert "can't be built" not in made.text
    again = call(
        ctx,
        "site_page",
        {"name": "index", "title": "Planners", "description": "Planners.", "source": "site/index.md", "menu": "Home"},
    )
    assert again.ok and "rewritten" in again.text
    assert rows(agent, "SELECT slug, title, menu FROM site_pages") == [
        {"slug": "index", "title": "Planners", "menu": "Home"}
    ]
    agent.roots()[0].write("site/bad.md", "---\ntheme: nope\n---\nText\n")
    refused = call(
        ctx, "site_page", {"name": "about", "title": "About", "description": "Who.", "source": "site/bad.md"}
    )
    assert not refused.ok and "theme must be one of" in refused.text
    missing = call(ctx, "site_page", {"name": "about", "source": "site/index.md"})
    assert not missing.ok and "a page needs title, description" in missing.text
    assert call(ctx, "site_page", {"name": "index", "remove": True}).ok
    assert rows(agent, "SELECT removed_at IS NOT NULL AS gone FROM site_pages") == [{"gone": 1}]
    assert not call(ctx, "site_page", {"name": "index", "remove": True}).ok  # already off
    with agent.db.connection() as conn:
        assert "none yet" in website.planner_text(conn, agent.scope(), ctx.site)
    ctx.site = None
    off = call(ctx, "site_page", {"name": "index", "remove": True})
    assert not off.ok and "no tool called 'site_page'" in off.text


def test_the_site_holds_at_most_its_pages(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=SITE)
    with agent.db.transaction() as conn:
        for number in range(site.MAX_PAGES):
            assert website.save(conn, agent.scope(), home(slug=f"p{number}"), None, "2026-09-30T10:00:00Z")
        with pytest.raises(site.SiteError, match="8 pages already"):
            website.save(conn, agent.scope(), home(slug="more"), None, "2026-09-30T10:00:00Z")
        assert not website.save(conn, agent.scope(), home(slug="p0", title="Again"), None, "2026-09-30T11:00:00Z")
        assert website.remove(conn, agent.scope(), "p1", "2026-09-30T11:00:00Z")
        assert website.save(conn, agent.scope(), home(slug="more"), None, "2026-09-30T12:00:00Z")
        with pytest.raises(site.SiteError, match="8 pages already"):  # a removed page back is a new one on the site
            website.save(conn, agent.scope(), home(slug="p1"), None, "2026-09-30T12:00:00Z")


# --- the owner's side ---------------------------------------------------------------------------------------------


@pytest.fixture
def site_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(**OWNER))) as client:
        yield client


def _write(client: TestClient, page: site.Page) -> None:
    agent = client.app.state.ember.agent  # type: ignore[attr-defined]
    with agent.db.transaction() as conn:
        website.save(conn, agent.scope(), page, None, "2026-09-30T10:00:00Z")


def test_the_owner_previews_and_downloads_it(site_client: TestClient) -> None:
    card = site_client.get("api/dashboard").json()["integrations"]["site"]
    assert (card["status"], card["pages"], card["downloaded_at"]) == ("not_ready", [], None)
    assert "no home page" in card["reason"] and "Musterstraße" not in str(card)  # never the owner's address
    early = site_client.get("api/site/preview/index.html")
    assert early.status_code == 409 and "no home page" in early.text
    assert site_client.get("api/site/download").status_code == 409
    _write(site_client, home())
    page = site_client.get("api/site/preview/index.html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    assert policy.startswith("sandbox allow-same-origin; default-src 'none'; style-src 'sha256-")
    assert "script" not in policy and policy.endswith("frame-ancestors 'none'")
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["cache-control"] == "no-store"
    assert "&lt;script&gt;" in page.text
    assert site_client.get("api/site/preview/robots.txt").headers["content-type"].startswith("text/plain")
    for name in ("about.html", "style.css", "..%2Foptions.json", "INDEX.html"):
        assert site_client.get(f"api/site/preview/{name}").status_code == 404, name
    download = site_client.get("api/site/download")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert 'attachment; filename="website.zip"' in download.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "index.html" in archive.namelist() and "Musterstraße 1" in archive.read("impressum.html").decode()
    card = site_client.get("api/dashboard").json()["integrations"]["site"]
    assert card["status"] == "ok" and card["downloaded_at"] and card["changed"] == []
    _write(site_client, home(title="Planners, printed at home"))
    card = site_client.get("api/dashboard").json()["integrations"]["site"]
    assert card["changed"] == ["index.html"]


def test_nothing_while_the_website_is_off(ingress_client: TestClient, data_dir: Path) -> None:
    assert ingress_client.get("api/dashboard").json()["integrations"]["site"] == {"status": "disabled"}
    off = ingress_client.get("api/site/preview/index.html")
    assert off.status_code == 409 and "the website is off" in off.text
    fake = FakeTransport()
    run(data_dir, fake, cycles=1)
    assert "== WEBSITE ==" not in texts(next(r for r in fake.sent if request_kind(r) == "plan"))


def test_diagnostics_never_show_the_owner_s_data(site_client: TestClient) -> None:
    _write(site_client, home())
    report = diagnostics.report(site_client.app.state.ember, full=True)  # type: ignore[attr-defined]
    assert "Stefan Muster" not in report and "Musterstraße" not in report and "shop@example.org" not in report
    assert '"site_owner_name_set": true' in report and '"site_address_set": true' in report
    assert "-- website" in report and '"slug": "index"' in report
