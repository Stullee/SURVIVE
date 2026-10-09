"""Exploratory: a realistic mix (seeded ventures, three products in research/create, a live product), cycles every
2 hours for 3 days where nothing gets done: which steps / kinds / lines take the cycles; then the same with one
owner-project promise due in 4 days (does anything else get a cycle?)."""

from collections import Counter

from harness import fresh_dir, keep, project, promise, run_cycle, describe, rows
from tests.test_fixes_0360 import exploring_agent

for with_promise in (False, True):
    d = fresh_dir("mix")
    agent = exploring_agent(d)
    for title, hyp in (("Budget planner", "An Etsy download for your monthly budget"),
                       ("Bauhaus posters", "People hang the posters in the living room"),
                       ("Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch für 2027")):
        project(agent, title, hyp)
    keep(agent)
    if with_promise:
        promise(agent, "Report the Bluesky reactions and Etsy view deltas of the week", days=4)
    kinds, lines, steps = Counter(), Counter(), Counter()
    for i in range(36):
        s = run_cycle(agent, exploring=True)
        kinds[s.kind] += 1
        lines[s.line if s.kind != "venture" else f"v{s.venture}"] += 1
        steps[(s.step.title[:45] if s.step else "none")] += 1
        agent.clock.advance(hours=2)
    print(f"=== with an owner-project promise: {with_promise}")
    print("  kinds:", dict(kinds))
    print("  lines:", dict(lines))
    print("  steps:", steps.most_common(6))
