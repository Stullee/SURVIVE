"""The cap ends the work after N steps; only step N's tool was slow (5 minutes: a workshop run, a few searches).
The reflection the cycle kept money for is refused anyway: its reserve was priced with a warm cache."""
import tempfile
from pathlib import Path
from app.agent import fake_llm, loop, tools as agent_tools
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from tests.economy_helpers import FakeClock, make_economy

def run(cap: float, slow_step: int | None, steps_max: int = 40):
    data = Path(tempfile.mkdtemp())
    settings = Settings(starting_balance_usd=50, cycle_spend_cap_usd=cap, max_tool_steps=steps_max)
    clock = FakeClock()
    economy = make_economy(data, settings, clock)
    script = [fake_llm.Plan({"assessment": "a", "goal": "g", "money_path": "m", "steps": ["look"], "sleep_minutes": 120})]
    script += [fake_llm.ToolCalls([("workspace_list", {})]) for _ in range(steps_max)] + [fake_llm.Reply("reflected")]
    fake = fake_llm.FakeTransport(script=script, clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    original, steps, seen = agent_tools.run, [], {}
    def timed(ctx, name, raw, use_id, call_id, phase):
        out = original(ctx, name, raw, use_id, call_id, phase)
        if phase == "act":
            steps.append(call_id)
            clock.advance(seconds=300 if len(steps) == slow_step else 20)
        return out
    orig_reflect = loop.CycleRunner._reflect
    def spy(self, cycle_id, ctx, brief, act):
        seen["reserve"] = ctx.state.reflect_reserve
        seen["room"] = self.meter.rooms(cycle_id, "reflect")[0]
        return orig_reflect(self, cycle_id, ctx, brief, act)
    orig_aff = agent.meter.affordable
    def aff(request, purpose, cycle_id, *a, **k):
        fits, expected, worst = orig_aff(request, purpose, cycle_id, *a, **k)
        if purpose == "reflect":
            seen["reflect_expected"], seen["fits"] = expected, fits
        return fits, expected, worst
    agent.meter.affordable = aff
    agent_tools.run, loop.CycleRunner._reflect = timed, spy
    try:
        end = agent.run_cycle("owner")
    finally:
        agent_tools.run, loop.CycleRunner._reflect = original, orig_reflect
    with economy.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
        spent = conn.execute("SELECT SUM(cost_micros) FROM llm_calls").fetchone()[0]
    print(f"cap=${cap} slow step={slow_step}: work steps={purposes.count('work')} reflected={'reflect' in purposes}"
          f" end={end.status}/{end.note!r} spent=${micros_to_usd(spent):.4f}")
    if seen:
        print(f"   kept for the reflection ${micros_to_usd(seen['reserve']):.4f}; room left under the cap"
              f" ${micros_to_usd(seen['room']):.4f}; the reflection judged at"
              f" ${micros_to_usd(seen.get('reflect_expected', 0)):.4f} -> fits={seen.get('fits')}")
    return purposes.count("work")

for cap in (0.12, 0.20):
    n = run(cap, None)
    run(cap, n)
