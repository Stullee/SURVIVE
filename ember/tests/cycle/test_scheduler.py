"""The scheduler's rounds: a poke that arrives while a round is still running starts the next round at once, and a
failing check of the Etsy shop never stops them.

Also tests from 0.12.0 (formerly test_small_defects.py)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.scheduler import ROUND_SECONDS, Scheduler
from app.agent.service import Agent, Decision
from app.economy.clock import from_iso
from tests.cycle.test_agent import make_agent, plan


def test_a_poke_during_a_round_starts_the_next_round_at_once() -> None:
    """A message that comes in while decide() is looking must not wait for the next minute's round."""
    rounds: list[float] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        scheduler: Scheduler | None = None

        def decide() -> Decision:
            rounds.append(loop.time())
            if len(rounds) == 1 and scheduler is not None:
                scheduler.poke()  # the owner's message arrives during this round
            return Decision(False, reason="waiting")

        agent = SimpleNamespace(
            running_cycle=False,
            stop=threading.Event(),
            execute_approved=lambda: None,
            sync_shop=lambda: None,
            decide=decide,
        )
        economy = SimpleNamespace(tick=lambda: None, clock=SimpleNamespace(now=lambda: datetime.now(UTC)))
        db = SimpleNamespace(prune_events=lambda keep: None)
        scheduler = Scheduler(db, economy, agent)  # type: ignore[arg-type]
        scheduler.start()
        for _ in range(100):
            if len(rounds) >= 2:
                break
            await asyncio.sleep(0.02)
        await scheduler.stop()

    asyncio.run(main())
    assert len(rounds) >= 2
    assert rounds[1] - rounds[0] < ROUND_SECONDS / 10  # the poke wasn't lost


def test_a_failing_check_of_the_etsy_shop_never_stops_the_rounds(caplog: pytest.LogCaptureFixture) -> None:
    steps: list[str] = []
    schedulers: list[Scheduler] = []

    async def main() -> None:
        def sync_shop() -> None:
            steps.append("shop")
            raise RuntimeError("Etsy is down")

        def decide() -> Decision:
            steps.append("decide")
            if steps.count("decide") == 1:
                schedulers[0].poke()  # go round again at once
            return Decision(False, reason="waiting")

        agent = SimpleNamespace(
            running_cycle=False,
            stop=threading.Event(),
            execute_approved=lambda: None,
            sync_shop=sync_shop,
            decide=decide,
        )
        economy = SimpleNamespace(tick=lambda: None, clock=SimpleNamespace(now=lambda: datetime.now(UTC)))
        db = SimpleNamespace(prune_events=lambda keep: None)
        schedulers.append(Scheduler(db, economy, agent))  # type: ignore[arg-type]
        schedulers[0].start()
        for _ in range(100):
            if steps.count("decide") >= 2:
                break
            await asyncio.sleep(0.02)
        await schedulers[0].stop()

    asyncio.run(main())
    assert steps[:4] == ["shop", "decide", "shop", "decide"]  # every round checks the shop, then decides
    assert schedulers[0].last_error is None and "Checking the Etsy shop failed" in caplog.text


# --- From 0.12.0 (formerly test_small_defects.py) ---


def test_a_failure_while_scheduling_never_leaves_the_old_wake_time(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600), plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    before = agent.db.get_meta("agent.dry_run.next_wake_at") or agent.db.get_meta("agent.live.next_wake_at")
    agent.clock.advance(days=1)  # the scheduled time is long past

    def broken(self: Agent, trigger: str, end: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(Agent, "_after", broken)
    agent.run_cycle("schedule")
    key = "agent.dry_run.next_wake_at" if agent.mode == "dry_run" else "agent.live.next_wake_at"
    after = agent.db.get_meta(key)
    assert after is not None and after != before
    assert from_iso(after) > agent.clock.now()  # never a time already past, which woke the agent again at once
    reason = agent.db.get_meta(key.replace("next_wake_at", "next_wake_reason")) or ""
    assert reason.startswith("working out the next wake failed")
    assert any("Working out the next wake failed" in e["message"] for e in agent.db.recent_events(limit=10))
