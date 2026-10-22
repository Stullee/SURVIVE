"""Only work cuts the agent's sleep: chores, a channel just above its floor and a venture's step don't.

Tests from 0.39.6 and 0.40.2 (formerly test_fixes_0396.py, test_fixes_0402.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, weights  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.plan.test_cycle_line import lined, now  # noqa: E402
from tests.plan.test_plan_tree import keep  # noqa: E402

# --- From 0.39.6 (formerly test_fixes_0396.py) ---


# --- 1. only work cuts the agent's sleep ---


def works(kind: str, channel: float = 1.0, urgency: float = 0.0, product: int | None = 1) -> bool:
    return plan._works(weights.weigh(weights.Step(1, product, "a step", kind, 2.0, channel=channel, urgency=urgency)))


def test_only_work_cuts_the_agents_sleep() -> None:
    assert not works("chore") and not works("report")  # the critic's quiet suggestions, a views baseline
    assert works("fix") and works("ship") and works("create") and works("launch")
    assert works("create", product=None)  # a venture's step: exploring is work
    assert works("market") and not works("market", channel=weights.CHANNEL_MIN)  # a channel at its floor
    assert works("market", channel=weights.CHANNEL_MIN, urgency=weights.REACH)  # a launch's first marketing
    assert works("report", urgency=weights.RECURRING_DUE)  # a promise to report on its day


def steer(*steps: weights.Step) -> plan.Steer:
    ranked = tuple(weights.rank(steps))
    return plan.Steer(weights.Pick(None, "none", None, ranked), (), "ordinary")


def test_a_plan_with_chores_alone_leaves_the_agent_its_own_sleep(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    chore = weights.Step(901, 1, "The critic passes it", "chore", 2.0)
    report = weights.Step(902, 1, "Track views after the photo goes live", "report", 2.0)
    floor = weights.Step(903, 1, "This week's pin", "market", 2.0, channel=weights.CHANNEL_MIN)
    fix = weights.Step(904, 1, "Propose the book again", "fix", 2.5)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steer(chore, report, floor), now(agent))
        assert plan.busy(conn, agent.scope(), steer(chore, report, floor, fix), now(agent))


# --- From 0.40.2 (formerly test_fixes_0402.py) ---


# --- 1. a venture's step cuts the sleep only when it is urgent ---


@pytest.mark.exploring
def test_a_ventures_step_cuts_the_sleep_only_when_it_is_urgent(data_dir: Path) -> None:
    """Live on 2026-10-21 a brainstorm and a triage cut every sleep to 30 minutes from 11:03: six ideas, all parked,
    and the next brainstorm ready at once."""
    create = weights.weigh(weights.Step(1, None, "Brainstorm ideas", "create", 2.0))
    assert plan._works(create) and not plan._works(create, venture=True)  # noqa: SLF001
    urgent = weights.weigh(weights.Step(1, None, "Triage it", "create", 2.0, urgency=weights.RECURRING_DUE))
    assert plan._works(urgent, venture=True)  # noqa: SLF001 - the owner's wish, a park within days
    agent, _ = lined(data_dir, titles=())
    keep(agent)
    templates_ = ", ".join(f"'{t}'" for t in sorted(plan.VENTURE_TEMPLATES))
    venture = rows(agent, f"SELECT id FROM plan_nodes WHERE status = 'open' AND template IN ({templates_})")[0]
    step = weights.Step(venture["id"], None, "Brainstorm ideas", "create", 2.0)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steer(step), now(agent))
        wished = weights.Step(venture["id"], None, "Brainstorm ideas", "create", 2.0, urgency=weights.REACH)
        assert plan.busy(conn, agent.scope(), steer(wished), now(agent))
        product = weights.Step(999_999, 1, "Make the planner", "create", 2.0)  # a line's work, as before
        assert plan.busy(conn, agent.scope(), steer(product), now(agent))
