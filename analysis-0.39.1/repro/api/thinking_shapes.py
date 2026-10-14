"""Which `thinking` each request builder sends for each model the owner can configure.
Current API (claude-api skill, cached 2026-10-06): {"type": "disabled"} is a 400 on claude-sonnet-5-5 and
claude-opus-5-5 at every effort; on claude-opus-5 and claude-haiku-5-5 only at effort high or below."""
import os, tempfile
os.environ.setdefault("EMBER_DATA_DIR", tempfile.mkdtemp())
from app.config import Settings  # noqa: E402
from app.agent import prompts  # noqa: E402
from app.economy import pricing  # noqa: E402

base = Settings()
row = type(base.price_table[0])
extra = [
    row(model="claude-sonnet-5-5", input=2, output=10, cache_write_5m=2.5, cache_write_1h=4, cache_read=0.2),
    row(model="claude-opus-5", input=5, output=25, cache_write_5m=6.25, cache_write_1h=10, cache_read=0.5),
    row(model="claude-haiku-5-5", input=0.1, output=0.5, cache_write_5m=0.125, cache_write_1h=0.2, cache_read=0.01),
]
table = [*base.price_table, *extra]
REJECTS_DISABLED = {"claude-sonnet-5-5", "claude-opus-5-5"}
for model in ["claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-haiku-4-5", "claude-haiku-5-5"]:
    s = Settings(planner_model=model, worker_model=model, price_table=table)
    plan = prompts.plan_request(s, "ctx")
    work = prompts.work_request(s, "brief", [])
    t_plan, t_work = plan["thinking"]["type"], work["thinking"]["type"]
    bad = model in REJECTS_DISABLED and "disabled" in (t_plan, t_work)
    print(f"{model:18s} always_thinks={pricing.always_thinks(model)!s:5s} plan={t_plan:8s} work={t_work:8s} "
          f"plan.max_tokens={plan['max_tokens']:5d}{'  -> HTTP 400 on every call' if bad else ''}")
