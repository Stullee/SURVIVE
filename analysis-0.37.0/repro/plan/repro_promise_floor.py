"""Finding: a promise (or owner's decision) at its urgency floor still outweighs every non-urgent product step, so the
0.37.0 "taken three times in a day without keeping it -> floor, so it doesn't take every cycle" rule changes nothing
in realistic states. Simulates hourly cycles in which Ember never keeps the promise."""

from harness import *  # noqa: F403
from harness import fresh_dir, keep, lined, no_ventures, project, promise, run_cycle, describe, ranked, weights, plan
from tests.test_fixes_0340 import BOOK

no_ventures()

for case in ("product promise due today", "owner-project promise due in 3 days"):
    d = fresh_dir("floor")
    agent, _ = lined(d)  # lines #1 Planner, #2 Poster, #3 Checklist
    if case.startswith("product"):
        book = project(agent, *BOOK)
        pid = promise(agent, "Propose the Haushaltsbuch 2027 KDP book", days=0, line=book)
    else:
        pid = promise(agent, "Report the Bluesky reactions of the week", days=3)
    keep(agent)
    print(f"=== {case}: promise #{pid}")
    taken = 0
    for hour in range(30):
        s = run_cycle(agent)
        is_promise = s.step is not None and s.step.title.startswith(f"Keep promise #{pid}")
        taken += is_promise
        if hour < 6 or hour % 6 == 0 or hour == 29:
            print(f"  h{hour:02d} {'PROMISE' if is_promise else '       '} {describe(s)}")
            if hour in (4, 29):
                print("   ranking:\n" + ranked(s, 4))
        agent.clock.advance(hours=1)
    print(f"  -> the promise took {taken} of 30 hourly cycles (PROMISE_TRIES={plan.PROMISE_TRIES},"
          f" floor urgency {weights.PROMISE_FLOOR}, worth >= {weights.PROMISE_WORTH})")
