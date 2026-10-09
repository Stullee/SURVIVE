"""Review reproductions for the workshop: input count, a released script handed over as a file, a task naming a
root-level file, and a kept file overwriting the agent's text without a version.

Run from ember/ with:  python -m pytest -p tests.conftest -q -s <this file>
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.fake_llm import Plan, Reply, ToolCalls
from tests.test_agent import rows
from tests.test_loop_shapes import JOURNAL, PLAN
from tests.test_workshop import asked, png, workshop_answer, workshop_cycle


def workshop_requests(fake: Any) -> list[dict[str, Any]]:
    return [r for r in fake.sent if r.get("tools") and r["tools"][0].get("name") == "code_execution"]


def test_six_files_are_handed_over(data_dir: Path) -> None:
    def six(agent: Any) -> None:
        for n in range(6):
            agent.roots()[0].write(f"data/part{n}.csv", f"n,{n}\n")

    files = ", ".join(f"data/part{n}.csv" for n in range(6))
    agent, fake = workshop_cycle(data_dir, {"chart.png": png()}, {"task": "Chart of all parts.", "files": files}, six)
    run = rows(agent, "SELECT status, inputs FROM workshop_runs")[0]
    print("run:", run)
    sent = [r for r in fake.sent if any(b.get("type") == "container_upload" for b in r["messages"][0]["content"])]
    uploads = [b for b in sent[0]["messages"][0]["content"] if b.get("type") == "container_upload"]
    print("files uploaded to the run:", len(uploads))
    assert len(uploads) == 6


def test_a_released_script_runs_when_handed_over_as_a_file(data_dir: Path) -> None:
    agent, fake = workshop_cycle(data_dir, {"script.py": b"print(1)\n", "chart.png": png()}, {"task": "Price chart."})
    path = "workshop/scripts/price-chart-1.py"
    asked(agent, path, "released")
    ids = [fake._new_file("chart.png", png(), True)]
    fake.script.extend(
        [
            Plan(PLAN),
            ToolCalls([("workshop", {"task": "Run price-chart-1.py again with new prices.", "files": path})]),
            workshop_answer(ids),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent.run_cycle("schedule")
    calls = rows(agent, "SELECT tool, status, summary FROM tool_calls WHERE tool = 'workshop' ORDER BY id")
    print("workshop calls:", calls)
    paid = rows(agent, "SELECT id, cost_micros FROM llm_calls WHERE purpose = 'workshop'")
    print("paid workshop calls:", paid)
    assert len(paid) == 2 and calls[-1]["status"] == "ok"


def test_a_task_naming_a_root_file_is_not_refused(data_dir: Path) -> None:
    def prices(agent: Any) -> None:
        agent.roots()[0].write("prices.csv", "shop,price\nA,4.5\n")

    agent, fake = workshop_cycle(
        data_dir, {"chart.png": png()}, {"task": "Make chart.png from the numbers in prices.csv."}, prices
    )
    run = rows(agent, "SELECT status, inputs FROM workshop_runs")[0]
    print("run without prices.csv handed over:", run)
    assert run["inputs"] == "[]"


def test_a_kept_file_overwrites_the_agents_text_without_a_version(data_dir: Path) -> None:
    def original(agent: Any) -> None:
        agent.roots()[0].write("drafts/guide.md", "ORIGINAL guide the agent wrote\n")

    agent, fake = workshop_cycle(
        data_dir,
        {"guide.md": b"# replaced by the run\n"},
        {"task": "Write a table of contents as guide.md.", "folder": "drafts"},
        original,
    )
    print("drafts/guide.md now:", agent.roots()[0].read("drafts/guide.md").strip())
    print("versions:", rows(agent, "SELECT path FROM workspace_versions"))
    assert agent.roots()[0].read("drafts/guide.md").startswith("# replaced")
    assert rows(agent, "SELECT path FROM workspace_versions") == []
