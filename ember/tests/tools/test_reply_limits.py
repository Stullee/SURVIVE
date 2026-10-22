"""0.12.0: follow-ups to 0.11.1. Every text a tool takes whole fits in one work reply (a payload of 8,000 characters,
an email of 5,000 or a listing description of 4,000 never could), the venture brief no longer contradicts the rule on
the owner's quick fixes, and a draft's photos are counted, each live listing's in its own line.

Also tests from 0.15.0 (formerly test_fixes_0140_live_fixes.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent import context, prompts, tools
from app.agent.fake_llm import FakeTransport
from app.integrations import etsy_publisher
from app.integrations.etsy import FakeShop, NotSent
from tests.agent_helpers import owner, rows, run
from tests.etsy_helpers import listed, proposed
from tests.plan.test_money_goal import call
from tests.plan.test_roadmap import plan as idle


def test_every_whole_text_fits_in_one_reply() -> None:
    assert prompts.WORK_MAX_TOKENS == tools.WORK_MAX_TOKENS  # the limits are derived from the reply's length
    assert int(prompts.WORK_MAX_TOKENS * 1.7) - 900 == tools.ONE_REPLY_CHARS == 2_500
    too_long = [
        f"{spec.name}.{name} ({f.max_len:,})"
        for spec in tools.SPECS.values()
        for name, f in spec.fields.items()
        if f.type == "string" and f.max_len > tools.ONE_REPLY_CHARS
    ]
    assert too_long == []
    for name in ("propose_etsy_listing", "propose_etsy_edit"):  # a listing's other fields come with its description
        assert tools.SPECS[name].fields["description"].max_len == tools.ONE_REPLY_CHARS - 500


def test_the_venture_brief_leaves_the_quick_fixes_to_the_rules() -> None:
    # 0.12.0: Ember's code makes a cycle with the owner's message waiting an ordinary one (test_obligations), and a
    # venture cycle has no tools for building or selling (test_tool_sets)
    assert "ordinary cycle" not in context.VENTURE_BRIEF
    assert "quick fix" not in prompts.VENTURE_RULES


def shop(agent: object) -> str:
    with agent.db.connection() as conn:  # type: ignore[attr-defined]
        return etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)  # type: ignore[attr-defined]


def test_each_live_listing_shows_its_photos(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    text = shop(agent)
    assert re.search(r"\(sold s, views v, favorites f, photos p\): #900000001 [^\n·]* 0s \?v \?f 1p\n", text)
    assert f"Fewer than 5 photos: #{listing_id} (1)." in text


def test_a_drafts_photos_are_counted(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = proposed(data_dir)

    def refuse(self: FakeShop, listing_id: int, name: str, data: bytes, rank: int) -> None:
        raise NotSent("HTTP 400: file too large")

    monkeypatch.setattr(FakeShop, "upload_file", refuse)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "draft")]
    text = shop(agent)
    assert "Fewer than 5 photos: #900000001 (1)." in text  # a draft had no count at all
    assert "live listings" not in text  # a draft isn't live


# --- From 0.15.0 (formerly test_fixes_0140_live_fixes.py) ---


# --- LIVE 6: length refusals with the numbers, notes cut --------------------------------------------------------------


def test_a_too_long_field_is_refused_with_its_length_and_what_to_do(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    long = call(agent, "workspace_write", path="drafts/article.md", mode="create", content="ä" * 2_742)
    assert not long.ok
    assert (  # 0.39.1: and how much to cut
        "content is too long: 2,742 of 2,500 characters (cut 242): create with the first part, then append the rest"
        in long.text
    )
    path = call(agent, "workspace_read", path="p" * 230 + ".md")
    assert not path.ok and "path is too long: 233 of 200 characters (cut 33)." in path.text  # no remedy: none needed


def test_owner_facing_notes_are_cut_not_refused(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    filed = call(
        agent,
        "request_upgrade",
        title="Read big PNGs",
        problem="p" * 742,
        proposed_change="c" * 650,
        expected_benefit="Unblocks the poster venture.",
        priority="high",
    )
    assert filed.ok and "problem was cut to 600 of its 742 characters" in filed.text, filed.text
    [row] = rows(agent, "SELECT problem, proposed_change FROM upgrades")
    assert len(row["problem"]) == 600 and row["problem"].endswith("…") and len(row["proposed_change"]) == 600
    assert tools.SPECS["venture_update"].fields["learned"].cut


# --- LIVE 10: a folder read as a file --------------------------------------------------------------------------------


def test_reading_a_folder_points_at_workspace_list(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    assert call(agent, "workspace_write", path="shop/notes.md", mode="create", content="Hi").ok
    read = call(agent, "workspace_read", path="shop")
    assert not read.ok and "shop is a folder: workspace_list lists it" in read.text
    missing = call(agent, "workspace_read", path="nothing")
    assert not missing.ok and "only text files" in missing.text
