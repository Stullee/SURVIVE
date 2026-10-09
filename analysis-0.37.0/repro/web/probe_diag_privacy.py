"""The shareable diagnostics report: the owner's HA name and ID masked, the Impressum data never in it."""

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
OWNER = "8f14e45fceea167a5a36dedd4bea2543"
OTHER = "c9f0f895fb98ab9159f51fd0297e236d"
PERSONAL = {
    "site_owner_name": "Erika Musterfrau",
    "site_address": "Musterweg 7, 54321 Koelnstadt",
    "site_phone": "+49 151 98765432",
    "site_vat_id": "DE987654321",
    "site_email": "erika@musterfrau.example",
    "email_owner_name": "Erika M. Musterfrau",
    "kdp_author": "Erika Penname",
}
opts = {"owner_user_ids": [OWNER], "site_enabled": True, "site_url": "https://musterfrau.example",
        "blog_enabled": True, "live_enabled": True, "kdp_enabled": True, **PERSONAL}
(base / "options.json").write_text(json.dumps(opts))
app = create_app(load_settings(), dev_mode=False)
OWN = {"X-Remote-User-Id": OWNER, "X-Remote-User-Display-Name": "Erika Hausherrin"}
with TestClient(app, client=("172.30.32.2", 1)) as c:
    P = {**OWN, "X-Ember-Request": "1"}
    print("message:", c.post("/api/inbox", json={"text": "Bitte kümmere dich um die Website."}, headers=P).status_code)
    print("pause:", c.post("/api/control/pause", json={}, headers=P).status_code)
    print("resume:", c.post("/api/control/resume", json={}, headers=P).status_code)
    # a refused other household member
    print("other:", c.get("/api/dashboard", headers={"X-Remote-User-Id": OTHER, "X-Remote-User-Display-Name": "Kind"}).status_code)
    agent = app.state.ember.agent
    for _ in range(3):
        agent.run_cycle("schedule")
    app.state.ember.log_handler.flush(2)
    shareable = c.get("/api/diagnostics", headers=OWN).text
    full = c.get("/api/diagnostics?full=true", headers=OWN).text
for label, text in (("shareable", shareable), ("full", full)):
    print(f"\n{label}: {len(text):,} chars")
    for what, value in [("owner id", OWNER), ("HA display name", "Erika Hausherrin"), ("other user id", OTHER),
                        *PERSONAL.items(), ("town", "Koelnstadt"), ("surname", "Musterfrau")]:
        n = text.count(value)
        if n:
            i = text.find(value)
            print(f"   {what:18} x{n}: …{text[max(0, i - 90):i + len(value) + 30]!r}")
        else:
            print(f"   {what:18} not found")
