"""0.12.0: one source for every limit a prompt states. Limits were typed into the prompts by hand, next to the constants
the code keeps, and drifted: write_workspace said 5 MB in all while the workspace holds 50, and the last will asked for
5,000 characters in a reply of 1,000 tokens. Now each prompt and tool description reads the constant the code uses, a
text the model is asked to keep short fits what the code keeps of it, and no limit is typed into their texts again."""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

from app.agent import context, library, loop, prompts, review, tools, ventures, workshop
from app.agent.memory import CAPS, MAX_APPEND_LINES
from app.agent.sandbox import NAME, NAME_CHARS, Limits
from app.economy.pricing import LAST_WILL
from app.products import make
from tests.test_agent_requests import SETTINGS
from tests.test_autonomy import with_rules

AGENT = Path(__file__).resolve().parents[1] / "app" / "agent"
# A limit typed into a text: "at most 12", "up to 13", "within 14 days", "<= 300", "(1 to 10)".
TYPED = re.compile(r"(?:\b(?:at most|up to|within)|<=)\s+\d|\(\d+ to \d", re.IGNORECASE)
CHARS_PER_TOKEN = 3  # at least, in plain prose (a reply of N tokens holds 3N characters)


def flat(text: str) -> str:
    return " ".join(text.split())


def test_no_limit_is_typed_into_a_prompt_or_a_tool_description() -> None:
    for name in ("prompts.py", "tools.py", "context.py"):
        source = (AGENT / name).read_text(encoding="utf-8")
        typed = [
            f"{name}:{token.start[0]}: {token.string[:80]!r}"
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE) and TYPED.search(token.string)
        ]
        assert typed == []


def test_the_plan_is_told_the_limits_its_parser_keeps() -> None:
    rules = flat(prompts.PLANNER_RULES)
    for field, chars in prompts.PLAN_CHARS.items():
        assert f"- {field}: " in rules and f"(<= {chars} characters)" in rules
    assert f"at most {prompts.PLAN_STEPS} short concrete steps (each <= {prompts.STEP_CHARS} characters)" in rules
    assert loop.STEP_CHARS == prompts.STEP_CHARS
    brief, _ = context.brief(with_rules(""), False, {"goal": "g", "steps": ["s"]}, None, 12)
    assert f"At most 12 steps this cycle and {tools.MAX_TOOL_CALLS_PER_TURN} tool calls per step." in brief
    assert loop.MAX_TOOL_CALLS_PER_TURN == tools.MAX_TOOL_CALLS_PER_TURN
    assert f"(at most {tools.MAX_TOOL_CALLS_PER_TURN} besides write_journal)" in flat(prompts.reflect_prompt())


def test_what_a_reply_is_asked_to_keep_short_fits_what_is_kept() -> None:
    assert review.WHY_CHARS == prompts.REVIEW_WHY_CHARS
    for field, chars in prompts.REVIEW_CHARS.items():
        assert f"(<= {chars} characters)" in flat(prompts.REVIEW_RULES) and chars <= review.LIMITS[field], field
    asked, kept = prompts.BRAINSTORM_CHARS, ventures.LIMITS
    assert asked["title"] <= kept["title"] and asked["pitch"] <= kept["pitch"]
    assert asked["first_question"] <= kept["next_question"]
    assert f"{ventures.BRAINSTORM_IDEAS} ideas" in flat(prompts.BRAINSTORM_RULES)
    assert f"At most {library.LEARNINGS_PER_CALL};" in flat(prompts.STUDY_RULES)
    assert prompts.RESEARCH_ANSWER_CHARS <= loop.RESEARCH_DIGEST_CHARS
    assert prompts.RESEARCH_ANSWER_CHARS <= prompts.RESEARCH_MAX_TOKENS * CHARS_PER_TOKEN
    assert prompts.WORKSHOP_ANSWER_CHARS <= workshop.ANSWER_CHARS
    assert f"at most {NAME_CHARS} characters" in flat(prompts.WORKSHOP_RULES) and NAME.fullmatch("a" * NAME_CHARS)
    assert not NAME.fullmatch("a" * (NAME_CHARS + 1))
    # the will asked for 5,000 characters in a reply of 1,000 tokens: it would have been cut off
    assert f"(at most {prompts.WILL_CHARS:,} characters)" in flat(prompts.WILL_RULES)
    assert prompts.WILL_CHARS <= prompts.WILL_MAX_TOKENS * CHARS_PER_TOKEN <= LAST_WILL.max_tokens * CHARS_PER_TOKEN


def test_the_tools_state_the_limits_their_code_keeps() -> None:
    described = {name: spec.description for name, spec in tools.SPECS.items()}
    limits = Limits()
    assert f"{limits.max_total_bytes // (1024 * 1024)} MB in total" in described["workspace_write"]  # it said 5
    assert f"{limits.max_file_bytes // 1024} KB per file" in described["workspace_write"]
    memory = described["memory_update"]
    assert f"strategy (at most {CAPS['strategy']:,} bytes" in memory and f"({CAPS['identity']:,} bytes)" in memory
    assert f"lessons ({CAPS['lessons']:,} bytes; append up to {MAX_APPEND_LINES} short lines" in memory
    files = tools.SPECS["workshop"].fields["files"].description
    assert f"(at most {workshop.MAX_INPUTS}, {workshop.MAX_INPUT_BYTES // (1024 * 1024)} MB)" in files
    assert f"1 to {make.MAX_LISTING_PAGES} of your pages" in described["make_image"]
    assert f"the first {make.PAGE_PREVIEWS} pages" in tools.SPECS["make_document"].fields["pictures"].description
    assert f"{ventures.RESEARCH_TO_PROPOSE} such research calls" in described["venture_update"]
    assert prompts.work_request(SETTINGS, "brief", [])["tools"]  # built from them
