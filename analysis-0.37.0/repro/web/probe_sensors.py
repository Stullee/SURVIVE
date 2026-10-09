"""The sensors JSON: documented fields, dry-run numbers, broken DB at start and at runtime."""

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402

from app.config import LoadedSettings, Settings  # noqa: E402
from app.main import create_app  # noqa: E402

base = Path(os.environ["EMBER_DATA_DIR"])
DOCUMENTED = {
    "balance_usd", "runway_days", "state", "mode", "runway_known", "net_runway_days", "today_api_spend_usd",
    "daily_cap_usd", "safe_mode", "kill_switch_engaged", "next_wake_at", "cycle_running", "approvals_pending",
    "approvals_todo", "inbox_unread", "upgrades_new", "ventures_proposed", "milestone_proposals", "waiting_on_you",
    "email_unread", "email_waiting", "agenda_open", "event_wakes_today", "unlocks", "digest_day", "digest_actions",
    "digest_failed", "digest_taken_back",
}
CORE = ("172.30.32.1", 40000)


def fresh():
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)


fresh()
app = create_app(LoadedSettings(Settings()), dev_mode=False)
with TestClient(app, client=CORE) as c:
    body = c.get("/api/sensors").json()
    print("fields:", sorted(body))
    print("documented but missing:", sorted(DOCUMENTED - set(body)))
    print("undocumented extra:", sorted(set(body) - DOCUMENTED))
    print("values:", {k: body[k] for k in ("state", "mode", "balance_usd", "runway_days", "runway_known", "dry_run")})
    db_path = app.state.ember.db.path

# broken at start
fresh()
(base / "ember.db").write_bytes(b"this is not a sqlite database" * 100)
app = create_app(LoadedSettings(Settings()), dev_mode=False)
with TestClient(app, client=CORE) as c:
    r = c.get("/api/sensors")
    print("\nbroken at start:", r.status_code, r.json())

# broken at runtime: the file is damaged while Ember runs (an SD card)
fresh()
app = create_app(LoadedSettings(Settings()), dev_mode=False)
with TestClient(app, client=("172.30.32.2", 1), raise_server_exceptions=False) as c:
    print("\nbefore damage:", c.get("/api/sensors").status_code)
    path = app.state.ember.db.path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    data = bytearray(path.read_bytes())
    for page in range(1, len(data) // 4096):  # scribble over every page but the header page
        data[page * 4096: page * 4096 + 64] = b"\xff" * 64
    path.write_bytes(bytes(data))
    # drop the page cache of the shared connection so the damage is read
    db = app.state.ember.db
    with db._lock:
        db._conn.close()
        db._conn = None
    r = c.get("/api/sensors")
    print("sensors after damage:", r.status_code, r.text[:200])
    r = c.get("/api/health")
    print("health after damage:", r.status_code, r.text[:200])
    r = c.get("/api/dashboard")
    print("dashboard after damage:", r.status_code, r.text[:200])
    r = c.get("/api/diagnostics")
    print("diagnostics after damage:", r.status_code, r.text[:300].replace("\n", " | "))
