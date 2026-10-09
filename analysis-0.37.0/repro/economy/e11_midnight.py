"""The daily cap per local day (Europe/Berlin), across midnight; a call reserved at 23:59 and settled after midnight is
booked to its reservation day; no more than the cap is admitted within one local day."""
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.metering import CallRefused, Completed
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, request

clock = FakeClock(datetime(2026, 10, 24, 21, 0, tzinfo=UTC), tz=ZoneInfo("Europe/Berlin"))  # 23:00 local, the night before DST ends
e = make_economy(Path(tempfile.mkdtemp()), Settings(starting_balance_usd=50, daily_spend_cap_usd=0.30, cycle_spend_cap_usd=0.30), clock)
class Slow(ScriptedTransport):
    def send(self, req):
        clock.advance(minutes=2)  # the answer takes two minutes
        return super().send(req)
model = e.metered(Slow(outcomes=[Completed(message(1_000, 9_800)) for _ in range(20)]))  # $0.10 a call (worst $0.102)
log = []
for i in range(8):
    cycle = model.open_cycle("owner")
    try:
        r = model.call(cycle, "work", request(max_tokens=9_800))
        model.close_cycle(cycle)
        log.append((clock.current.astimezone(clock.tz).strftime("%m-%d %H:%M"), "ok", round(micros_to_usd(r.cost_micros), 3)))
    except CallRefused as exc:
        log.append((clock.current.astimezone(clock.tz).strftime("%m-%d %H:%M"), "refused", exc.reason[:60]))
        model.close_cycle(cycle, "refused")
        clock.advance(minutes=29)
for row in log:
    print(row)
with e.db.connection() as conn:
    print([tuple(r) for r in conn.execute("SELECT occurred_on, SUM(amount_micros) FROM ledger WHERE type='api_cost' GROUP BY occurred_on")])
