"""The sensor's `unlocks` ("the unlocks that stand") vs policy.standing after the owner drops a milestone while paused."""
import json, os, shutil, sys, time
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.config import load_settings
from app.main import create_app
from app.agent import policy
base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
OWNER = "8f14e45fceea167a5a36dedd4bea2543"
(base / "options.json").write_text(json.dumps({"owner_user_ids": [OWNER]}))
app = create_app(load_settings(), dev_mode=False)
P = {"X-Remote-User-Id": OWNER, "X-Ember-Request": "1"}
with TestClient(app, client=("172.30.32.2", 1)) as c:
    agent = app.state.ember.agent
    due = (date.today() + timedelta(days=10)).isoformat()
    r = c.post("/api/roadmap", json={"title": "Answer replies", "measure": "replies answered", "due": due}, headers=P)
    print("milestone:", r.status_code, r.text[:120])
    mid = r.json()["id"]
    r = c.post(f"/api/milestones/{mid}/autonomy", json={"rule": "email_reply", "level": "veto_window"}, headers=P)
    print("unlock:", r.status_code, r.text[:160])
    def counts(label):
        s = c.get("/api/sensors").json()["unlocks"]
        a = c.get("/api/dashboard", headers=P).json()["audit"]["unlocks"]
        with app.state.ember.db.connection() as conn:
            st = sum(len(v) for v in policy.standing(conn, agent.scope()).values())
        print(f"{label}: sensor unlocks={s}  audit card unlocks={a}  policy.standing={st}")
    counts("after the unlock")
    print("pause:", c.post("/api/control/pause", json={}, headers=P).status_code)
    m = c.get("/api/roadmap", headers=P).json()
    version = next((x.get("version") for x in m.get("milestones", []) if x.get("id") == mid), None)
    r = c.post(f"/api/roadmap/{mid}/decide", json={"action": "drop", "comment": "not needed", "expected_version": version}, headers=P)
    print("drop:", r.status_code, r.text[:160])
    counts("dropped, paused")
    agent.run_policy()  # what the scheduler's round does
    counts("after a policy round while paused")
    print("resume:", c.post("/api/control/resume", json={}, headers=P).status_code)
    agent.run_policy()
    counts("after a policy round (resumed)")
