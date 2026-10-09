"""With live_show_money off (and revenue off), the memorial of a dead Ember still shows what it spent in all; the
revenue switch is honoured there, the money switch isn't."""
import harness  # noqa: F401
from tests.test_live_view import OWNER, snapshot
from app.products import live

dead = snapshot(state="dead", born_at="2026-09-01T10:00:00Z", died_at="2026-10-01T09:00:00Z", api_cost=27.4, expenses=3.1)
page = live.render(dead, live.Parts(money=False, revenue=False, grants=False), OWNER)[live.PAGE].decode()
i = page.find("Ausgegeben")
print("money off, revenue off -> 'Ausgegeben' row shown:", i >= 0, "|", page[i - 20 : i + 60].replace("\n", " ") if i >= 0 else "")
print("revenue row shown:", "Verdient" in page or "Eingenommen" in page)
alive = live.render(snapshot(api_cost=27.4), live.Parts(money=False, revenue=False, grants=False), OWNER)[live.PAGE].decode()
print("alive with money off shows the API costs:", "27,40" in alive)
print("dead with money off shows 27,40+3,10=30,50:", "30,50" in page)
