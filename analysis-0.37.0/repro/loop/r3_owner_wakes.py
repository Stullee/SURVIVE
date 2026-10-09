"""R3: the owner's message/decision wake timing against DOCS.md line 51 (5 min after the last, at most 15 after the
first, at least 30 after the last such wake; after a running cycle unless it saw them; not while paused/dormant)."""

from datetime import timedelta

from harness import cleanup, fresh_dir, no_ventures

no_ventures()


def scenario(name, steps):
    data = fresh_dir(f"r3-{name}")
    from app import web
    from app.config import Settings
    from tests.test_agent import ROOMY, make_agent, plan
    from tests.test_autonomy import request_for, send

    agent, _ = make_agent(data, [plan(steps=[], sleep=600) for _ in range(10)], ROOMY)
    request, _ = request_for(agent, Settings())
    t0 = agent.clock.now()
    agent.db.set_meta(agent._key("next_wake_at"), "2026-09-02T12:00:00Z")  # the schedule far away
    log = []
    for at, action in steps:
        agent.clock.current = t0 + timedelta(minutes=at)
        if action == "msg":
            send(agent, f"message at +{at}")
            log.append(f"+{at:>5.1f} msg -> {web._wake_for_message(request)}")
        elif action == "decide":
            d = agent.decide()
            log.append(f"+{at:>5.1f} decide -> run={d.run} trigger={d.trigger} wait={d.wait_until and (d.wait_until - t0)}")
            if d.run:
                end = agent.run_cycle(d.trigger)
                log.append(f"        ran {d.trigger} cycle: {end.status}")
        elif action == "pause":
            agent.economy.set_paused(True, "owner")
            log.append(f"+{at:>5.1f} paused")
        elif action == "resume":
            agent.economy.set_paused(False, "owner")
            log.append(f"+{at:>5.1f} resumed")
    print(f"--- {name}")
    print("\n".join(log))
    cleanup()


scenario("quiet5", [(0, "msg"), (4.9, "decide"), (5, "decide")])
scenario("max15", [(0, "msg"), (4, "msg"), (8, "msg"), (12, "msg"), (14.9, "decide"), (15, "decide")])
scenario("gap30", [(0, "msg"), (5, "decide"), (12, "msg"), (17, "decide"), (34.9, "decide"), (35, "decide")])
scenario("paused", [(0, "pause"), (1, "msg"), (10, "decide"), (20, "resume"), (21, "decide")])
scenario("paused_after", [(0, "msg"), (1, "pause"), (10, "decide"), (60, "resume"), (61, "decide")])
