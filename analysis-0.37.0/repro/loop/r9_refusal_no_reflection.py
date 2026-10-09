"""R9: DOCS "Every cycle that worked reflects": a cycle whose work ran tools and then got a stop_reason 'refusal' ends
'stopped' without a reflection (loop._plan_act_reflect returns before _reflect)."""

from harness import cleanup, fresh_dir, no_ventures

no_ventures()
data = fresh_dir("r9")
from tests.test_agent import ROOMY, make_agent, plan, reply, rows, tools

work = tools(("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "draft"}))
refused = reply([{"type": "text", "text": "I can't continue with this."}], "refusal")
agent, _ = make_agent(data, [plan(steps=["write", "more"]), work, refused, tools(("write_journal", {"summary": "x", "entry": "y"}))], ROOMY)
end = agent.run_cycle("schedule")
print("status:", end.status, "| note:", end.note)
print("calls:", [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")])
print("tools:", [(r["tool"], r["status"]) for r in rows(agent, "SELECT tool, status FROM tool_calls ORDER BY id")])
print("journal:", rows(agent, "SELECT author, summary FROM journal"))
print("next wake reason:", agent.db.get_meta(agent._key("next_wake_reason")))
cleanup()
