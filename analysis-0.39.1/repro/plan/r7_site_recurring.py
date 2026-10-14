"""R7: a website-content product (SITE_CONTENT, the blog lines, live line #10) gets the weekly pin / Bluesky post /
monthly blog post steps in its maintain stage, whose checks count only pins/posts/blog posts that link one of the
line's Etsy listings (reach.funnels: no listing, no funnel). 0.39.1 dropped these for the CHANNEL template only
(plan.py:1834). They can never pass: a pin is refused (propose_pin needs a live listing of the line), and a Bluesky post
that links the line's blog post goes out but counts for nothing, so the step stays open and ready again after every
post request (its mark reads its own requests)."""
import h
from app.agent import plan, store
from tests.test_fixes_0340 import request
from tests.test_fixes_0280 import cycle

agent, _ = h.new_agent()
line = h.project(agent, "Blog articles for job seekers", "Articles on the blog with job-search tips bring readers")
h.keep(agent)
print("template:", h.rows(agent, "SELECT template, audience FROM plan_nodes WHERE level='product'"))
with agent.db.transaction() as conn:
    conn.execute("INSERT INTO demand_notes (mode, session, cycle_id, project_id, created_at, keywords, demand, source)"
                 " VALUES (?, ?, 1, ?, ?, 'job search tips', 'People search', 'google')",
                 (agent.scope().mode, agent.scope().session, line, h.now(agent)))
asked = request(agent, line, "site_post")
with agent.db.transaction() as conn:
    conn.execute("UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?", (h.now(agent), asked))
    conn.execute("UPDATE approvals SET status = 'done', closed_at = ? WHERE id = ?", (h.now(agent), asked))
print(h.keep(agent))
for c in h.candidates(agent):
    if c.step.product == line:
        print(f"  #{c.step.id} {c.step.title[:55]!r} waiting={c.waiting} kind={c.step.kind}")
[post_step] = [c for c in h.candidates(agent) if c.step.product == line and "Bluesky" in c.step.title]
s = h.steer(agent)
print("steer:", s.kind, s.step.id if s.step else None, s.step.title[:50] if s.step else None)
# a marketing cycle takes the Bluesky step and posts a link to the line's blog post (allowed: _post_link takes a page
# of the owner's site); the post goes live
cid = cycle(agent, line)
with agent.db.transaction() as conn:
    plan.record(conn, agent.scope(), cid, h.now(agent), s)
    a = store.insert_approval(conn, agent.scope(), cid, h.now(agent), project_id=line, type="publish",
                              title="Bluesky post", description="d", payload='{"link": "https://example.org/blog/tips.html"}',
                              expected_cost="0", expected_benefit="readers", executor="bluesky_post",
                              action='{"link": "https://example.org/blog/tips.html"}')
    conn.execute("UPDATE approvals SET status='approved', decided_at=? WHERE id=?", (h.now(agent), a))
    conn.execute("UPDATE approvals SET status='done', closed_at=? WHERE id=?", (h.now(agent), a))
    conn.execute("INSERT INTO bluesky_posts (mode, session, approval_id, text, link, status, sent, started_at, finished_at)"
                 " VALUES (?, ?, ?, 'Tips', 'https://example.org/blog/tips.html', 'active', 1, ?, ?)",
                 (agent.scope().mode, agent.scope().session, a, h.now(agent), h.now(agent)))
agent.clock.advance(hours=1)
h.keep(agent)
row = h.rows(agent, "SELECT id, title, status, check_spec FROM plan_nodes WHERE id = ?", (s.step.id,))[0]
c = next(c for c in h.candidates(agent) if c.step.id == s.step.id)
print("after a live post linking its blog post:", row, "waiting:", c.waiting)
post = next(c for c in h.candidates(agent) if c.step.product == line and "Bluesky" in c.step.title)
prow = h.rows(agent, "SELECT id, status, check_kind, check_spec FROM plan_nodes WHERE id = ?", (post.step.id,))[0]
from app.agent import reach
with agent.db.connection() as conn:
    f = reach.funnels(conn, agent.scope()).get(line)
print("Bluesky step after its live post:", prow, "waiting:", post.waiting, "| funnel of the line:", f)
for _ in range(10):
    agent.clock.advance(days=1)
    h.keep(agent)
print("10 days on:", h.rows(agent, "SELECT id, title, status FROM plan_nodes WHERE project_id = ? AND template LIKE 'recurring/%'", (line,)))
