"""LOOP: the owner's text is shortened to fill SINCE YOUR LAST WAKE's budget exactly (context._owner), then the section
is cut again with cut()'s 40-byte margin once an agenda line follows: the owner's last (shortened) message goes too."""
from harness import fresh_dir, make_agent, plan
from app.agent import agenda, store
from app.economy.clock import to_iso

def preview(with_event: bool) -> str:
    d = fresh_dir(f"t03_{with_event}")
    agent, _ = make_agent(d, [plan(steps=[], sleep=60)])
    agent.decide(); agent.clock.advance(minutes=3); agent.run_cycle(agent.decide().trigger)
    now = to_iso(agent.clock.now())
    words = ("Please change the listing photos for the planner and also think about the price; " * 40)
    with agent.db.transaction() as conn:
        store.insert_message(conn, agent.scope(), None, words[:1500], now)
        store.insert_message(conn, agent.scope(), None, "Second message: " + words[:700], now)
        if with_event:
            agenda._add(conn, agent.scope(), "order", "77", "An Etsy order (receipt #77) of your listing #5", now)
    text = agent.planner_preview()
    since = text.split("== SINCE YOUR LAST WAKE ==")[1].split("\n== ")[0]
    return since

for flag in (False, True):
    since = preview(flag)
    print(f"agenda event present={flag}: section {len(since.encode())} bytes; 'Message #2' shown: {'Message #2' in since};"
          f" agenda shown: {'Agenda (' in since}; tail: {since.strip()[-40:]!r}")
