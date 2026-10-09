"""No request-size limit: a body far over the library's 8 MB is read and parsed whole before it is refused."""
import json, os, resource, shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.config import LoadedSettings, Settings
from app.main import create_app
base = Path(os.environ["EMBER_DATA_DIR"])
if base.exists():
    shutil.rmtree(base)
base.mkdir(parents=True)
app = create_app(LoadedSettings(Settings()), dev_mode=False)
P = {"X-Ember-Request": "1", "Content-Type": "application/json"}
with TestClient(app, client=("172.30.32.2", 1)) as c:
    c.get("/api/dashboard")
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    body = b'{"title": "x", "file_name": "a.pdf", "file_data": "' + b"A" * 300_000_000 + b'"}'
    r = c.post("/api/library", content=body, headers=P)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("300 MB library upload ->", r.status_code, r.text[:120])
    print(f"peak RSS before {before/1024:.0f} MB, after {after/1024:.0f} MB")
