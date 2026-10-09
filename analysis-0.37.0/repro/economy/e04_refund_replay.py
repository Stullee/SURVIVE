"""'Sending the same form twice records it once.' An API cost decrease sent twice (a double click, a retry after a lost
answer): the second is re-validated against the day's API cost the first already lowered, and gets a 422 instead of
the replay."""
import tempfile, uuid
from pathlib import Path
from app.config import Settings
from app.economy.metering import Completed
from tests.economy_helpers import ScriptedTransport, make_economy, message, request

economy = make_economy(Path(tempfile.mkdtemp()), Settings(starting_balance_usd=50, dry_run=False, daily_spend_cap_usd=10, cycle_spend_cap_usd=5))
model = economy.metered(ScriptedTransport(simulated=False, outcomes=[Completed(message(1_000, 5_000))]))
cycle = model.open_cycle("test")
print("API cost today: $%.4f" % (model.call(cycle, "work", request(max_tokens=6_000)).cost_micros / 1e6))
body = {"amount": "0.04", "direction": "decrease", "note": "Console says less", "idempotency_key": uuid.uuid4().hex}
first = economy.record("api-correction", dict(body))
print("first submission:", first.status, first.body.get("entry", {}).get("id"), first.body.get("error"))
again = economy.record("api-correction", dict(body))
print("same form again: ", again.status, again.body)
grant = {"amount": "5", "idempotency_key": uuid.uuid4().hex}
print("a grant twice:   ", economy.record("grant", dict(grant)).status, economy.record("grant", dict(grant)).status)
