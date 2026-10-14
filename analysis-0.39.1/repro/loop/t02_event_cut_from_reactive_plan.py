"""LOOP: an event wakes a reactive cycle, but the owner's unanswered messages fill SINCE YOUR LAST WAKE's budget, so
the plan never shows the event (agenda lines come after the owner's and are cut first). The event is marked woke
before the cycle, so it never wakes again; the TASK still says 'an event woke you (Agenda in SINCE YOUR LAST WAKE)'."""
import sys
from harness import fresh_dir, make_agent, plan, rows
from app.agent import agenda, store
from app.economy.clock import to_iso

LONG = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
d = fresh_dir(f"t02_{LONG}")
agent, transport = make_agent(d, [plan(steps=[], sleep=60), plan(steps=[], sleep=60)])
# one ordinary cycle first (so the agent is past its first wake)
agent.decide()
agent.clock.advance(minutes=3)
first = agent.decide()
print("first decision:", first.run, first.trigger)
agent.run_cycle(first.trigger)
agent.clock.advance(minutes=40)
now = to_iso(agent.clock.now())
words = ("Please change the listing photos for the planner and also think about the price; " * 40)
with agent.db.transaction() as conn:
    store.insert_message(conn, agent.scope(), None, words[:LONG], now)          # the owner's message, unanswered
    store.insert_message(conn, agent.scope(), None, words[:700], now)
    agenda._add(conn, agent.scope(), "reply", "9001", 'Email #9001 from buyer@example.org answers one you sent: "Re: custom order"', now)
decision = agent.decide()
print("decision:", decision.run, decision.trigger, "|", decision.reason[:100])
end = agent.run_cycle(decision.trigger)
print("cycle:", end.status, end.note)
planner = transport.sent[-1]["messages"][0]["content"][0]["text"]
since = planner.split("== SINCE YOUR LAST WAKE ==")[1].split("\n== ")[0]
print("SINCE YOUR LAST WAKE section (", len(since.encode()), "bytes ):")
print("   ...", since[-260:].replace("\n", "\n   "))
print("agenda line in the plan:", "Agenda (reply" in planner)
print("TASK:", planner.split("== TASK ==")[1].strip()[:160])
print("agenda rows:", rows(agent, "SELECT id, kind, woke_at IS NOT NULL AS woke, seen_cycle_id FROM agenda"))
print("decide again:", agent.decide().reason[:120])
