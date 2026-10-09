"""Shared harness: an Agent built the way tests/test_loop_shapes.run builds one (fake model, dry run)."""
import os, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_ventures import VENTURING, DROPSHIPPING, SCORES, CASE  # noqa: E402

BASE = Path(os.environ["EMBER_DATA_DIR"])

def fresh(name, settings=VENTURING, fake=None, cycles=0, before=None):
    d = BASE / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    os.environ["EMBER_DATA_DIR"] = str(d)
    agent, ends = run(d, fake or FakeTransport(), cycles=cycles, before=before, settings=settings)
    return agent

def now(agent):
    return to_iso(agent.clock.now())

from app.agent import tools as _tools  # noqa: E402

def ctx(agent, cycle_id=0, **kw):
    return _tools.ToolContext(db=agent.db, clock=agent.clock, scope=agent.scope(), cycle_id=cycle_id,
                              workspace=agent.roots()[0], memory=agent.memory(), min_sleep=30, max_sleep=1440,
                              state=_tools.CycleTools(), **kw)

def call(agent, name, args, c=None):
    c = c or ctx(agent)
    with agent.db.transaction() as conn:
        try:
            out = _tools.HANDLERS[name](c, args, conn)
            return "OK: " + out.text
        except _tools.ToolError as exc:
            return "REFUSED: " + str(exc)
