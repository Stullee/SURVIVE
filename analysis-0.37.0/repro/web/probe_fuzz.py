"""Bad input to every route: does anything answer 500 (an unhandled error)?"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.web import router  # noqa: E402

base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
(base / "options.json").write_text(json.dumps({"site_enabled": True, "blog_enabled": True, "live_enabled": True,
                                               "etsy_enabled": True, "pinterest_enabled": True, "kdp_enabled": True,
                                               "email_enabled": True, "email_address": "e@example.org"}))
app = create_app(load_settings(), dev_mode=False)
P = {"X-Ember-Request": "1", "X-Remote-User-Id": "8f14e45fceea167a5a36dedd4bea2543"}
BODIES = [None, [], "x", 1, 1e308, True, {}, {"x": 1}, {"amount": "1e309"}, {"amount": float("1e308")},
          {"text": "\x00" * 10}, {"text": "a" * 200_000}, {"confirm_name": ["Ember"]}, {"status": {}},
          {"decision": "approve", "comment": None}, {"title": "t", "text": "x", "file_name": "a.pdf", "file_data": "!!"},
          {"show": True}, {"action": "drop"}, {"worth": "NaN"}, {"worth": 1e400 if False else 1e308},
          {"until": "9999-99-99"}, {"day": "2026-02-30"}, {"ids": list(range(5000))}]
fails = []
with TestClient(app, client=("172.30.32.2", 1), raise_server_exceptions=False) as c:
    agent = app.state.ember.agent
    agent.run_cycle("schedule")  # some rows to act on
    for r in router.routes:
        if not isinstance(r, APIRoute):
            continue
        for ident in ("1", "2", "3", "999999"):
            path = re.sub(r"\{text_id\}", "a" * 64, r.path)
            path = re.sub(r"\{kind\}", "grant", path)
            path = re.sub(r"\{name\}", "index.html", path)
            path = re.sub(r"\{[a-z_]+\}", ident, path)
            if "POST" in r.methods:
                for body in BODIES:
                    try:
                        content = json.dumps(body, allow_nan=True).encode()
                    except ValueError:
                        continue
                    resp = c.post(path, content=content, headers={**P, "Content-Type": "application/json"})
                    if resp.status_code >= 500 and resp.status_code != 503:
                        fails.append(("POST", path, body if len(str(body)) < 80 else str(body)[:80], resp.status_code))
                resp = c.post(path, content=b"{not json", headers={**P, "Content-Type": "application/json"})
                if resp.status_code >= 500 and resp.status_code != 503:
                    fails.append(("POST", path, "{not json", resp.status_code))
            if "GET" in r.methods:
                for q in ("", "?path=../../etc/passwd", "?path=%00", "?path=" + "a/" * 3000, "?path=/abs.md",
                          "?path=notes.md&inline=yes", "?before=0", "?before=-1&limit=0", "?limit=99999999999999999999",
                          "?full=maybe", "?path=x.PNG", "?path=CON.md", "?path=.hidden.md", "?path=a\\b.md"):
                    resp = c.get(path + q, headers=P)
                    if resp.status_code >= 500 and resp.status_code != 503:
                        fails.append(("GET", path + q[:40], None, resp.status_code))
print("routes:", len([r for r in router.routes if isinstance(r, APIRoute)]))
print("500s:", len(fails))
for f in fails[:60]:
    print("  ", f)
