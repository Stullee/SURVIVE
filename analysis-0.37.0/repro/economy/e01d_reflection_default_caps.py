"""F1 at the default caps ($0.50 a cycle, $1.50 a day, 15 tool steps): each step reads 4 x 6,000 characters of a file
(as a step reading a long draft does), so the cycle cap ends the work; the last step's tools took 5 minutes."""
import random, string, tempfile
from pathlib import Path
from app.agent import fake_llm, loop, tools as agent_tools
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from tests.economy_helpers import FakeClock, make_economy

def run(slow_step):
    data = Path(tempfile.mkdtemp())
    settings = Settings(starting_balance_usd=50)  # default caps, default 15 tool steps
    clock = FakeClock()
    economy = make_economy(data, settings, clock)
    reads = [("workspace_read", {"path": "notes/long.md", "offset": i * 6000, "max_chars": 6000}) for i in range(4)]
    script = [fake_llm.Plan({"assessment": "a", "goal": "g", "money_path": "m", "steps": ["read the draft"], "sleep_minutes": 120})]
    script += [fake_llm.ToolCalls(reads) for _ in range(15)] + [fake_llm.Reply("reflected")]
    fake = fake_llm.FakeTransport(script=script, clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    workspace, _ = agent.roots()
    rnd = random.Random(1)
    workspace.write("notes/long.md", " ".join("".join(rnd.choices(string.ascii_lowercase, k=rnd.randint(2, 9))) for _ in range(5000)))
    original, steps, seen = agent_tools.run, [], {}
    def timed(ctx, name, raw, use_id, call_id, phase):
        out = original(ctx, name, raw, use_id, call_id, phase)
        if phase == "act":
            if call_id not in steps:
                steps.append(call_id)
            clock.advance(seconds=75 if len(steps) == slow_step else 5)  # 4 tools x 75 s = 5 minutes on the slow step
        return out
    orig_aff = agent.meter.affordable
    def aff(request, purpose, cycle_id, *a, **k):
        fits, expected, worst = orig_aff(request, purpose, cycle_id, *a, **k)
        if purpose == "reflect":
            seen.update(expected=expected, fits=fits, room=agent.meter.rooms(cycle_id, "reflect")[0])
        return fits, expected, worst
    agent.meter.affordable = aff
    orig_reflect = loop.CycleRunner._reflect
    def spy(self, cycle_id, ctx, brief, act):
        seen["reserve"] = ctx.state.reflect_reserve
        return orig_reflect(self, cycle_id, ctx, brief, act)
    agent_tools.run, loop.CycleRunner._reflect = timed, spy
    try:
        end = agent.run_cycle("owner")
    finally:
        agent_tools.run, loop.CycleRunner._reflect = original, orig_reflect
    with economy.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
        spent = conn.execute("SELECT SUM(cost_micros) FROM llm_calls").fetchone()[0]
        journal = conn.execute("SELECT COUNT(*) FROM journal WHERE cycle_id = 1 AND author = 'agent'").fetchone()[0]
    print(f"slow step={slow_step}: work steps={purposes.count('work')} reflected={'reflect' in purposes} agent journal={journal}"
          f" end={end.status}/{end.note!r} spent=${micros_to_usd(spent):.4f} of $0.50")
    if "reserve" in seen:
        print(f"   kept for the reflection ${micros_to_usd(seen['reserve']):.4f}; room ${micros_to_usd(seen['room']):.4f};"
              f" reflection judged at ${micros_to_usd(seen['expected']):.4f} -> fits={seen['fits']}")
    return purposes.count("work")

n = run(None)
run(n)
