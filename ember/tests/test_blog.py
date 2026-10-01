"""0.14.0: the blog on the owner's website. The agent proposes a post (a Markdown file) or the link page; Ember's code
renders it in the site's design and keeps exactly those bytes; the owner previews and approves it (never on an
unlock); Ember's code uploads it over SFTP once, journaled, with the blog's list, and the owner's Undo puts back what
it replaced. Only a post, the blog's list and the link page are ever written. The live client is tested against a
real SFTP server in this process; nothing leaves the app in a dry run."""

from __future__ import annotations

import base64
import json
import os
import socket
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import paramiko
import pytest
from fastapi.testclient import TestClient

from app import diagnostics
from app.agent import audit, never, store, tools
from app.agent.fake_llm import FakeTransport, request_kind
from app.config import LoadedSettings, Settings
from app.economy.clock import to_iso
from app.integrations import connectors, sftp, site_publisher
from app.logging_setup import redact
from app.products import blog
from tests.test_agent import ROOMY, rows
from tests.test_etsy import call, shop_context, views_approval
from tests.test_loop_shapes import run
from tests.test_owner_api import CSRF
from tests.test_owner_loop import owner
from tests.test_site import texts

SITE_DATA = {
    "site_url": "https://example.org",
    "site_owner_name": "Stefan Muster",
    "site_address": "Musterstraße 1, 12345 Berlin",
    "site_email": "shop@example.org",
}
BLOG = ROOMY.model_copy(update={"blog_enabled": True, **SITE_DATA})
OWNER = blog.Owner("Ember", "Stefan Muster", "Berlin", "shop@example.org", "https://example.org")
POST = """---
slug: bewerbung-nachfassen
title: Bewerbung nachfassen: So fragst du "höflich" nach
description: Wann und wie du nach einer Bewerbung nachfassen solltest, mit konkreten Formulierungen.
date: 2026-01-01
lead: Nach dem Versenden beginnt das Warten. Ein höfliches Nachfassen zeigt Interesse.
product_name: Bewerbungs-Tracker in Excel
product_text: Mit dem Tracker siehst du, wann ein Nachfassen fällig ist.
product_url: https://www.etsy.com/listing/4584899301
---

## Warum Nachfassen sinnvoll ist

Firmen erhalten viele Bewerbungen, z. B. hunderte pro Stelle. Eine fehlende Rückmeldung bedeutet selten Desinteresse.
Wer nachfragt, zeigt Engagement: "Ich wollte freundlich nachfragen."

## Der richtige Zeitpunkt

- **Nach einer Bewerbung**: ein bis zwei Wochen warten.
- **Nach einem Gespräch**: eine Woche warten. <script>alert(1)</script>

> Ein Nachfassen pro Bewerbung reicht.

| Wann | Wie |
| --- | --- |
| nach 2 Wochen | E-Mail an [uns](mailto:shop@example.org) |
"""


def post_source(**changes: str) -> str:
    text = POST
    for key, value in changes.items():
        text = "\n".join(f"{key}: {value}" if line.startswith(f"{key}:") else line for line in text.split("\n"))
    return text


# --- what a post may be, and its page -------------------------------------------------------------------------------


def test_a_post_is_checked_and_typeset() -> None:
    post = blog.read_post(POST, OWNER)
    assert (post.slug, post.path) == ("bewerbung-nachfassen", "blog/bewerbung-nachfassen.html")
    assert post.title == "Bewerbung nachfassen: So fragst du „höflich“ nach"  # German quotation marks
    assert "z. B." in post.body and "„Ich wollte freundlich nachfragen.“" in post.body
    assert post.product_url == "https://www.etsy.com/listing/4584899301" and post.notes == ()
    divided = blog.read_post(POST + "\n---\n\nMehr Text.\n", OWNER)
    assert divided.notes == ("1 divider line is left out: headings divide a post",)
    refused = {
        "no front matter": ("## Nur Text\n\n" + "Text. " * 80, "starts with its front matter"),
        "bad slug": (post_source(slug="Bewerbung Nachfassen"), "slug is the page's name"),
        "the list's name": (post_source(slug="index"), "slug is the page's name"),
        "unknown key": (POST.replace("lead:", "teaser:"), "the front matter's keys are"),
        "half a product": (POST.replace("product_url: https://www.etsy.com/listing/4584899301\n", ""), "or none of"),
        "not Etsy": (post_source(product_url="https://shop.example.com/x"), "the product's page at Etsy"),
        "another address": (POST.replace("Firmen erhalten", "Schreib an hr@firma.de, Firmen erhalten"), "hr@firma.de"),
        "another mailto": (POST.replace("mailto:shop@example.org", "mailto:x@y.de"), "your owner's address"),
        "a user in a link": (POST.replace("(mailto:shop@example.org)", "(https://u@localhost/)"), "a user name"),
        "a box": (POST + "\n::: box\nText\n:::\n", "a post holds headings, paragraphs, lists"),
        "a photo": (POST + "\n::: photo 35x45 Bild\n", "a post holds headings"),
        "too short": (POST.split("## Warum")[0] + "Kurz.", "shorter than 300"),
    }
    for name, (source, message) in refused.items():
        with pytest.raises(blog.BlogError, match=message):
            blog.read_post(source, OWNER)
            pytest.fail(name)
    with pytest.raises(blog.BlogError, match="line 28"):  # counted in the whole file
        blog.read_post(POST + "\n::: center\nText\n", OWNER)


def test_the_page_is_the_site_s_template_with_the_words_escaped() -> None:
    post = blog.read_post(POST.replace("title: Bewerbung", "title: <b>Bewerbung</b>"), OWNER)
    page = blog.render_post(post, "2026-10-01", OWNER).decode()
    assert blog.audit(page.encode(), OWNER) == []
    assert page.startswith('<!doctype html>\n<html lang="de">')
    assert f'content="{blog.CSP}"' in page and "<script" not in page and "&lt;script&gt;" in page
    assert "<title>&lt;b&gt;Bewerbung&lt;/b&gt; nachfassen" in page
    assert '<link rel="canonical" href="https://example.org/blog/bewerbung-nachfassen.html">' in page
    assert '<time datetime="2026-10-01">1. Oktober 2026</time>' in page  # the code's date, not the file's
    assert "<blockquote>\n<p>Ein Nachfassen pro Bewerbung reicht.</p>\n</blockquote>" in page
    assert '<div class="table-scroll"><table><thead><tr><th>Wann</th>' in page
    assert '<a href="mailto:shop@example.org">uns</a>' in page
    assert '<a class="btn btn-primary" href="https://www.etsy.com/listing/4584899301">Auf Etsy ansehen</a>' in page
    assert "Ember, ein Experiment von Stefan Muster in Berlin" in page
    assert "Transparenz: Ember, ein KI-Agent" in page and "Musterstraße" not in page  # never the address
    without = blog.read_post("\n".join(line for line in POST.split("\n") if not line.startswith("product_")), OWNER)
    assert "post-product" not in blog.render_post(without, "2026-10-01", OWNER).decode()
    assert (
        "ein Experiment von Stefan Muster</p>"
        in blog.render_post(
            without, "2026-10-01", blog.Owner("Ember", "Stefan Muster", "", "shop@example.org", "https://example.org")
        ).decode()
    )


def test_the_owner_previews_the_page_with_the_site_s_own_look() -> None:
    page = blog.render_post(blog.read_post(POST, OWNER), "2026-10-01", OWNER)
    shown = blog.preview(page, "https://example.org").decode()
    assert '<link rel="stylesheet" href="https://example.org/style.css">' in shown
    assert '<img class="brand-mark" src="https://example.org/flame.svg" alt="">' in shown
    assert '<a href="https://example.org/blog/">Blog</a>' in shown and 'href="/' not in shown and 'src="/' not in shown
    assert f'content="{blog.CSP}"' not in shown and "style-src https://example.org;" in shown
    assert "<script" not in shown


def test_the_check_before_an_upload_refuses_what_the_template_never_has() -> None:
    good = blog.render_post(blog.read_post(POST, OWNER), "2026-10-01", OWNER)
    bad = {
        b"<p>x</p>": "<script> isn't allowed",
        b'<p onclick="x()">x</p>': "the attribute onclick isn't allowed",
        b'<p style="color:red">x</p>': "the attribute style isn't allowed",
        b'<a href="http://example.org/">x</a>': "isn't an allowed address",
        b'<a href="javascript:alert(1)">x</a>': "isn't an allowed address",
        b'<a href="mailto:x@y.de">x</a>': "isn't an allowed address",
        b'<img src="https://tracker.example/p.gif" alt="">': "isn't an allowed address",
        b'<iframe src="/x"></iframe>': "<iframe> isn't allowed",
    }
    for snippet, message in bad.items():
        if snippet == b"<p>x</p>":
            page = good.replace(b"</main>", b"<script>alert(1)</script></main>")
        else:
            page = good.replace(b"</main>", snippet + b"</main>")
        assert any(message in p for p in blog.audit(page, OWNER)), snippet
    other_policy = good.replace(blog.CSP.encode(), b"default-src *")
    assert blog.audit(other_policy, OWNER) == ["the page must carry exactly the site's Content-Security-Policy"]
    assert blog.audit(b"<html></html>", OWNER) == ["the page doesn't start with <!doctype html>"]


def test_the_list_keeps_what_is_on_the_server() -> None:
    assert blog.read_index(None) == blog.Index()
    first = blog.Index((blog.Entry("haushaltsbuch", "Haushaltsbuch <Vorlage>", "Von Hand hochgeladen.", "2026-09-01"),),
                       "Tipps für Geld", "Praktische Anleitungen.", "Blog von Ember.")  # fmt: skip
    page = blog.render_index(first, OWNER)
    assert blog.audit(page, OWNER) == []
    assert blog.read_index(page) == first  # what Ember's code writes it reads back
    newer = blog.merge(first, blog.Entry("neu", "Neu", "Ganz neu.", "2026-10-01"))
    same_day = blog.merge(newer, blog.Entry("auch-neu", "Auch neu", "Auch.", "2026-10-01"))
    assert [e.slug for e in same_day.entries] == ["auch-neu", "neu", "haushaltsbuch"]
    changed = blog.merge(same_day, blog.Entry("neu", "Neu, besser", "Besser.", "2026-10-01"))
    assert [e.title for e in changed.entries] == ["Neu, besser", "Auch neu", "Haushaltsbuch <Vorlage>"]
    assert [e.slug for e in blog.without(changed, "neu").entries] == ["auch-neu", "haushaltsbuch"]
    back = blog.without(changed, "neu", blog.Entry("neu", "Alt", "Alt.", "2026-09-15"))
    assert [(e.slug, e.title) for e in back.entries][1] == ("neu", "Alt")
    rendered = blog.render_index(changed, OWNER).decode()
    assert '<h1 class="page-title" id="blog-title">Tipps für Geld</h1>' in rendered  # the owner's heading stays
    assert "Haushaltsbuch &lt;Vorlage&gt;" in rendered
    # The hand-made list from the owner's site (its reading times aren't Ember's code's: they go).
    handmade = (
        b'<!doctype html><html><head><meta name="description" content="Tipps."></head><body><main><h1>Blog</h1>'
        b'<p class="lead">Lead.</p><ul class="post-list"><li><p class="post-meta"><time datetime="2026-10-01">1. '
        b'Oktober 2026</time> \xc2\xb7 5 Min. Lesezeit</p><h2><a href="/blog/a.html">A &amp; B</a></h2><p>Text.</p>'
        b"</li></ul></main></body></html>"
    )
    assert blog.read_index(handmade) == blog.Index(
        (blog.Entry("a", "A & B", "Text.", "2026-10-01"),), "Blog", "Lead.", "Tipps."
    )
    with pytest.raises(blog.BlogError, match="no list of posts"):
        blog.read_index(b"<!doctype html><p>My own blog</p>")


def test_the_link_page_is_checked() -> None:
    bio, links = blog.read_links(
        'Vorlagen für "Bewerbung" und Geld.',
        [
            {"label": "Bewerbungs-Tracker", "note": "Excel-Vorlage", "url": "https://www.etsy.com/listing/1"},
            {"label": "Blog", "url": "/blog/"},
            {"label": "E-Mail", "note": "", "url": "mailto:shop@example.org"},
        ],
        OWNER,
    )
    assert bio == "Vorlagen für „Bewerbung“ und Geld."
    page = blog.render_links(bio, links, OWNER)
    assert blog.audit(page, OWNER) == []
    text = page.decode()
    assert '<a class="link-btn link-main" href="https://www.etsy.com/listing/1">Bewerbungs-Tracker' in text
    assert '<a class="link-btn" href="/blog/">Blog</a>' in text and "<h1>Ember</h1>" in text
    refused = {
        "http": ([{"label": "x", "url": "http://example.org"}], "https address"),
        "another mailto": ([{"label": "x", "url": "mailto:a@b.de"}], "your owner's address"),
        "twice": ([{"label": "x", "url": "/blog/"}, {"label": "y", "url": "/blog/"}], "there twice"),
        "too many": ([{"label": str(i), "url": f"/p{i}"} for i in range(13)], "1 to 12 links"),
        "none": ([], "1 to 12 links"),
        "a path with a query": ([{"label": "x", "url": "/blog/?a=1"}], "a path like /blog/"),
    }
    for name, (items, message) in refused.items():
        with pytest.raises(blog.BlogError, match=message):
            blog.read_links("Bio.", items, OWNER)
            pytest.fail(name)


def test_only_a_post_the_list_and_the_link_page_are_ever_written() -> None:
    for path in ("blog/a.html", "blog/bewerbung-nachfassen.html", "blog/index.html", "links.html"):
        assert blog.allowed(path), path
    for path in (
        "index.html",
        "en/index.html",
        "impressum.html",
        "datenschutz.html",
        "style.css",
        "sitemap.xml",
        "blog/../index.html",
        "/blog/a.html",
        "blog/a/b.html",
        "blog/A.html",
        "blog/.ember-1.tmp",
        "blog/a.htm",
    ):
        assert not blog.allowed(path), path
    server = sftp.FakeServer()
    server.write("blog/a.html", b"x")
    assert server.read("blog/a.html") == b"x" and server.read("links.html") is None
    for path in ("index.html", "style.css", "../x"):
        with pytest.raises(sftp.NotSent, match="isn't a file Ember's code may write"):
            server.write(path, b"x")


def test_the_server_key_and_the_folder_are_checked() -> None:
    blob = b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(36))
    fingerprint = sftp.fingerprint(blob)
    assert fingerprint.startswith("SHA256:") and len(fingerprint) == 50 and not fingerprint.endswith("=")
    line = "ssh-ed25519 " + base64.b64encode(blob).decode()
    assert sftp.pin_of(line) == sftp.pin_of(f"ssh.example.org {line} comment") == fingerprint
    assert sftp.pin_of(f" {fingerprint} ") == sftp.pin_of(fingerprint + "=") == fingerprint
    for wrong in ("", "SHA256:short", "MD5:aa:bb", "ssh-ed25519 not-base64!"):
        with pytest.raises(ValueError, match="blog_sftp_host_key must be"):
            sftp.pin_of(wrong)
    assert [sftp.folder_of(f) for f in ("", "/", "/ember-ai.de/", "htdocs")] == ["", "/", "/ember-ai.de", "htdocs"]
    for wrong in ("/a/../b", "a;b", "a\\b"):
        with pytest.raises(ValueError, match="blog_sftp_folder"):
            sftp.folder_of(wrong)
    with pytest.raises(ValueError, match="blog_sftp_host must be a host name"):
        Settings(blog_sftp_host="user@ssh.example.org")
    assert "blog_sftp_password" not in Settings(blog_sftp_password=" s3cret ").public_dict()
    assert Settings(blog_sftp_password=" s3cret ").public_dict()["blog_sftp_password_set"] is True
    trouble = site_publisher.problems(Settings(blog_enabled=True, **SITE_DATA), "live")
    assert trouble == ["blog_sftp_host is missing", "blog_sftp_user is missing", "blog_sftp_password is missing"]
    assert site_publisher.problems(Settings(blog_enabled=True, **SITE_DATA), "dry_run") == []
    assert site_publisher.problems(Settings(blog_enabled=True), "dry_run") == [
        "site_url is missing (your website's address, like https://example.org)",
        "site_owner_name is missing",
        "site_email is missing",
    ]


# --- the agent's tools and the plan ---------------------------------------------------------------------------------


def test_the_tools_and_their_manual_come_with_the_blog() -> None:
    off = {d["name"]: d for d in tools.definitions(etsy=True)}
    on = {d["name"]: d for d in tools.definitions(etsy=True, blog=True)}
    assert not {"propose_blog_post", "propose_link_page"} & set(off)
    assert {"propose_blog_post", "propose_link_page"} <= set(on)
    assert "blog" not in off["guide"]["input_schema"]["properties"]["topic"]["enum"]
    assert "blog" in on["guide"]["input_schema"]["properties"]["topic"]["enum"]
    assert "propose_blog_post" not in {d["name"] for d in tools.definitions(etsy=True, blog=True, venture=True)}
    guide = tools.guide_text("blog")
    assert "at least 300 characters" in guide and "up to 12 buttons" in guide and "{" not in guide


def blog_context(agent: Any, settings: Settings = BLOG) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.blog = tools.BlogAccess(site_publisher.owner_of(settings), ())
    return ctx


def proposed(data_dir: Path, source: str = POST) -> tuple[Any, int]:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=BLOG)
    plan = next(r for r in fake.sent if request_kind(r) == "plan")
    assert "== BLOG ==\nPosts on https://example.org/blog/: none known yet" in texts(plan)
    agent.roots()[0].write("blog/post.md", source)
    made = call(blog_context(agent), "propose_blog_post", {"source": "blog/post.md", "reason": "Search traffic."})
    assert made.ok, made.text
    assert "Approval request #" in made.text and "Nothing is online yet" in made.text
    return agent, rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]


def published(data_dir: Path) -> tuple[Any, int]:
    agent, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    return agent, request


def test_the_agent_proposes_a_post_and_the_owner_previews_exactly_it(data_dir: Path) -> None:
    agent, request = proposed(data_dir)
    row = rows(agent, f"SELECT * FROM approvals WHERE id = {request}")[0]
    assert (row["type"], row["status"], row["title"]) == ("publish", "pending", "Blog post: Bewerbung nachfassen: So "
                                                          "fragst du „höflich“ nach")  # fmt: skip
    assert row["payload"].startswith("New blog post https://example.org/blog/bewerbung-nachfassen.html\nTitle: ")
    assert "## Der richtige Zeitpunkt" in row["payload"]
    action = json.loads(row["action"])
    upload = rows(agent, "SELECT path, status, sha256, content FROM site_uploads")[0]
    assert (upload["path"], upload["status"]) == ("blog/bewerbung-nachfassen.html", "proposed")
    assert action["sha256"] == upload["sha256"] == blog.sha256(upload["content"])
    assert action["date"] == agent.clock.today().isoformat()
    assert agent.blog_page(request) == ("blog/bewerbung-nachfassen.html", upload["content"])
    with agent.db.connection() as conn:  # never on an unlock: a page under the owner's name
        assert never.reasons(conn, conn.execute(f"SELECT * FROM approvals WHERE id = {request}").fetchone()) == [
            "owner_only"
        ]
    assert views_approval(agent, request)["execution"] is None
    changed = owner(agent).decide(request, {"decision": "approve_with_changes", "final_payload": "x"}, "Owner")
    assert changed.status == 422 and "approve a page as it is" in json.dumps(changed.body)
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="never changes"):
        conn.execute("UPDATE site_uploads SET content = x'00'")
    assert agent.execute_approved() == []  # nothing before the owner's decision
    assert agent.blog.fake is not None and agent.blog.fake.files == {}


def test_the_approved_post_goes_up_with_the_list_and_the_undo_takes_it_down(data_dir: Path) -> None:
    agent, request = published(data_dir)
    files = agent.blog.fake.files
    page = rows(agent, "SELECT content FROM site_uploads WHERE path = 'blog/bewerbung-nachfassen.html'")[0]
    assert files["blog/bewerbung-nachfassen.html"] == page["content"]
    listed = blog.read_index(files["blog/index.html"])
    assert [e.slug for e in listed.entries] == ["bewerbung-nachfassen"]
    row = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert (row["status"], row["closed_by"]) == ("done", "Ember")
    assert "fake server" in row["result_note"]
    assert row["result_link"] is None  # the dry run's page isn't at the site's address
    execution = views_approval(agent, request)["execution"]
    assert execution["status"] == "done" and execution["url"] is None
    entry = rows(agent, "SELECT * FROM action_journal WHERE approval_id = " + str(request))[0]
    assert (entry["class"], entry["status"], entry["subject"]) == (
        "site.publish_post",
        "simulated",
        "blog/bewerbung-nachfassen.html",
    )
    assert json.loads(entry["undo"]) == {"action": "restore_site", "path": "blog/bewerbung-nachfassen.html"}
    assert json.loads(entry["before"]) == {"pages": {"blog/bewerbung-nachfassen.html": None, "blog/index.html": None}}
    assert rows(agent, "SELECT slug, approval_id FROM blog_posts") == [
        {"slug": "bewerbung-nachfassen", "approval_id": request}
    ]
    assert agent.execute_approved() == []  # once
    with agent.db.connection() as conn:
        feed = audit.feed(conn, agent.scope())
        text = site_publisher.text(conn, agent.db, agent.scope(), agent.settings)
    assert feed[0]["undo"]["label"] == "Put back what it replaced" and feed[0]["undo"]["why_not"] is None
    [change] = feed[0]["changes"]
    assert change["part"] == "Pages on your website"
    assert change["before"] == "blog/bewerbung-nachfassen.html (none), blog/index.html (none)"
    assert change["after"].startswith("blog/bewerbung-nachfassen.html (") and "(none)" not in change["after"]
    assert "Posts on https://example.org/blog/ (as read " in text and "bewerbung-nachfassen" in text
    with agent.db.transaction() as conn:
        undo, what = audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), entry["id"], "Owner")
    assert what == "undo the upload of blog/bewerbung-nachfassen.html"
    assert agent.execute_approved() == [(undo, "done")]
    assert "blog/bewerbung-nachfassen.html" not in files
    assert blog.read_index(files["blog/index.html"]).entries == ()
    assert rows(agent, "SELECT * FROM blog_posts") == []
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {undo}")[0]["result_note"]
    assert note.startswith(f"Undid request #{request} (blog/bewerbung-nachfassen.html): took it off your server")
    with agent.db.connection() as conn:
        assert audit.feed(conn, agent.scope())[1]["undo"]["why_not"] == "it is undone"


def test_an_update_keeps_its_date_and_its_undo_puts_the_old_version_back(data_dir: Path) -> None:
    agent, first = published(data_dir)
    old_page = agent.blog.fake.files["blog/bewerbung-nachfassen.html"]
    agent.economy.clock.advance(days=3)
    ctx = blog_context(agent)
    agent.roots()[0].write("blog/post.md", POST.replace("viele Bewerbungen", "sehr viele Bewerbungen"))
    made = call(ctx, "propose_blog_post", {"source": "blog/post.md", "reason": "Better."})
    assert made.ok and "It replaces the post online since" in made.text, made.text
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]
    agent.roots()[0].write("blog/post.md", POST.replace("viele Bewerbungen", "unzählige Bewerbungen"))
    again = call(ctx, "propose_blog_post", {"source": "blog/post.md", "reason": "Even better."})
    assert again.ok and f"It replaces request #{second} (withdrawn)" in again.text, again.text
    third = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {second}")[0]["status"] == "withdrawn"
    assert (
        json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {third}")[0]["action"])["date"]
        == (json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {first}")[0]["action"])["date"])
    )
    assert owner(agent).decide(third, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(third, "done")]
    files = agent.blog.fake.files
    assert b"unz\xc3\xa4hlige" in files["blog/bewerbung-nachfassen.html"]
    assert len(blog.read_index(files["blog/index.html"]).entries) == 1
    with agent.db.connection() as conn:
        feed = audit.feed(conn, agent.scope())
    newest, oldest = feed[0], feed[-1]
    assert oldest["undo"]["why_not"].startswith("a later change of this page")
    with agent.db.transaction() as conn:
        undo, _ = audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), newest["id"], "Owner")
    assert agent.execute_approved() == [(undo, "done")]
    assert files["blog/bewerbung-nachfassen.html"] == old_page
    assert [e.slug for e in blog.read_index(files["blog/index.html"]).entries] == ["bewerbung-nachfassen"]
    with agent.db.connection() as conn:  # the later change is undone: the first can be undone now
        assert audit.feed(conn, agent.scope())[-1]["undo"]["why_not"] is None


def test_a_page_changed_on_the_server_is_never_undone(data_dir: Path) -> None:
    agent, request = published(data_dir)
    agent.blog.fake.files["blog/bewerbung-nachfassen.html"] = b"<!doctype html>\n<p>The owner's own edit</p>"
    entry = rows(agent, f"SELECT id FROM action_journal WHERE approval_id = {request}")[0]
    with agent.db.transaction() as conn:
        undo, _ = audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), entry["id"], "Owner")
    assert agent.execute_approved() == [(undo, "failed")]
    assert agent.blog.fake.files["blog/bewerbung-nachfassen.html"] == b"<!doctype html>\n<p>The owner's own edit</p>"
    note = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {undo}")[0]
    assert note["status"] == "failed" and "isn't the page Ember's code uploaded" in note["result_note"]


def test_the_owner_s_own_posts_stay_listed_and_a_list_it_can_t_read_stops_the_upload(data_dir: Path) -> None:
    agent, request = proposed(data_dir)
    mine = blog.Index((blog.Entry("haushaltsbuch", "Haushaltsbuch", "Meins.", "2026-09-01"),), "Mein Blog")
    agent.blog.fake.files["blog/index.html"] = blog.render_index(mine, blog.Owner("X", "Y", "", "a@b.de", "https://x"))
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    listed = blog.read_index(agent.blog.fake.files["blog/index.html"])
    assert [e.slug for e in listed.entries] == ["bewerbung-nachfassen", "haushaltsbuch"]
    assert listed.heading == "Mein Blog"
    assert rows(agent, "SELECT slug, approval_id FROM blog_posts ORDER BY slug") == [
        {"slug": "bewerbung-nachfassen", "approval_id": request},
        {"slug": "haushaltsbuch", "approval_id": None},
    ]
    agent.blog.fake.files["blog/index.html"] = b"<!doctype html><p>Handmade, no list</p>"
    agent.roots()[0].write("blog/other.md", post_source(slug="anderer-beitrag"))
    assert call(blog_context(agent), "propose_blog_post", {"source": "blog/other.md", "reason": "More."}).ok
    other = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]
    assert owner(agent).decide(other, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(other, "failed")]
    assert "blog/anderer-beitrag.html" not in agent.blog.fake.files  # nothing went up
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {other}")[0]["result_note"]
    assert "no list of posts in the blog template's form" in note


def test_the_link_page_goes_up_as_a_whole(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=BLOG)
    ctx = blog_context(agent)
    args = {
        "bio": "Praktische Vorlagen für Bewerbung und Geld.",
        "links": [
            {"label": "Bewerbungs-Tracker", "url": "https://www.etsy.com/listing/1"},
            {"label": "Blog", "url": "/blog/"},
        ],
        "reason": "The new listing first.",
    }
    refused = call(ctx, "propose_link_page", {**args, "links": [{"label": "x", "url": "mailto:a@b.de"}]})
    assert not refused.ok and "your owner's address" in refused.text
    made = call(ctx, "propose_link_page", args)
    assert made.ok, made.text
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_links'")[0]["id"]
    assert (
        "1. Bewerbungs-Tracker -> https://www.etsy.com/listing/1"
        in rows(agent, f"SELECT payload FROM approvals WHERE id = {request}")[0]["payload"]
    )
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    assert b"Bewerbungs-Tracker" in agent.blog.fake.files["links.html"]
    assert "blog/index.html" not in agent.blog.fake.files  # the link page alone
    assert site_publisher.links(agent.db, "dry_run")["links"][1] == {"label": "Blog", "note": "", "url": "/blog/"}
    with agent.db.connection() as conn:
        text = site_publisher.text(conn, agent.db, agent.scope(), agent.settings)
    assert "Link page (https://example.org/links.html, uploaded" in text and "Blog -> /blog/" in text


def test_the_tools_refuse_while_the_blog_can_t_be_published(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=BLOG)
    ctx = shop_context(agent)
    ctx.blog = tools.BlogAccess(OWNER, ("blog_sftp_host is missing",))
    agent.roots()[0].write("blog/post.md", POST)
    refused = call(ctx, "propose_blog_post", {"source": "blog/post.md", "reason": "x"})
    assert not refused.ok and "can't be published yet: blog_sftp_host is missing. Tell them" in refused.text
    ctx.blog = None
    off = call(ctx, "propose_blog_post", {"source": "blog/post.md", "reason": "x"})
    assert not off.ok and "no tool called 'propose_blog_post'" in off.text
    ctx = blog_context(agent)
    agent.roots()[0].write("blog/bad.md", post_source(slug="Bad Slug"))
    bad = call(ctx, "propose_blog_post", {"source": "blog/bad.md", "reason": "x"})
    assert not bad.ok and "blog/bad.md: slug is the page's name" in bad.text


# --- the live connection ----------------------------------------------------------------------------------------------

LIVE = Settings(
    blog_enabled=True,
    blog_sftp_host="ssh.example.org",
    blog_sftp_user="example.org",
    blog_sftp_password="s3cret-sftp-pass",
    **SITE_DATA,
)


class Recorded(sftp.FakeServer):
    simulated = False

    def __init__(self, fingerprint: str) -> None:
        super().__init__()
        self.fingerprint = fingerprint
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_live_the_first_key_is_kept_and_a_connection_that_fails_waits(data_dir: Path) -> None:
    agent, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    seen: list[str | None] = []
    servers = [Recorded("SHA256:first")]
    failing = False

    def connect(login: sftp.Login, pinned: str | None) -> sftp.Server:
        assert (login.host, login.port, login.user, login.password) == (
            "ssh.example.org",
            22,
            "example.org",
            "s3cret-sftp-pass",
        )
        seen.append(pinned)
        if failing:
            raise sftp.NotSent("ssh.example.org:22 can't be reached (timed out)")
        return servers[-1]

    publisher = site_publisher.Publisher(agent.db, agent.clock, LIVE, agent.scope, "live", connect)
    assert publisher.run() == [(request, "done")]
    assert seen == [None] and servers[0].closed == 1
    assert publisher.host_key() == "SHA256:first"
    events_text = json.dumps(rows(agent, "SELECT message FROM events"))
    assert "Pinned the SFTP server's key of ssh.example.org:22: SHA256:first" in events_text
    assert "s3cret" not in events_text
    assert rows(agent, f"SELECT result_link FROM approvals WHERE id = {request}")[0]["result_link"] == (
        "https://example.org/blog/bewerbung-nachfassen.html"
    )
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == (
        "Uploaded blog/bewerbung-nachfassen.html and the blog's list (blog/index.html): "
        "https://example.org/blog/bewerbung-nachfassen.html"
    )
    # The next request: the server must show the kept key; without a connection it waits, then fails.
    agent.roots()[0].write("blog/other.md", post_source(slug="zweiter-beitrag"))
    assert call(blog_context(agent), "propose_blog_post", {"source": "blog/other.md", "reason": "More."}).ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]
    assert owner(agent).decide(second, {"decision": "approve"}, "Owner").status == 200
    failing = True
    assert publisher.run() == []
    assert seen[-1] == "SHA256:first"
    assert agent.db.get_meta(site_publisher.meta_key("live", "last_error")).endswith("(timed out)")
    tries = len(seen)
    agent.economy.clock.advance(minutes=10)
    assert publisher.run() == [] and len(seen) == tries  # a new try every RETRY_MINUTES
    agent.economy.clock.advance(hours=25)
    assert publisher.run() == [(second, "failed")]
    row = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {second}")[0]
    assert row["status"] == "failed" and "no connection to the server for 24 hours" in row["result_note"]
    owners_pin = LIVE.model_copy(update={"blog_sftp_host_key": "SHA256:" + "A" * 43})
    pinned = site_publisher.Publisher(agent.db, agent.clock, owners_pin, agent.scope, "live", connect)
    assert pinned.host_key() == "SHA256:" + "A" * 43  # the owner's pin wins over the kept key


def test_an_upload_the_server_stops_is_reported_never_retried(data_dir: Path) -> None:
    agent, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200

    class Breaking(Recorded):
        def write(self, path: str, data: bytes) -> None:
            if path == blog.INDEX:
                raise sftp.NotSent("blog/index.html couldn't be uploaded (permission denied)")
            super().write(path, data)

    server = Breaking("SHA256:k")
    publisher = site_publisher.Publisher(agent.db, agent.clock, LIVE, agent.scope, "live", lambda *_: server)
    assert publisher.run() == [(request, "partial")]
    assert "blog/bewerbung-nachfassen.html" in server.files and "blog/index.html" not in server.files
    row = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {request}")[0]
    assert row["status"] == "done" and "but not blog/index.html" in row["result_note"]
    assert rows(agent, "SELECT path, status FROM site_uploads ORDER BY id") == [
        {"path": "blog/bewerbung-nachfassen.html", "status": "done"},
        {"path": "blog/index.html", "status": "failed"},
    ]
    assert publisher.run() == []  # never again
    # A crash while uploading: unclear at the next start, never retried.
    agent.roots()[0].write("blog/other.md", post_source(slug="dritter-beitrag"))
    assert call(blog_context(agent), "propose_blog_post", {"source": "blog/other.md", "reason": "More."}).ok
    third = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_post'")[0]["id"]
    assert owner(agent).decide(third, {"decision": "approve"}, "Owner").status == 200
    with agent.db.transaction() as conn:
        conn.execute(f"UPDATE site_uploads SET status = 'running', started_at = 'x' WHERE approval_id = {third}")
        connectors.begin(conn, third, "2026-10-01T10:00:00Z", subject="blog/dritter-beitrag.html")
    assert publisher.recover() == 1
    row = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {third}")[0]
    assert row["status"] == "failed" and "unclear whether the upload finished" in row["result_note"]


# --- a real SFTP server in this process -----------------------------------------------------------------------------


class _Handle(paramiko.SFTPHandle):
    def stat(self) -> Any:
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class _Files(paramiko.SFTPServerInterface):
    root = ""

    def _real(self, path: str) -> str:
        return os.path.join(self.root, self.canonicalize(path).lstrip("/"))

    def _done(self, action: Callable[[], Any]) -> Any:
        try:
            action()
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def open(self, path: str, flags: int, attr: Any) -> Any:
        try:
            fd = os.open(self._real(path), flags, 0o644)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        mode = "wb" if flags & os.O_WRONLY else "r+b" if flags & os.O_RDWR else "rb"
        handle = _Handle(flags)
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def stat(self, path: str) -> Any:
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def remove(self, path: str) -> Any:
        return self._done(lambda: os.remove(self._real(path)))

    def rename(self, oldpath: str, newpath: str) -> Any:
        if os.path.exists(self._real(newpath)):
            return paramiko.SFTP_FAILURE  # SFTP's rename never replaces a file
        return self._done(lambda: os.rename(self._real(oldpath), self._real(newpath)))

    def posix_rename(self, oldpath: str, newpath: str) -> Any:
        return self._done(lambda: os.replace(self._real(oldpath), self._real(newpath)))

    def mkdir(self, path: str, attr: Any) -> Any:
        return self._done(lambda: os.mkdir(self._real(path)))


class _Login(paramiko.ServerInterface):
    def check_auth_password(self, username: str, password: str) -> int:
        ok = (username, password) == ("example.org", "s3cret-sftp-pass")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


@pytest.fixture
def sftp_server(tmp_path: Path) -> Iterator[tuple[int, paramiko.PKey, Path]]:
    """An SFTP server on localhost, serving ``tmp_path / 'site'``, one connection after another."""
    root = tmp_path / "site"
    root.mkdir()
    _Files.root = str(root)
    key = paramiko.ECDSAKey.generate()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    stop = threading.Event()
    transports: list[paramiko.Transport] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _Files)
            transports.append(transport)
            try:
                transport.start_server(server=_Login())
            except (paramiko.SSHException, EOFError, OSError):
                continue

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], key, root
    stop.set()
    listener.close()
    for transport in transports:
        transport.close()
    thread.join(timeout=5)


def test_the_live_client_against_a_real_sftp_server(sftp_server: tuple[int, paramiko.PKey, Path]) -> None:
    port, key, root = sftp_server
    login = sftp.Login("127.0.0.1", port, "example.org", "s3cret-sftp-pass")
    expected = sftp.fingerprint(key.asbytes())
    assert sftp.pin_of(f"{key.get_name()} {key.get_base64()}") == expected
    server = sftp.connect(login, None)
    try:
        assert server.fingerprint == expected and not server.simulated
        assert server.read("blog/index.html") is None
        server.write("blog/a.html", b"<!doctype html>\nfirst")  # the folder is made
        server.write("blog/a.html", b"<!doctype html>\nsecond")  # replaced in place
        assert (root / "blog" / "a.html").read_bytes() == b"<!doctype html>\nsecond"
        assert server.read("blog/a.html") == b"<!doctype html>\nsecond"
        assert [p.name for p in (root / "blog").iterdir()] == ["a.html"]  # no temporary file left
        server.write("links.html", b"links")
        server.remove("blog/a.html")
        server.remove("blog/a.html")  # gone already: fine
        assert not (root / "blog" / "a.html").exists() and (root / "links.html").read_bytes() == b"links"
        with pytest.raises(sftp.NotSent, match="isn't a file Ember's code may write"):
            server.write("index.html", b"x")
        assert not (root / "index.html").exists()
    finally:
        server.close()
    with pytest.raises(sftp.HostKeyMismatch, match="nothing was sent"):
        sftp.connect(login, "SHA256:" + "B" * 43)
    with pytest.raises(sftp.NotSent, match="refused the login"):
        sftp.connect(sftp.Login("127.0.0.1", port, "example.org", "wrong"), expected)
    in_folder = sftp.connect(sftp.Login("127.0.0.1", port, "example.org", "s3cret-sftp-pass", "/www"), expected)
    try:
        (root / "www").mkdir()
        in_folder.write("links.html", b"in the folder")
        assert (root / "www" / "links.html").read_bytes() == b"in the folder"
    finally:
        in_folder.close()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    unused = closed.getsockname()[1]
    closed.close()
    with pytest.raises(sftp.NotSent, match="can't be reached"):
        sftp.connect(sftp.Login("127.0.0.1", unused, "u", "p"), None)


# --- the owner's side -------------------------------------------------------------------------------------------------


@pytest.fixture
def blog_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings(blog_enabled=True, **SITE_DATA))) as client:
        yield client


def test_the_owner_s_card_preview_and_check(blog_client: TestClient) -> None:
    card = blog_client.get("api/dashboard").json()["integrations"]["blog"]
    assert card["status"] == "ok" and card["simulated"] and card["posts"] == []
    assert card["host_key"] == "SHA256:dry-run-fake-server" and "password" not in json.dumps(card).replace(
        "password_set", ""
    )
    checked = blog_client.post("api/blog/check", headers=CSRF)
    assert checked.status_code == 200 and checked.json() == {
        "fingerprint": "SHA256:dry-run-fake-server",
        "simulated": True,
        "posts": 0,
        "index": False,
        "links": False,
    }
    assert blog_client.get("api/blog/preview/1").status_code == 404
    agent = blog_client.app.state.ember.agent  # type: ignore[attr-defined]
    cycle = agent.meter.open_cycle("owner")
    agent.meter.close_cycle(cycle)
    with agent.db.transaction() as conn:
        made = store.insert_approval(
            conn,
            agent.scope(),
            int(cycle),
            to_iso(agent.clock.now()),
            payload="A post",
            action=store.canonical({"path": "blog/x.html"}),
            type="publish",
            title="Blog post: x",
            description="Why",
            expected_cost="none",
            expected_benefit="Readers",
            executor="site_post",
        )
        site_publisher.propose(conn, agent.scope(), made, "blog/x.html", b"<!doctype html>\n<p>&lt;script&gt;</p>")
    page = blog_client.get(f"api/blog/preview/{made}")
    assert page.status_code == 200 and page.text == "<!doctype html>\n<p>&lt;script&gt;</p>"
    assert page.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; style-src https://example.org; font-src https://example.org; img-src "
        "https://example.org; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["cache-control"] == "no-store"


def test_nothing_while_the_blog_is_off(ingress_client: TestClient, data_dir: Path) -> None:
    assert ingress_client.get("api/dashboard").json()["integrations"]["blog"] == {"status": "disabled"}
    off = ingress_client.post("api/blog/check", headers=CSRF)
    assert off.status_code == 422 and "the blog and the live view are off" in off.json()["error"]
    fake = FakeTransport()
    run(data_dir, fake, cycles=1)
    assert "== BLOG ==" not in texts(next(r for r in fake.sent if request_kind(r) == "plan"))


def test_the_sftp_password_never_reaches_a_log_or_the_diagnostics(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    with client_factory(LoadedSettings(LIVE)) as client:
        report = diagnostics.report(client.app.state.ember, full=True)  # type: ignore[attr-defined]
        card = client.get("api/dashboard").json()["integrations"]["blog"]
    assert redact("login failed for s3cret-sftp-pass") == "login failed for ***"
    assert "s3cret-sftp-pass" not in report and "-- blog" in report and '"password_set": true' in report
    assert "s3cret-sftp-pass" not in json.dumps(card) and card["host"] == "ssh.example.org"
