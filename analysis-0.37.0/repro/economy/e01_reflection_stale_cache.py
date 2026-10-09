"""Does the reflection 'always run'? A cycle near its cap whose last step's tool took over 4 minutes (a workshop run,
a few research calls): the reflection's reserve was priced with a warm cache, the reflection itself is judged cold."""
import sys, tempfile
from pathlib import Path
from app.agent import fake_llm, tools as agent_tools
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from tests.economy_helpers import FakeClock, make_economy

def run(slow_seconds: int, cap: float) -> None:
    data = Path(tempfile.mkdtemp())
    settings = Settings(starting_balance_usd=50, cycle_spend_cap_usd=cap)  # daily cap 1.50 (default)
    clock = FakeClock()
    economy = make_economy(data, settings, clock)
    script = [fake_llm.Plan({"assessment": "a", "goal": "g", "money_path": "m", "steps": ["look", "look more"],
                              "sleep_minutes": 120})]
    script += [fake_llm.ToolCalls([("workspace_list", {})]) for _ in range(20)]
    script += [fake_llm.Reply("reflected")]
    fake = fake_llm.FakeTransport(script=script, clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    original = agent_tools.run
    def timed(ctx, name, raw, use_id, call_id, phase):
        out = original(ctx, name, raw, use_id, call_id, phase)
        clock.advance(seconds=slow_seconds if phase == "act" else 5)  # every act tool takes this long
        return out
    agent_tools.run = timed
    try:
        end = agent.run_cycle("owner")
    finally:
        agent_tools.run = original
    with economy.db.connection() as conn:
        calls = conn.execute("SELECT purpose, status, cost_micros, estimate_micros, guard_reason FROM llm_calls ORDER BY id").fetchall()
        cyc = conn.execute("SELECT status, note, act_end_reason, cap_micros FROM cycles").fetchone()
    spent = sum(c["cost_micros"] for c in calls)
    print(f"slow={slow_seconds}s cap=${cap}: end={end.status!r} note={end.note!r}")
    print("   act_end_reason:", cyc["act_end_reason"])
    print("   calls:", [(c["purpose"], c["status"], round(micros_to_usd(c["cost_micros"]), 4)) for c in calls])
    print(f"   spent ${micros_to_usd(spent):.4f} of the cycle cap ${micros_to_usd(cyc['cap_micros']):.2f}")

for cap in (0.5, 0.3):
    run(0, cap)
    run(300, cap)
