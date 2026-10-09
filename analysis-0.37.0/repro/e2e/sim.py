"""End-to-end dry run of Ember 0.37.0 over simulated days, driven like app/agent/scheduler.py drives it.

Usage: python sim.py <data_dir> <days> <profile> [owner]
  profile: default (a new install's options) or live (the owner's live-like options from the analyses)
  owner:   passive (never answers) or active (approves every request at 09:00 and 18:00, marks owner-run ones done
           the round after, answers nothing else)
Writes <data_dir>/../<profile>-<owner>-report.json and prints a summary.
"""

from __future__ import annotations

import collections
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

data_dir = Path(sys.argv[1]).resolve()
days = float(sys.argv[2])
profile = sys.argv[3]
owner_mode = sys.argv[4] if len(sys.argv) > 4 else "passive"
data_dir.mkdir(parents=True, exist_ok=True)
os.environ["EMBER_DATA_DIR"] = str(data_dir)
os.environ["EMBER_SCHEDULER"] = "off"
os.environ["EMBER_FAKE_DELAY_MS"] = "0"

sys.path.insert(0, ".")

from app.agent.owner import Owner  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.config import LoadedSettings, Settings  # noqa: E402
from app.db import Database, migrate  # noqa: E402
from app.economy.service import Economy  # noqa: E402
from app.economy.clock import from_iso  # noqa: E402
from tests.economy_helpers import FakeClock  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # a Monday, 08:00 in Berlin

if profile == "default":
    settings = Settings()
elif profile == "live":
    # The owner's live options as the analyses report them: $200 granted, about $5 a day, cycle cap 1, short sleeps.
    settings = Settings(
        starting_balance_usd=200,
        daily_spend_cap_usd=5,
        cycle_spend_cap_usd=1,
        min_sleep_minutes=30,
        wake_interval_minutes=60,
        owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",),
        etsy_enabled=True,
        pinterest_enabled=True,
        bluesky_enabled=True,
        blog_enabled=False,
        kdp_enabled=True,
    )
else:
    raise SystemExit(f"unknown profile {profile}")

clock = FakeClock(START, tz=TZ)
db = Database(data_dir / "ember.db")
migrate(db.path)
loaded = LoadedSettings(settings)
economy = Economy(db, loaded, clock=clock)
economy.start()
agent = Agent(db, loaded, economy, cycles_enabled=True)
agent.recover()
who = "Sim Owner (8f14…)"


def owner() -> Owner:
    return Owner(
        db,
        clock,
        economy,
        agent.scope(),
        settings.agent_name,
        unlocks_off=agent.unlocks_off(),
        ready=agent.channels_ready(),
    )


def rows(sql: str, *params) -> list[dict]:
    with db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


end_at = START + timedelta(days=days)
log: list[dict] = []
errors: list[str] = []
owner_actions: list[str] = []
rounds = 0
last_owner_slot = None
wall = time.time()
while clock.now() < end_at:
    rounds += 1
    now = clock.now()
    local = now.astimezone(TZ)
    # the simulated owner
    if owner_mode == "active":
        slot = (local.date(), 9 if local.hour < 18 else 18)
        if local.hour >= 9 and slot != last_owner_slot:
            last_owner_slot = slot
            o = owner()
            for a in rows("SELECT id, type AS kind, executor, version FROM approvals WHERE status = 'pending'"):
                r = o.decide(a["id"], {"decision": "approve", "expected_version": a["version"]}, who)
                owner_actions.append(f"{local:%m-%d %H:%M} approve #{a['id']} {a['kind']}/{a['executor']} -> {r.status}")
            for a in rows(
                "SELECT id, type AS kind, executor, version FROM approvals WHERE status IN ('approved','approved_with_changes')"
                " AND (executor IS NULL OR executor IN ('reddit_link','kdp_package'))"
            ):
                r = o.close(a["id"], {"outcome": "done", "result_note": "done (sim)", "expected_version": a["version"]}, who)
                owner_actions.append(f"{local:%m-%d %H:%M} done #{a['id']} {a['kind']} -> {r.status}")
    try:
        economy.tick()
        agent.run_policy()
        agent.execute_approved()
        agent.sync_shop()
        agent.publish_live()
        agent.check_events()
        decision = agent.decide()
    except Exception:  # noqa: BLE001
        errors.append(traceback.format_exc())
        clock.advance(minutes=1)
        continue
    if decision.run and decision.trigger:
        before = rows("SELECT COALESCE(MAX(id),0) AS m FROM cycles")[0]["m"]
        t0 = time.time()
        try:
            end = agent.run_cycle(decision.trigger)
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())
            clock.advance(minutes=1)
            continue
        cyc = rows("SELECT * FROM cycles WHERE id > ? ORDER BY id", before)
        for c in cyc:
            pick = rows("SELECT kind, line, node_id, product, decided, weight FROM plan_picks WHERE cycle_id = ?", c["id"])
            node = rows("SELECT title, kind, stage, platform FROM plan_nodes WHERE id = ?", pick[0]["node_id"]) if pick and pick[0]["node_id"] else []
            tools = rows("SELECT tool, status FROM tool_calls WHERE cycle_id = ?", c["id"])
            calls = rows("SELECT purpose, cost_micros FROM llm_calls WHERE cycle_id = ?", c["id"])
            log.append(
                {
                    "id": c["id"],
                    "at": f"{from_iso(c['started_at']).astimezone(TZ):%a %m-%d %H:%M}",
                    "trigger": decision.trigger,
                    "status": c["status"],
                    "kind": pick[0]["kind"] if pick else None,
                    "decided": pick[0]["decided"] if pick else None,
                    "weight": pick[0]["weight"] if pick else None,
                    "step": node[0]["title"] if node else None,
                    "step_kind": node[0]["kind"] if node else None,
                    "product": pick[0]["product"] if pick else None,
                    "project_id": c.get("project_id"),
                    "venture_id": c.get("venture_id"),
                    "marketing": c.get("marketing"),
                    "end_reason": c.get("act_end_reason"),
                    "sleep": c.get("sleep_minutes"),
                    "cost_usd": round(sum(x["cost_micros"] or 0 for x in calls) / 1e6, 4),
                    "calls": collections.Counter(x["purpose"] for x in calls),
                    "tools": collections.Counter(f"{t['tool']}:{t['status']}" for t in tools),
                    "secs": round(time.time() - t0, 1),
                }
            )
        if end.status in ("completed", "idle") or end.rerun:
            continue
        clock.advance(minutes=1)
    else:
        if decision.wait_until is not None:
            step = max(timedelta(minutes=1), min(timedelta(minutes=60), decision.wait_until - clock.now()))
        else:
            step = timedelta(minutes=30)
        clock.advance(seconds=step.total_seconds())

# --- the end state ---
summary: dict = {"profile": profile, "owner": owner_mode, "days": days, "rounds": rounds, "wall_secs": round(time.time() - wall)}
summary["cycles"] = len(log)
summary["by_kind"] = collections.Counter(str(x["kind"]) for x in log)
summary["by_status"] = collections.Counter(str(x["status"]) for x in log)
summary["by_decided"] = collections.Counter(str(x["decided"]) for x in log)
summary["by_trigger"] = collections.Counter(str(x["trigger"]) for x in log)
summary["by_step_kind"] = collections.Counter(str(x["step_kind"]) for x in log)
tool_totals: collections.Counter = collections.Counter()
for x in log:
    tool_totals.update(x["tools"])
summary["tools"] = dict(tool_totals.most_common())
refused = sum(v for k, v in tool_totals.items() if not k.endswith(":ok"))
summary["tool_calls"] = sum(tool_totals.values())
summary["tool_not_ok"] = refused
daily = collections.defaultdict(float)
for c in rows("SELECT ts AS created_at, cost_micros FROM llm_calls"):
    from app.economy.clock import from_iso as _fi

    daily[f"{_fi(c['created_at']).astimezone(TZ):%m-%d}"] += (c["cost_micros"] or 0) / 1e6
summary["spend_by_day"] = {k: round(v, 3) for k, v in sorted(daily.items())}
summary["projects"] = rows("SELECT id, title, status, venture_id FROM projects ORDER BY id")
summary["plan_products"] = rows(
    "SELECT n.id, n.project_id, n.template, n.status, n.hold_by, n.live_since,"
    " (SELECT group_concat(s.stage || ':' || s.status, ' ') FROM plan_nodes s WHERE s.parent_id = n.id AND s.level='stage') AS stages"
    " FROM plan_nodes n WHERE n.level = 'product' ORDER BY n.id"
)
summary["plan_steps_open"] = rows("SELECT COUNT(*) AS n FROM plan_nodes WHERE level='step' AND status='open'")[0]["n"]
summary["plan_steps_done"] = rows("SELECT COUNT(*) AS n FROM plan_nodes WHERE level='step' AND status='done'")[0]["n"]
summary["approvals"] = rows("SELECT type, executor, status, COUNT(*) AS n FROM approvals GROUP BY type, executor, status")
summary["ventures"] = rows("SELECT stage, COUNT(*) AS n FROM ventures GROUP BY stage")
summary["etsy_listings"] = rows("SELECT state, COUNT(*) AS n FROM etsy_listings GROUP BY state") if rows("SELECT name FROM sqlite_master WHERE name='etsy_listings'") else []
summary["obligations"] = rows("SELECT kind, status, COUNT(*) AS n FROM obligations GROUP BY kind, status")
summary["ledger"] = rows("SELECT type, COUNT(*) AS n, SUM(amount_micros)/1e6 AS usd FROM ledger GROUP BY type") if rows("SELECT name FROM sqlite_master WHERE name='ledger'") else []
summary["life"] = str(economy.status())
summary["errors"] = errors[:5]
summary["error_count"] = len(errors)
summary["events_error"] = rows("SELECT level, kind, message FROM events WHERE level IN ('error','warning') ORDER BY id DESC LIMIT 40")
summary["owner_actions"] = owner_actions[:60]
out = data_dir.parent / f"{profile}-{owner_mode}-report.json"
out.write_text(json.dumps({"summary": summary, "cycles": log}, indent=1, default=str))
print(json.dumps({k: v for k, v in summary.items() if k not in ("events_error", "owner_actions", "plan_products", "projects")}, indent=1, default=str))
print("report:", out)
