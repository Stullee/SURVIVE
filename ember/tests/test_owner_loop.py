"""Phase 4: the owner's side of the queues, what the agent hears about it, and the kill switch."""

from __future__ import annotations

import ast
import json
import math
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.agent import news, store
from app.agent.owner import Owner, apply_kill_switch_reset, kill
from app.agent.service import Agent
from app.economy.life import KILLED_KEY
from tests.test_agent import make_agent, plan, rows, text, tools
from tests.test_owner_api import CSRF, post

APPROVAL = {
    "type": "publish",
    "title": "Post the guide",
    "description": "Publish the guide on my blog.",
    "payload": "Hello world, written by an AI.",
    "expected_cost": "none",
    "expected_benefit": "first readers",
}


def owner(agent: Agent) -> Owner:
    return Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)


def sent_text(request: dict[str, Any]) -> str:
    return json.dumps(request["messages"], ensure_ascii=False)


def cycle_with_approval() -> list[Any]:
    return [
        plan(steps=["ask to publish"]),
        tools(("request_approval", APPROVAL), ("message_owner", {"text": "Please look at request 1."})),
        text("Asked."),
        tools(("write_journal", {"summary": "Asked to publish", "entry": "Waiting."})),
    ]


def test_a_decision_reaches_the_next_plan_once(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir, [*cycle_with_approval(), plan(steps=[], sleep=600), plan(steps=[], sleep=600)]
    )
    assert agent.run_cycle("schedule").status == "completed"
    approval = rows(agent, "SELECT id, version, status FROM approvals")[0]
    assert approval["status"] == "pending"
    who = owner(agent)
    decided = who.decide(
        approval["id"],
        {"decision": "approve_with_changes", "final_payload": "Hi there, written by an AI.", "comment": "shorter"},
        "Stefan",
    )
    assert decided.status == 200 and decided.body["approval"]["status"] == "approved_with_changes"
    assert who.send_message({"text": "Good morning!"}, "Stefan").status == 201

    agent.run_cycle("schedule")
    planned = sent_text(transport.sent[-1])
    assert "approved with changes" in planned and "Hi there, written by an AI." in planned
    assert "shorter" in planned and "Good morning!" in planned
    seen = rows(
        agent, "SELECT seen_cycle_id FROM approvals UNION ALL SELECT seen_cycle_id FROM messages WHERE sender = 'owner'"
    )
    assert all(r["seen_cycle_id"] for r in seen)

    agent.run_cycle("schedule")
    third = sent_text(transport.sent[-1])
    assert "Hi there" not in third  # a decision is news once
    assert ', not answered yet): \\"Good morning!\\"' in third  # a message waits for an answer (0.9.1)


def test_decisions_are_checked(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, cycle_with_approval())
    agent.run_cycle("schedule")
    who = owner(agent)
    approval_id = rows(agent, "SELECT id FROM approvals")[0]["id"]
    assert who.decide(approval_id, {"decision": "maybe"}, None).body["field"] == "decision"
    assert who.decide(approval_id, {"decision": "approve_with_changes"}, None).body["field"] == "final_payload"
    assert who.decide(approval_id, {"decision": "approve", "extra": 1}, None).body["field"] == "extra"
    assert who.decide(approval_id, {"decision": "approve", "expected_version": 5}, None).status == 409
    assert who.close(approval_id, {"outcome": "done"}, None).status == 409  # not approved yet
    same = who.decide(approval_id, {"decision": "approve_with_changes", "final_payload": APPROVAL["payload"]}, "Stefan")
    assert same.body["approval"]["status"] == "approved"  # no change is a plain approval
    assert who.decide(approval_id, {"decision": "reject"}, None).status == 409
    assert who.close(approval_id, {"outcome": "failed"}, None).body["field"] == "result_note"
    bad = who.close(approval_id, {"outcome": "done", "result_link": "https://user:pw@example.com/x"}, None)
    assert bad.body["field"] == "result_link"
    done = who.close(approval_id, {"outcome": "done", "result_link": "https://example.com/post"}, "Stefan")
    assert done.status == 200 and done.body["approval"]["status"] == "done"
    assert who.decide(10_000, {"decision": "approve"}, None).status == 404


def test_the_database_keeps_decisions_final(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, cycle_with_approval())
    agent.run_cycle("schedule")
    approval_id = rows(agent, "SELECT id FROM approvals")[0]["id"]
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = 'done' WHERE id = ?", (approval_id,))
    owner(agent).decide(approval_id, {"decision": "reject", "comment": "no"}, None)
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET decision_comment = 'yes' WHERE id = ?", (approval_id,))
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = 'approved' WHERE id = ?", (approval_id,))


def test_upgrade_requests_and_inbox(data_dir: Path) -> None:
    upgrade = {
        "title": "Let me read RSS feeds",
        "problem": "I can't follow news.",
        "proposed_change": "An RSS tool.",
        "expected_benefit": "Better ideas.",
        "priority": "low",
    }
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["ask"]),
            tools(("request_upgrade", upgrade), ("message_owner", {"text": "Filed an upgrade request."})),
            text("Done."),
            tools(("write_journal", {"summary": "Asked for RSS", "entry": "."})),
            plan(steps=[], sleep=600),
        ],
    )
    agent.run_cycle("schedule")
    who = owner(agent)
    upgrade_id = rows(agent, "SELECT id FROM upgrades")[0]["id"]
    assert who.update_upgrade(upgrade_id, {"status": "released"}, None).body["field"] == "version"
    assert who.update_upgrade(upgrade_id, {"status": "accepted", "note": "next week"}, None).status == 200
    assert who.update_upgrade(upgrade_id, {"status": "accepted"}, None).status == 409
    assert who.update_upgrade(upgrade_id, {"status": "released", "version": "0.4.0"}, None).status == 200
    assert who.update_upgrade(upgrade_id, {"status": "declined"}, None).status == 409
    assert agent.dashboard()["badges"] == {
        "approvals_pending": 0,
        "approvals_todo": 0,
        "inbox_unread": 1,
        "upgrades_new": 0,
        "ventures_proposed": 0,
        "milestone_proposals": 0,
    }
    message_id = rows(agent, "SELECT id FROM messages WHERE sender = 'agent'")[0]["id"]
    assert who.mark_read({"up_to_id": message_id}).body == {"marked": 1}
    assert agent.dashboard()["badges"]["inbox_unread"] == 0
    assert who.send_message({"text": "  "}, None).body["field"] == "text"
    assert who.send_message({"text": "bad \x07 bell"}, None).body["field"] == "text"
    agent.run_cycle("schedule")
    planned = sent_text(transport.sent[-1])
    assert 'released in version \\"0.4.0\\"' in planned and "next week" in planned


def test_a_reply_reads_the_agents_earlier_messages(data_dir: Path) -> None:
    def cycle(*texts: str) -> list[Any]:
        calls = [("message_owner", {"text": t}) for t in texts]
        journal = ("write_journal", {"summary": "Wrote to my owner", "entry": "."})
        return [plan(steps=["report"]), tools(*calls), text("Done."), tools(journal)]

    agent, fake = make_agent(data_dir, [*cycle("Update 1.", "Update 2."), *cycle("Update 6.")])
    agent.run_cycle("schedule")
    scope, cycle_id = agent.scope(), rows(agent, "SELECT id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        for n in (3, 4, 5):
            store.insert_message(conn, scope, cycle_id, f"Update {n}.", "2026-09-27T10:00:00Z")
        other = replace(scope, session=scope.session + 1)  # another dry-run session's message stays unread
        store.insert_message(conn, other, cycle_id, "Elsewhere.", "2026-09-27T10:00:00Z")
    agent.run_cycle("schedule")
    last = "SELECT status, result FROM tool_calls WHERE tool = 'message_owner' ORDER BY id DESC LIMIT 1"
    refused = rows(agent, last)[0]
    assert refused["status"] == "error" and "hasn't read your last 5 messages" in refused["result"]

    # The owner reads and replies, but never clicks "Mark all read".
    assert owner(agent).send_message({"text": "Thanks, go on with #1."}, "Stefan").status == 201
    [theirs] = rows(agent, "SELECT id FROM messages WHERE sender = 'owner'")
    journal = ("write_journal", {"summary": "Answered my owner", "entry": "."})
    fake.outcomes += [
        plan(steps=["answer"]),
        tools(("message_owner", {"text": "Thanks!", "answers": str(theirs["id"])})),
        text("Done."),
        tools(journal),
    ]
    unread = rows(agent, "SELECT text FROM messages WHERE read_at IS NULL ORDER BY id")
    assert [m["text"] for m in unread] == ["Elsewhere.", "Thanks, go on with #1."]  # never the owner's own
    assert agent.dashboard()["badges"]["inbox_unread"] == 0

    agent.run_cycle("schedule")
    assert rows(agent, last)[0]["status"] == "ok"  # (it answers the owner: 0.12.0 limits only the unasked ones)
    assert agent.dashboard()["badges"]["inbox_unread"] == 1  # the new one is unread until the next reply


def test_a_reply_reads_only_what_the_owner_was_shown(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["ask"]),
            tools(("message_owner", {"text": "Question 1?"})),
            text("Asked."),
            tools(("write_journal", {"summary": "Asked my owner", "entry": "."})),
        ],
    )
    agent.run_cycle("schedule")
    shown = max(m["id"] for m in agent.dashboard()["inbox"] if m["sender"] == "agent")  # the dashboard's last poll
    with agent.db.transaction() as conn:  # a running cycle writes before the next poll
        cycle_id = rows(agent, "SELECT id FROM cycles")[0]["id"]
        store.insert_message(conn, agent.scope(), cycle_id, "May I publish?", "2026-09-27T10:00:00Z")
    who = owner(agent)
    for bad in (True, -1, "1", 1.5):
        refused = who.send_message({"text": "Yes.", "read_up_to": bad}, None)
        assert refused.status == 422 and refused.body["field"] == "read_up_to", bad
    assert rows(agent, "SELECT COUNT(*) AS n FROM messages WHERE sender = 'owner'") == [{"n": 0}]

    unread = "SELECT text FROM messages WHERE sender = 'agent' AND read_at IS NULL ORDER BY id"
    assert who.send_message({"text": "Answer to question 1.", "read_up_to": shown}, None).status == 201
    assert rows(agent, unread) == [{"text": "May I publish?"}]
    assert agent.dashboard()["badges"]["inbox_unread"] == 1
    assert who.send_message({"text": "One more thing.", "read_up_to": 0}, None).status == 201  # it showed none
    assert rows(agent, unread) == [{"text": "May I publish?"}]
    assert who.send_message({"text": "Yes, publish.", "read_up_to": 2**70}, None).status == 201
    assert rows(agent, unread) == [] and agent.dashboard()["badges"]["inbox_unread"] == 0


def test_only_a_released_upgrade_has_a_version(data_dir: Path) -> None:
    upgrade = {
        "title": "Let me read RSS feeds",
        "problem": "I can't follow news.",
        "proposed_change": "An RSS tool.",
        "expected_benefit": "Better ideas.",
        "priority": "low",
    }
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["ask"]),
            tools(("request_upgrade", upgrade)),
            text("Done."),
            tools(("write_journal", {"summary": "Asked for RSS", "entry": "."})),
        ],
    )
    agent.run_cycle("schedule")
    first, cycle_id = rows(agent, "SELECT id, cycle_id FROM upgrades")[0].values()
    with agent.db.transaction() as conn:
        second = store.insert_upgrade(conn, agent.scope(), cycle_id, "2026-09-27T10:00:00Z", **upgrade)
    who = owner(agent)
    for status in ("accepted", "declined"):
        for version in ("0.4.0", "soon", 4):
            refused = who.update_upgrade(first, {"status": status, "version": version}, None)
            assert refused.status == 422 and refused.body["field"] == "version", (status, version)
    new = rows(agent, f"SELECT status, released_version FROM upgrades WHERE id = {first}")
    assert new == [{"status": "new", "released_version": None}]
    assert who.update_upgrade(first, {"status": "accepted", "note": "next week"}, None).status == 200
    assert who.update_upgrade(first, {"status": "released", "version": "0.4"}, None).body["field"] == "version"
    assert who.update_upgrade(first, {"status": "released", "version": "0.4.0"}, None).status == 200

    # A version an earlier Ember stored for a request that wasn't released goes once the request is decided.
    assert who.update_upgrade(second, {"status": "accepted"}, None).status == 200
    with agent.db.transaction() as conn:
        conn.execute("UPDATE upgrades SET released_version = 'soon' WHERE id = ?", (second,))
    assert who.update_upgrade(second, {"status": "declined"}, None).status == 200
    assert rows(agent, "SELECT status, released_version FROM upgrades ORDER BY id") == [
        {"status": "released", "released_version": "0.4.0"},
        {"status": "declined", "released_version": None},
    ]


def test_the_kill_switch_and_its_reset(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [])
    economy = agent.economy
    assert apply_kill_switch_reset(agent.db, economy, 0) is False  # first start: remembered
    wrong = kill(agent.db, economy, "Ember", {"confirm_name": "ember"}, None)
    assert wrong.status == 422 and economy.status().state == "alive"
    done = kill(agent.db, economy, "Ember", {"confirm_name": "Ember", "reason": "testing"}, "Stefan")
    assert done.status == 200 and done.body["state"] == "killed"
    assert agent.decide().run is False
    end = agent.run_cycle("owner")
    assert end.status in ("skipped", "refused") and transport.sent == []
    assert apply_kill_switch_reset(agent.db, economy, 0) is False  # unchanged option: still killed
    assert economy.status().state == "killed"
    assert apply_kill_switch_reset(agent.db, economy, 1) is True
    assert economy.status().state == "alive"
    assert agent.db.get_meta(KILLED_KEY) == "0"


CHANGELOG = """<!-- notes -->

## 0.3.0

Third.

## 0.2.0

Second.

## 0.1.10

Tenth.

## 0.1.1

First fix.
"""


def test_changelog_news(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    first = news.changelog_news(path, None, "0.3.0")
    assert "Third." in first and "Second." not in first
    upgraded = news.changelog_news(path, "0.1.1", "0.3.0")
    assert upgraded.index("Third.") < upgraded.index("Second.") < upgraded.index("Tenth.")
    assert "First fix." not in upgraded and "from 0.1.1 to 0.3.0" in upgraded
    assert news.changelog_news(path, "0.3.0", "0.3.0") == ""
    assert "downgraded from 0.4.0 to 0.3.0" in news.changelog_news(path, "0.4.0", "0.3.0")
    assert news.changelog_news(tmp_path / "missing.md", None, "0.3.0") == ""


def test_the_agent_hears_about_its_upgrade_once(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(paths, "CHANGELOG_PATH", path)
    monkeypatch.setattr("app.agent.loop.app_version", lambda: "0.3.0")
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600), plan(steps=[], sleep=600)])
    agent.db.set_meta(news.changelog_key("dry_run"), "0.2.0")
    agent.run_cycle("schedule")
    assert "YOUR SOFTWARE" in sent_text(transport.sent[-1]) and "Third." in sent_text(transport.sent[-1])
    agent.run_cycle("schedule")
    assert "YOUR SOFTWARE" not in sent_text(transport.sent[-1])


def test_the_agent_never_imports_the_owner_side() -> None:
    agent_dir = Path(__file__).resolve().parent.parent / "app" / "agent"
    for source in agent_dir.glob("*.py"):
        if source.name == "owner.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
                assert "owner" not in names and not (node.module or "").endswith(".owner"), source.name
            elif isinstance(node, ast.Import):
                assert not any(a.name.endswith(".owner") for a in node.names), source.name


def test_owner_routes(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/inbox", {"text": "Hello Ember"}).status_code == 201
    inbox = ingress_client.get("api/dashboard").json()["inbox"]
    assert inbox[0]["text"] == "Hello Ember" and inbox[0]["entered_by"] == "Stefan"
    assert post(ingress_client, "api/approvals/7/decide", {"decision": "approve"}).status_code == 404
    assert post(ingress_client, "api/upgrades/7", {"status": "accepted"}).status_code == 404
    assert post(ingress_client, "api/inbox/read", {"up_to_id": 1}).json() == {"marked": 0}
    assert post(ingress_client, "api/control/kill", {"confirm_name": "nope"}).status_code == 422
    assert post(ingress_client, "api/control/kill", {"confirm_name": "Ember"}, headers={}).status_code == 403
    assert post(ingress_client, "api/control/kill", {"confirm_name": "Ember"}).json() == {"state": "killed"}
    sensors = ingress_client.get("api/sensors").json()
    assert sensors["state"] == "killed" and sensors["kill_switch_engaged"] is True
    assert {"approvals_pending", "inbox_unread", "next_wake_at"} <= set(sensors)
    assert post(ingress_client, "api/control/resume").json()["state"] == "killed"  # resume never clears a kill
    assert CSRF["X-Ember-Request"] == "1"


def test_a_cycle_cap_too_small_for_a_working_cycle_is_warned_about(data_dir: Path) -> None:
    from app.config import Settings
    from tests.economy_helpers import make_economy

    economy = make_economy(data_dir, Settings(daily_spend_cap_usd=1, cycle_spend_cap_usd=0.1))
    assert any("below one working cycle" in w for w in economy.warnings())
    economy.stop()
    roomy = make_economy(data_dir, Settings(daily_spend_cap_usd=1, cycle_spend_cap_usd=0.5))
    assert not any("below one working cycle" in w for w in roomy.warnings())


def test_the_default_caps_leave_room_and_a_tight_cycle_cap_is_warned_about(data_dir: Path) -> None:
    """0.12.0 (FIX NOW 19): the default cycle cap ($0.25) fit a working cycle by $0.0001, so any addition to the
    prompts or any rise of the safety factor stopped the scheduled wake-ups."""
    from app.config import Settings
    from app.economy.metering import usd_cap_to_micros
    from app.economy.pricing import working_cycle_cost
    from app.economy.service import ROOMY_CYCLE
    from tests.economy_helpers import make_economy

    defaults = make_economy(data_dir, Settings())
    working = working_cycle_cost(Settings(), defaults.db) or 0
    assert working * ROOMY_CYCLE <= usd_cap_to_micros(Settings().cycle_spend_cap_usd)
    assert usd_cap_to_micros(Settings().daily_spend_cap_usd) >= 3 * usd_cap_to_micros(Settings().cycle_spend_cap_usd)
    assert not any("cycle spend cap" in w for w in defaults.warnings())
    defaults.stop()
    tight = make_economy(data_dir, Settings(daily_spend_cap_usd=1, cycle_spend_cap_usd=0.30))
    [warning] = [w for w in tight.warnings() if "cycle spend cap" in w]
    roomy = math.ceil(working * ROOMY_CYCLE / 10_000) / 100
    assert warning == (
        "The cycle spend cap ($0.30) leaves little room for work: a working cycle (plan, a work step and the"
        f" reflection) can cost up to ${working / 1e6:.2f}, and below 1.5 times that (${roomy:.2f}) most cycles end"
        " after a step or two."
    )
    tight.stop()
    short = make_economy(data_dir, Settings(daily_spend_cap_usd=1, cycle_spend_cap_usd=0.2))
    assert [w for w in short.warnings() if "cycle spend cap" in w][0].endswith("may end right after its plan.")
