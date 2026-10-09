"""Does any GET change any row? Full-content hash of every table before/after each GET route (events excluded)."""
import hashlib, json, os, re, shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from app.config import load_settings
from app.main import create_app
from app.web import router
base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
(base / "options.json").write_text(json.dumps({"site_enabled": True, "site_url": "https://example.org",
    "site_owner_name": "A B", "site_address": "Street 1, 12345 Town", "site_email": "a@example.org",
    "blog_enabled": True, "live_enabled": True, "etsy_enabled": True, "pinterest_enabled": True,
    "bluesky_enabled": True, "printify_enabled": True, "kdp_enabled": True, "email_enabled": True,
    "email_address": "ember@example.org"}))
app = create_app(load_settings(), dev_mode=False)

def snapshot(db):
    out = {}
    with db.connection() as conn:
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if t in ("events",):
                continue
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            out[t] = hashlib.sha256(json.dumps([tuple(r) for r in rows], default=str).encode()).hexdigest()
    return out

with TestClient(app, client=("172.30.32.2", 1)) as c:
    db = app.state.ember.db
    agent = app.state.ember.agent
    for _ in range(4):
        agent.run_cycle("schedule")
    routes = [r for r in router.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    for r in routes:
        for ident in ("1", "2"):
            path = re.sub(r"\{name\}", "index.html", r.path)
            path = re.sub(r"\{[a-z_]+\}", ident, path)
            for q in ("", "?full=true"):
                before = snapshot(db)
                resp = c.get(path + q)
                after = snapshot(db)
                changed = sorted(t for t in before if before[t] != after.get(t))
                if changed:
                    print(f"GET {path}{q} ({resp.status_code}) changed tables: {changed}")
print("done")
