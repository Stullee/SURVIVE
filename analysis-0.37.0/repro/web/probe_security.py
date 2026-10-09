"""Probe the security boundary: IP filter, CSRF, owner identity, dev-mode Host check, 403 headers."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

data = Path(os.environ["EMBER_DATA_DIR"])
data.mkdir(parents=True, exist_ok=True)
for f in data.glob("*"):
    if f.is_file():
        f.unlink()
OWNER = "0123456789abcdef0123456789abcdef"
STRANGER = "fedcba9876543210fedcba9876543210"
(data / "options.json").write_text(json.dumps({"owner_user_ids": [OWNER]}))

INGRESS = ("172.30.32.2", 50000)
app = create_app(load_settings(), dev_mode=False)
from app.web import router as _router
routes = [(r.path, sorted(r.methods)) for r in _router.routes if isinstance(r, APIRoute)]
print("routes:", len(routes), "methods:", sorted({m for _, ms in routes for m in ms}))

with TestClient(app, client=INGRESS) as c:
    own = {"X-Remote-User-Id": OWNER}
    # 1. every POST without the CSRF header -> 403 from the middleware
    missing = []
    for path, methods in routes:
        p = path.replace("{kind}", "grant").replace("{text_id}", "a" * 64)
        p = __import__("re").sub(r"\{[a-z_]+\}", "1", p)
        for m in methods:
            if m == "POST":
                r = c.post(p, headers=own, json={})
                if r.status_code != 403 or "X-Ember-Request" not in r.text:
                    missing.append((m, p, r.status_code, r.text[:80]))
    print("POST routes not refused without CSRF header:", missing)
    # 2. GET routes for a stranger
    leaks = []
    for path, methods in routes:
        p = __import__("re").sub(r"\{[a-z_]+\}", "1", path)
        if "GET" in methods:
            r = c.get(p, headers={"X-Remote-User-Id": STRANGER})
            if r.status_code != 403:
                leaks.append((p, r.status_code))
    print("GET routes answered to a stranger:", leaks)
    # 3. first header counts
    hdrs = [("X-Remote-User-Id", STRANGER), ("X-Remote-User-Id", OWNER)]
    print("stranger first, owner second ->", c.get("/api/dashboard", headers=hdrs).status_code)
    hdrs = [("X-Remote-User-Id", OWNER), ("X-Remote-User-Id", STRANGER)]
    print("owner first, stranger second ->", c.get("/api/dashboard", headers=hdrs).status_code)
    hdrs = [("X-Remote-User-Id", " "), ("X-Remote-User-Id", OWNER)]
    print("blank first, owner second ->", c.get("/api/dashboard", headers=hdrs).status_code)
    print("no user header ->", c.get("/api/dashboard").status_code)
    # 4. 403 page headers
    r = c.get("/", headers={"X-Remote-User-Id": STRANGER})
    print("stranger page:", r.status_code, repr(r.text[:60]), "csp" if "content-security-policy" in r.headers else "NO CSP",
          "nosniff" if "x-content-type-options" in r.headers else "NO nosniff", r.headers.get("cache-control"))
    # 5. state-changing GET? site download
    r = c.get("/api/site/download", headers=own)
    print("GET /api/site/download:", r.status_code, r.text[:100])

# Host-network gateway
with TestClient(app, client=("172.30.32.1", 40000)) as c:
    print("core GET /api/sensors", c.get("/api/sensors").status_code)
    print("core HEAD /api/sensors", c.head("/api/sensors").status_code)
    print("core GET /api/health", c.get("/api/health").status_code)
    print("core GET /static/js/theme.js", c.get("/static/js/theme.js").status_code)
    print("core POST /api/sensors", c.post("/api/sensors", headers={"X-Ember-Request": "1"}).status_code)
for addr in ("::ffff:172.30.32.2", "172.30.32.02", "172.030.032.002", " 172.30.32.2"):
    with TestClient(app, client=(addr, 1)) as c:
        print(f"client {addr!r} ->", c.get("/api/health").status_code)

# Dev mode Host header check
dev = create_app(load_settings(), dev_mode=True)
with TestClient(dev, client=("10.0.0.5", 1)) as c:
    for host in ("localhost", "localhost:8099", "127.0.0.1", "[::1]:8099", "localhost.", "LocalHost:80",
                 "127.0.0.2", "0.0.0.0:8099", "evil.example", "localhost:8099@evil", "127.1"):
        r = c.get("/api/health", headers={"Host": host})
        print(f"dev Host {host!r} ->", r.status_code)
