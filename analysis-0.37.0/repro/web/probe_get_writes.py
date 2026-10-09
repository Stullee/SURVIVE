"""Does a GET change state? /api/site/download records a download; check rows before and after each GET route."""
import json, os, re, shutil, sqlite3, sys
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
    "blog_enabled": True, "live_enabled": True}))
app = create_app(load_settings(), dev_mode=False)

def snapshot(db):
    with db.connection() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    return counts, meta

with TestClient(app, client=("172.30.32.2", 1)) as c:
    db = app.state.ember.db
    agent = app.state.ember.agent
    for _ in range(2):
        agent.run_cycle("schedule")
    # give the site a page so it can be built
    from app.agent import website
    for r in router.routes:
        if not isinstance(r, APIRoute) or "GET" not in r.methods:
            continue
        path = re.sub(r"\{name\}", "index.html", r.path)
        path = re.sub(r"\{[a-z_]+\}", "1", path)
        before = snapshot(db)
        resp = c.get(path)
        app.state.ember.log_handler.flush(1)
        after = snapshot(db)
        changed = {t: (before[0][t], after[0].get(t)) for t in before[0] if before[0][t] != after[0].get(t)}
        mchanged = {k for k in set(before[1]) | set(after[1]) if before[1].get(k) != after[1].get(k)}
        if changed or mchanged:
            print(f"GET {path} ({resp.status_code}) changed: {changed} meta: {sorted(mchanged)}")
