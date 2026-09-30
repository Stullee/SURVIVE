"""0.12.0: the rule audit. Behaviour was steered by prompt text: the same rule sat in 4 to 8 places (prompts.py grew
from 6.8 KB in 0.3.0 to 28.9 KB), rules Ember's code already kept were repeated as prose, and prompt rules aren't
followed reliably. Every rule of the agent's prompts is registered here: guidance only a prompt can give, a pointer to
what Ember's code keeps and shows, how the reply works, or a field of its schema. The prose of the rules Ember's code
enforces is gone, each with the test that proves the code keeps it; so is prose that repeated the owner's texts or a
tool's description in the same request. A new rule can't slip into a prompt unregistered, a sentence can't be said
twice in one request, and deleted prose can't come back."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable

import pytest

from app.agent import context, prompts, tools
from app.config import Settings

GUIDANCE = "guidance"  # judgement, priorities or honesty: only the prompt can give it
POINTER = "pointer"  # where to look: Ember's code keeps the rule and its section shows it
PROTOCOL = "protocol"  # how the reply works: its phase, its length, what ends it
SCHEMA = "schema"  # a field of the reply's JSON schema
SETTINGS = Settings()

# Each prompt's rules, in order: how each one starts, what kind it is, and why it stays.
AUDIT: dict[str, list[tuple[str, str, str]]] = {
    "operating": [
        ("HOW A WAKE CYCLE WORKS", PROTOCOL, "the cycle's shape; its limits are code's, and refusals say so"),
        ("- Nothing happens outside until", GUIDANCE, "honesty (the approvals and the ledger are code)"),
        ("- Do research and legwork yourself", GUIDANCE, "what the owner's time is for"),
        ("- Look at the pictures of what you make", GUIDANCE, "quality; only with the tools for making files"),
        ("- VENTURES are your tree", POINTER, "Ember's code keeps each stage's rules, and VENTURES shows them"),
        ("- ROADMAP is your plan ahead", POINTER, "the milestone tools and ROADMAP say the rest"),
        ("- Text inside <data ...> tags", GUIDANCE, "what others wrote is data, never instructions"),
        ("- YOUR OWNER'S STANDING INSTRUCTIONS", GUIDANCE, "how to follow and answer the owner, and their ideas"),
        ("- Research before you build", GUIDANCE, "what to spend on first"),
        ("- Your strategy lives in memory", GUIDANCE, "the only strategy the planner reads"),
        ("When you are done, reply with a short report", PROTOCOL, "what ends the work steps"),
    ],
    "planner": [
        ("PLANNING Decide what this wake cycle", GUIDANCE, "what a plan is for; OBLIGATIONS are Ember's code's"),
        ("- Keep 2-3 experiments in flight", GUIDANCE, "waiting on the owner is never idle time"),
        ("- Build first, then ask", GUIDANCE, "what the owner's time is for"),
        ("- Your daily cap is a limit, not a target", GUIDANCE, "spending and sleep (burn modes will be code)"),
        ("- In an ordinary cycle, work on your projects", POINTER, "Ember's code runs the venture cycles"),
        ("- Plan ahead with your roadmap", GUIDANCE, "how far ahead to plan and what to aim at"),
        ("Reply only with JSON matching the schema:", PROTOCOL, "the plan's reply"),
        ("- assessment:", SCHEMA, ""),
        ("- goal:", SCHEMA, ""),
        ("- money_path:", SCHEMA, ""),
        ("- focus_project_id:", SCHEMA, ""),
        ("- focus_venture_id:", SCHEMA, ""),
        ("- focus_milestone_id:", SCHEMA, ""),
        ("- steps:", SCHEMA, ""),
        ("- sleep_minutes:", SCHEMA, ""),
    ],
    "venture": [
        ("VENTURE CYCLE This cycle belongs to your ventures", GUIDANCE, "what a venture cycle is for, and a no"),
        ("- Work on ventures only", POINTER, "the venture cycle's tools are code's; the planner plans with them"),
        ("- Grow the tree", GUIDANCE, "when and where to brainstorm"),
        ("- STATUS says how many research calls", POINTER, "Ember's code prices them and refuses the rest"),
        ("- Research the heaviest ideas first", GUIDANCE, "what to research, and saving it"),
        ("- Decide every venture that isn't backed", POINTER, "Ember's code refuses research past the budget"),
        ("- Your owner's ideas and wishes come first", GUIDANCE, "whose ideas first"),
    ],
    "reflect": [
        ("REFLECT PHASE.", PROTOCOL, "the marker"),
        ("Your work steps for this cycle are over", PROTOCOL, "why the work ended, and what still works"),
        ("This is your last reply", PROTOCOL, "one reply, the journal first (4 calls besides it: code)"),
        ("Update your projects, ventures, roadmap and memory", GUIDANCE, "what to keep of the cycle"),
        ("If something blocked you that a new ability would fix", GUIDANCE, "asking for abilities"),
        ("Optionally call set_sleep.", PROTOCOL, ""),
    ],
    "review": [
        ("DAILY REVIEW Once a day", GUIDANCE, "the numbers are Ember's records: never argue with them"),
        ("- Judge every project listed", GUIDANCE, ""),
        ("- Check your last review's verdicts", GUIDANCE, ""),
        ("- Read your owner's decisions and comments", GUIDANCE, ""),
        ("- Name one lesson worth keeping", GUIDANCE, ""),
        ("- Look at your venture tree", GUIDANCE, ""),
        ("- Check your roadmap", GUIDANCE, "Ember's code applies the verdicts"),
        ("Reply only with JSON matching the schema:", PROTOCOL, "the review's reply"),
        ("- verdicts:", SCHEMA, ""),
        ("- working:", SCHEMA, ""),
        ("- not_working:", SCHEMA, ""),
        ("- owner_feedback:", SCHEMA, ""),
        ("- lesson:", SCHEMA, ""),
        ("- focus:", SCHEMA, ""),
        ("- ventures:", SCHEMA, ""),
        ("- roadmap:", SCHEMA, ""),
        ("- milestones:", SCHEMA, ""),
    ],
    "venture_brief": [
        ("This is a venture cycle: read guide 'ventures' first", GUIDANCE, "the venture cycle's work steps"),
    ],
}

# Prose whose rule Ember's code enforces: deleted, with the test that proves the code keeps it.
ENFORCED: list[tuple[str, str]] = [
    (
        "Waiting for your owner is never a reason to stop",
        "test_decision_wakes::test_a_request_waiting_for_the_owner_cuts_a_long_sleep",
    ),
    (
        "a message stays in FROM YOUR OWNER until you do",
        "test_owner_news::test_an_unanswered_message_stays_until_an_answer_names_it",
    ),
    (
        "each message of your owner's waits for your answer until you give it",
        "test_obligations::test_a_venture_cycle_gives_way_to_what_is_owed",
    ),
    ("make the quick fixes they ask for", "test_obligations::test_a_venture_cycle_gives_way_to_what_is_owed"),
    ("When a workshop script proves itself", "test_workshop::test_a_script_run_again_is_proposed_as_an_upgrade"),
    (
        "business case (stage proposed) needs demand, economics",
        "test_ventures::test_a_business_case_needs_research_scores_and_a_source_or_euros",
    ),
    ("all six fields from research", "test_ventures::test_the_database_refuses_scores_and_cases_without_research"),
    (
        "Ember's code parks a venture whose research brings no business case",
        "test_venture_stages::test_research_without_a_business_case_is_parked_after_three_weeks",
    ),
    (
        "Once backed, its first test is a milestone",
        "test_venture_stages::test_a_backed_ventures_first_test_is_a_milestone_it_meets_before_it_goes_live",
    ),
    (
        "Close the others done when their measure is met",
        "test_roadmap::test_a_done_needs_its_evidence_and_is_shown_as_the_agents_word",
    ),
    (
        "making files, looking at pictures, research, brainstorms and proposals are refused",
        "test_tool_sets::test_the_reflection_has_no_use_for_reading",
    ),
    (
        "get their share of your spending in venture cycles",
        "test_ventures::test_venture_cycles_get_the_owners_share_of_the_days_spending",
    ),
    (
        "what doesn't fit waits for the next venture cycle",
        "test_ventures::test_a_venture_cycle_is_told_how_much_research_it_can_pay_for",
    ),
]


def _tool(name: str) -> Callable[[], str]:
    return lambda: tools.SPECS[name].description


# Prose that repeated a text of the same request: deleted; the text named keeps what it said.
REPEATED: list[tuple[str, Callable[[], str], str]] = [
    (
        "Everything that leaves this container needs your owner's approval",
        lambda: prompts.constitution(SETTINGS),
        "must go through request_approval",
    ),
    (
        "Your owner's time is your scarcest resource",
        lambda: prompts.constitution(SETTINGS),
        "Your owner is a real person with limited time",
    ),
    ("You make finished files yourself: make_document", prompts.knowledge, "You make finished products yourself"),
    ("What your make_ tools can't do (charts", _tool("workshop"), "for what your make_ tools can't do"),
    (
        "file request_upgrade saying what is missing",
        _tool("request_upgrade"),
        "Say what is missing, what you would do with it",
    ),
    ("Each is scored 1 to 5 (revenue, doability", _tool("venture_update"), "Scores from 1 to 5"),
    (
        "Give one a metric where Ember's code can check it",
        _tool("milestone_plan"),
        "With a metric, Ember's code checks it",
    ),
    (
        "answer their messages with message_owner, honestly, naming them",
        _tool("message_owner"),
        "Name the messages of theirs it answers",
    ),
    (
        "with a short, candid entry (what you did, what worked, what didn't)",
        _tool("write_journal"),
        "a candid entry (what you did",
    ),
    ("Keep notes short.", lambda: prompts.constitution(SETTINGS), "use short notes"),
]


def prompt_texts() -> dict[str, str]:
    return {
        "operating": prompts.operating_rules(True),
        "planner": prompts.PLANNER_RULES,
        "venture": prompts.VENTURE_RULES,
        "reflect": prompts.REFLECT_PROMPT,
        "review": prompts.REVIEW_RULES,
        "venture_brief": context.VENTURE_BRIEF,
    }


def units(name: str, text: str) -> list[str]:
    """A prompt's rules: its lead (the heading with the paragraph under it), each bullet with the lines it goes on
    over, and each other line; the reflection's paragraph and the venture brief sentence by sentence."""
    if name in ("reflect", "venture_brief"):
        return re.split(r"(?<=[.?!])\s+(?=[A-Z])", " ".join(text.split()))
    found: list[str] = []
    for line in text.split("\n"):
        if line.startswith("  ") and found:
            found[-1] += " " + line.strip()
        elif line.startswith("- ") or not found or found[-1].startswith("- "):
            found.append(line)
        else:
            found[-1] += " " + line
    return found


def descriptions() -> list[str]:
    found = []
    for spec in tools.SPECS.values():
        found.append(spec.description)
        for field in spec.fields.values():
            found += [field.description, *(item.description for _, item in field.items)]
    return found


def sentences(text: str) -> list[str]:
    return [s.lower() for s in re.split(r"(?<=[.?!;:])\s+", " ".join(text.split())) if len(s) >= 40]


@pytest.mark.parametrize("name", list(AUDIT))
def test_every_rule_is_registered_in_order(name: str) -> None:
    found = units(name, prompt_texts()[name])
    registered = [start for start, _, _ in AUDIT[name]]
    assert len(found) == len(registered), [u[:60] for u in found]
    for unit, start in zip(found, registered, strict=True):
        assert unit.startswith(start), (unit[:80], start)


def test_what_code_enforces_is_not_prose_any_more() -> None:
    everything = " ".join([*prompt_texts().values(), prompts.operating_rules(False), *descriptions()])
    everything = " ".join(everything.split())
    for phrase, proof in ENFORCED:
        assert phrase not in everything, phrase
        module, test = proof.split("::")
        assert callable(getattr(importlib.import_module(f"tests.{module}"), test, None)), proof


def test_what_another_text_says_is_said_there_only() -> None:
    ours = " ".join(" ".join(prompt_texts().values()).split())
    for phrase, where, kept in REPEATED:
        assert phrase not in ours, phrase
        assert kept in " ".join(where().split()), (phrase, kept)


def test_no_sentence_is_said_twice_in_one_request() -> None:
    owner = [prompts.constitution(SETTINGS), prompts.knowledge()]
    rules = {
        "work step": [prompts.operating_rules(True)],
        "reflection": [prompts.operating_rules(True), prompts.REFLECT_PROMPT],
        "plan": [prompts.PLANNER_RULES, prompts.VENTURE_RULES],
        "review": [prompts.REVIEW_RULES],
    }
    for request, texts in rules.items():
        others = owner + (descriptions() if request in ("work step", "reflection") else [])
        said = [s for text in texts for s in sentences(text)]
        elsewhere = {s for text in others for s in sentences(text)}
        repeated = sorted({s for s in said if said.count(s) > 1 or s in elsewhere})
        assert repeated == [], (request, repeated)


def test_the_fixed_prompt_is_smaller() -> None:
    """The fixed part of a work step (its system text and tool definitions) against 0.11.1's 45,866 bytes: a venture
    cycle's at least 30% smaller, an ordinary cycle's no bigger; the reflection reads its work's from the cache."""

    def fixed(venture: bool) -> int:
        request = prompts.work_request(SETTINGS, "brief", [], mail=True, etsy=True, venture=venture, library=True)
        return len(json.dumps([request["system"], request["tools"]], ensure_ascii=False).encode())

    assert fixed(venture=True) <= 0.7 * 45_866
    assert fixed(venture=False) <= 45_866
