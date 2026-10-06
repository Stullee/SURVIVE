"""0.15.0: reflection in every cycle, and a stopped cycle that keeps its handoff (FIX NOW 14, 2, 26; X6).

Live, every cycle since 0.12.0 wrote its journal in a work step, which skipped the reflection: nothing checked the
undone calls, wrote lessons or updated the strategy. A cycle the budget guard stopped was recorded as "the plan was
done", its journal named the refusal that followed the stop, the next plan lost the last handoff, and the owner's
comment on an approved request was marked seen by the cycle that was stopped. A cycle killed by a restart got no digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent import context, digest, prompts, tools
from app.agent.fake_llm import FakeTransport, Raw, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from app.config import LoadedSettings
from app.economy.metering import MeteredModel
from tests.economy_helpers import restart
from tests.test_agent import ROOMY, rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import APPROVAL, owner
from tests.test_roadmap import plan, planner_texts, section

HANDOFF = "Fix the 4 live listings under 5 photos first: each needs a different angle."
REFLECT_JOURNAL = ToolCalls(
    [("write_journal", {"summary": "Made the poster", "entry": "It went well.", "next": HANDOFF})]
)
# A cycle the model's refusal stops in its first work step: no reflection follows it (whatever the money guard allows
# after a stop of its own), so Ember's code writes its journal.
STOPPED = [plan(steps=["propose the poster"]), Reply("I can't help with that.", "refusal")]


def overrun(*calls: tuple[str, dict[str, Any]], number: int = 1) -> Raw:
    """A work reply billed far over its worst case: the budget guard stops the cycle (live: #44 and #46)."""
    return Raw(
        {
            "id": f"msg_overrun_{number}",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "tool_use", "id": f"toolu_overrun_{number}_{i}", "name": name, "input": args}
                for i, (name, args) in enumerate(calls)
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 400_000, "output_tokens": 50},
        }
    )


def purposes(agent: Agent, cycle_id: int) -> list[str]:
    return [r["purpose"] for r in rows(agent, f"SELECT purpose FROM llm_calls WHERE cycle_id = {cycle_id} ORDER BY id")]


def digest_of(agent: Agent, cycle_id: int) -> list[str]:
    [row] = rows(agent, f"SELECT text FROM cycle_digests WHERE cycle_id = {cycle_id}")
    return str(row["text"]).split("\n")


# --- FIX NOW 14: every cycle that worked reflects ---


def test_a_journal_written_while_working_ends_the_work_and_the_reflection_still_runs(data_dir: Path) -> None:
    # Live (#42, #43, #45): set_sleep and write_journal in a work step, one more work call that only reported, and no
    # reflection. Now write_journal is the reflection's: the work ends with that reply, and the reflection writes it.
    early = ToolCalls(
        [
            ("write_journal", {"summary": "Drafted the post", "entry": "What worked..."}),
            ("set_sleep", {"minutes": 600, "reason": "Waiting for the owner"}),
        ]
    )
    fake = FakeTransport(script=[plan(steps=["draft the post"]), early, REFLECT_JOURNAL])
    agent, ends = run(data_dir, fake)
    assert (ends[0].status, ends[0].sleep_minutes) == ("completed", 600)
    assert purposes(agent, 1) == ["plan", "work", "reflect"]
    journal = rows(agent, "SELECT phase, status, result FROM tool_calls WHERE tool = 'write_journal' ORDER BY id")
    # 0.24.0: kept as the reflection's draft, which the reflection may correct (it did: its journal is the one kept)
    assert [(j["phase"], j["status"]) for j in journal] == [("act", "ok"), ("reflect", "ok")]
    assert journal[0]["result"].startswith("Kept as your journal's draft")
    assert rows(agent, "SELECT author, summary, handoff FROM journal") == [
        {"author": "agent", "summary": "Made the poster", "handoff": HANDOFF}
    ]
    assert rows(agent, "SELECT act_end_reason FROM cycles") == [{"act_end_reason": "done"}]
    [reflection] = [r for r in fake.sent if request_kind(r) == "reflect"]
    told = reflection["messages"][-1]["content"][-1]["text"]
    assert "(you ended them)" in told and "Not done in this cycle" not in told  # the draft isn't work left undone
    assert prompts.JOURNAL_DRAFTED in told and prompts.JOURNAL_FIRST not in told
    lines = digest_of(agent, 1)
    assert lines[-2:] == ["Work ended: the agent ended it.", "Reflection: yes; journal by the agent."]
    assert not any(line.startswith("Not done") for line in lines)


def test_the_tool_says_the_journal_is_the_reflections() -> None:
    spec = tools.SPECS["write_journal"]
    assert spec.reflect and not spec.act
    assert "without a separate reflection" not in spec.description


def test_a_paused_researchs_continuation_leaves_the_reflections_reserve(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first research call is checked against the cycle's room less the reflection's reserve; its continuation
    # after pause_turn was sent without that check, and could spend the reflection's money.
    checked: list[tuple[int, int]] = []
    real = MeteredModel.affordable

    def affordable(self: MeteredModel, request: Any, purpose: str, cycle_id: int, *args: Any, **kw: Any) -> Any:
        fits, expected, worst = real(self, request, purpose, cycle_id, *args, **kw)
        if purpose != "research":
            return fits, expected, worst
        keep = args[0] if args else kw.get("keep", 0)
        checked.append((len(request["messages"]), keep))
        return len(request["messages"]) == 1, expected, worst  # the continuation doesn't fit

    monkeypatch.setattr(MeteredModel, "affordable", affordable)
    paused = Raw(
        {
            "id": "msg_paused",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "text", "text": "Poster prices start at 12 EUR."},
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "A3 posters"}},
            ],
            "stop_reason": "pause_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 40},
        }
    )
    ask = ToolCalls([("research", {"question": "What do A3 posters sell for?"})])
    fake = FakeTransport(script=[plan(steps=["research poster prices"]), ask, paused, Reply("Done."), REFLECT_JOURNAL])
    agent, ends = run(data_dir, fake)
    assert ends[0].status == "completed"
    assert [n for n, _ in checked] == [1, 2] and all(keep > 0 for _, keep in checked)
    assert purposes(agent, 1) == ["plan", "work", "research", "work", "reflect"]  # the continuation wasn't sent
    [research] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'research'")
    assert research["status"] == "ok" and "Poster prices start at 12 EUR." in research["result"]
    assert "The search paused and wasn't continued" in research["result"]  # not taken for a full search


# --- FIX NOW 2: a stopped cycle's digest and journal say how it really ended ---


def test_a_cycle_the_guard_stopped_mid_work_is_not_recorded_as_done(data_dir: Path) -> None:
    steps = ["Design poster #1 in the workshop", "Propose poster #1 as the first Printify product"]
    fake = FakeTransport(script=[plan(steps=steps), overrun(("workspace_list", {})), Reply("")])  # no journal
    agent, ends = run(data_dir, fake)
    assert ends[0].status == "stopped"
    assert rows(agent, "SELECT status, note FROM cycles") == [
        {"status": "stopped", "note": "a call cost more than its worst-case estimate"}
    ]
    lines = digest_of(agent, 1)
    assert lines[0].startswith("Cycle #1 stopped (a call cost more than its worst-case estimate) · $")
    assert "Work ended: the plan was done." not in lines
    assert f"Work ended: stopped at work step 1 of {ROOMY.max_tool_steps}, before its plan was done." in lines
    assert f"Its plan's steps (not all done): 1. {json.dumps(steps[0])}; 2. {json.dumps(steps[1])}" in lines
    [journal] = rows(agent, "SELECT author, summary, entry FROM journal")
    assert (journal["author"], journal["summary"]) == (
        "system",
        "Cycle ended stopped: a call cost more than its worst-case estimate",
    )
    entry = journal["entry"].split("\n")  # the code journal says it too, as the digest does
    assert f"Work ended: stopped at work step 1 of {ROOMY.max_tool_steps}, before its plan was done." in entry
    assert f"Its plan's steps (not all done): 1. {json.dumps(steps[0])}; 2. {json.dumps(steps[1])}" in entry


def test_only_the_journal_tried_while_working_is_left_out_of_not_done(data_dir: Path) -> None:
    # The early journal (0.24.0: kept as a draft) isn't work left undone; a refused set_sleep still is.
    early = ToolCalls(
        [
            ("set_sleep", {"minutes": -5, "reason": "Waiting"}),
            ("write_journal", {"summary": "Drafted the post", "entry": "What worked..."}),
        ]
    )
    fake = FakeTransport(script=[plan(steps=["draft the post"]), early, REFLECT_JOURNAL])
    agent, ends = run(data_dir, fake)
    assert ends[0].status == "completed"
    [not_done] = [line for line in digest_of(agent, 1) if line.startswith("Not done")]
    assert not_done.startswith("Not done (1): set_sleep (error: ")
    with agent.db.connection() as conn:
        [undone] = digest.undone(conn, 1)
    assert undone.startswith("set_sleep (error: ")


@pytest.mark.parametrize("status", ["failed", "interrupted", "stopped", "refused"])
def test_no_digest_of_an_unfinished_cycle_says_the_plan_was_done(data_dir: Path, status: str) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[]), cycles=0)
    cycle = agent.meter.open_cycle("schedule")
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE cycles SET plan = ?, phase = 'plan' WHERE id = ?",
            (json.dumps({"goal": "Make the poster", "steps": ["make it"]}), cycle),
        )
    agent.meter.close_cycle(cycle, status, "whatever ended it")
    with agent.db.connection() as conn:
        text, _ = digest.build(conn, cycle, status, "whatever ended it")
    assert "Work ended: the plan was done." not in text
    assert f"Work ended: {status}, before its plan was done." in text


# --- X6: a stopped cycle keeps the last handoff, and the owner's comment ---


def test_the_last_handoff_reaches_the_plan_after_a_stopped_cycle(data_dir: Path) -> None:
    first = [plan(steps=["make the poster"]), Reply("Done."), REFLECT_JOURNAL]
    fake = FakeTransport(script=[*first, *STOPPED, plan(steps=[])])
    agent, ends = run(data_dir, fake, cycles=3)
    assert [e.status for e in ends] == ["completed", "stopped", "idle"]
    shown = section(planner_texts(fake)[2], "YOUR LAST CYCLE")
    assert shown.startswith(
        f"Your last handoff, from cycle #1 (the cycles after it left none): {json.dumps(HANDOFF)}\n"
    )
    assert "Its journal:" not in shown  # Ember's code's journal of #2: its digest says more
    older = shown.split("\nCycle #1 completed", 1)[1]
    assert "Reflection: yes; journal by the agent." in older  # the older digest isn't cut down to its goal


def test_the_last_handoff_outlasts_an_idle_cycle(data_dir: Path) -> None:
    # An idle cycle's journal (the agent's, with no handoff) hid the handoff before it, after it and after a stopped
    # cycle that followed it.
    first = [plan(steps=["make the poster"]), Reply("Done."), REFLECT_JOURNAL]
    fake = FakeTransport(script=[*first, plan(steps=[]), *STOPPED, plan(steps=[])])
    agent, ends = run(data_dir, fake, cycles=4)
    assert [e.status for e in ends] == ["completed", "idle", "stopped", "idle"]
    line = f"Your last handoff, from cycle #1 (the cycles after it left none): {json.dumps(HANDOFF)}"
    after_idle, after_stop = (section(t, "YOUR LAST CYCLE") for t in planner_texts(fake)[2:4])
    assert after_idle.startswith(line + "\n")
    assert after_stop.startswith(line + "\n")


def test_each_digest_keeps_its_share_of_the_section() -> None:
    long = "\n".join(["Cycle #9 stopped · $0.5000", 'Goal: "x"', "Done (8): " + "workspace_list (ok); " * 60])
    older = "\n".join(["Cycle #8 completed · $0.1000", 'Goal: "y"', "Work ended: the agent ended it."])
    snap = context.Snapshot(
        status=None,  # type: ignore[arg-type]
        local_time="",
        version="",
        agent_name="",
        today_spend=0,
        daily_cap=0,
        cycle_cap=0,
        digests=[long, older],
    )
    text = context.last_cycle_text(snap, 1_000)
    assert context.json_bytes(text) <= 1_000
    assert text.endswith(older)  # whole: the newest digest takes only what the older one leaves
    assert "bytes cut]" in text.split("\nCycle #8", 1)[0]


def test_the_owners_comment_stays_news_until_a_cycle_that_saw_it_ends_normally(data_dir: Path) -> None:
    comment = "I approve, but the additional pictures just seem like duplicates."
    ask = ToolCalls([("request_approval", APPROVAL)])
    asking = [plan(steps=["ask to publish"]), ask, Reply("Asked."), REFLECT_JOURNAL]
    later = [plan(steps=["act on my owner's comment"]), Reply("Done."), REFLECT_JOURNAL]

    def decide(agent: Agent) -> None:
        agent.run_cycle("schedule")
        [approval] = rows(agent, "SELECT id FROM approvals")
        decided = owner(agent).decide(approval["id"], {"decision": "approve", "comment": comment}, "Stefan")
        assert decided.status == 200

    fake = FakeTransport(script=[*asking, *STOPPED, *later, plan(steps=[])])
    agent, ends = run(data_dir, fake, cycles=3, before=decide)
    assert [e.status for e in ends] == ["stopped", "completed", "idle"]
    plans = planner_texts(fake)[1:]
    line = f"Owner's comment: {json.dumps(comment)}"
    assert line in plans[0] and line in plans[1]  # the stopped cycle #2 saw it: #3 sees it again
    assert line not in plans[2]
    assert rows(agent, "SELECT seen_cycle_id FROM approvals") == [{"seen_cycle_id": 3}]


# --- FIX NOW 26d: a cycle killed by a restart gets its digest and journal ---


def test_a_cycle_killed_by_a_restart_gets_its_digest_and_journal(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[]), cycles=0)
    cycle = agent.meter.open_cycle("schedule")
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE cycles SET plan = ?, phase = 'act', step = 3, max_steps = 15 WHERE id = ?",
            (json.dumps({"goal": "Make the poster", "steps": ["make it", "propose it"]}), cycle),
        )
    economy = restart(agent.economy)  # the process died: the cycle was still running
    fresh = Agent(economy.db, LoadedSettings(ROOMY), economy, transport=FakeTransport(script=[]), cycles_enabled=True)
    fresh.recover()
    fresh.recover()  # once only
    lines = digest_of(fresh, cycle)
    assert lines[0].startswith(f"Cycle #{cycle} interrupted (the app stopped during this cycle) · $")
    assert "Work ended: interrupted at work step 3 of 15, before its plan was done." in lines
    assert rows(fresh, "SELECT author, summary FROM journal") == [
        {"author": "system", "summary": "Cycle ended interrupted: the app stopped during this cycle"}
    ]
    with fresh.db.connection() as conn:
        assert digest.latest(conn, fresh.scope())[0] == "\n".join(lines)  # what the next plan shows first


def test_only_the_cycles_killed_since_the_last_one_that_ended_get_records(data_dir: Path) -> None:
    # A cycle killed long ago, with cycles after it, is history: no journal is written for it at the next start (the
    # newest journal is what the next plan reads). Here the cycle after it has no digest either, as before 0.12.0.
    agent, _ = run(data_dir, FakeTransport(script=[]), cycles=0)
    old = agent.meter.open_cycle("schedule")
    economy = restart(agent.economy)  # killed; 0.13.0 wrote nothing for it
    later = economy.metered(FakeTransport(script=[])).open_cycle("schedule")
    economy.metered(FakeTransport(script=[])).close_cycle(later, "idle", "nothing to do")
    killed = economy.metered(FakeTransport(script=[])).open_cycle("schedule")
    economy = restart(economy)
    fresh = Agent(economy.db, LoadedSettings(ROOMY), economy, transport=FakeTransport(script=[]), cycles_enabled=True)
    fresh.recover()
    assert [r["status"] for r in rows(fresh, "SELECT status FROM cycles ORDER BY id")] == [
        "interrupted",
        "idle",
        "interrupted",
    ]
    assert rows(fresh, "SELECT cycle_id FROM cycle_digests") == [{"cycle_id": killed}]
    assert rows(fresh, "SELECT cycle_id FROM journal") == [{"cycle_id": killed}]
    assert old < later < killed
