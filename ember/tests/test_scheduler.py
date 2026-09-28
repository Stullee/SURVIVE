"""The scheduler's rounds: a poke that arrives while a round is still running starts the next round at once."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agent.scheduler import ROUND_SECONDS, Scheduler
from app.agent.service import Decision


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
            running_cycle=False, stop=threading.Event(), execute_approved=lambda: None, decide=decide
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
