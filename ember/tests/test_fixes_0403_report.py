"""0.40.3: the shareable report, the website's download and the options file (analysis-0.40.1, 4.3 W1, 4.4 W2-W4, 6.2).

1. W1: the report holds the database's lock only to read: the Masker's pattern has only the other people's words a text
   has (300 emails made 200 KB take 3 s), each text is masked once (not as a cell and again in its section), and the
   rows are shown (masked) after the lock.
2. W2: the owner's personal options (the KDP author, the SFTP user and folder) are masked wherever they are written.
3. 6.2: phone numbers and IBANs are masked in the shareable report.
4. W3: GET /api/site/download records nothing: the dashboard records the download it saved with a POST.
5. W4: an options.json nested too deep starts safe mode instead of stopping the container.
6. No GET route writes to the database.
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import diagnostics, privacy
from app.agent import store, website
from app.config import LoadedSettings, Settings, load_settings
from app.integrations import kdp
from app.products import site
from app.web import router
from tests.test_agent import ROOMY, make_agent
from tests.test_agent import plan as scripted_plan
from tests.test_fixes_0140_privacy import LABEL, section, shared_and_full, state_of
from tests.test_owner_loop import owner

CSRF = {"X-Ember-Request": "1"}
PERSONAL = ROOMY.model_copy(
    update={
        "kdp_enabled": True,
        "kdp_author": "Stefanie Federkiel",
        "blog_enabled": True,
        "blog_sftp_host": "ssh.strato.de",
        "blog_sftp_user": "stefan.mustermann",
        "blog_sftp_folder": "/home/stefan.mustermann/www",
    }
)


def started(data_dir: Path, settings: Settings = ROOMY) -> Any:
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)], settings)
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    return agent


def a_kdp_request(agent: Any, author: str) -> None:
    upload = kdp.Upload("books/a.docx", "0" * 64, 1000)
    book = kdp.Book(
        format="ebook",
        title="Haushaltsbuch 2027",
        subtitle="",
        author=author,
        description="A planner.",
        keywords=("planner",),
        categories=("Home",),
        language="German",
        price="4.99",
        manuscript=upload,
        cover=kdp.Upload("books/c.png", "0" * 64, 1000),
    )
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            "2026-10-21T10:00:00Z",
            type="sell",
            title="KDP ebook: Haushaltsbuch 2027",
            description="A book",
            payload=kdp.payload(book),
            expected_cost="none",
            expected_benefit="sales",
        )


# --- 1. W1: the report holds the database only to read, and masks each text once ---


def test_the_maskers_pattern_has_only_the_words_a_text_has() -> None:
    others = {f"Order question planner {n}": f"[subject of email #{n}]" for n in range(1, 1001)}
    others |= {"Lena Hoffmann": "[sender of email #3]", "Order question planner 7 again": "[subject of email #1001]"}
    masker = privacy.Masker("ember@mailbox.org", privacy.Redactor(), False, others)
    text = "Re: Order question planner 7 again, from Lena Hoffmann <lena@example.org>, and Order question planner 70."
    assert masker(text) == (
        "Re: [subject of email #1001], from [sender of email #3] <[email 1]>, and [subject of email #70]."
    )
    quoted = masker._pattern(text).pattern
    assert re.escape("Lena Hoffmann") in quoted and re.escape("planner 70") in quoted
    assert re.escape("planner 700") not in quoted and re.escape("planner 1000") not in quoted
    assert masker._pattern("nothing of theirs") is privacy._LINKS
    # whole only, as before: not a part of a word or an address, and a name of one word isn't one
    assert masker("XLena Hoffmann and lena.hoffmann@x.de and Order question planner 7x") == (
        "XLena Hoffmann and [email 2] and Order question planner 7x"
    )
    # 300 emails made 200 KB take 3 s, 1,000 emails 7.8 s: each text is now searched for its own words only
    report_like = '| 412 | ok | workspace_write | {"path": "books/x-v18.md"} | Woche 12 Montag Bilanz ⏎ \n' * 2500
    begun = time.perf_counter()
    assert masker(report_like) == report_like
    assert time.perf_counter() - begun < 2


@contextmanager
def watched(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """What the Masker masks during a report, and whether it masks while the database's lock is held."""
    seen: dict[str, Any] = {"chars": 0, "under_lock": 0}
    local = threading.local()

    def held(orig: Callable[..., Any]) -> Callable[..., Any]:
        @contextmanager
        def wrapper(self: Any) -> Iterator[Any]:
            local.depth = getattr(local, "depth", 0) + 1
            try:
                with orig(self) as conn:
                    yield conn
            finally:
                local.depth -= 1

        return wrapper

    call = privacy.Masker.__call__

    def masking(self: privacy.Masker, text: str) -> str:
        seen["chars"] += len(text or "")
        seen["under_lock"] += len(text or "") if getattr(local, "depth", 0) else 0
        return call(self, text)

    monkeypatch.setattr(dbmod.Database, "connection", held(dbmod.Database.connection))
    monkeypatch.setattr(dbmod.Database, "transaction", held(dbmod.Database.transaction))
    monkeypatch.setattr(privacy.Masker, "__call__", masking)
    yield seen


def test_the_report_masks_each_text_once_and_none_under_the_databases_lock(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycles and the agent's records were shown (every cell masked) under the lock, and each text was masked
    twice: 300 emails and a report of 960,000 characters took 31.6 s, the lock held 15.7 s."""
    agent = started(data_dir)
    with agent.db.transaction() as conn:
        cycle, call = conn.execute("SELECT cycle_id, MAX(id) FROM llm_calls").fetchone()
        for seq in range(40):
            conn.execute(
                "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
                " started_at, summary, result) VALUES (?, ?, ?, 'act', 'local', 'workspace_read', ?, '{}', 'ok', ?,"
                " 'read', ?)",
                (cycle, call, 1000 + seq, f"t{seq}", "2026-10-21T10:00:00Z", "Woche 12: Montag | Bilanz ⏎ " * 200),
            )
    a_kdp_request(agent, "Jane Doe")
    with watched(monkeypatch) as seen:
        report = diagnostics.report(state_of(agent))
    assert "Woche 12: Montag" in report and "\ue000" not in report and "\ue001" not in report  # no mark is left
    assert seen["under_lock"] == 0
    assert seen["chars"] < 1.2 * len(report)  # before: every cell's text, and the whole report again


def test_a_masked_cell_and_its_section_are_still_masked_whole(data_dir: Path) -> None:
    """Masked once: a cell before its cut (no part of an address is left), the rest of its section after it."""
    agent = started(data_dir)
    address = "stefan.beispiel@example.org"
    note = "x" * (diagnostics.TEXT_CHARS - 10) + address  # the cut falls inside the address
    owner(agent).send_message({"text": note}, LABEL)
    shared, full = shared_and_full(agent)
    for report in (shared, full):
        assert "stefan.beis" not in report and "[email " in report
        assert LABEL not in shared and "Felix Beispiel" not in shared


# --- 2. W2: the owner's personal options are masked wherever they are written ---


def test_the_kdp_author_and_the_sftp_login_are_masked_everywhere(data_dir: Path) -> None:
    agent = started(data_dir, PERSONAL)
    a_kdp_request(agent, "Stefanie Federkiel")
    shared, full = shared_and_full(agent)
    assert "Author: Stefanie Federkiel" in full  # the owner's own report
    for words in ("Stefanie Federkiel", "stefan.mustermann", "/home/"):
        assert words not in shared, words
    assert "Author: [the author's name]" in section(shared, "AGENT RECORDS")
    blog = section(shared, "INTEGRATIONS").split("-- blog\n", 1)[1]
    assert '"user": "[the SFTP user]"' in blog and '"folder": "[the SFTP folder]"' in blog
    for report in (shared, full):  # OPTIONS shows them as flags, in both reports
        options = section(report, "OPTIONS (public)")
        assert '"blog_sftp_user_set": true' in options and '"blog_sftp_folder_set": true' in options
        assert '"blog_sftp_user":' not in options and '"blog_sftp_folder":' not in options
        assert '"blog_sftp_host": "ssh.strato.de"' in options


def test_an_author_of_one_ordinary_word_is_masked_only_after_its_label(data_dir: Path) -> None:
    """The live author is "Ember": masked everywhere, every "Ember" of the report would go."""
    agent = started(data_dir, PERSONAL.model_copy(update={"kdp_author": "Ember", "blog_sftp_user": "stefan"}))
    a_kdp_request(agent, "Ember")
    shared, _ = shared_and_full(agent)
    assert "Author: [the author's name]" in shared and "Author: Ember" not in shared
    assert shared.startswith("Ember diagnostics") and "Ember's" in shared
    assert '"user": "[the SFTP user]"' in shared and '"user": "stefan"' not in shared


# --- 3. 6.2: phone numbers and IBANs ---


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("Her number is 0151 23456789, call her", f"Her number is {privacy.PHONE}, call her"),
        ("Tel. +49 30 1234567", f"Tel. {privacy.PHONE}"),
        ("+49 (0)151 2345 6789 or 030/123 456 78", f"{privacy.PHONE} or {privacy.PHONE}"),
        ("IBAN: DE89 3704 0044 0532 0130 00, BIC COBADEFFXXX", f"IBAN: {privacy.IBAN}, BIC COBADEFFXXX"),
        ("DE89370400440532013000.", f"{privacy.IBAN}."),
        ("Woche 05\n12 13 14 15 16 17 18", "Woche 05\n12 13 14 15 16 17 18"),  # numbers of two lines: no phone
    ],
)
def test_phone_numbers_and_ibans_are_masked(text: str, shown: str) -> None:
    assert privacy.Masker()(text) == shown
    assert privacy.Masker(full=True)(text) == text  # the owner's own report


@pytest.mark.parametrize(
    "text",
    [
        "listing #1234567890 at 4.99 EUR, 12,345 views",
        "2026-10-05 08:00 | 2026-10-05T08:00:00Z | cap_micros=2500000 | -2500000",
        "version 0.40.3, build 0123abc, migration 0098",
        "+12 views (300 to 312) and +3 favorites",
        "request ce93a1f0b2c4d5e6f7a8b9c0d1e2f304 and toolu_01ABCdef234",  # an id with an IBAN's shape
        "books/haushaltsbuch-2027-interior-v18.md, 6 x 9 in, 120 pages",
    ],
)
def test_numbers_that_are_no_phone_or_iban_stay(text: str) -> None:
    assert privacy.Masker()(text) == text


def test_the_shareable_report_masks_a_phone_number_and_an_iban(data_dir: Path) -> None:
    agent = started(data_dir)
    message = "Lena called from 0151 23456789; her refund goes to DE89 3704 0044 0532 0130 00."
    assert owner(agent).send_message({"text": message}, LABEL).status == 201
    shared, full = shared_and_full(agent)
    assert "0151 23456789" in full and "DE89 3704" in full
    assert "0151 23456789" not in shared and "DE89" not in shared and "0532 0130" not in shared
    assert f"Lena called from {privacy.PHONE}; her refund goes to {privacy.IBAN}." in shared


# --- 4. W3: the website's download is recorded with a POST ---

OWNER = {
    "site_enabled": True,
    "site_owner_name": "Stefan Muster",
    "site_address": "Musterstraße 1, 12345 Berlin",
    "site_email": "shop@example.org",
}


def home(title: str = "Printable planners") -> site.Page:
    return site.check(slug="index", title=title, description="Planners to print.", source="# Planners\n")


@pytest.fixture
def site_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(**OWNER))) as client:
        agent = client.app.state.ember.agent  # type: ignore[attr-defined]
        with agent.db.transaction() as conn:
            website.save(conn, agent.scope(), home(), None, "2026-09-30T10:00:00Z")
        yield client


def downloads(client: TestClient) -> int:
    with client.app.state.ember.db.connection() as conn:  # type: ignore[attr-defined]
        return conn.execute("SELECT COUNT(*) FROM site_downloads").fetchone()[0]


def test_the_download_is_recorded_by_the_dashboard_not_by_the_get(site_client: TestClient) -> None:
    for _ in range(3):  # a prefetch, a second tab
        download = site_client.get("api/site/download")
        assert download.status_code == 200 and zipfile.ZipFile(io.BytesIO(download.content)).namelist()
    assert downloads(site_client) == 0
    assert site_client.get("api/dashboard").json()["integrations"]["site"]["downloaded_at"] is None
    version = download.headers["x-ember-site-version"]
    assert site_client.post("api/site/download", json={"version": version}).status_code == 403  # no CSRF header
    assert site_client.post("api/site/download", json={}, headers=CSRF).status_code == 422
    saved = site_client.post("api/site/download", json={"version": version}, headers=CSRF)
    assert saved.status_code == 200 and saved.json()["downloaded_at"] and downloads(site_client) == 1
    card = site_client.get("api/dashboard").json()["integrations"]["site"]
    assert card["downloaded_at"] == saved.json()["downloaded_at"] and card["changed"] == []
    # a page changed after the zip was fetched: that zip isn't this site, so it isn't taken for downloaded
    agent = site_client.app.state.ember.agent  # type: ignore[attr-defined]
    with agent.db.transaction() as conn:
        website.save(conn, agent.scope(), home("Planners, printed"), None, "2026-10-01T10:00:00Z")
    late = site_client.post("api/site/download", json={"version": version}, headers=CSRF)
    assert late.status_code == 409 and "changed after your download" in late.json()["error"]
    assert downloads(site_client) == 1
    assert site_client.get("api/dashboard").json()["integrations"]["site"]["changed"] == ["index.html"]


def test_the_dashboard_saves_the_zip_then_records_it() -> None:
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    handler = script.split('fetch("api/site/download"', 1)[1].split('replace($("site-actions")', 1)[0]
    assert 'response.headers.get("x-ember-site-version")' in handler
    assert handler.index("link.click()") < handler.index('request("POST", "api/site/download", { version: version })')
    assert 'href: "api/site/download"' not in script  # no link that the browser follows (and records) by itself


# --- 5. W4: an options.json nested too deep ---


@pytest.mark.parametrize("text", ["[" * 100_000, '{"a": ' * 100_000])
def test_an_options_file_nested_too_deep_starts_safe_mode(write_options: Callable[[dict], Path], text: str) -> None:
    path = write_options({})
    path.write_text(text, encoding="utf-8")
    loaded = load_settings(path)  # RecursionError: the container stopped instead of starting safe mode
    assert loaded.safe_mode and loaded.settings.dry_run and loaded.owner_unknown
    assert loaded.errors[0].startswith("could not read options.json: maximum recursion depth exceeded")


# --- 6. no GET route writes to the database ---


def contents(client: TestClient) -> str:
    """Every row of every table, in one digest."""
    digest = hashlib.sha256()
    with client.app.state.ember.db.connection() as conn:  # type: ignore[attr-defined]
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 - the schema's own names
                digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def concrete(path: str) -> str:
    path = path.replace("{text_id}", "a" * 64).replace("{kind}", "owner_grant").replace("{name}", "index.html")
    path = re.sub(r"\{[^}]+\}", "1", path)
    return path + ("?before=10" if path == "/api/inbox" else "")


def test_no_get_route_writes_to_the_database(site_client: TestClient) -> None:
    """0.40.1's audit: GET /api/site/download inserted a row, so a prefetch counted as a download. A GET needs no CSRF
    header, so none may change anything (the report's planner preview is rolled back)."""
    agent = site_client.app.state.ember.agent  # type: ignore[attr-defined]
    assert owner(agent).send_message({"text": "Hello"}, LABEL).status == 201
    gets = sorted({route.path for route in router.routes if "GET" in getattr(route, "methods", ())})
    assert "/api/site/download" in gets and "/api/diagnostics" in gets and len(gets) > 15
    answered = {}
    for path in gets:
        before = contents(site_client)
        answered[path] = site_client.get(concrete(path)).status_code
        assert contents(site_client) == before, f"GET {path} changed the database"
    assert answered["/api/site/download"] == 200 and answered["/api/diagnostics"] == 200
    assert answered["/api/dashboard"] == 200
