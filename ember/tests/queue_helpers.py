"""0.37.8: the publishing queue in tests: an approved pin or post goes out in its slot; a test moves the clock."""

from __future__ import annotations

from typing import Any

from app.integrations import publish_queue


def per_day(agent: Any, channel: str) -> int:
    return agent.settings.pinterest_pins_per_day if channel == "pinterest" else agent.settings.bluesky_posts_per_day


def next_slot(agent: Any, channel: str) -> publish_queue.Slot:
    with agent.db.connection() as conn:
        found = publish_queue.slots(conn, agent.scope(), agent.clock, channel, per_day(agent, channel))
    assert found, f"nothing waits in the {channel} queue"
    return found[0]


def slot_comes(agent: Any, channel: str) -> None:
    """The clock at the slot of the channel's next approved request (it goes out at the next round)."""
    agent.clock.current = max(agent.clock.current, next_slot(agent, channel).at)
