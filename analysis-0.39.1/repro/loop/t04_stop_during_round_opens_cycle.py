"""LOOP: Scheduler.stop() arriving while a round runs its pre-steps (execute_approved, sync, publish_live, check_events:
network) doesn't stop that round from deciding and opening a cycle: the cycle runs its keepers and records a plan pick,
then ends 'interrupted' at its first model call. Three such interrupted cycles in a row are the 'crash loop': no
scheduled or event wake until Wake now, while the dashboard shows 'Due now' with the old reason."""
import asyncio
import time

from harness import fresh_dir, make_agent, plan, rows
from app.agent.scheduler import Scheduler
from app.agent.service import Agent
from app.config import LoadedSettings
from tests.economy_helpers import ScriptedTransport, restart

d = fresh_dir("t04")
agent, transport = make_agent(d, [])
agent.decide()
agent.clock.advance(minutes=3)            # the first wake is due


def boot(economy):
    """The next start: a new process (new boot id), recover() as main._start does."""
    fresh = restart(economy)
    a = Agent(fresh.db, LoadedSettings(fresh.settings), fresh, transport=ScriptedTransport(simulated=True), cycles_enabled=True)
    a.recover()
    return a


async def one_boot(a: Agent) -> None:
    """Start the scheduler; Home Assistant asks the app to stop while the round reads the mailbox/agenda."""
    slow = a.check_events

    def check_events():
        time.sleep(0.5)                    # e.g. reading the mailbox or uploading the live view
        slow()

    a.check_events = check_events
    s = Scheduler(a.db, a.economy, a)
    s.start()
    await asyncio.sleep(0.2)               # the round is inside check_events
    await s.stop()                         # main.lifespan's shutdown

for n in range(3):
    agent.clock.advance(minutes=2)         # BOOT_GRACE passed, the wake is due
    asyncio.run(one_boot(agent))
    print(f"boot {n + 1}:", rows(agent, "SELECT id, trigger, status, note FROM cycles ORDER BY id DESC LIMIT 1"))
    agent = boot(agent.economy)

print("plan picks recorded by those cycles:", rows(agent, "SELECT cycle_id, node_id FROM plan_picks"))
agent.clock.advance(minutes=5)
d1 = agent.decide()
print("decide():", d1.run, "|", d1.reason)
f = agent.agent_fields()
print("dashboard:", {k: f[k] for k in ("next_wake_at", "next_wake_reason", "can_wake", "wake_blocked_reason")}, "now:", agent.clock.now())
print("agenda:", rows(agent, "SELECT kind, text, woke_at, seen_cycle_id FROM agenda"))
print("journal:", rows(agent, "SELECT cycle_id, author, summary FROM journal"))
