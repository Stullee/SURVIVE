"""SIM: drive Agent like scheduler.Scheduler does (check_events, decide, run_cycle; wait until decision.wait_until, at
most a minute) over simulated days with the fake model, in Europe/Berlin across the DST change of 2026-10-25, and check
the wake rules' invariants. Usage: sim_days.py [hours] [seed]"""

import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from harness import cleanup, fresh_dir, no_ventures

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 48
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 5
no_ventures()
data = fresh_dir("sim")
from app.agent import agenda
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy import metering
from app.economy.costs import micros_to_usd
from tests.economy_helpers import FakeClock, make_economy
from tests.test_agent import rows

BERLIN = ZoneInfo("Europe/Berlin")
settings = Settings(
    starting_balance_usd=200, owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",), mailbox_enabled=False
)
start = datetime(2026, 10, 24, 6, 0, tzinfo=BERLIN).astimezone(UTC)
clock = FakeClock(start, tz=BERLIN)
economy = make_economy(data, settings, clock=clock)
agent = Agent(economy.db, LoadedSettings(settings), economy, transport=FakeTransport(seed=SEED), cycles_enabled=True)
agent.recover()
end_at = start + timedelta(hours=HOURS)
log = []
problems = []
daily = metering.usd_cap_to_micros(settings.daily_spend_cap_usd)
rounds = 0
INJECT = sorted(
    [
        (datetime(2026, 10, 24, 9, 10, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 24, 9, 20, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 24, 12, 0, tzinfo=BERLIN), "message"),
        (datetime(2026, 10, 24, 18, 30, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 24, 19, 0, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 24, 19, 40, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 24, 22, 0, tzinfo=BERLIN), "event"),
        (datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN), "event"),
    ]
)
from app.economy.clock import to_iso
from tests.test_autonomy import send

n_injected = 0
while clock.now() < end_at and rounds < 20000:
    rounds += 1
    while INJECT and INJECT[0][0] <= clock.now():
        at, what = INJECT.pop(0)
        n_injected += 1
        if what == "event":
            with agent.db.transaction() as conn:
                agenda._add(conn, agent.scope(), "reply", f"sim{n_injected}", f"Email sim{n_injected} answers one you sent", to_iso(clock.now()))
        else:
            send(agent, "How is it going?")
            agent.wake_for_message()
        log.append(f"{clock.now().astimezone(BERLIN):%m-%d %H:%M %Z} injected {what}")
    economy.tick()
    agent.check_events()
    d = agent.decide()
    if d.run:
        local = clock.now().astimezone(BERLIN)
        before = economy.books.cap_spend_on(economy.life.scope(), clock.today())
        held = metering.event_reserve(settings, clock, "schedule", 1)
        e = agent.run_cycle(d.trigger)
        after = economy.books.cap_spend_on(economy.life.scope(), clock.today())
        wake = agent._meta_time("next_wake_at")
        reason = agent.db.get_meta(agent._key("next_wake_reason"))
        log.append(
            f"{local:%m-%d %H:%M %Z} {d.trigger:<8} #{e.cycle_id} {e.status:<9} ${micros_to_usd(after - before):.3f}"
            f" day ${micros_to_usd(after):.2f} -> next {wake.astimezone(BERLIN):%m-%d %H:%M} ({reason[:70]})"
        )
        if d.trigger == "schedule" and local.hour < 20 and after > daily - held + 50_000:
            problems.append(f"scheduled cycle #{e.cycle_id} spent into the evening share: day ${micros_to_usd(after)}")
        if after > daily + 100_000:
            problems.append(f"day over its cap after #{e.cycle_id}: ${micros_to_usd(after)}")
        clock.advance(minutes=4)  # a cycle takes a few minutes
        if e.status in ("completed", "idle") or e.rerun:
            continue
    wait = 60.0
    if not d.run and d.wait_until is not None:
        wait = max(1.0, min(60.0, (d.wait_until - clock.now()).total_seconds()))
    clock.advance(seconds=wait)
print("\n".join(log))
cycles = rows(agent, "SELECT trigger, status, started_at FROM cycles ORDER BY id")
print("cycles by trigger/status:", Counter((c["trigger"], c["status"]) for c in cycles))
events = [c for c in cycles if c["trigger"] == "event"]
print("event cycles:", len(events))
print("PROBLEMS:", problems or "none")
cleanup()
