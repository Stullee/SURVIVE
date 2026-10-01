"""0.12.0: tool sets per cycle kind. Every work step sent all 37 tool definitions (34 KB, three quarters of the fixed
prompt) whatever its cycle was for. A venture cycle researches and decides: it no longer carries the tools for building
and selling (making and looking at files, the workshop, the shop, email and Reddit), nor the rules for making files.
The reflection keeps its work's list: it reads it from the cache at a tenth of the price, and a list of its own would
write the whole conversation again. What it can't use there any more: tools whose answer nothing reads after its one
reply (memory_read, knowledge_search)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent import prompts, tools
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls, request_kind
from tests.test_agent import rows
from tests.test_agent_requests import SETTINGS
from tests.test_loop_shapes import run
from tests.test_ventures import JOURNAL, VENTURING, plan

EVERYTHING = {
    "mail": True,
    "etsy": True,
    "library": True,
    "pinterest": True,
    "printify": True,
    "site": True,
    "blog": True,
}


def names(request: dict[str, Any]) -> set[str]:
    return {t["name"] for t in request["tools"]}


def fixed(request: dict[str, Any]) -> int:
    """The fixed part of a work step's prompt: its system text and tool definitions (JSON bytes)."""
    return len(json.dumps([request["system"], request["tools"]], ensure_ascii=False).encode())


def test_a_venture_cycle_carries_no_tools_for_building_or_selling() -> None:
    ordinary = prompts.work_request(SETTINGS, "brief", [], **EVERYTHING)
    venture = prompts.work_request(SETTINGS, "brief", [], venture=True, **EVERYTHING)
    assert names(ordinary) >= tools.ORDINARY_TOOLS and "brainstorm" not in names(ordinary)
    assert not names(venture) & tools.ORDINARY_TOOLS and "brainstorm" in names(venture)
    assert "research" in names(venture) and "venture_update" in names(venture)
    building = "Look at the pictures of what you make"
    assert building in json.dumps(ordinary["system"]) and building not in json.dumps(venture["system"])
    assert fixed(venture) <= 0.75 * fixed(ordinary)
    reflection = prompts.reflect_request(SETTINGS, "brief", [], [], venture=True, **EVERYTHING)
    assert names(reflection) == names(venture)  # the reflection reads the work's list from the cache


def test_a_venture_cycle_refuses_them(data_dir: Path) -> None:
    make = ("make_document", {"source": "drafts/a.md", "output": "a.pdf"})
    fake = FakeTransport(script=[plan(steps=["Make the planner"]), ToolCalls([make]), Reply("Done."), JOURNAL])
    agent, ends = run(data_dir, fake, settings=VENTURING)
    assert ends[0].status == "completed" and rows(agent, "SELECT venture FROM cycles") == [{"venture": 1}]
    [made] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'make_document'")
    assert made["status"] == "error"
    refusal = (
        "make_document is not one of your tools in a venture cycle: making files, the shop, Pinterest, email, Reddit "
        "and laying out the roadmap belong to ordinary cycles"
    )
    assert refusal in made["result"]
    work = [r for r in fake.sent if request_kind(r) == "work"]
    assert work and all(not names(r) & tools.ORDINARY_TOOLS for r in work)


def test_the_reflection_has_no_use_for_reading(data_dir: Path) -> None:
    assert not tools.SPECS["memory_read"].reflect and not tools.SPECS["knowledge_search"].reflect
    reads = ToolCalls([("memory_read", {"file": "lessons"}), ("write_journal", {"summary": "Done", "entry": "Done."})])
    fake = FakeTransport(script=[plan(steps=["Look around"]), Reply("Done."), reads])
    agent, _ = run(data_dir, fake)
    [read] = rows(agent, "SELECT phase, status, result FROM tool_calls WHERE tool = 'memory_read'")
    assert (read["phase"], read["status"]) == ("reflect", "error")
    assert "(nothing reads a tool's answer after this last reply)" in read["result"]
    assert rows(agent, "SELECT status FROM tool_calls WHERE tool = 'write_journal'") == [{"status": "ok"}]
