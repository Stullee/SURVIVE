"""The owner's blog list on the server holds their own posts. Ember's code publishes an approved post "with the list as
it is on your server, with the post added (posts you uploaded yourself stay listed)". An entry not exactly in the
template's form (a relative link, an absolute one, no <time>, an underscore in the file name) is dropped from the list
silently, and the owner's Undo of the post doesn't bring it back."""

from __future__ import annotations

import harness  # noqa: F401

from app.agent import audit
from app.economy.clock import to_iso
from app.products import blog
from tests.test_blog import proposed
from tests.test_owner_loop import owner

agent, request = proposed(harness.DATA)


def li(href: str, title: str, when: str | None = "2026-09-01") -> str:
    meta = f'<p class="post-meta"><time datetime="{when}">1. September 2026</time></p>' if when else "<p>1.9.2026</p>"
    return f'<li>{meta}<h2><a href="{href}">{title}</a></h2><p>Mein eigener Beitrag.</p></li>'


mine = (
    '<!doctype html>\n<html lang="de"><head><meta name="description" content="Mein Blog."></head><body><main>'
    '<h1>Mein Blog</h1><p class="lead">Was ich schreibe.</p><ul class="post-list">'
    + li("/blog/haushaltsbuch.html", "Template form")
    + li("sparen-im-alltag.html", "Relative link (the owner's own post in /blog/)")
    + li("https://example.org/blog/steuer-tipps.html", "Absolute link to the owner's own post")
    + li("/blog/ohne-datum.html", "Date as text, no <time>", None)
    + li("/blog/Mein_Rezept.html", "File name with capitals and an underscore")
    + "</ul></main></body></html>"
).encode()
agent.blog.fake.files["blog/index.html"] = mine
print("entries Ember's code reads:", [e.slug for e in blog.read_index(mine).entries], "of 5 on the server")
owner(agent).decide(request, {"decision": "approve"}, "Owner")
print("execute:", agent.execute_approved())
after = agent.blog.fake.files["blog/index.html"].decode()
for title in ("Template form", "Relative link", "Absolute link", "Date as text", "File name with capitals"):
    print(f"  {title!r:28} still listed: {title in after}")
[entry] = harness.rows(agent, "SELECT id FROM action_journal WHERE approval_id = ?", (request,))
with agent.db.transaction() as conn:
    undo, _ = audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), entry["id"], "Owner")
print("undo:", agent.execute_approved())
after = agent.blog.fake.files["blog/index.html"].decode()
print("after the Undo, entries:", [e.slug for e in blog.read_index(after.encode()).entries])
print("  relative-link entry back:", "Relative link" in after)
print("events:", [r["message"][:110] for r in harness.rows(agent, "SELECT message FROM events WHERE kind = 'website'")])
