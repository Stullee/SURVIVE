"""0.47.0 (vision/generations.md): the estate and the generation change. The owner sorts the world's products (adopt,
passive, retire; Ember's code suggests a verdict by its rule) and begins the next generation: Ember's life ends by
their choice, not by money, and the next begins with a clean mind in the same world. Her old folders move aside, her
per-mode keys start over, and the products she adopts become her lines at her first cycle, with their files copied.

A dress rehearsal in dry run: the same change the live agent will make, on the fake shop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import estate, generation, memory, store, ventures  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.etsy_helpers import listed, proposed  # noqa: E402
from tests.generation_helpers import decide_all, files_in, settled  # noqa: E402

# --- the estate ---


def test_the_estate_is_the_worlds_products_with_the_rules_suggestion(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    with agent.db.transaction() as conn:
        [row] = estate.refresh(conn, agent.scope(), agent.clock)
    assert (row["kind"], row["ref"], row["listing_id"]) == ("etsy_listing", str(listing), listing)
    assert row["suggested"] == "adopt" and row["suggested_why"].startswith("too new to judge: 0 days live")
    assert row["verdict"] is None and row["to_session"] is None and row["from_session"] == agent.scope().session
    numbers = json.loads(row["numbers"])
    assert numbers["state"] == "active" and numbers["sold"] == 0 and numbers["rule"] == ""
    agent.clock.advance(days=30)
    with agent.db.transaction() as conn:
        [row] = estate.refresh(conn, agent.scope(), agent.clock)  # read again, the same row
    assert row["suggested"] == "passive" and row["suggested_why"].startswith("a fair test without a buyer's signal")


@pytest.mark.parametrize(
    ("kind", "numbers", "verdict", "why"),
    [
        ("etsy_listing", {"state": "active", "days": 3}, "adopt", "too new to judge"),
        ("etsy_listing", {"state": "active", "days": 40, "favorites": 2}, "adopt", "a buyer's signal: 2 favorites"),
        ("etsy_listing", {"state": "active", "days": 40, "sold": 1}, "adopt", "a buyer's signal: 1 sold"),
        ("etsy_listing", {"state": "active", "days": 40, "views": 3, "reach": 19}, "passive", "a fair test"),
        ("etsy_listing", {"state": "expired", "days": 40}, "retire", "not live at Etsy (expired)"),
        ("etsy_listing", {"state": "active", "days": 3, "rule": "it says a person made it"}, "retire", "breaks a rule"),
        ("printify_product", {"state": "publishing", "days": 2}, "adopt", "too new to judge"),
        ("kdp_book", {"days": 4, "royalties": 0}, "adopt", "too new to judge: published 4 days ago"),
        ("kdp_book", {"days": 60, "royalties": 0}, "passive", "without a sale recorded"),
        ("kdp_book", {"days": 60, "royalties": 2}, "adopt", "2 royalty entries"),
        ("kdp_book", {"gone": True}, "retire", "gone from the shop"),
    ],
)
def test_the_rule_adopts_the_new_and_the_signalled_and_rests_a_fair_test(
    kind: str, numbers: dict[str, Any], verdict: str, why: str
) -> None:
    suggested, said = estate.suggest(kind, numbers)
    assert suggested == verdict and why in said and len(said) <= 300


def test_an_adopted_product_has_a_first_test_and_a_verdict_can_change(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    [row] = decide_all(agent, "adopt")
    today = agent.clock.today()
    assert (row["verdict"], row["test_metric"], row["test_target"]) == ("adopt", "favorites", 1)
    assert row["test_due"] == (today.fromordinal(today.toordinal() + 28)).isoformat()  # the default: 4 weeks
    with agent.db.transaction() as conn:
        wrong = (({"due": today.isoformat()}, "due"), ({"metric": "sales"}, "metric"), ({"target": 0}, "target"))
        for test, field in wrong:
            with pytest.raises(estate.EstateError) as caught:
                estate.decide(conn, agent.scope(), agent.clock, "etsy_listing", str(listing), "adopt", "Owner", test)
            assert caught.value.field == field
        with pytest.raises(estate.EstateError):
            estate.decide(conn, agent.scope(), agent.clock, "etsy_listing", str(listing), "passive", "Owner", {"x": 1})
        row = estate.decide(conn, agent.scope(), agent.clock, "etsy_listing", str(listing), "passive", "Owner")
    assert (row["verdict"], row["test_metric"], row["test_due"]) == ("passive", None, None)


def test_an_evaluations_proposal_sits_beside_the_rules(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    with agent.db.transaction() as conn:
        estate.refresh(conn, agent.scope(), agent.clock)
        estate.propose(
            conn,
            agent.scope(),
            agent.clock,
            "etsy_listing",
            str(listing),
            "passive",
            "3 views in 24 days with 19 pins and posts: Etsy's search shows it to no one",
            "evaluation 11-01",
        )
        [row] = estate.handover(conn, agent.scope())
    assert (row["suggested"], row["proposed"], row["verdict"]) == ("adopt", "passive", None)  # the owner decides
    assert row["proposed_by"] == "evaluation 11-01" and "Etsy's search" in row["proposed_why"]


# --- the change ---


def test_the_change_waits_for_a_request_and_for_every_products_verdict(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    before = agent.scope()
    workspace = Path(agent.roots()[0].root)
    with pytest.raises(generation.GenerationError) as caught:
        agent.begin_generation("Owner")
    checks = {c.name: c for c in caught.value.checks}
    assert not checks["No request waits"].ok and f"#{request}" in checks["No request waits"].detail
    assert agent.scope() == before and workspace.is_dir()  # nothing changed
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    for other in rows(agent, f"SELECT id FROM approvals WHERE status = 'pending' AND id <> {request}"):
        owner(agent).decide(other["id"], {"decision": "reject", "comment": "Not now."}, "Owner")
    with pytest.raises(generation.GenerationError) as caught:
        agent.begin_generation("Owner")
    checks = {c.name: c for c in caught.value.checks}
    assert checks["No request waits"].ok
    assert not checks["Every product has a verdict"].ok and "Etsy listing 900000001" in (
        checks["Every product has a verdict"].detail
    )
    decide_all(agent, "retire")
    with pytest.raises(generation.GenerationError) as caught:
        agent.begin_generation("Owner")
    checks = {c.name: c for c in caught.value.checks}
    assert not checks["The retired products are down"].ok  # still live at Etsy


def test_the_change_ends_her_life_and_begins_a_clean_mind_in_the_same_world(data_dir: Path) -> None:
    agent, listing = settled(data_dir)
    old = agent.scope()
    workspace, memory_root = (Path(j.root) for j in agent.roots())
    kept = files_in(workspace)
    old_lessons = (memory_root / "lessons.md").read_text(encoding="utf-8")
    agent.db.set_meta(f"agent.{old.mode}.sleep_cut", '{"cycle": 4}')
    [decided] = decide_all(agent, "adopt")
    done = agent.begin_generation("Owner")
    new = agent.scope()
    assert done["generation"] == 2 and (new.session, new.world_session) == (old.session + 1, old.world_session)
    lives = rows(agent, "SELECT id, state, end_reason, revived_from_life_id FROM lives WHERE mode = 'dry_run'")
    ended, born = lives[-2], lives[-1]
    assert (ended["id"], born["id"]) == (old.life_id, new.life_id) == (done["ended"], done["born"])
    assert ended["state"] == "ended" and ended["end_reason"].startswith("succeeded by generation 2")
    assert born["revived_from_life_id"] == old.life_id
    assert agent.economy.life.evaluate().state == "alive"  # the money carried on
    assert agent.db.get_meta(f"agent.{old.mode}.sleep_cut") is None  # the old mind's
    assert (paths.backups_dir() / done["backup"]).is_file()
    # her old folders are kept beside the new ones, which begin with the seeds, the adopted listing's files and (0.48.0)
    # the facts of her predecessor's work
    archive = workspace.parent / "generations" / "1"
    assert files_in(archive / "workspace") == kept
    assert (archive / "memory" / "lessons.md").read_text(encoding="utf-8") == old_lessons
    assert (memory_root / "lessons.md").read_text(encoding="utf-8") == memory.SEEDS["lessons"]
    assert done["copied"] and files_in(workspace) == sorted(done["copied"] + ["inheritance/facts.md"])
    # her records: empty but for what she received
    with agent.db.connection() as conn:
        assert store.open_projects(conn, new) == []
        assert store.pending_requests(conn, new) == []
        [received] = estate.received(conn, new)
    assert (received["id"], received["verdict"], received["project_id"]) == (decided["id"], "adopt", None)
    # at her first cycle the adopted listing becomes her line, and STATUS says who she is
    agent.run_cycle("schedule")
    with agent.db.connection() as conn:
        [line] = store.open_projects(conn, new)
        assert ventures.listing_line(conn, new, listing) == ("etsy", line["id"])
        [received] = estate.received(conn, new)
        context = store.plan_context(conn, rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"])
    assert received["project_id"] == line["id"] and "first test, your owner's: 1 favorites by" in line["hypothesis"]
    assert context is not None and "You are generation 2 of this agent" in context
    assert "1 of her products as lines of yours" in context


def test_a_change_that_fails_leaves_everything_as_it_was(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = settled(data_dir)
    old = agent.scope()
    workspace = Path(agent.roots()[0].root)
    kept = files_in(workspace)
    decide_all(agent, "passive")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(agent.economy.life, "succeed", broken)
    with pytest.raises(RuntimeError):
        agent.begin_generation("Owner")
    assert agent.scope() == old and files_in(workspace) == kept
    assert not (workspace.parent / "generations" / "1" / "workspace").exists()
    with agent.db.connection() as conn:
        assert all(r["to_session"] is None for r in estate.handover(conn, old))


def test_a_passive_product_is_the_new_minds_but_not_hers_to_work_on(data_dir: Path) -> None:
    agent, listing = settled(data_dir)
    decide_all(agent, "passive")
    agent.begin_generation("Owner")
    agent.run_cycle("schedule")
    scope = agent.scope()
    with agent.db.connection() as conn:
        [received] = estate.received(conn, scope)
        assert received["project_id"] is None  # no line for it
        assert rows(agent, f"SELECT 1 FROM listing_lines WHERE session = {scope.session}") == []
        assert estate.not_hers(conn, scope, listing) is not None
        assert estate.not_hers_ids(conn, scope) == {listing}
        assert etsy_listing_row_exists(conn, scope, listing)  # the world still has it


def etsy_listing_row_exists(conn: Any, scope: Any, listing: int) -> bool:
    from app.integrations import etsy_publisher

    return etsy_publisher.listing_row(conn, scope, listing) is not None
