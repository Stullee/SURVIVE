"""R7: a Reddit request approved with changes (the server accepts it; the dashboard offers no such button)."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import store  # noqa: E402
from app.agent.owner import Owner  # noqa: E402
from app.integrations import reddit  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402

agent, _ = mail_cycle(data, calls(("email_inbox", {})))
cycle_id = rows(agent, "SELECT MAX(id) AS c FROM cycles")[0]["c"]
action = reddit.action("post", "SideProject", "I built a planner", "Here is what I learned.", None)
with agent.db.transaction() as conn:
    made = store.insert_approval(conn, agent.scope(), cycle_id, "2026-09-28T08:00:00Z", type="publish",
                                 title="Reddit post", description="d", payload=reddit.payload(action),
                                 expected_cost="none", expected_benefit="b", executor="reddit_link",
                                 action=store.canonical(action))
print("payload the owner sees:", json.dumps(reddit.payload(action)))
owner = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)
said = owner.decide(made, {"decision": "approve_with_changes", "final_payload": "My own words, shorter."}, "Stefan")
print("decide:", said.status, said.body.get("approval", said.body).get("status"))
card = next(a for a in agent.dashboard()["approvals"] if a["id"] == made)
url = card["reddit_url"]
query = parse_qs(urlsplit(url).query)
print("prefilled title:", query["title"])
print("prefilled text:", query["text"])
print("disclosure in the prefilled text:", reddit.DISCLOSURE in query["text"][0])
