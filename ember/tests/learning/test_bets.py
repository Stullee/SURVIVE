"""Bets: the agent writes what it expects of a change as Ember shows it, a refused bet keeps the rest of the
update, and an open bet is named first.

Tests from 0.19.2 and 0.24.0 (formerly test_fixes_0192.py, test_fixes_0240.py)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import bets, store  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from tests.channels.test_etsy import (  # noqa: E402
    call,
    shop_context,
    started,  # noqa: E402
)
from tests.cycle.test_agent import rows  # noqa: E402
from tests.cycle.test_loop_shapes import run  # noqa: E402
from tests.plan.test_roadmap import plan  # noqa: E402

# --- From 0.19.2 (formerly test_fixes_0192.py) ---


# --- bets ----------------------------------------------------------------------------------------------------------


def test_a_bet_can_be_written_as_ember_shows_it() -> None:
    today = date(2026, 10, 4)
    assert bets.parse("+15 views in 13 days (by 10-17): the SEO terms match", today) == (
        15,
        "views",
        13,
        "the SEO terms match",
    )
    assert bets.parse("+10 views by 2026-10-09 (+2 so far): the blog links them", today)[:3] == (10, "views", 5)
    assert bets.parse("+15 views by 10-17: x", today)[2] == 13
    assert bets.parse("+8 views by 17.10.: x", today)[2] == 13
    assert bets.parse("+5 Etsy listing views in 10 days: posts", today)[:3] == (5, "views", 10)
    assert bets.parse("+3 favourites in 14 days: x", today)[1] == "favorites"
    for wrong in ("+15 views by 2026-10-05: too soon", "+5 views by 13-45: no such day", "+15 views by 10-17: x"):
        with pytest.raises(bets.BetError, match="write it as"):
            bets.parse(wrong, None if wrong.endswith(": x") else today)


def test_a_refused_bet_keeps_the_rest_of_the_update(data_dir: Path) -> None:
    # 0.24.0: 0.19.2 refused it all ("nothing was changed"), and live the agent sent the bet again without its note
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    ctx = shop_context(agent)
    with agent.db.transaction() as conn:
        project = store.create_project(
            conn,
            agent.scope(),
            cycle_id=1,
            title="Posters",
            hypothesis="h",
            next_step="Old",
            status="active",
            now="2026-09-01T12:00:00Z",
        )
    made = call(ctx, "project_update", {"project_id": project, "note": "New", "bet": "more views soon"})
    assert made.ok and "Your bet was not placed: write it as" in made.text
    assert rows(agent, f"SELECT notes FROM projects WHERE id = {project}")[0]["notes"].endswith("New")
    alone = call(ctx, "project_update", {"project_id": project, "bet": "more views soon"})
    assert not alone.ok and alone.text.startswith("Error: bet: write it as")


# --- From 0.24.0 (formerly test_fixes_0240.py) ---


# --- bets -----------------------------------------------------------------------------------------------------------


def test_a_bet_s_why_may_follow_its_day_without_a_colon() -> None:
    today = date(2026, 10, 6)
    assert bets.parse("+5 views by 10-14 on listing #4584899301 from the new readable first photo", today) == (
        5,
        "views",
        8,
        "on listing #4584899301 from the new readable first photo",
    )
    said = "+5 views by 2026-10-14 from the tag refresh; under 3 means a category change"
    assert bets.parse(said, today)[:3] == (5, "views", 8)


def test_an_open_bet_is_named_first_and_the_rest_of_the_update_is_made(data_dir: Path) -> None:
    agent, project = started(data_dir)
    ctx = shop_context(agent)
    assert call(ctx, "project_update", {"project_id": project, "bet": "+10 views in 7 days: the new title"}).ok
    made = call(ctx, "project_update", {"project_id": project, "note": "Rebuilt the photo.", "bet": "+5 views soon"})
    assert made.ok and "Your bet was not placed: project #" in made.text
    assert "has an open bet on views (bet #1, +10 views by " in made.text and "a new one on it waits" in made.text
    [row] = rows(agent, f"SELECT notes FROM projects WHERE id = {project}")
    assert row["notes"].endswith("Rebuilt the photo.")
