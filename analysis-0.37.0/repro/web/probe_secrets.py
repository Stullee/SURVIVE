"""Secrets never in logs, DB, dashboard JSON or diagnostics: set every secret option, run dry-run cycles with every
integration on (fake accounts), hit every GET endpoint, then grep everything."""

import io
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.main import create_app  # noqa: E402
from app.web import router  # noqa: E402

base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
OWNER = "8f14e45fceea167a5a36dedd4bea2543"
SECRETS = {
    "anthropic_api_key": "zz-API-KEY-1111aaaa2222bbbb",
    "email_password": "zz-MAILPW-3333cccc",
    "etsy_shared_secret": "zz-ETSYSECRET-4444dddd",
    "etsy_keystring": "zzETSYKEYSTRING5555eeee",
    "pinterest_app_secret": "zz-PINSECRET-6666ffff",
    "bluesky_app_password": "zzbl-uesk-ypw7-7777",
    "printify_api_token": "zz-PRINTIFYTOKEN-8888gggg",
    "blog_sftp_password": "zz-SFTPPW-9999hhhh",
}
options = {
    "owner_user_ids": [OWNER],
    "email_enabled": True,
    "email_address": "ember@example.org",
    "etsy_enabled": True,
    "pinterest_enabled": True,
    "pinterest_app_id": "1234567",
    "bluesky_enabled": True,
    "bluesky_handle": "ember-test.bsky.social",
    "printify_enabled": True,
    "site_enabled": True,
    "site_url": "https://example.org",
    "site_owner_name": "Max Mustermann",
    "site_address": "Hauptstr. 1, 12345 Berlin",
    "site_email": "max@example.org",
    "blog_enabled": True,
    "blog_sftp_host": "ssh.example.org",
    "blog_sftp_user": "u123",
    "live_enabled": True,
    "kdp_enabled": True,
    **SECRETS,
}
(base / "options.json").write_text(json.dumps(options))

stream = io.StringIO()
setup_logging("debug")
root = logging.getLogger()
capture = logging.StreamHandler(stream)
capture.setFormatter(next(h for h in root.handlers if getattr(h, "ember_stdout", False)).formatter)
root.addHandler(capture)

loaded = load_settings()
print("safe mode:", loaded.safe_mode, loaded.errors)
app = create_app(loaded, dev_mode=False)
H = {"X-Remote-User-Id": OWNER, "X-Remote-User-Display-Name": "Max Mustermann"}
P = {**H, "X-Ember-Request": "1"}
bodies = []
with TestClient(app, client=("172.30.32.2", 50000)) as c:
    state = app.state.ember
    print("grant:", c.post("/api/ledger/grant", json={"amount": "5"}, headers=P).status_code)
    agent = state.agent
    for i in range(3):
        try:
            end = agent.run_cycle("schedule")
            print("cycle", i, getattr(end, "outcome", end))
        except Exception as exc:  # noqa: BLE001
            print("cycle failed", type(exc).__name__, exc)
    # every GET route (ids 1)
    for r in router.routes:
        if isinstance(r, APIRoute) and "GET" in r.methods:
            path = re.sub(r"\{[a-z_]+\}", "1", r.path)
            for q in ("", "?full=true", "?path=notes.md"):
                resp = c.get(path + q, headers=H)
                bodies.append((path + q, resp.status_code, resp.content))
    # error paths: etsy finish with a pasted address containing a secret-looking code, blog check
    resp = c.post("/api/etsy/finish", json={"address": "https://localhost/ember-etsy?code=" + SECRETS["etsy_shared_secret"]}, headers=P)
    bodies.append(("etsy finish", resp.status_code, resp.content))
    resp = c.post("/api/blog/check", json={}, headers=P)
    bodies.append(("blog check", resp.status_code, resp.content))
    state.log_handler.flush(2)
    db_path = state.db.path

for h in list(root.handlers):
    if h is capture:
        root.removeHandler(h)
log_text = stream.getvalue()
db_bytes = b"".join(p.read_bytes() for p in base.rglob("*") if p.is_file() and p.name != "options.json")
print("GET responses collected:", len(bodies), "statuses:", sorted({s for _, s, _ in bodies}))
for name, value in SECRETS.items():
    where = []
    if value in log_text:
        where.append("app log")
    if value.encode() in db_bytes:
        files = [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and p.name != "options.json"
                 and value.encode() in p.read_bytes()]
        where.append(f"data files {files}")
    for path, status, content in bodies:
        if value.encode() in content:
            where.append(f"{path} ({status})")
    print(f"{name:22} -> {where or 'nowhere'}")
print("log lines:", log_text.count("\n"))
