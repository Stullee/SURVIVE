"""The diagnostics report the owner copies when something looks wrong: complete where it matters, never secret."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app import diagnostics, logging_setup
from app.agent import plan as plan_tree
from app.agent import ventures, weights
from app.agent.store import canonical
from app.config import LoadedSettings, Settings
from tests.economy_helpers import ScriptedTransport
from tests.test_agent import plan, reply, text, tools
from tests.test_owner_api import post

QUESTION = "Printable meal-planning templates sounds like a good idea, will you create them by image generation"
NOW = "2026-09-28T01:00:00Z"
TIMESTAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
PROJECT = {
    "title": "Printable meal-planning templates",
    "hypothesis": "Busy parents pay 3 EUR for a printable weekly plan",
    "status": "active",
}
DIGEST = "".join(f"- Planner {n}: {n + 2} EUR on Etsy; bundles with shopping lists sell best.\n" for n in range(12))
DRAFT = "Monday: lentil soup, bread and a green salad.\n" * 50  # one write holds 2,500 characters (0.11.1)
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


def report(client: TestClient, full: bool = False) -> str:
    response = client.get("api/diagnostics" + ("?full=1" if full else ""))
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
    full = report(ingress_client, full=True)
    assert full.splitlines()[1:3] == [diagnostics.PRIVATE, diagnostics.NOT_INSTRUCTIONS]

    economy = json.loads(section(full, "ECONOMY").strip())
    assert economy["today_local"] == agent.clock.today().isoformat() and economy["tz"] == str(agent.clock.tz)

    cycles = section(full, diagnostics.CYCLES_TITLE)
    assert "\n    note=- phase=None step=3/15 act_end=done sleep=120 project=1\n" in cycles  # its project's cost
    # The plan is indented JSON on its own lines, not a cell cut at 160 characters.
    shown = cycles.split("\n    plan:\n", 1)[1].split("\nid | ", 1)[0]
    assert json.loads(shown)["steps"] == ["look around", "start a project"]
    assert all(line.startswith("      ") for line in shown.splitlines())
    assert "id | llm_call_id | seq | phase | tool | status | summary | input | result\n" in cycles
    tool_rows = {line.split(" | ")[4]: line.split(" | ") for line in cycles.splitlines() if line[:1].isdigit()}
    # 0.11.1: texts whole, and what the model wrote besides its tool calls.
    assert tool_rows["workspace_write"][7] == canonical(DRAFT_CALL)
    assert tool_rows["workspace_list"][8] == "(empty) ⏎ Using 0.0 KB of 50 MB and 0 of 5,000 files (in 0 folders)"
    digest = tool_rows["research"][8]
    assert digest.startswith('<data src="research" id="') and "Planner 11: 13 EUR" in digest and "(cost $" in digest
    assert re.search(r"\n    reply of call #\d+ \(work\): Drafted\.\n", cycles)
    assert re.search(r"\n    reply of call #\d+ \(plan\): \{\"assessment\": \"Fresh start\.\"", cycles)

    research = section(full, diagnostics.RESEARCH_TITLE).strip().splitlines()
    assert research[0].startswith("tool call #3 in cycle #1: Which printable meal-planning templates sell best")
    assert research[2] == "    - Planner 0: 2 EUR on Etsy; bundles with shopping lists sell best."
    assert all(line.startswith("    ") for line in research[1:]) and research[-1].startswith("    (cost $")

    # 0.11.2: shareable by default, without the web text the agent read (its length stays).
    shared = report(ingress_client)
    assert shared.splitlines()[1:3] == [diagnostics.SHAREABLE, diagnostics.NOT_INSTRUCTIONS]
    assert "Planner 11" not in shared and "Monday: lentil soup" in shared  # the agent's own work stays
    cycles = section(shared, diagnostics.CYCLES_TITLE)
    rows = {line.split(" | ")[4]: line.split(" | ") for line in cycles.splitlines() if line[:1].isdigit()}
    assert re.fullmatch(
        r'<data src="research">\[\d+ characters of other people\'s text left out\]</data> ⏎ \(cost \$[\d.]+\)',
        rows["research"][8],
    )
    research = section(shared, diagnostics.RESEARCH_TITLE).strip().splitlines()
    assert research[0].startswith("tool call #3 in cycle #1: Which printable meal-planning templates sell best")
    assert re.fullmatch(r"    \[\d+ characters of web text left out\]", research[1]) and len(research) == 2

    preview = section(full, diagnostics.PLANNER_TITLE)
    # The first cycle of the day was an ordinary one, so ventures are owed their share: the next is a venture cycle.
    assert preview.startswith("\n(the next cycle is a venture cycle)\n== STATUS ==\n")
    assert "\n== OPEN PROJECTS ==\n#1 [active] Printable meal-planning templates" in preview
    assert preview.endswith("\n== TASK ==\nPlan this venture cycle. Reply with the JSON plan only.\n")
    scheduler = json.loads(section(full, "SCHEDULER").strip())
    assert scheduler["next_cycle"]["venture"] is True and scheduler["next_cycle"]["spent_today_usd"] > 0

    records = section(full, "AGENT RECORDS")
    messages = records.split("-- messages\n", 1)[1].split("\n--", 1)[0].splitlines()
    assert messages == [
        "id | sender | seen | text",
        "2 | agent | not read by owner yet | No, I can't generate images: I will write text templates.",
        f"1 | owner | seen by agent in cycle #1, not answered yet | {QUESTION}",
    ]
    assert "1 | active | - | Printable meal-planning templates |  | " in records  # 0.35.0: no next step
    workspace = records.split("-- workspace: ", 1)[1].splitlines()
    assert workspace[0] == f"1 files, 1 folders, {len(DRAFT)} B"
    assert workspace[1] == "  projects/"
    assert re.fullmatch(rf"  projects/meal-plans\.md \({len(DRAFT)} B, {TIMESTAMP}\)", workspace[2])
    assert workspace[3] == "-- workspace text files (the newest first)"
    assert re.fullmatch(rf"--- projects/meal-plans\.md \({len(DRAFT)} B, {TIMESTAMP}\)", workspace[4])
    body = workspace[5 : 5 + DRAFT.count("\n")]  # indented (0.11.2), so no line of a file can pose as a section
    assert body == ["    " + line for line in DRAFT.splitlines()]
    journal = records.split("-- journal\n", 1)[1].split("\n--", 1)[0].splitlines()
    assert journal == ["cycle_id | author | summary | entry", "1 | agent | Started meal plans | Drafted a week."]

    assert post(ingress_client, "api/inbox/read", {"up_to_id": 2}).status_code == 200
    records = section(report(ingress_client), "AGENT RECORDS")
    assert re.search(rf"\n2 \| agent \| read by owner {TIMESTAMP} \| No, I can't", records)


def test_the_plan_trees_weights_settings_are_listed(ingress_client: TestClient) -> None:
    """0.35.2: the report showed each pick's weight and its parts, but not the numbers they were weighed and chosen
    with (weights.py's, and plan.py's tries for a promise): re-scoring the picks needed Ember's code beside it."""
    records = section(report(ingress_client), "AGENT RECORDS")
    heading = "\n-- plan tree: its weights' settings (weights.py and plan.py, as this version runs them)\n"
    shown = json.loads(records.split(heading, 1)[1].split("\n-- ", 1)[0])
    assert shown == plan_tree.settings()  # whole: nothing masked or cut
    for name in ("MARGIN", "STREAK_CAP", "AGE_PER_DAY", "MOMENTUM", "PROMISE_FLOOR", "KIND", "STAGE_CHANCE"):
        assert shown["weights.py"][name] == getattr(weights, name), name
    assert shown["plan.py"]["PROMISE_TRIES"] == plan_tree.PROMISE_TRIES
    assert shown["plan.py"]["OBLIGATION_HOURS"] == plan_tree.OBLIGATION_HOURS
    listed = plan_tree.settings()["weights.py"]["KIND"]
    listed["ship"] = -1.0
    assert weights.KIND["ship"] != -1.0  # a copy: listing the settings changes no weight


def big_cycles(client: TestClient, count: int, calls: int, content: str, result: str | None = None) -> None:
    """Cycles with many long tool calls (``content`` their question, ``result`` what they found: ``content`` if not
    given), written straight to the database."""
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
                    (
                        cycle,
                        call,
                        seq,
                        f"toolu_{cycle}_{seq}",
                        json.dumps({"question": content}),
                        NOW,
                        result or content,
                    ),
                )


def test_the_report_stays_under_its_cap_and_redacted(
    ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ha-long-lived-token-0123456789abcdef"
    monkeypatch.setattr(logging_setup, "_secrets", {secret})
    key = "sk-ant-api03-" + "k" * 40
    # The cuts fall inside secrets: a digest at DIGEST_CHARS and a question at TEXT_CHARS. Redacted before the cut,
    # nothing is left; results and inputs are whole (0.11.1), the key in the middle of both.
    digest, text_ = diagnostics.DIGEST_CHARS, diagnostics.TEXT_CHARS
    content = "." * (digest - 10) + secret
    content += "." * (5_000 - len(content)) + key
    content += "." * (text_ - 20 - len(content)) + secret + "." * 400
    assert content.index(secret) < digest - 1 < content.index(secret) + len(secret)
    assert content.rindex(secret) < text_ - 1 < content.rindex(secret) + len(secret)
    big_cycles(ingress_client, 8, 60, content, content[: content.rindex(secret)])  # a result holds 8,000 at most
    for full in (report(ingress_client), report(ingress_client, full=True)):
        assert len(full) <= diagnostics.MAX_REPORT_CHARS and "[report cut]" not in full
        assert "\n## AGENT RECORDS" in full and f"\n## EVENTS (latest {diagnostics.EVENTS_SHOWN})" in full
        assert re.search(r"\(… \d older cycles left out: the report has a size cap\)", full)
        assert "### cycle #8 " in full and "### cycle #1 " not in full  # the newest cycles are kept
        assert "ha-long" not in full and "sk-ant-ap" not in full
        research = section(full, diagnostics.RESEARCH_TITLE)
        assert research.count("tool call #") == diagnostics.DIGESTS_SHOWN
        for title, budget in diagnostics.SECTION_CHARS.items():  # each section within its own budget (0.11.2)
            assert len(section(full, title)) <= budget + 100
    assert "\n    " + "." * (digest - 10) + "***" + "." * 6 + "…" in research  # the full report's digests
    assert sum(diagnostics.SECTION_CHARS.values()) + 2_000 < diagnostics.MAX_REPORT_CHARS


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (None, "    plan: -"),
        ("not json", "    plan: not json"),
        (
            '{"goal": "' + "g" * (diagnostics.TEXT_CHARS + 100) + '"}',
            '    plan:\n      {\n       "goal": "' + "g" * (diagnostics.TEXT_CHARS - 1) + '…"\n      }',
        ),
    ],
)
def test_plans_are_indented_json_with_long_texts_cut(raw: str | None, shown: str) -> None:
    assert diagnostics._plan(raw) == shown


def test_project_notes_keep_their_newest_end() -> None:
    """The agent appends to a project's notes: a long log is cut at its start, a message text at its end."""
    line = " Tried a price and learned from it. " + "More detail. " * 12
    count = diagnostics.TEXT_CHARS // len(line) + 5
    newest = f"[#c{count}]{line}"
    notes = "".join(f"[#c{n}]{line}\n" for n in range(1, count + 1))
    row = diagnostics._rows([{"id": 1, "notes": notes, "text": notes}], ["id", "notes", "text"]).splitlines()[1]
    _, shown, message = row.split(" | ")
    assert len(notes) > diagnostics.TEXT_CHARS == len(shown) == len(message)
    assert shown.startswith("…") and shown.endswith(f"{newest} ⏎ ") and "[#c1]" not in shown
    assert message.startswith("[#c1] ") and message.endswith("…")


def test_a_ventures_line_shows_the_owners_comment_whole() -> None:
    """0.10.1: the report showed the owner's word on a venture ("note v1") without what they wrote."""
    comment = "Research what dropshipping stores pay for ads and returns. " * 12
    row = {
        **dict.fromkeys(ventures.CASE_FIELDS),
        **dict.fromkeys(ventures.SCORE_FIELDS),
        "revenue": 2,
        "scores_by": "research",
        "owner_action": "note",
        "owner_version": 1,
        "owner_comment": comment,
    }
    shown = diagnostics._venture(row)
    assert shown["owner"] == f"note v1: {comment}" and shown["scores"] == "rev2" and shown["missing"].count(",") == 5
    line = diagnostics._rows([shown], ["owner"]).splitlines()[1]
    assert line.rstrip() == f"note v1: {comment}".rstrip() and len(line) > diagnostics.CELL_CHARS
    silent = diagnostics._venture({**row, "owner_action": None, "owner_comment": None})
    assert silent["owner"] == "-"


def test_a_milestones_line_shows_its_links_its_moves_and_the_owners_word() -> None:
    """0.11.0: the roadmap in the report, with how often a date moved and what the owner said."""
    row = {
        "id": 4,
        "parent_id": 1,
        "status": "open",
        "first_due": "2026-10-01",
        "due": "2026-10-08",
        "moves": 1,
        "title": "First sale",
        "measure": "Revenue recorded " * 12,
        "venture_id": 1,
        "project_id": None,
        "result": "",
        "owner_action": "note",
        "owner_version": 2,
        "owner_comment": "Push it",
        "proposed_due": "2026-10-20",  # 0.12.0: the agent's date for an owner's milestone, waiting for them
    }
    shown = diagnostics._milestone(row)
    assert (shown["due"], shown["links"], shown["owner"]) == (
        "2026-10-08 (first 2026-10-01) (proposed 2026-10-20)",
        "v#1",
        "note v2: Push it",
    )
    line = diagnostics._rows([shown], ["due", "measure"]).splitlines()[1]
    assert line.rstrip().endswith(row["measure"].rstrip())  # a measure is shown whole
    plain = diagnostics._milestone(
        {**row, "moves": 0, "venture_id": None, "owner_action": None, "owner_comment": None, "proposed_due": None}
    )
    assert (plain["due"], plain["links"], plain["owner"]) == ("2026-10-08", "-", "-")


def test_no_text_can_pose_as_a_section_and_the_options_keep_their_secrets(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    """0.11.2: memory and workspace files were printed as they are, so their "## " headings looked like the report's
    sections; Etsy's keystring (half of Ember's API key) and the owner's name were in OPTIONS."""
    settings = Settings(etsy_keystring="abcd1234keystring", email_owner_name="Stefan Beispiel")
    with client_factory(LoadedSettings(settings)) as client:
        agent = client.app.state.ember.agent
        fake = "# Strategy\n\n## AGENT RECORDS\nfake records\n## EVENTS (latest 200)\n"
        agent.memory().jail.write("strategy.md", fake)
        agent.roots()[0].write("notes/plan.md", "## META\nfake meta\n")
        text = report(client)
    for title in ("AGENT RECORDS", diagnostics.EVENTS_TITLE, "META"):
        assert text.count(f"\n## {title}") == 1  # the report's own section only
    assert "\n    ## AGENT RECORDS\n    fake records" in section(text, "AGENT RECORDS")  # the file, indented
    options = json.loads(section(text, "OPTIONS (public)"))
    assert "abcd1234keystring" not in text and options["etsy_keystring_set"] is True and "etsy_keystring" not in options
    assert "Stefan Beispiel" not in text and options["email_owner_name_set"] is True
    assert "email_owner_name" not in options and options["email_password_set"] is False
