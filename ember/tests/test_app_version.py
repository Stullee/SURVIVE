"""Every wake cycle, model call and daily review records the version of Ember that ran it (0.12.0)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.agent import review
from app.version import app_version
from tests.economy_helpers import ScriptedTransport
from tests.test_agent import plan, rows, text
from tests.test_agent import tools as calls


def test_every_cycle_call_and_review_records_the_version_that_ran_it(ingress_client: TestClient) -> None:
    """0.12.0: 21 releases came in about 46 hours, and no cycle recorded which one ran it."""
    agent = ingress_client.app.state.ember.agent
    agent.transport = ScriptedTransport(
        simulated=True, outcomes=[plan(steps=[]), text("Done."), calls(("write_journal", {"summary": "s"}))]
    )
    agent.meter = agent.economy.metered(agent.transport)
    cycle_id = agent.run_cycle("owner").cycle_id
    version = app_version()
    with agent.db.transaction() as conn:
        review.save(conn, agent.scope(), cycle_id, "now", agent.clock.today(), review.Scorecard("card"), None, "n")
    assert rows(agent, "SELECT app_version FROM reviews")[0]["app_version"] == version
    assert {r["app_version"] for r in rows(agent, "SELECT app_version FROM cycles")} == {version}
    assert {r["app_version"] for r in rows(agent, "SELECT app_version FROM llm_calls")} == {version}
    assert ingress_client.get("api/dashboard").json()["activity"][0]["app_version"] == version
    assert f" version={version}\n" in ingress_client.get("api/diagnostics").text
    with pytest.raises(sqlite3.IntegrityError, match="fixed"), agent.db.transaction() as conn:
        conn.execute("UPDATE cycles SET app_version = 'other'")
    with pytest.raises(sqlite3.IntegrityError, match="fixed"), agent.db.transaction() as conn:
        conn.execute("UPDATE llm_calls SET app_version = 'other'")
