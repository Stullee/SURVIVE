"""The quality critic judges each listing of a product line, names the listing it judged, and checks the edited
one again; a verdict older than a change of its listing waits for the next check.

Tests from 0.24.0 and 0.40.1 (formerly test_fixes_0240.py, test_fixes_0401.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import plan as plan_tree  # noqa: E402
from app.agent import quality, ventures  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.etsy_helpers import call, listed, shop_context, started  # noqa: E402
from tests.products.test_kdp import project  # noqa: E402

# --- From 0.24.0 (formerly test_fixes_0240.py) ---


def now(agent: Any) -> str:
    return to_iso(agent.clock.now())


# --- the quality critic judges each listing, and names it ---------------------------------------------------------


def two_listings(data_dir: Path) -> tuple[Any, int, int, int]:
    """A product line with two live listings, as project #4 had: its first one and a licence bundle made later.
    (agent, project, first listing, second listing)"""
    agent, project = started(data_dir)
    [first] = [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    second = first + 1
    with agent.db.transaction() as conn:
        approval = dict(conn.execute("SELECT * FROM approvals WHERE executor = 'etsy_listing'").fetchone())
        approval.pop("id")
        approval["payload_sha256"] = "f" * 64
        made = conn.execute(
            f"INSERT INTO approvals ({', '.join(approval)}) VALUES ({', '.join('?' for _ in approval)})",
            list(approval.values()),
        ).lastrowid
        listing = dict(conn.execute("SELECT * FROM etsy_listings").fetchone())
        listing.pop("id")
        listing.update(approval_id=made, listing_id=second, title="Resume Template Commercial License Bundle")
        conn.execute(
            f"INSERT INTO etsy_listings ({', '.join(listing)}) VALUES ({', '.join('?' for _ in listing)})",
            list(listing.values()),
        )
    return agent, project, first, second


def checked(agent: Any, project: int, listing: int, fixes: str = "Fix the tags.") -> None:
    answer = {"score": 5, "verdict": "improve", "fixes": fixes}
    with agent.db.transaction() as conn:
        quality.save(conn, agent.scope(), project, None, answer, None, now(agent), listing)


def test_the_critic_checks_each_listing_of_a_line_and_the_edited_one_again(data_dir: Path) -> None:
    agent, project, first, second = two_listings(data_dir)
    scope, today = agent.scope(), agent.clock.today()
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, first)  # never checked: the oldest listing first
    checked(agent, project, first)
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, second)  # before, it judged only the newest one
        case, _ = quality.case(conn, scope, agent.roots()[0], project, second)
    assert f"LISTING: #{second}" in case and "Title: " in case
    agent.clock.advance(hours=1)
    checked(agent, project, second, "The licence lists a German Anschreiben the files lack.")
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) is None
    agent.clock.advance(hours=1)
    with agent.db.transaction() as conn:  # an edit of the first listing carried out: that one is checked again
        approval = conn.execute("SELECT MAX(id) AS id FROM approvals").fetchone()["id"]
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, finished_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'done')",
            (scope.mode, scope.session, approval, first, now(agent), now(agent)),
        )
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, first)


def test_every_verdict_names_the_listing_it_judged(data_dir: Path) -> None:
    agent, project, first, second = two_listings(data_dir)
    checked(agent, project, first, "Add the phrase bank.")
    checked(agent, project, second, "The cover letter is PDF only: add the .docx.")
    scope = agent.scope()
    with agent.db.connection() as conn:
        named = quality.label(conn, scope, second)
        older = quality.label(conn, scope, first)
        said = quality.review_text(conn, scope, project)
        items = quality.fixes(conn, scope, project)  # 0.35.0: YOUR STEP quotes them on the critic's step
        assert quality.verdict(conn, scope, project) == "improve"
    assert named == f'listing #{second} "Resume Template Commercial License Bundle"'
    assert said.startswith(f"quality 5/10, improve ({agent.clock.today().isoformat()}, {named}): The cover letter")
    assert f"{older}): Add the phrase bank." in said
    assert items == [
        f"the quality critic said of {named}: The cover letter is PDF only: add the .docx.",
        f"the quality critic said of {older}: Add the phrase bank.",
    ]


def test_a_verdict_older_than_a_change_of_its_listing_waits_for_the_next_check(data_dir: Path) -> None:
    """Live, the cover letter listing's check of 10-03 (upload the phrase bank) was done by an edit on 10-04."""
    agent, project, first, second = two_listings(data_dir)
    checked(agent, project, first, "Upload the phrase bank.")
    checked(agent, project, second, "Swap the tags 'resell rights' and 'white label'.")
    agent.clock.advance(hours=1)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        approval = conn.execute("SELECT MAX(id) AS id FROM approvals").fetchone()["id"]
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, finished_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'done')",
            (scope.mode, scope.session, approval, first, now(agent), now(agent)),
        )
    with agent.db.connection() as conn:
        ready = quality.fixes(conn, scope, project)
        said = quality.review_text(conn, scope, project)
        assert quality.due(conn, scope, agent.clock.today()) == (project, first)
    assert ready == [
        f"the quality critic said of {quality_label(agent, second)}: Swap the tags 'resell rights' and 'white label'."
    ]  # noqa: E501
    assert f"{quality_label(agent, first)}, changed at Etsy since): Upload the phrase bank." in said


def quality_label(agent: Any, listing: int) -> str:
    with agent.db.connection() as conn:
        return quality.label(conn, agent.scope(), listing)


def test_a_line_whose_work_the_owner_s_park_stopped_gets_no_check_and_no_step(data_dir: Path) -> None:
    """With 0.23.3 nothing may change, pin, post or recommend such a line's listings: no paid check, no step taken
    (0.35.0: its steps wait while the park holds; READY until 0.34.0)."""
    agent, project = started(data_dir)
    with agent.db.transaction() as conn:
        leg = ventures.create(conn, agent.scope(), title="A", pitch="p.", stage="building", now=now(agent))
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = ?", (leg, project))
    [listing] = [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    checked(agent, project, listing)
    with agent.db.connection() as conn:
        assert quality.fixes(conn, agent.scope(), project)
    assert owner(agent).decide_venture(leg, {"action": "park", "comment": "Stop."}, "Stefan").status == 200
    agent.clock.advance(days=quality.RECHECK_DAYS + 1)  # due again by its age, if it weren't stopped
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) is None
    with agent.db.transaction() as conn:
        plan_tree.keep(conn, agent.scope(), now(agent), agent.clock.today(), {})
        found = plan_tree.candidates(conn, agent.scope(), now(agent), agent.clock.today(), {})
    mine = [c for c in found if c.step.product == project]
    assert mine and {c.waiting for c in mine} == {"hold"}


def test_the_upgrade_names_the_listing_each_earlier_check_judged(data_dir: Path) -> None:
    """Before 0.24.0 the critic judged the newest listing of the line live by then: 0080 fills that in."""
    agent, project, first, second = two_listings(data_dir)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        live = conn.execute("SELECT finished_at FROM etsy_listings WHERE listing_id = ?", (second,)).fetchone()[0]
        before = to_iso(agent.clock.now() - timedelta(days=1))
        conn.execute("UPDATE etsy_listings SET finished_at = ? WHERE listing_id = ?", (before, first))
        for made in (before, live):  # one check before the bundle was live, one after
            conn.execute(
                "INSERT INTO quality_checks (mode, session, project_id, created_at, status, score, verdict, fixes)"
                " VALUES (?, ?, ?, ?, 'ok', 5, 'improve', 'x')",
                (scope.mode, scope.session, project, made),
            )
        sql = (paths.APP_DIR / "migrations" / "0080_quality_listing.sql").read_text(encoding="utf-8")
        conn.execute(sql[sql.index("UPDATE quality_checks") : sql.index("CREATE INDEX")])
    assert [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM quality_checks ORDER BY id")] == [
        first,
        second,
    ]


# --- From 0.40.1 (formerly test_fixes_0401.py) ---


# --- 7. etsy_listing shows the critic ---

FIXES = (
    "Add an editable cover letter .docx: the description promises Word/Docs editing, but that file is PDF only. Drop"
    " the 'resell rights' and 'white label' tags, since the licence forbids both."
)


def test_a_listing_read_shows_what_the_critic_found(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.5), a plan named "the critic's findings" for the coach bundle, but YOUR STEP was another
    step's and no tool showed them: the cycle researched the niche instead and staged a fix without them."""
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    assert "quality critic" not in call(ctx, "etsy_listing", {"listing_id": listing_id}).text  # none yet
    with agent.db.transaction() as conn:
        answer = {"score": 5, "verdict": "improve", "fixes": FIXES}
        quality.save(conn, agent.scope(), project(agent), None, answer, None, now(agent), listing_id)
    full = call(ctx, "etsy_listing", {"listing_id": listing_id}).text
    day = agent.clock.today().isoformat()
    critic = f"The quality critic ({day}: 5/10, improve): {FIXES}"
    assert critic in full and full.index(critic) < full.index("Description (Ember adds")  # before a long description
