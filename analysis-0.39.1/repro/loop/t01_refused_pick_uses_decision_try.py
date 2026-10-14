"""LOOP: a cycle refused at its plan (daily cap spent) still records its plan pick, and that pick counts as the owner's
decision's one "try" (plan._taken_counts), so the decision loses its urgency for 24 hours. The 0.37.6 brake
(_last_picks) ignores such picks (FINISHED only); the tries count doesn't."""
from harness import fresh_dir, rows  # noqa: F401
import harness  # noqa: F401  (sets env, sys.path)

from app.agent import plan
from tests.test_fixes_0280 import lined, request, now
from tests.test_fixes_0340 import keep, ALL

d = fresh_dir("t01")
agent, fake = lined(d)                      # lines #1..#3 with steps ready, one idle cycle run
asked = request(agent, 1)                   # the owner rejected a request of line #1: a decision obligation
keep(agent)


def decision_urgency():
    with agent.db.connection() as conn:
        s = plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    found = [(c.step.id, c.step.title[:50], c.step.urgency) for c in s.found if c.step.title.startswith("React")
             or "decision" in c.step.title.lower() or "request #" in c.step.title]
    top = [(st.id, st.title[:38], round(pt.total, 2)) for st, pt in s.pick.ranked[:4]]
    return s, found, top

s, found, top = decision_urgency()
print("ranked before:", top)
print("before: pick =", s.step.id if s.step else None, s.step.title[:60] if s.step else None, "| decision steps:", found)

# The day's cap is spent (as in the live install by midday); the owner's decision still wakes a cycle (never held back)
daily = 5_000_000
agent.economy.books.cap_spend_on = lambda scope, day: daily  # the meter and decide read this
agent.clock.advance(minutes=10)
end = agent.run_cycle("owner")
print("owner-woken cycle:", end.status, "|", end.note)
print("plan_picks:", rows(agent, "SELECT k.cycle_id, k.node_id, c.status FROM plan_picks k JOIN cycles c ON c.id = k.cycle_id ORDER BY k.id"))
print("llm_calls of that cycle:", rows(agent, "SELECT purpose, status, cost_micros FROM llm_calls WHERE cycle_id = ?", (end.cycle_id,)))

# next morning, the cap is back: is the decision still urgent?
del agent.economy.books.cap_spend_on
agent.clock.advance(hours=10)
s, found, top = decision_urgency()
print("ranked next morning:", top)
print("next morning: pick =", s.step.id if s.step else None, s.step.title[:60] if s.step else None, "| decision steps:", found)
with agent.db.connection() as conn:
    print("_taken_counts:", plan._taken_counts(conn, agent.scope(), "2000-01-01T00:00:00Z"))
    print("_last_picks (0.37.6 brake, FINISHED only):", {k: dict(v) for k, v in plan._last_picks(conn, agent.scope()).items()})
