"""R7: after a cycle an overrun stopped, DOCS (Spending limits) says the next wake follows the agent's chosen sleep "as
after a completed cycle (never sooner than the usual back-off)". Check (a) whether the 0.35.1 cut (steps ready) applies
as it would after a completed cycle, and (b) how long events are held (backoff_until)."""

from datetime import timedelta

from harness import cleanup, fresh_dir, no_ventures

no_ventures()
for overrun in (False, True):
    data = fresh_dir(f"r7-{overrun}")
    from app.agent import store
    from tests.test_agent import ROOMY, make_agent, plan, rows, tools
    from tests.test_fixes_0140_wakes import urgent_milestone
    from tests.test_fixes_0280 import now

    work = tools(("project_list", {}))
    if overrun:
        work.response["usage"]["output_tokens"] = 60_000  # far more than max_tokens: the guard stops the cycle
    reflection = tools(
        ("write_journal", {"summary": "Worked", "entry": "The poster is next."}),
        ("set_sleep", {"minutes": 420, "reason": "nothing urgent"}),
    )
    agent, _ = make_agent(data, [plan(steps=[], sleep=420), plan(steps=["work"], focus=1, sleep=420), work, reflection], ROOMY)
    assert agent.run_cycle("schedule").status == "idle"  # cycle #1, so a project can name it
    with agent.db.transaction() as conn:
        store.create_project(conn, agent.scope(), cycle_id=1, title="Planner", hypothesis="Someone pays 5 EUR",
                             next_step="make it", status="active", now=now(agent))
    agent.clock.advance(minutes=421)
    end = agent.run_cycle("schedule")
    wake = agent._meta_time("next_wake_at") - agent.clock.now()
    backoff = agent._meta_time("backoff_until")
    print(f"[overrun={overrun}] cycle #{end.cycle_id} {end.status}; cut={end.sleep_cut!r}; next wake in"
          f" {wake.total_seconds() / 60:.0f} min; events held until"
          f" {'-' if backoff is None else f'+{(backoff - agent.clock.now()).total_seconds() / 60:.0f} min'}")
    print("   reason:", agent.db.get_meta(agent._key("next_wake_reason")))
    with agent.db.connection() as conn:
        picks = conn.execute("SELECT ranked FROM plan_picks ORDER BY id DESC LIMIT 1").fetchone()
    print("   steps ready at the cycle's start:", picks and picks[0][:120])
    agent.clock.advance(minutes=45)
    urgent_milestone(agent)
    d = agent.decide()
    print(f"   +45 min, an urgent event: decide -> run={d.run} trigger={d.trigger} reason={d.reason!r}")
    cleanup()
