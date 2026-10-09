import json, os, shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.config import load_settings
from app.main import create_app
from app.agent import website
from app.products import site
base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
(base / "options.json").write_text(json.dumps({"site_enabled": True, "site_url": "https://example.org",
    "site_owner_name": "A B", "site_address": "Street 1, 12345 Town", "site_email": "a@example.org"}))
app = create_app(load_settings(), dev_mode=False)
with TestClient(app, client=("172.30.32.2", 1)) as c:
    agent = app.state.ember.agent
    with app.state.ember.db.transaction() as conn:
        website.save(conn, agent.scope(), site.Page(site.HOME, "Home", "My site", "Hello world, this is my site.", "Home"), None, "2026-10-09T00:00:00Z")
    count = lambda: app.state.ember.db.connection().__enter__().execute("SELECT COUNT(*) FROM site_downloads").fetchone()[0]
    print("downloads before:", count())
    r = c.get("/api/site/download")  # no X-Ember-Request header
    print("GET /api/site/download without X-Ember-Request:", r.status_code, r.headers.get("content-type"), len(r.content))
    print("downloads after:", count())
    print("dashboard website card:", {k: v for k, v in c.get("/api/dashboard").json()["integrations"]["site"].items() if k in ("downloaded_at", "changed")})
