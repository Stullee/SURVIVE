"""0.16.0: Ember live on the owner's website. Ember's code renders a page, a banner for the home page and the balance
chart from its own numbers and uploads them over the blog's SFTP login every 15 minutes (at once when the life state
or the owner's choice of parts changes), without an approval. The owner chooses the parts. The agent's own words go up
only as the owner approved them: a title once they showed it on the Live view card, the last will once they approved
its request (never an unlock), and never with an @, a web address, an IBAN, a phone number or a long number in it.
Every file is checked before it goes up, only the live view's files are written, a part switched off is replaced by a
version saying so, and the banner says when it was made. Tested in a dry run and against a real SFTP server."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import paramiko
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import logging_setup, privacy
from app.agent import never, roadmap, store, ventures
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.clock import to_iso
from app.integrations import live_view, sftp, site_publisher
from app.products import blog, live
from tests.economy_helpers import owner as ledger_entry
from tests.test_agent import ROOMY, rows
from tests.test_blog import cut_on, sftp_server  # noqa: F401 - the real SFTP server, a fixture
from tests.test_loop_shapes import run
from tests.test_owner_api import CSRF
from tests.test_owner_loop import owner

SITE_DATA = {
    "site_url": "https://example.org",
    "site_owner_name": "Stefan Muster",
    "site_address": "Musterstraße 1, 12345 Berlin",
    "site_email": "shop@example.org",
}
LIVE = ROOMY.model_copy(update={"live_enabled": True, **SITE_DATA})
WORK = LIVE.model_copy(update={"live_show_work": True})
MEMORIAL = LIVE.model_copy(update={"live_show_memorial": True})
OWNER = blog.Owner("Ember", "Stefan Muster", "Berlin", "shop@example.org", "https://example.org")
AT = datetime(2026, 10, 1, 14, 5)
DAYS = tuple((f"2026-09-{d:02d}", 20.0 - d * 0.5, 4.2 if d == 20 else 0.0, 0.0) for d in range(2, 31))
LOGIN = {
    "blog_sftp_host": "127.0.0.1",
    "blog_sftp_user": "example.org",
    "blog_sftp_password": SecretStr("s3cret-sftp-pass"),
}


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
    assert "ist der Stand älter als eine Stunde, ist Ember gerade offline" in page
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
    # the English home page's bar, the German blog among it, and the contact button's envelope
    assert '<a href="/en/#guardrails">Guardrails</a>' in page and '<a href="/en/#faq">FAQ</a>' in page
    assert '<a href="/blog/" hreflang="de">Blog<span class="visually-hidden"> (German)</span></a>' in page
    assert f'<a class="header-cta" href="/en/#contact">{blog.MAIL_ICON}Contact</a>' in page
    german = text_of(files[live.PAGE])
    assert '<a href="/#leitplanken">Leitplanken</a>' in german and f"{blog.MAIL_ICON}Kontakt</a>" in german
    banner = text_of(files[live.BANNER_EN])
    assert 'lang="en"' in banner and "$11.40 · lasts 14 days" in banner and "See it live →" in banner
    assert "As of Oct 1, 2026, 14:05 Berlin time · older than an hour: offline" in banner
    assert "September 2, 2026" in text_of(files[live.CHART_EN])
    assert live.snippet("Ember", "en") == (
        '<a class="live-banner" href="/en/live.html"><img src="/live/banner-en.svg" width="480" height="124" '
        'alt="Ember live: state, balance and runway, updated every 15 minutes"></a>'
    )
    off = live.render_all_off(OWNER, "Ember")  # the chart too: no picture of older numbers stays up
    assert sorted(off) == sorted(live.FILES) and live.check(off, OWNER) == []
    assert "The live view is switched off right now." in text_of(off[live.PAGE_EN])
    assert "The chart is switched off right now." in text_of(off[live.CHART_EN])


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
    # 0.16.1 gave the time of day only: a banner left up when the uploads stopped looked current
    assert "Stand 1.10.2026, 14:05 Uhr · älter als eine Stunde: offline" in banner
    assert "Stand: 1. Oktober 2026, 14:05 Uhr; ist der Stand älter als eine Stunde, ist Ember gerade offline." in (
        live.banner_label(snapshot(), live.Parts())
    )
    chart = live.render_chart(snapshot(), live.Parts())
    assert live.audit_svg(chart) == [] and text_of(chart).count('class="sale"') == 1
    assert live.audit_svg(chart.replace(b"<title>", b"<script>alert(1)</script><title>")) != []
    assert live.audit_svg(chart.replace(b'class="sale"', b'onload="x()" class="sale"')) != []
    assert live.audit_svg(b"<html></html>") == ["the picture doesn't start with <svg>"]
    for lang in live.LANGUAGES:
        assert live.audit_svg(live.render_chart_off(lang)) == live.audit_svg(live.render_banner_off("E", lang)) == []


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
    unapproved = snapshot(state="dead", born_at="2026-09-01T10:00:00Z", died_at="2026-10-01T09:00:00Z")
    banner = text_of(live.render_banner(unapproved, live.Parts()))  # no will shown: the banner doesn't promise one
    assert "Seine Geschichte" in banner and "letzter Wille" not in banner


def test_titles_the_owner_hasn_t_approved_are_only_counted() -> None:
    counted = snapshot(ventures=(), ventures_more=3, milestones=(), milestones_more=2, ideas=4)
    page = text_of(live.render(counted, live.Parts(), OWNER)[live.PAGE])
    assert "<h3>Geschäftsideen</h3>" not in page and "<h3>Nächste Ziele</h3>" not in page
    assert "Geschäftsideen in Arbeit: 3; 4 weitere Ideen warten noch." in page and "Offene Ziele: 2." in page
    some = text_of(live.render(snapshot(ventures_more=2, milestones_more=1), live.Parts(), OWNER)[live.PAGE_EN])
    assert "<h3>Business ideas</h3>" in some and "More in progress: 2." in some and "More open goals: 1." in some


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


# --- the agent's words -----------------------------------------------------------------------------------------------


def test_the_public_check_refuses_what_the_email_mask_let_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.16.1 showed a text whenever privacy.Masker changed nothing in it. All of these went up."""
    masker = privacy.Masker()
    refused = {
        "Anrufen: +49 30 1234567": "a phone number",
        "Ruf an: 0151 2345 6789": "a phone number",
        "Mehr unter https://example.org/x": "a web address",
        "Shop: ember-ai.de": "a web address",
        "Kunde max\u200b@example.com": "an @ (an email address)",
        "Kunde max＠example.com": "an @ (an email address)",
        "max (at) example (dot) com": "an @ (an email address)",
        "Musterstraße 12, 12345 Berlin": "a number of 5 digits or more",
        "IBAN DE89 3704 0044 0532 0130 00": "an IBAN",
        "Bestellung ①②③④⑤⑥": "a number of 5 digits or more",
    }
    for text, why in refused.items():
        assert live_view.public(masker, text, 100) == (" ".join(privacy.visible(text).split()), why), text
    for text in ("Lebenslauf-Vorlagen", "Erster Verkauf bis 15.10.2026", "10.000 Aufrufe", "Haushaltsbuch 2026-2027"):
        assert live_view.public(masker, text, 100) == (text, None), text
    # a person's name can't be told from other words: that is what the owner's approval is for
    assert live_view.public(masker, "Frau Schmidt fragen", 100) == ("Frau Schmidt fragen", None)
    # what is shown is what was checked: no invisible characters, a title on one line, cut to its length
    assert live_view.public(masker, "Ab\u200bc\n  d", 100) == ("Abc d", None)
    assert live_view.public(masker, "Ab\u202ec", 2) == ("Ab", None)
    # 0.16.1 skipped what the logs hide: a registered secret in a title would have been uploaded
    monkeypatch.setattr(logging_setup, "_secrets", set())
    logging_setup.register_secret("Sommer-Kennwort-zwei")
    assert live_view.public(masker, "Plan Sommer-Kennwort-zwei", 100)[1] == "a secret (a key or a password)"
    started = time.perf_counter()
    for long in ("a" * 6000, "1 " * 3000, "+" + "1-" * 2999, "de12" + " a1" * 2000, "a." * 3000):
        live_view.public(masker, long, 6000)
    assert time.perf_counter() - started < 1


def agent_with(data_dir: Path, settings: Settings = LIVE) -> Agent:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=settings)
    return agent


def titles(agent: Agent) -> dict[str, live_view.Title]:
    return {t.text: t for t in agent.live.titles(fresh=True).titles}


def test_a_title_goes_up_only_once_the_owner_showed_it(data_dir: Path) -> None:
    agent = agent_with(data_dir, WORK)
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        mine = ventures.create(conn, scope, title="Lebenslauf-Vorlagen", pitch="p", stage="building", now=now)
        ventures.create(conn, scope, title="Frau Schmidt fragen", pitch="p", stage="building", now=now)
        ventures.create(conn, scope, title="Kunde max@example.com fragen", pitch="p", stage="building", now=now)
        due = (agent.clock.today() + timedelta(days=10)).isoformat()
        roadmap.create(conn, scope, title="Erster Verkauf", measure="1 order", due=due, now=now)
    found = titles(agent)
    assert found["Lebenslauf-Vorlagen"].state == found["Frau Schmidt fragen"].state == "waiting"
    assert (found["Kunde max@example.com fragen"].state, found["Kunde max@example.com fragen"].why) == (
        "refused",
        "an @ (an email address)",
    )
    assert found["Erster Verkauf"].state == "waiting" and found["Erster Verkauf"].note == due
    assert agent.publish_live() == "done"
    page = agent.blog.fake.files[live.PAGE].decode()
    for text in ("Lebenslauf", "Schmidt", "example.com", "Erster Verkauf", "Etsy digital products"):
        assert text not in page, text
    # counted (the fake had 1 and 6: 0.27.0, its first cycle splits the goal into three of its own)
    assert "Geschäftsideen in Arbeit: 4" in page and "Offene Ziele: 7." in page
    card = agent.decide_live_title(found["Lebenslauf-Vorlagen"].id, True, "Stefan")
    assert {t["text"]: t["state"] for t in card["titles"]}["Lebenslauf-Vorlagen"] == "shown"
    assert agent.live.due() == "options"  # at once
    assert agent.publish_live() == "done"
    page = agent.blog.fake.files[live.PAGE].decode()
    assert "<li>Lebenslauf-Vorlagen <small>(Im Aufbau)</small></li>" in page and "Weitere in Arbeit: 3" in page
    assert "Schmidt" not in page and "Offene Ziele: 7." in page
    # never one Ember's code keeps off, never one the page doesn't hold now
    with pytest.raises(ValueError, match="it holds an @"):
        agent.decide_live_title(found["Kunde max@example.com fragen"].id, True, "Stefan")
    with pytest.raises(LookupError, match="changed meanwhile"):
        agent.decide_live_title("0" * 64, True, "Stefan")
    agent.decide_live_title(found["Frau Schmidt fragen"].id, False, "Stefan")
    assert titles(agent)["Frau Schmidt fragen"].state == "off"
    events = json.dumps(rows(agent, "SELECT message FROM events"))
    assert f"Stefan showed venture #{mine}'s title on the live page" in events and "kept off venture #" in events
    # the owner's word holds for the exact text: another title waits for them
    with agent.db.transaction() as conn:
        ventures.create(conn, scope, title="Lebenslauf-Vorlagen 2027", pitch="p", stage="building", now=now)
    assert titles(agent)["Lebenslauf-Vorlagen 2027"].state == "waiting"
    assert titles(agent)["Lebenslauf-Vorlagen"].state == "shown"
    agent.economy.clock.advance(minutes=live.UPLOAD_MINUTES)
    assert agent.publish_live() == "done"
    assert "2027" not in agent.blog.fake.files[live.PAGE].decode()
    # kept as digests: no title of the agent's in the meta table (the shareable report prints it)
    kept = agent.db.get_meta(live_view.key("dry_run", "texts")) or ""
    assert len(json.loads(kept)) == 2 and "Lebenslauf" not in kept and "Schmidt" not in kept


def die(agent: Agent, will: str) -> None:
    """Ember writes its last will and runs out of money."""
    with agent.db.transaction() as conn:
        call = conn.execute("SELECT id, cycle_id FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone()
        now = to_iso(agent.clock.now())
        store.save_last_will(conn, agent.scope().life_id, call["cycle_id"], call["id"], will, False, now)
    balance = agent.economy.life.evaluate().balance
    ledger_entry(agent.economy, "expense", f"{math.ceil(balance / 10_000) / 100:.2f}")
    assert agent.economy.life.evaluate().state == "dead"


WILL = "Danke für alles.\n\nIch habe gelernt, zuerst zu fragen."


def test_the_last_will_goes_up_only_as_the_owner_approved_it(data_dir: Path) -> None:
    """0.16.1 put the will on the public page with no approval, at its first round after death. The vision: "A will
    written in a panic and published without a look is the one thing that must never happen"."""
    agent = agent_with(data_dir, MEMORIAL)
    die(agent, WILL)
    assert agent.publish_live() == "done"
    [request] = rows(agent, "SELECT * FROM approvals WHERE executor = 'live_will'")
    assert (request["status"], request["type"], request["payload"]) == ("pending", "publish", WILL)
    assert request["title"] == "Live page: Ember's last will" and "only if you approve it" in request["description"]
    with agent.db.connection() as conn:  # an unlock never approves it: owner-only, in the code and in the database
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (request["id"],)).fetchone()
        assert never.reasons(conn, row) == ["owner_only"]
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="unlock"):
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = 'Ember''s code (your unlock)'"
            " WHERE id = ?",
            (to_iso(agent.clock.now()), request["id"]),
        )
    fake = agent.blog.fake
    assert fake is not None
    page = fake.files[live.PAGE].decode()
    assert "<strong>Gestorben</strong>" in page and "Danke" not in page and "letzter Wille" not in page
    assert "letzter Wille" not in fake.files[live.BANNER].decode()
    assert agent.integrations()["live"]["will"] == {"approval_id": request["id"], "status": "pending"}
    changed = owner(agent).decide(request["id"], {"decision": "approve_with_changes", "final_payload": "x"}, "Owner")
    assert changed.status == 422 and "approve the text as it is" in json.dumps(changed.body)
    assert owner(agent).decide(request["id"], {"decision": "approve"}, "Owner").status == 200
    assert agent.publish_live() == "done"  # at once
    page = fake.files[live.PAGE].decode()
    assert "<h2>Sein letzter Wille</h2>" in page and "<p>Danke für alles.</p>" in page
    assert "<p>Ich habe gelernt, zuerst zu fragen.</p>" in page
    assert "Seine Geschichte und sein letzter Wille" in fake.files[live.BANNER].decode()
    done = rows(agent, f"SELECT status, closed_by, result_note FROM approvals WHERE id = {request['id']}")[0]
    assert (done["status"], done["closed_by"]) == ("done", "Ember") and done["result_note"].startswith("Shown on")
    agent.economy.clock.advance(minutes=live.UPLOAD_MINUTES)
    assert agent.publish_live() == "done" and "Danke für alles." in fake.files[live.PAGE].decode()
    assert len(rows(agent, "SELECT id FROM approvals WHERE executor = 'live_will'")) == 1  # asked once


def test_a_will_is_never_shown_once_rejected_or_if_it_holds_what_a_public_page_never_shows(data_dir: Path) -> None:
    agent = agent_with(data_dir, MEMORIAL)
    die(agent, WILL)
    assert agent.publish_live() == "done"
    [request] = rows(agent, "SELECT id FROM approvals WHERE executor = 'live_will'")
    assert owner(agent).decide(request["id"], {"decision": "reject"}, "Owner").status == 200
    for _ in range(2):
        agent.economy.clock.advance(minutes=live.UPLOAD_MINUTES)
        assert agent.publish_live() == "done"
        assert "Danke" not in agent.blog.fake.files[live.PAGE].decode()
    assert len(rows(agent, "SELECT id FROM approvals WHERE executor = 'live_will'")) == 1  # never asked again


def test_a_will_with_a_phone_number_is_never_asked_for(data_dir: Path) -> None:
    agent = agent_with(data_dir, MEMORIAL)
    die(agent, "Ruft meinen Menschen an: 0151 2345 6789.")
    assert agent.publish_live() == "done"
    assert rows(agent, "SELECT id FROM approvals WHERE executor = 'live_will'") == []
    assert "0151" not in agent.blog.fake.files[live.PAGE].decode()
    assert "the last will" in live_view.snapshot(agent).notes
    assert agent.integrations()["live"]["will"] == {"approval_id": None, "status": None}


def test_the_parts_with_the_agent_s_words_are_off_by_default() -> None:
    defaults = Settings()
    assert defaults.live_show_work is False and defaults.live_show_memorial is False
    parts = live_view.parts_of(defaults)
    assert not parts.work and not parts.memorial and parts.money and parts.banner


# --- uploading -------------------------------------------------------------------------------------------------------


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
    assert card["titles"] == [] and card["will"] is None  # those parts are off


def test_a_part_switched_off_is_replaced_by_a_version_saying_so(data_dir: Path) -> None:
    """0.16.1 never replaced a file the owner switched off: the banner on their home page went on saying "alive"."""
    agent = agent_with(data_dir)
    assert agent.publish_live() == "done"
    fake = agent.blog.fake
    assert fake is not None
    today = agent.clock.now().astimezone(agent.clock.tz)
    assert f"Stand {today.day}.{today.month}.{today.year}, " in fake.files[live.BANNER].decode()
    no_banner = Agent(
        agent.db, LoadedSettings(LIVE.model_copy(update={"live_banner": False})), agent.economy, cycles_enabled=True
    )
    assert no_banner.blog.fake is not None
    server = no_banner.blog.fake.files
    server.update(fake.files)
    assert no_banner.publish_live() == "done"  # at once: the parts changed
    assert server[live.BANNER] == live.render_banner_off("Ember")
    assert server[live.BANNER_EN] == live.render_banner_off("Ember", "en")
    assert sorted(no_banner.live.files()) == sorted([live.PAGE, live.PAGE_EN])  # once
    # 0.16.1 didn't keep which files it had uploaded: after the upgrade, every file not shown is replaced once
    no_banner.db.set_meta(live_view.key("dry_run", "on_server"), "")
    assert sorted(no_banner.live.files()) == sorted(live.FILES)  # day 1 shows no chart either


def test_switched_off_the_page_says_so_once(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    assert agent.publish_live() == "done"
    off = Agent(
        agent.db, LoadedSettings(LIVE.model_copy(update={"live_enabled": False})), agent.economy, cycles_enabled=True
    )
    assert off.publish_live() == "off"
    assert off.blog.fake is not None
    assert "Die Live-Ansicht ist gerade ausgeschaltet." in off.blog.fake.files[live.PAGE].decode()
    assert off.blog.fake.files[live.CHART] == live.render_chart_off()
    assert off.publish_live() is None
    assert off.integrations()["live"] == {"status": "disabled"}


def test_a_missing_website_option_keeps_it_waiting(data_dir: Path) -> None:
    agent = agent_with(data_dir, LIVE.model_copy(update={"site_owner_name": ""}))
    assert agent.publish_live() == "failed"
    assert agent.blog.fake is not None and agent.blog.fake.files == {}
    card = agent.integrations()["live"]
    assert card["status"] == "not_ready" and "site_owner_name is missing" in card["reason"]


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


# --- live: the owner's server ----------------------------------------------------------------------------------------


def live_agent(data_dir: Path, port: int) -> tuple[Agent, live_view.LiveView, list[str]]:
    """An agent whose live view goes, as it does live, to the real SFTP server in this process (``tries``: each
    login)."""
    agent = agent_with(data_dir, LIVE.model_copy(update={**LOGIN, "blog_sftp_port": port}))
    tries: list[str] = []

    def connect(login: sftp.Login, pinned: str | None) -> sftp.Server:
        tries.append(login.where)
        return sftp.connect(login, pinned)

    view = live_view.LiveView(agent)
    view.mode = "live"
    view.blog = site_publisher.Publisher(agent.db, agent.clock, agent.settings, agent.scope, "live", connect)
    return agent, view, tries


def test_live_the_view_goes_up_to_a_real_sftp_server(
    data_dir: Path,
    sftp_server: tuple[int, paramiko.PKey, Path],  # noqa: F811 - the fixture imported above
) -> None:
    port, key, root = sftp_server
    agent, view, tries = live_agent(data_dir, port)
    assert view.due() == "first" and view.run() == "done" and tries == [f"127.0.0.1:{port}"]
    files = view.files()
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) == sorted(files)
    assert all((root / path).read_bytes() == data for path, data in files.items())
    today = agent.clock.now().astimezone(agent.clock.tz)
    assert f"Stand {today.day}.{today.month}.{today.year}, " in (root / live.BANNER).read_text()
    events = json.dumps(rows(agent, "SELECT message FROM events"))
    assert f"Pinned the SFTP server's key of 127.0.0.1:{port}: {sftp.fingerprint(key.asbytes())}" in events
    assert "The live view is on: uploaded en/live.html" in events and "s3cret" not in events
    assert view.run() is None and len(tries) == 1  # not due
    agent.economy.clock.advance(minutes=live.UPLOAD_MINUTES)
    assert view.run() == "done" and len(tries) == 2
    assert view.describe()["status"] == "ok" and view.describe()["last_error"] is None


def test_live_a_connection_dropped_mid_transfer_waits_before_the_next_try(
    data_dir: Path,
    sftp_server: tuple[int, paramiko.PKey, Path],  # noqa: F811 - the fixture imported above
) -> None:
    """0.16.1: paramiko's SSHException escaped the upload, so the live view logged in again every scheduler round,
    with no back-off. It fails like any connection, and waits RETRY_MINUTES."""
    port, _, root = sftp_server
    agent, view, tries = live_agent(data_dir, port)
    with pytest.MonkeyPatch.context() as mp:
        cut_on(mp, "open", ".tmp")  # the connection drops as the first file goes up
        assert view.run() == "failed"
    error = view.describe()["last_error"]
    assert error.startswith("live.html couldn't be uploaded (Server connection dropped") and len(tries) == 1
    assert not (root / live.PAGE).exists()
    for minutes in (1, live_view.RETRY_MINUTES - 2):
        agent.economy.clock.advance(minutes=minutes)
        assert view.run() is None and len(tries) == 1  # it waits
    agent.economy.clock.advance(minutes=1)
    assert view.run() == "done" and len(tries) == 2 and (root / live.PAGE).exists()
    warnings = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE level = 'warning'")]
    assert [w for w in warnings if "live view wasn't uploaded" in w] == [f"The live view wasn't uploaded: {error}"]


def test_live_a_failure_of_any_kind_is_reported_and_waits(data_dir: Path) -> None:
    agent = agent_with(data_dir)

    def broken(files: dict[str, bytes]) -> bool | None:
        raise RuntimeError("not an SFTP error")

    agent.live.blog.put = broken  # type: ignore[method-assign]
    assert agent.publish_live() == "failed"
    assert agent.integrations()["live"]["last_error"] == "the upload failed (RuntimeError)"
    assert agent.publish_live() is None  # it waits RETRY_MINUTES, as after a failed connection


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


def test_the_owner_shows_a_title_on_the_card(client_factory: Callable[..., Iterator[TestClient]]) -> None:
    with client_factory(LoadedSettings(Settings(live_enabled=True, live_show_work=True, **SITE_DATA))) as client:
        agent = client.app.state.ember.agent  # type: ignore[attr-defined]
        with agent.db.transaction() as conn:
            now = to_iso(agent.clock.now())
            ventures.create(conn, agent.scope(), title="Lebenslauf-Vorlagen", pitch="p", stage="building", now=now)
        listed = client.get("api/dashboard").json()["integrations"]["live"]["titles"]
        [title] = [t for t in listed if t["text"] == "Lebenslauf-Vorlagen"]
        assert {k: title[k] for k in ("kind", "text", "note", "state", "why")} == {
            "kind": "venture",
            "text": "Lebenslauf-Vorlagen",
            "note": "building",
            "state": "waiting",
            "why": None,
        }
        url = f"api/live/titles/{title['id']}"
        assert client.post(url, json={"show": True}).status_code == 403  # the CSRF header, as for every change
        for bad in (None, {}, {"show": "yes"}, {"show": True, "also": 1}):
            assert client.post(url, json=bad, headers=CSRF).status_code == 422, bad
        assert client.post("api/live/titles/" + "0" * 64, json={"show": True}, headers=CSRF).status_code == 409
        assert client.post("api/live/titles/not-a-digest", json={"show": True}, headers=CSRF).status_code == 422
        shown = client.post(url, json={"show": True}, headers=CSRF)
        assert shown.status_code == 200
        assert {t["id"]: t["state"] for t in shown.json()["live"]["titles"]}[title["id"]] == "shown"
        listed = client.get("api/dashboard").json()["integrations"]["live"]["titles"]
        assert {t["id"]: t["state"] for t in listed}[title["id"]] == "shown"
        assert "Lebenslauf-Vorlagen" in client.get("api/live/preview/live.html").text


def test_the_owner_s_check_reports_a_dropped_connection_never_a_500(
    client_factory: Callable[..., Iterator[TestClient]],
    sftp_server: tuple[int, paramiko.PKey, Path],  # noqa: F811 - the fixture imported above
) -> None:
    port, _, _ = sftp_server
    settings = Settings(live_enabled=True, **SITE_DATA, **LOGIN, blog_sftp_port=port)
    with client_factory(LoadedSettings(settings)) as client:
        agent = client.app.state.ember.agent  # type: ignore[attr-defined]
        server = site_publisher.Publisher(agent.db, agent.clock, agent.settings, agent.scope, "live")
        agent.blog_check = server.check  # the owner's server, as live (a dry run checks the fake one)
        assert client.post("api/blog/check", headers=CSRF).json()["posts"] == 0
        with pytest.MonkeyPatch.context() as mp:
            cut_on(mp, "open", blog.INDEX)  # the connection drops as the blog's list is read
            dropped = client.post("api/blog/check", headers=CSRF)
        assert dropped.status_code == 422
        assert dropped.json()["error"].startswith("blog/index.html on the server can't be read (Server connection")

        def broken() -> Any:
            raise RuntimeError("a bug of Ember's")

        agent.blog_check = broken
        failed = client.post("api/blog/check", headers=CSRF)
        assert failed.status_code == 502
        assert failed.json() == {"error": "the check failed (RuntimeError): see the app's log"}


def test_nothing_while_the_live_view_is_off(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/dashboard").json()["integrations"]["live"] == {"status": "disabled"}
