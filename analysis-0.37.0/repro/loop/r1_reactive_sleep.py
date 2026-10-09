"""R1: an event (reactive) cycle's chosen sleep is never cut while the plan has steps ready (0.35.1 says every cycle),
and it replaces the scheduled wake.

Compare: the same state, a scheduled cycle -> cut to the shortest sleep (30 min)."""

from harness import cleanup, fresh_dir, no_ventures

for trigger in ("schedule", "event"):
    data = fresh_dir(f"r1-{trigger}")
    no_ventures()
    from app.agent import plan as plan_tree
    from app.agent.fake_llm import Reply, ToolCalls
    from tests.test_fixes_0140_wakes import urgent_milestone
    from tests.test_fixes_0280 import lined, now, take
    from tests.test_fixes_0340 import ALL
    from tests.test_loop_shapes import JOURNAL

    agent, fake = lined(data)  # three product lines after one idle cycle
    with agent.db.connection() as conn:
        steer = plan_tree.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    print(f"[{trigger}] plan tree: {len(steer.pick.ranked)} steps ready; step {steer.step and steer.step.title!r}")
    print(f"[{trigger}] next wake before: {agent._meta_time('next_wake_at')}  now={agent.clock.now()}")
    if trigger == "event":
        urgent_milestone(agent)
        decision = agent.decide()
        print(f"[{trigger}] decide -> run={decision.run} trigger={decision.trigger} reason={decision.reason!r}")
    else:
        agent.clock.advance(minutes=121)
        decision = agent.decide()
        print(f"[{trigger}] decide -> run={decision.run} trigger={decision.trigger}")
    fake.script.extend([take(1, sleep_minutes=360), ToolCalls([("project_list", {})]), Reply("Done."), JOURNAL])
    start = agent.clock.now()
    end = agent.run_cycle(decision.trigger)
    wake = agent._meta_time("next_wake_at")
    print(
        f"[{trigger}] end status={end.status} asked={end.asked_minutes} sleep={end.sleep_minutes} cut={end.sleep_cut!r}"
    )
    print(f"[{trigger}] next wake in {(wake - start).total_seconds() / 60:.0f} min; reason: "
          f"{agent.db.get_meta(agent._key('next_wake_reason'))!r}")
    with agent.db.connection() as conn:
        after = plan_tree.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    print(f"[{trigger}] steps still ready after the cycle: {len(after.pick.ranked)}")
    cleanup()
