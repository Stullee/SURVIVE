"""The owner pauses Ember and presses Undo on a blog post Ember's code uploaded. The Undo is accepted ("carried out in
its next round"; DOCS: an Undo is carried out while Ember is paused or waits for money too), but while paused the
rounds carry out the listings', pins', products' and posts' Undos only: the post stays on the website."""

from __future__ import annotations

import harness  # noqa: F401

from tests.test_blog import published
from tests.test_owner_loop import owner

agent, request = published(harness.DATA)
files = agent.blog.fake.files
print("on the server:", sorted(files))
agent.economy.set_paused(True, "Owner")
print("state:", agent.economy.life.evaluate().state)
[entry] = harness.rows(agent, "SELECT id FROM action_journal WHERE approval_id = ?", (request,))
reply = owner(agent).undo(entry["id"], "Owner")
print("Undo:", reply.status, reply.body)
for _ in range(3):
    print("round: execute_approved ->", agent.execute_approved())
print("post still on the server:", "blog/bewerbung-nachfassen.html" in files)
print(harness.rows(agent, "SELECT id, executor, status FROM approvals WHERE executor = 'site_restore'"))
agent.economy.set_paused(False, "Owner")
print("resumed: execute_approved ->", agent.execute_approved())
print("post still on the server:", "blog/bewerbung-nachfassen.html" in files)
