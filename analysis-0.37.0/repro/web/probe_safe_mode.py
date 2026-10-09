"""Safe mode: owner IDs kept or locked, dry run forced, kill switch held; corrections."""

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
OWNER = "0123456789abcdef0123456789abcdef"
STRANGER = "fedcba9876543210fedcba9876543210"
INGRESS = ("172.30.32.2", 50000)


def fresh(options):
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    path = base / "options.json"
    if isinstance(options, str):
        path.write_text(options)
    else:
        path.write_text(json.dumps(options))


def run(label, options, extra=None):
    fresh(options)
    loaded = load_settings()
    print(f"\n== {label}: safe_mode={loaded.safe_mode} owner_unknown={loaded.owner_unknown} "
          f"reset_unknown={loaded.reset_unknown} owners={loaded.settings.owner_user_ids} dry_run={loaded.settings.dry_run}")
    print("   errors:", loaded.errors, "corrections:", loaded.corrections)
    app = create_app(loaded, dev_mode=False)
    with TestClient(app, client=INGRESS) as c:
        for who in (OWNER, STRANGER, None):
            h = {"X-Remote-User-Id": who} if who else {}
            r = c.get("/api/dashboard", headers=h)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            print(f"   {who and who[:6]} /api/dashboard ->", r.status_code, body.get("code") if isinstance(body, dict) else "")
        r = c.get("/", headers={"X-Remote-User-Id": STRANGER})
        print("   / page for stranger:", r.status_code, repr(r.text[-60:]))
        print("   /api/health:", c.get("/api/health").status_code, " /api/sensors:", c.get("/api/sensors").status_code)
        if extra:
            extra(c)


run("A invalid model, owner kept", {"owner_user_ids": [OWNER], "planner_model": "claude-unknown"})
run("B unreadable options file", "{not json")
run("C owner ids a string", {"owner_user_ids": OWNER, "planner_model": "claude-unknown"})
run("D owner id too long", {"owner_user_ids": [OWNER, "x" * 101]})
run("E owner ids with only blanks (valid options)", {"owner_user_ids": ["  "]})
run("F owner ids with a number (hand-edited)", {"owner_user_ids": [12345]})
run("G owner ids null", {"owner_user_ids": None, "planner_model": "claude-unknown"})
run("H dry_run false + invalid -> forced dry run", {"owner_user_ids": [OWNER], "dry_run": False,
                                                     "anthropic_api_key": "sk-ant-api03-" + "k" * 90,
                                                     "planner_model": "claude-unknown"})
# corrections
for opts in (
    {"daily_spend_cap_usd": 1.0, "cycle_spend_cap_usd": 2.0},
    {"min_sleep_minutes": 300, "max_sleep_minutes": 200, "wake_interval_minutes": 240},
    {"min_sleep_minutes": 30, "max_sleep_minutes": 200, "wake_interval_minutes": 500},
    {"etsy_usd_per_eur": 0.3},
    {"agent_name": "   ", "planner_model": "  ", "worker_model": "", "etsy_redirect_uri": " ",
     "pinterest_redirect_uri": "\t"},
    {"strategy_model": "   ", "research_model": " ", "workshop_model": "  "},
    {"daily_spend_cap_usd": 1.0, "cycle_spend_cap_usd": 2.0, "min_sleep_minutes": 3000},
):
    fresh(opts)
    loaded = load_settings()
    s = loaded.settings
    print("\ncorrect", opts, "->", "SAFE" if loaded.safe_mode else "ok", loaded.errors or loaded.corrections)
    print("   cycle", s.cycle_spend_cap_usd, "daily", s.daily_spend_cap_usd, "min", s.min_sleep_minutes, "max",
          s.max_sleep_minutes, "default", s.wake_interval_minutes, "rate", s.etsy_usd_per_eur, "name", repr(s.agent_name),
          "planner", repr(s.planner_model), "worker", repr(s.worker_model), "etsy_uri", repr(s.etsy_redirect_uri),
          "pin_uri", repr(s.pinterest_redirect_uri), "strategy", repr(s.strategy_model))
