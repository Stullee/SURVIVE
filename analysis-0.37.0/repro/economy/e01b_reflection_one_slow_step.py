"""One slow step (the last before the cap ends the work): the reflection kept for is refused anyway."""
import tempfile
from pathlib import Path
from app.agent import fake_llm, loop, tools as agent_tools
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from tests.economy_helpers import FakeClock, make_economy

def run(cap: float, slow_step: int | None) -> None:
    data = Path(tempfile.mkdtemp())
    settings = Settings(starting_balance_usd=50, cycle_spend_cap_usd=cap)
    clock = FakeClock()
    economy = make_economy(data, settings, clock)
    script = [fake_llm.Plan({"assessment": "a", "goal": "g", "money_path": "m", "steps": ["look"], "sleep_minutes": 120})]
    script += [fake_llm.ToolCalls([("workspace_list", {})]) for _ in range(20)] + [fake_llm.Reply("reflected")]
    fake = fake_llm.FakeTransport(script=script, clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    original, steps = agent_tools.run, []
    def timed(ctx, name, raw, use_id, call_id, phase):
        out = original(ctx, name, raw, use_id, call_id, phase)
        if phase == "act":
            steps.append(call_id)
            clock.advance(seconds=300 if len(steps) == slow_step else 20)
        return out
    agent_tools.run = timed
    # what the guard says about the reflection, right before _reflect asks
    orig_reflect, seen = loop.CycleRunner._reflect, {}
    def spy(self, cycle_id, ctx, brief, act):
        seen["reserve"] = ctx.state.reflect_reserve
        seen["room"] = self.meter.rooms(cycle_id, "reflect")[0]
        return orig_reflect(self, cycle_id, ctx, brief, act)
    loop.CycleRunner._reflect = spy
    orig_aff = agent.meter.affordable
    def aff(request, purpose, cycle_id, *a, **k):
        fits, expected, worst = orig_aff(request, purpose, cycle_id, *a, **k)
        if purpose == "reflect":
            seen["reflect_expected"], seen["fits"] = expected, fits
        return fits, expected, worst
    agent.meter.affordable = aff
    try:
        end = agent.run_cycle("owner")
    finally:
        agent_tools.run, loop.CycleRunner._reflect = original, orig_reflect
    with economy.db.connection() as conn:
        calls = conn.execute("SELECT purpose, cost_micros FROM llm_calls ORDER BY id").fetchall()
        cyc = conn.execute("SELECT act_end_reason FROM cycles").fetchone()
    purposes = [c["purpose"] for c in calls]
    print(f"cap=${cap} slow step={slow_step}: work steps={purposes.count('work')} reflected={'reflect' in purposes}"
          f" end={end.status}/{end.note!r}")
    if seen:
        print(f"   kept for the reflection ${micros_to_usd(seen['reserve']):.4f}; room left under the cap"
              f" ${micros_to_usd(seen['room']):.4f}; the reflection judged at ${micros_to_usd(seen.get('reflect_expected', 0)):.4f}"
              f" -> fits={seen.get('fits')}")

for cap in (0.10, 0.15):
    run(cap, None)
    for n in range(1, 8):
        run(cap, n)
