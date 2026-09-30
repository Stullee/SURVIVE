"""The reflection's handoff reaches the next plan (0.12.0, FIX NOW 10): what the reflection said the next cycle should
do first, and the last plan's goal, never reached a plan (the 600 bytes kept for them went unused)."""

from __future__ import annotations

import re
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
    shown = section(second, "YOUR LAST CYCLE")  # 0.12.0: with the digest Ember's code wrote of it
    assert re.fullmatch(
        re.escape(f'Your handoff to this cycle: "{HANDOFF}"\nIts journal: "Drafted half the planner"\n')
        + re.escape("What your last cycles did, from Ember's records (newest first):\nCycle #1 completed · $")
        + r"0\.\d{4}"
        + re.escape('\nGoal: "Plan ahead"\nNo tool was used for the work.\nWork ended: the agent ended it.\n')
        + re.escape("Reflection: yes; journal by the agent."),
        shown,
    ), shown
    assert "Your last journal summary" not in second  # it moved from the news to this section
