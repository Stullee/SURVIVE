"""The owner's Undo of a change of a live listing's files, after the agent fixed the file in place (rebuilt it under the
same name, as make_document does) and proposed the fixed file: the Undo is offered, accepted and fails."""

from __future__ import annotations

import json

import harness  # noqa: F401

from app.agent import audit
from app.economy.clock import to_iso
from tests.test_etsy import call, listed, shop_context
from tests.test_owner_loop import owner

agent, listing_id = listed(harness.DATA)
ctx = shop_context(agent)
[approval] = harness.rows(agent, "SELECT action FROM approvals WHERE executor = 'etsy_listing'")
files = [f["path"] for f in json.loads(approval["action"])["files"]]
print("the listing's files:", files)
path = files[0]
old = agent.roots()[0].read_bytes(path)
agent.roots()[0].write_bytes(path, old + b"\n% fixed: a typo on page 2")  # the fix, rebuilt under the same name
made = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "files": ",".join(files), "reason": "Fix a typo."})
print("propose_etsy_edit:", made.ok, made.text[:100])
[edit] = harness.rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_edit'")
owner(agent).decide(edit["id"], {"decision": "approve"}, "Owner")
print("execute (the change):", agent.execute_approved())
[entry] = harness.rows(agent, "SELECT id, class, status, undo FROM action_journal WHERE approval_id = ?", (edit["id"],))
print("journal entry:", entry)
with agent.db.connection() as conn:
    shown = [e for e in audit.feed(conn, agent.scope()) if e["id"] == entry["id"]][0]
print("Undo offered:", shown["undo"]["label"], "| why_not:", shown["undo"]["why_not"])
with agent.db.transaction() as conn:
    undo, what = audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), entry["id"], "Owner")
print("Undo request:", undo, what)
print("execute (the Undo):", agent.execute_approved())
print(harness.rows(agent, "SELECT status, result_note FROM approvals WHERE id = ?", (undo,)))
