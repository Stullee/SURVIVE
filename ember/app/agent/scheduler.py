"""The one background task: re-evaluate the economy, send approved emails, read the Etsy shop, wake the agent when
it's time.

It runs on the event loop but does every piece of database or agent work in a
worker thread (``asyncio.to_thread``); only one cycle runs at a time. Between
rounds it sleeps until the next wake-up, a poke (Wake now, a grant, resume, the
owner's decision on an email) or at most a minute, so the life state stays
current even without cycles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime

from ..db import Database
from ..economy.service import Economy
from ..events import KEEP_EVENTS
from .service import Agent

log = logging.getLogger(__name__)

ROUND_SECONDS = 60.0
PRUNE_EVERY_SECONDS = 300.0
STOP_WAIT_SECONDS = 8.0  # Home Assistant kills the app 10 s after asking it to stop


class Scheduler:
    def __init__(self, db: Database, economy: Economy, agent: Agent | None) -> None:
        self.db = db
        self.economy = economy
        self.agent = agent
        self.status = "starting"
        self.last_error: str | None = None
        self._stopping = False
        self._poke: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_prune = 0.0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._poke = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="ember-scheduler")

    def poke(self) -> None:
        """Decide again now (safe to call from any thread)."""
        if self._loop is not None and self._poke is not None:
            self._loop.call_soon_threadsafe(self._poke.set)

    async def stop(self) -> None:
        self._stopping = True
        if self.agent is not None:
            self.agent.stop.set()
        self.poke()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), STOP_WAIT_SECONDS)
            except TimeoutError:
                log.warning("A wake cycle was still running at shutdown; it will be marked interrupted")
                self._task.cancel()
            except asyncio.CancelledError:
                pass

    @property
    def busy(self) -> bool:
        return bool(self.agent and self.agent.running_cycle)

    async def _run(self) -> None:
        while not self._stopping:
            timeout = ROUND_SECONDS
            if self._poke is not None:
                # Cleared before the round, not after it: a poke that arrives while the round runs (a message that
                # comes in while decide() is looking) makes the next round start at once instead of being lost.
                self._poke.clear()
            try:
                await asyncio.to_thread(self.economy.tick)
                if time.monotonic() - self._last_prune > PRUNE_EVERY_SECONDS:
                    await asyncio.to_thread(self.db.prune_events, KEEP_EVENTS)
                    try:  # 0.15.0: and the old texts of the model calls and tool calls (db.TEXT_DAYS)
                        await asyncio.to_thread(self.db.prune_texts, self.economy.clock.now())
                    except Exception:  # noqa: BLE001 - pruning must not stop the wake cycles
                        log.exception("Pruning old texts failed")
                    self._last_prune = time.monotonic()
                if self.agent is not None and not self._stopping:
                    try:  # 0.13.0: the owner's unlocks first: what their veto windows let through, and revocations
                        await asyncio.to_thread(self.agent.run_policy)
                    except Exception:  # noqa: BLE001 - the unlocks must not stop the wake cycles
                        log.exception("Running the owner's unlocks failed")
                    try:
                        await asyncio.to_thread(self.agent.execute_approved)
                    except Exception:  # noqa: BLE001 - a sending problem must not stop the wake cycles
                        log.exception("Sending approved emails failed")
                    try:
                        await asyncio.to_thread(self.agent.sync_shop)  # at most hourly, also while the agent sleeps
                    except Exception:  # noqa: BLE001 - the shop must not stop the wake cycles
                        log.exception("Checking the Etsy shop failed")
                    try:  # 0.16.0: the live view on the owner's website, every 15 minutes (also while it sleeps)
                        await asyncio.to_thread(self.agent.publish_live)
                    except Exception:  # noqa: BLE001 - the website must not stop the wake cycles
                        log.exception("Uploading the live view failed")
                    try:  # 0.13.0: the agenda: what happened since the last look (an urgent event wakes the agent)
                        await asyncio.to_thread(self.agent.check_events)
                    except Exception:  # noqa: BLE001 - the agenda must not stop the wake cycles
                        log.exception("Noting the agenda's events failed")
                    decision = await asyncio.to_thread(self.agent.decide)
                    if decision.run and decision.trigger:
                        self.status = f"running a {decision.trigger} cycle"
                        end = await asyncio.to_thread(self.agent.run_cycle, decision.trigger)
                        if end.status in ("completed", "idle") or end.rerun:
                            continue  # decide again right away (the next wake-up is set now)
                        # 0.15.0: after a refused, failed or stopped cycle, a round before the next decision: a wake
                        # that ignored its back-off ran a refused cycle every round until midnight.
                        self.status = f"after a {end.status} cycle"
                    else:
                        self.status = decision.reason or "waiting"
                        if decision.wait_until is not None:
                            timeout = _seconds_until(decision.wait_until, self.economy.clock.now())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the scheduler must keep going
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Scheduler round failed")
            if self._stopping or self._poke is None:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), timeout)
        self.status = "stopped"


def _seconds_until(moment: datetime, now: datetime) -> float:
    return max(1.0, min(ROUND_SECONDS, (moment - now).total_seconds()))
