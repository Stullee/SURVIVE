"""The diagnostics report the owner copies when something looks wrong: complete where it matters, never secret."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import diagnostics, logging_setup
from app.agent.store import canonical
from tests.economy_helpers import ScriptedTransport
from tests.test_agent import plan, reply, text, tools
from tests.test_owner_api import post

QUESTION = "Printable meal-planning templates sounds like a good idea, will you create them by image generation"
NOW = "2026-09-28T01:00:00Z"
TIMESTAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
PROJECT = {
    "title": "Printable meal-planning templates",
    "hypothesis": "Busy parents pay 3 EUR for a printable weekly plan",
    "next_step": "outline",
    "status": "active",
}
DIGEST = "".join(f"- Planner {n}: {n + 2} EUR on Etsy; bundles with shopping lists sell best.\n" for n in range(12))
DRAFT = "Monday: lentil soup, bread and a green salad.\n" * 80
DRAFT_CALL = {"path": "projects/meal-plans.md", "mode": "create", "content": DRAFT}
CYCLE = [
    plan(focus=None),
    tools(
        ("workspace_list", {}),
        ("project_create", PROJECT),
        ("research", {"question": "Which printable meal-planning templates sell best online, and at what prices?"}),
    ),
    reply([{"type": "text", "text": DIGEST}], "end_turn"),  # the research call
    tools(
        ("workspace_write", DRAFT_CALL),
        ("message_owner", {"text": "No, I can't generate images: I will write text templates."}),
    ),
    text("Drafted."),
    tools(
        ("write_journal", {"summary": "Started meal plans", "entry": "Drafted a week."}),
        ("set_sleep", {"minutes": 120, "reason": "wait for my owner"}),
    ),
]


def report(client: TestClient) -> str:
    response = client.get("api/diagnostics")
    assert response.status_code == 200
    return response.text


def section(text: str, title: str) -> str:
    return text.split(f"\n## {title}", 1)[1].split("\n## ", 1)[0]


def test_a_cycle_is_easy_to_read(ingress_client: TestClient) -> None:
    """The owner's dry run of 0.3.0: a question, a project, research and a draft, as the report shows them."""
    agent = ingress_client.app.state.ember.agent
    agent.transport = ScriptedTransport(simulated=True, outcomes=CYCLE)
    agent.meter = agent.economy.metered(agent.transport)
    assert post(ingress_client, "api/inbox", {"text": QUESTION}).status_code == 201
    assert agent.run_cycle("owner").status == "completed"
    full = report(ingress_client)

    economy = json.loads(section(full, "ECONOMY").strip())
    assert economy["today_local"] == agent.clock.today().isoformat() and economy["tz"] == str(agent.clock.tz)

    cycles = section(full, "WAKE CYCLES (latest 8, with every call and tool)")
    assert "\n    note=- phase=None step=3/15 act_end=done sleep=120 project=1\n" in cycles  # its project's cost
    # The plan is indented JSON on its own lines, not a cell cut at 160 characters.
    shown = cycles.split("\n    plan:\n", 1)[1].split("\nid | ", 1)[0]
    assert json.loads(shown)["steps"] == ["look around", "start a project"]
    assert all(line.startswith("      ") for line in shown.splitlines())
    assert "id | llm_call_id | seq | phase | tool | status | summary | input | result\n" in cycles
    tool_rows = {line.split(" | ")[4]: line.split(" | ") for line in cycles.splitlines() if line[:1].isdigit()}
    assert tool_rows["workspace_write"][7] == canonical(DRAFT_CALL)[:799] + "…"
    assert tool_rows["workspace_list"][8] == "(empty) ⏎ Using 0.0 KB of 5 MB and 0/300 entries (0 files, 0 folders)"
    assert tool_rows["research"][8].startswith('<data src="research" id="') and len(tool_rows["research"][8]) == 300

    research = section(full, "RESEARCH (latest 3 digests)").strip().splitlines()
    assert research[0].startswith("tool call #3 in cycle #1: Which printable meal-planning templates sell best")
    assert research[2] == "    - Planner 0: 2 EUR on Etsy; bundles with shopping lists sell best."
    assert all(line.startswith("    ") for line in research[1:]) and research[-1].endswith("…")

    records = section(full, "AGENT RECORDS")
    messages = records.split("-- messages\n", 1)[1].split("\n--", 1)[0].splitlines()
    assert messages == [
        "id | sender | seen | text",
        "2 | agent | not read by owner yet | No, I can't generate images: I will write text templates.",
        f"1 | owner | seen by agent in cycle #1 | {QUESTION}",
    ]
    assert "1 | active | Printable meal-planning templates | outline | " in records
    workspace = records.split("-- workspace: ", 1)[1].splitlines()
    assert workspace[0] == f"1 files, 1 folders, {len(DRAFT)} B"
    assert workspace[1] == "  projects/"
    assert re.fullmatch(rf"  projects/meal-plans\.md \({len(DRAFT)} B, {TIMESTAMP}\)", workspace[2])

    assert post(ingress_client, "api/inbox/read", {"up_to_id": 2}).status_code == 200
    records = section(report(ingress_client), "AGENT RECORDS")
    assert re.search(rf"\n2 \| agent \| read by owner {TIMESTAMP} \| No, I can't", records)


def big_cycles(client: TestClient, count: int, calls: int, content: str) -> None:
    """Cycles with many long tool calls, written straight to the database."""
    db = client.app.state.ember.db
    with db.transaction() as conn:
        life = conn.execute("SELECT id FROM lives ORDER BY id DESC LIMIT 1").fetchone()[0]
        for _ in range(count):
            cycle = conn.execute(
                "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros)"
                " VALUES (?, 'test', ?, ?, 'completed', 'schedule', 1, 250000)",
                (life, NOW, NOW),
            ).lastrowid
            call = conn.execute(
                "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day)"
                " VALUES ('test', ?, 'work', 'scripted', 1, 'ok', ?, '2026-09-28')",
                (cycle, NOW),
            ).lastrowid
            for seq in range(1, calls + 1):
                conn.execute(
                    "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input,"
                    " status, started_at, summary, result) VALUES (?, ?, ?, 'act', 'local', 'research', ?, ?, 'ok',"
                    " ?, 'research', ?)",
                    (cycle, call, seq, f"toolu_{cycle}_{seq}", json.dumps({"question": content}), NOW, content),
                )


def test_the_report_stays_under_its_cap_and_redacted(
    ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ha-long-lived-token-0123456789abcdef"
    monkeypatch.setattr(logging_setup, "_secrets", {secret})
    key = "sk-ant-api03-" + "k" * 40
    # The cuts fall inside secrets: a tool result at 300 characters, a digest at 600, a question at 800 (and
    # the tool input, which starts with '{"question": "', there too). Redacted before the cut, nothing is left.
    content = "." * 290 + secret + "." * 264 + key + "." * 137 + secret + "." * 400
    assert content.index(secret) < 299 < content.index(key) < 599 < content.rindex(secret) < 785 < 799 < 816
    big_cycles(ingress_client, 8, 60, content)
    full = report(ingress_client)
    assert len(full) <= diagnostics.MAX_REPORT_CHARS and "[report cut]" not in full
    assert "\n## AGENT RECORDS" in full and "\n## EVENTS (latest 80)" in full
    assert re.search(r"\(… \d older cycles left out: the report has a size cap\)", full)
    assert "### cycle #8 " in full and "### cycle #1 " not in full  # the newest cycles are kept
    assert "ha-long" not in full and "sk-ant-ap" not in full
    research = section(full, "RESEARCH (latest 3 digests)")
    assert research.count("tool call #") == 3 and "." * 290 + "***" + "." * 264 + "sk-ant-***." in research


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (None, "    plan: -"),
        ("not json", "    plan: not json"),
        ('{"goal": "' + "g" * 900 + '"}', '    plan:\n      {\n       "goal": "' + "g" * 799 + '…"\n      }'),
    ],
)
def test_plans_are_indented_json_with_long_texts_cut(raw: str | None, shown: str) -> None:
    assert diagnostics._plan(raw) == shown


def test_project_notes_keep_their_newest_end() -> None:
    """The agent appends to a project's notes: a long log is cut at its start, a message text at its end."""
    newest = "[#c9] Tried a price and learned from it. " + "More detail. " * 12
    notes = "".join(f"[#c{n}]{newest[5:]}\n" for n in range(1, 10))
    row = diagnostics._rows([{"id": 1, "notes": notes, "text": notes}], ["id", "notes", "text"]).splitlines()[1]
    _, shown, message = row.split(" | ")
    assert len(notes) > diagnostics.TEXT_CHARS == len(shown) == len(message)
    assert shown.startswith("…") and shown.endswith(f"{newest} ⏎ ") and "[#c1]" not in shown
    assert message.startswith("[#c1] ") and message.endswith("…")
