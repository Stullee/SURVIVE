"""Exploratory: seeded ventures vs three new products, cycles every 2 hours for 7 days, nothing gets done. When does
each product first get a cycle, and what share do the ventures take? Weights of the first cycle shown."""

from collections import Counter

from harness import fresh_dir, keep, project, run_cycle, ranked, rows
from tests.test_fixes_0360 import exploring_agent

d = fresh_dir("ventsim")
agent = exploring_agent(d)
lines = {}
for title, hyp in (("Budget planner", "An Etsy download for your monthly budget"),
                   ("Bauhaus posters", "People hang the posters in the living room"),
                   ("Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch für 2027")):
    lines[project(agent, title, hyp)] = title
keep(agent)
print("open ventures:", rows(agent, "SELECT id, stage FROM ventures WHERE stage IN ('idea','researching','proposed')"))
first: dict = {}
kinds = Counter()
for i in range(84):
    s = run_cycle(agent, exploring=True)
    if i == 0:
        print("first cycle's ranking:\n" + ranked(s, 10))
    kinds[s.kind] += 1
    if s.line in lines and s.line not in first:
        first[s.line] = round(i * 2 / 24, 1)
    agent.clock.advance(hours=2)
print("kinds over 7 days (84 cycles):", dict(kinds))
print("first cycle of each product (days):", {lines[k]: v for k, v in first.items()},
      "never:", [t for k, t in lines.items() if k not in first])
