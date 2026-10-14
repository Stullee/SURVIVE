"""R3: the owner's hold on a product ("its steps wait until you Resume it") doesn't hold its promise and decision
steps: candidates() gives owed steps reason 'hold' only for a parked/killed venture (``stopped``), never for
product["hold_reason"]. steer() then takes them and the loop aims the whole cycle at the held line
(loop.py:981: plan.focus_project_id = self.steered.line; only a closed or owner-stopped line is refused)."""
import h
from datetime import timedelta
from app.agent import plan, obligations, store
from tests.test_fixes_0350 import decided

agent, _ = h.new_agent()
book = h.project(agent, "Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch für 2027 mit Monatsübersicht")
tracker = h.project(agent, "Bewerbungs-Tracker", "Eine Vorlage für Bewerbungen und Absagen")
h.keep(agent)
# Ember promised the owner to resubmit the book by tomorrow (message_owner commits with project_id)
with agent.db.transaction() as conn:
    msg = store.insert_message(conn, agent.scope(), 1, "I'll resubmit the book tomorrow.", h.now(agent))
    due = (agent.clock.today() + timedelta(days=1)).isoformat()
    prom = obligations.promise(conn, agent.scope(), 1, msg, "Redo the interior and resubmit via propose_kdp_book",
                               due, h.now(agent), project_id=book)
owed = decided(agent, tracker)  # and the owner rejected the tracker's listing
h.keep(agent)
# the owner holds both products on the Plan tab
with agent.db.transaction() as conn:
    for line in (book, tracker):
        [p] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (line,))
        plan.owner_hold(conn, agent.scope(), p["id"], "hold it: I look at it next week", "owner", h.now(agent))
h.keep(agent)
print("held:", h.rows(agent, "SELECT project_id, hold_by, hold_reason FROM plan_nodes WHERE level='product'"))
for i in range(3):
    s = h.steer(agent)
    print(f"steer {i}: step #{s.step.id if s.step else None} line={s.line} kind={s.kind} decided={s.pick.decided} "
          f"title={s.step.title[:60] if s.step else None!r}")
    held_ready = [(c.step.id, c.step.title[:40], c.waiting) for c in s.found if c.step.product in (book, tracker)]
    print("   held lines' candidates:", held_ready)
    if s.step is None:
        break
    # pretend a cycle took it and closed nothing, next day
    from tests.test_fixes_0280 import cycle
    cid = cycle(agent, s.line)
    with agent.db.transaction() as conn:
        plan.record(conn, agent.scope(), cid, h.now(agent), s)
    agent.clock.advance(days=1)
    h.keep(agent)
