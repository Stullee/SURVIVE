"""The reflection's handoff reaches the next plan (0.12.0, FIX NOW 10): what the reflection said the next cycle should
do first, and the last plan's goal, never reached a plan (the 600 bytes kept for them went unused)."""

from __future__ import annotations

from pathlib import Path

from app.agent.fake_llm import FakeTransport, Reply, ToolCalls
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_roadmap import plan, planner_texts, section

HANDOFF = "Finish pages 3 to 5 of the planner, then make the PDF: the listing waits for it."


def test_the_reflections_handoff_reaches_the_next_plan(data_dir: Path) -> None:
    journal = {"summary": "Drafted half the planner", "entry": "Pages 1-2 are done.", "next": HANDOFF + "  "}
    fake = FakeTransport(
        script=[plan(steps=["draft the planner"]), Reply("Done."), ToolCalls([("write_journal", journal)]), plan([])]
    )
    agent, _ = run(data_dir, fake, cycles=2)
    assert rows(agent, "SELECT handoff FROM journal")[0]["handoff"] == HANDOFF
    first, second = planner_texts(fake)
    assert "== YOUR LAST CYCLE ==" not in first  # nothing before the first cycle
    assert section(second, "YOUR LAST CYCLE") == (
        f'Your handoff to this cycle: "{HANDOFF}"\nIts goal: "Plan ahead"\nIts journal: "Drafted half the planner"'
    )
    assert "Your last journal summary" not in second  # it moved from the news to this section
