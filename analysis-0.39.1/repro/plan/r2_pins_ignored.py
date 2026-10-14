"""R2: the owner's pin is accepted (API 200, "Pinned: the next cycle takes it first") but has no effect on
(a) a product's decision step (an obligation step: candidates() never passes pinned for owed steps), and
(b) a step of a product Ember holds (Ember's hold beats the owner's pin; plan.hold refuses only the reverse order)."""
import h
from app.agent import plan, weights, obligations
from tests.test_fixes_0350 import decided

agent, _ = h.new_agent()
tracker = h.project(agent, "Bewerbungs-Tracker", "Eine Vorlage für Bewerbungen und Absagen")
book = h.project(agent, "Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch für 2027 mit Monatsübersicht")
h.keep(agent)
owed = decided(agent, tracker)  # the owner rejected the tracker's listing request: a decision step of the product
h.keep(agent)
[dstep] = h.rows(agent, "SELECT id, kind, title, obligation_id, project_id FROM plan_nodes WHERE obligation_id = ?", (owed,))
print("decision step:", dstep)

# a cycle took the decision once today (so it has no urgency for 24 h, as plan._owed_urgency says): record a pick
from tests.test_fixes_0280 import cycle
cid = cycle(agent, tracker)
with agent.db.transaction() as conn:
    conn.execute("INSERT INTO plan_picks (mode, session, cycle_id, created_at, kind, line, node_id, product, decided, weight, ranked, mark, day)"
                 " VALUES (?, ?, ?, ?, 'ordinary', ?, ?, ?, 'weight', 1.0, '[]', 'x', ?)",
                 (agent.scope().mode, agent.scope().session, cid, h.now(agent), tracker, dstep["id"], tracker,
                  agent.clock.today().isoformat()))
# the owner sets the book's worth (so its step outweighs the decision), then pins the decision
# (the Plan tab offers Pin on it: planPinButton hides it only for kind promise/owner)
with agent.db.transaction() as conn:
    [bookp] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (book,))
    plan.set_worth(conn, agent.scope(), bookp["id"], 10.0, "owner", h.now(agent))
    plan.pin(conn, agent.scope(), dstep["id"], True, "owner", h.now(agent))
print("pinned in the tree:", h.rows(agent, "SELECT pinned FROM plan_nodes WHERE id = ?", (dstep["id"],))[0])
s = h.steer(agent)
cand = next(c for c in s.found if c.step.id == dstep["id"])
print(f"(a) candidate for the decision: pinned={cand.step.pinned} waiting={cand.waiting} blocked={cand.step.blocked}")
print(f"(a) steer picked #{s.step.id if s.step else None} {s.step.title[:50] if s.step else ''!r} decided={s.pick.decided}")

# (b) Ember holds the book, then the owner pins one of its steps
with agent.db.transaction() as conn:
    print(plan.hold(conn, agent.scope(), book, "the tracker first", None, h.now(agent)))
    [demand] = plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'step' AND status = 'open' AND stage = 'research'", (book,))
    plan.pin(conn, agent.scope(), demand["id"], True, "owner", h.now(agent))
s = h.steer(agent)
cand = next(c for c in s.found if c.step.id == demand["id"])
print(f"(b) pinned step #{demand['id']} of Ember-held line #{book}: waiting={cand.waiting} pinned={cand.step.pinned}")
print(f"(b) steer picked #{s.step.id if s.step else None} {s.step.title[:50] if s.step else ''!r} decided={s.pick.decided}")
# and the reverse order is refused:
with agent.db.transaction() as conn:
    plan.resume(conn, agent.scope(), book, "test", None, h.now(agent))
    try:
        plan.hold(conn, agent.scope(), book, "again", None, h.now(agent))
    except plan.PlanError as exc:
        print("(b) reverse order: Ember's hold refused:", exc)
