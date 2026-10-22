"""0.42.0 (analysis-0.40.1 codebase, 5.5, 6.2 and 9): text limits that cut, the conversation's room in each work step,
and a journal's closing tags taken off in linear time.

1. Text limits that cut. On 10-21, 6 of the 11 refused tool calls were a text over its length (a brainstorm's theme 471
   of 300, a draft's brief 3,435 of 2,500, KDP's qa 1,605 and 909 of 900, a file 3,194 of 2,500, a step's title 185 of
   120): each cost a tool step, and three cycles that day ended at 20 of 20. A note for Ember or its owner that loses
   little when cut is cut now, and the answer says what was cut; what buyers or a recipient read, a file, a brief, a
   path or an id still refuses. Each text field is classified here, so that a new one is classified on purpose.
2. Conversation room. A work step couldn't see how much of its 96,000 bytes the conversation used: 4 of the 5 cycles on
   the KDP book ran out unannounced. Each step's newest user turn ends with "Room: 61 of 96 KB ...", a warning from 75%
   on, and keeps it as sent: the next step still reads the whole conversation from the prompt cache.
3. A journal's closing tags were taken off by a pattern that took a run of spaces' length squared (20,000 spaces after a
   field's mark: 0.6 s; 200,000: about a minute).

Also tests from 0.32.0 (formerly test_fixes_0320.py)."""

from __future__ import annotations

import itertools
import re
import time
from pathlib import Path

import pytest

from app.agent import loop, tools
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind
from tests.channels.test_etsy import call
from tests.cycle.test_agent import check_conversations, make_agent, plan, rows, text
from tests.cycle.test_agent import tools as replied
from tests.cycle.test_loop_shapes import JOURNAL, PLAN, run
from tests.plan.test_cycle_line import lined, working
from tests.plan.test_plan_tree import keep
from tests.products.test_kdp import next_reply, project, publishing
from tests.products.test_kdp_pages import QA, book, check, haushaltsbuch, propose

# --- 1. every text field: cut, or refused ---

# Cut to their length, and the answer says so (Field.cut): notes for Ember or its owner that lose little when cut.
NOTES = frozenset(
    {
        "project_create.title",
        "project_create.hypothesis",
        "project_update.hypothesis",
        "project_update.note",
        "project_update.bet",
        "plan_step.steps.title",  # 0.42.0: live, 185 of 120 was refused
        "plan_step.why",
        "venture_create.title",
        "venture_create.pitch",
        "venture_create.next_question",
        "venture_update.learned",
        "venture_update.pitch",
        "venture_update.next_question",
        "venture_update.demand",
        "venture_update.economics",
        "venture_update.setup",
        "venture_update.first_euro",
        "venture_update.risks",
        "venture_update.first_test",
        "venture_update.note",
        "brainstorm.theme",  # 0.42.0: live, 471 of 300 was refused
        "milestone_update.note",
        "milestone_update.wait_for",  # 0.42.0: shown cut to 80 characters anyway
        "request_approval.title",  # 0.42.0: the request card's heading
        "request_approval.expected_benefit",  # 0.42.0: as request_upgrade's
        "withdraw_request.reason",
        "message_owner.commits",  # 0.42.0: OBLIGATIONS shows it cut to its length; the message holds the promise
        "obligation_done.result",
        "request_upgrade.title",  # 0.42.0
        "request_upgrade.problem",
        "request_upgrade.proposed_change",
        "request_upgrade.expected_benefit",
        "set_sleep.reason",
        "write_journal.summary",
        "write_journal.entry",
        "write_journal.next",
        "mark_opt_out.reason",  # 0.42.0: the suppression's why
        "inquiry_done.reason",  # 0.42.0
        *(
            f"{name}.reason"
            for name in (
                "propose_email",
                "propose_reddit_post",
                "propose_etsy_listing",
                "propose_etsy_edit",
                "propose_pin",
                "propose_bluesky_post",
                "propose_printify_product",
                "propose_printify_edit",
                "propose_blog_post",
                "propose_link_page",
            )
        ),
    }
)
# Refused when too long, with how much to cut. What buyers, readers or a recipient see: a cut would go out unseen.
PUBLIC = frozenset(
    {
        "request_approval.payload",
        "make_image.title",
        "make_image.subtitle",
        "make_image.badge",
        "make_image.table",
        "propose_email.subject",
        "propose_email.body",
        "propose_reddit_post.title",
        "propose_reddit_post.body",
        "propose_etsy_listing.title",
        "propose_etsy_listing.description",
        "propose_etsy_listing.tags",
        "propose_etsy_edit.title",
        "propose_etsy_edit.description",
        "propose_etsy_edit.tags",
        "propose_pin.title",
        "propose_pin.description",
        "propose_pin.alt_text",
        "propose_pin.board_name",
        "propose_bluesky_post.text",
        "propose_bluesky_post.alt_text",
        "propose_printify_product.title",
        "propose_printify_product.description",
        "propose_printify_product.tags",
        "propose_printify_edit.title",
        "propose_printify_edit.description",
        "propose_printify_edit.tags",
        "site_page.title",
        "site_page.description",
        "site_page.menu",
        "propose_link_page.bio",
        "propose_link_page.links.label",
        "propose_link_page.links.note",
    }
)
# A file's or a memory file's whole text, or what a paid call follows (a draft's brief, a workshop's task, a research
# question): cut, it would be damaged or paid for unseen. A longer text goes in parts, or in a file.
WHOLE = frozenset(
    {"workspace_write.content", "memory_update.content", "draft.brief", "workshop.task", "research.question"}
)
# What the owner acts on or decides by, a record that can't be corrected, or one Ember's code checks: a request's
# description and cost, a message, a milestone's final result, a claim graded with its page, a demand note.
ACTED_ON = frozenset(
    {
        "request_approval.description",
        "request_approval.expected_cost",
        "message_owner.text",
        "milestone_update.result",
        "evidence.claim",
        "demand_note.demand",
    }
)
# Paths and lists of them, links, addresses and numbers: a cut one names something else.
NAMES = frozenset(
    {
        "workspace_list.path",
        "workspace_read.path",
        "workspace_write.path",
        "evidence.url",
        "message_owner.answers",
        "obligation_done.numbers",
        "request_upgrade.workshop_script",
        "draft.path",
        "draft.sources",
        "research.url",
        "research.site",
        "workshop.files",
        "workshop.script",
        "workshop.folder",
        "make_document.source",
        "make_document.output",
        "make_document.pages",
        "make_spreadsheet.source",
        "make_spreadsheet.output",
        "make_image.output",
        "make_image.pages",
        "resize_image.source",
        "resize_image.output",
        "make_cost_statement.source",
        "make_cost_statement.output",
        "look.path",
        "propose_email.to",
        "propose_reddit_post.subreddit",
        "propose_reddit_post.thread_url",
        "propose_kdp_book.spec",
        "demand_note.source",
        "propose_etsy_listing.files",
        "propose_etsy_listing.photos",
        "propose_etsy_edit.photos",
        "propose_etsy_edit.files",
        "propose_pin.image",
        "propose_pin.board_id",
        "propose_bluesky_post.link",
        "propose_bluesky_post.image",
        "propose_printify_product.image",
        "propose_printify_edit.image",
        "site_page.name",
        "site_page.source",
        "propose_blog_post.source",
        "propose_link_page.links.url",
    }
)
# Choices, amounts, dates, colours and search words.
VALUES = frozenset(
    {
        "workspace_write.mode",
        "memory_update.file",
        "memory_update.mode",
        "memory_read.file",
        "project_create.status",
        "project_update.status",
        "plan_step.action",
        "plan_step.steps.kind",
        "plan_step.stage",
        "plan_step.on",
        "plan_step.until",
        "venture_create.stage",
        "evidence.metric",
        "evidence.low",
        "evidence.high",
        "evidence.unit",
        "evidence.region",
        "venture_case.channel",
        "venture_case.price_eur",
        "venture_case.unit_cost_eur",
        "venture_case.monthly_costs_eur",
        "venture_case.setup_eur",
        "venture_case.owner_hours",
        "venture_case.api_usd",
        "venture_case.needs",
        "venture_update.stage",
        "knowledge_search.query",
        "milestone_update.status",
        "milestone_update.due",
        "milestone_update.check_at",
        "request_approval.type",
        "message_owner.due",
        "request_upgrade.priority",
        "draft.mode",
        "make_image.shape",
        "make_image.layout",
        "make_image.accent",
        "make_image.background",
        "guide.topic",
        "propose_reddit_post.kind",
        "etsy_categories.search",
        "demand_note.keywords",
        "propose_etsy_listing.price",
        "propose_etsy_edit.state",
        "propose_etsy_edit.price",
        "propose_bluesky_post.language",
        "printify_catalog.search",
        "propose_printify_product.prices",
    }
)
# No maxLength: an edit's find (a call's texts together hold CALL_CHARS), a KDP check's pages and qa (lengths their
# code keeps, out of the fixed prompt: qa is cut to QA_CHARS, below).
UNBOUNDED = frozenset({"workspace_write.find", "propose_kdp_book.pages", "propose_kdp_book.qa"})
REFUSED = PUBLIC | WHOLE | ACTED_ON | NAMES | VALUES


def text_fields() -> list[tuple[str, tools.Field]]:
    """Every text field of every tool (an array's items' too), as each kind of cycle describes it."""
    found = []
    for specs in (tools.SPECS, tools.VENTURE_VARIANTS, tools.ORDINARY_VARIANTS):
        for spec in specs.values():
            for name, field in spec.fields.items():
                pairs = [(f"{name}.{item}", f) for item, f in field.items] if field.type == "array" else [(name, field)]
                found += [(f"{spec.name}.{key}", f) for key, f in pairs if f.type == "string"]
    return found


def test_every_text_field_is_cut_or_refused_on_purpose() -> None:
    groups = (NOTES, PUBLIC, WHOLE, ACTED_ON, NAMES, VALUES, UNBOUNDED)
    assert sum(map(len, groups)) == len(frozenset().union(*groups))  # each in one group
    fields = text_fields()
    assert {key for key, _ in fields} == frozenset().union(*groups)  # a new field: classify it here
    for key, field in fields:
        assert field.cut == (key in NOTES), key
        assert (field.max_len == 0) == (key in UNBOUNDED), key
        assert not (field.cut and field.enum), key
    # the model still sees each limit before it writes (and a cut one isn't refused for it)
    theme = {d["name"]: d for d in tools.definitions(venture=True)}["brainstorm"]["input_schema"]["properties"]
    assert theme["theme"]["maxLength"] == 300
    assert REFUSED.isdisjoint(NOTES)


def test_a_long_theme_is_cut_and_the_brainstorm_runs(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    ctx.venture = True
    asked: list[str] = []

    def brainstorm(theme: str, venture_id: int | None) -> tools.Outcome:
        asked.append(theme)
        return tools.Outcome(True, "Added 6 ideas to your tree.", "6 ideas", paid=True)

    ctx.brainstorm = brainstorm
    theme = ("Budget planners for German families " * 14)[:471]
    done = call(ctx, "brainstorm", {"theme": theme})
    assert done.ok, done.text  # before: "theme is too long: 471 of 300 characters (cut 171)", and a step lost
    assert done.text == "Added 6 ideas to your tree. (Saved, but theme was cut to 300 of its 471 characters.)"
    assert asked == [theme[:299].rstrip() + "…"]


def test_a_long_step_title_is_cut_and_the_step_added(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Hiking trail guides",))
    keep(agent)
    title = ("Count the top 20 listings of hiking trail guides, with their prices and reviews, " * 3)[:185]
    added = call(
        working(agent, 1),
        "plan_step",
        {"action": "add", "stage": "create", "steps": [{"title": title, "kind": "create"}], "why": "research"},
    )
    assert added.ok, added.text  # before: "title is too long: 185 of 120 characters (cut 65)"
    assert "steps item 1's title was cut to 120 of its 185 characters" in added.text
    kept = title[:119].rstrip() + "…"
    assert rows(agent, f"SELECT COUNT(*) AS n FROM plan_nodes WHERE title = '{kept}'") == [{"n": 1}]


def test_a_long_qa_is_cut_and_the_book_goes_to_the_owner(data_dir: Path) -> None:
    """The owner reads qa on the request card, beside what Ember's code found, and checks the files themselves before
    approving; the page check it needs is this cycle's, so a refusal at a cycle's last steps lost the check too."""
    agent, ctx = publishing(data_dir)
    path = book(ctx.workspace, haushaltsbuch(weeks=21), "books/hb", project(agent))
    check(ctx, path)
    next_reply(ctx)
    qa = f"{QA}\n" * 9  # about 1,600 characters, as live
    said = " ".join(qa.split())
    made = propose(ctx, path, qa)
    assert made.ok and "Approval request #" in made.text, made.text  # before: "qa has 1,610 characters: ..."
    assert f" Note: qa was cut to {tools.QA_CHARS:,} of its {len(said):,} characters on your owner's request." in (
        made.text
    )
    [row] = rows(agent, "SELECT description FROM approvals WHERE executor = 'kdp_package'")
    assert f"{said[: tools.QA_CHARS - 1]}…\n\n{tools.KDP_NOTE}" in row["description"]


# --- 2. the conversation's room, in each step ---


ROOM = re.compile(rf"Room: (\d+) of {loop.MAX_CONVERSATION_BYTES // 1_000} KB of this cycle's conversation used\.")


def test_each_step_is_told_the_room_its_conversation_uses(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            replied(("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "Notes. " * 340})),
            replied(("workspace_read", {"path": "notes/a.md"})),
            text("Done."),
            text("Reflected."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    check_conversations(transport.sent)  # the line comes after the step's tool results
    first, second, third, reflect = (transport.sent[n]["messages"] for n in (1, 2, 3, 4))
    assert len(first) == 1  # the brief alone: nothing used yet
    used = [ROOM.fullmatch(step[-1]["content"][-1]["text"]) for step in (second, third)]
    assert all(used) and 2 <= int(used[0][1]) < int(used[1][1]), used  # each step's own
    assert int(used[1][1]) == loop._size(third[1:-1] + [{**third[-1], "content": third[-1]["content"][:-1]}]) // 1_000
    # each request is the next one's start, its room line too: the next step reads it from the cache
    assert third[: len(second)] == second and reflect[: len(third)] == third
    assert not ROOM.search(str(transport.sent[0]))  # nor in the plan, the system prompt or the tools
    assert not any(ROOM.search(str([r["system"], r["tools"]])) for r in transport.sent[1:])


def test_the_room_line_warns_from_three_quarters(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "short" not in loop.room_line(71_999)
    assert loop.room_line(72_000) == (
        "Room: 72 of 96 KB of this cycle's conversation used. Room is short: finish, or save your work in a file "
        "before it runs out."
    )
    monkeypatch.setattr(loop, "MAX_CONVERSATION_BYTES", 3_000)  # the first step's write fills three quarters of it
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            replied(("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "Notes. " * 340})),
            text("Done."),
            text("Reflected."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    said = transport.sent[2]["messages"][-1]["content"][-1]["text"]
    assert said.startswith("Room: 2 of 3 KB") and "Room is short: finish, or save your work in a file" in said


def test_the_next_step_still_reads_the_whole_conversation_from_the_cache(data_dir: Path) -> None:
    """0.40.3's test_the_last_step_keeps_auto_and_reads_the_conversations_cache, over a whole cycle: each work step
    and the reflection read from the cache all the step before them read and wrote."""
    fake = FakeTransport(
        script=[
            Plan(PLAN),
            ToolCalls([("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "Notes. " * 300})]),
            ToolCalls([("workspace_read", {"path": "notes/a.md"})]),
            ToolCalls([("workspace_list", {})]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake)
    work = [r for r in fake.sent if request_kind(r) == "work"]
    assert sum(1 for r in work if ROOM.fullmatch(str(r["messages"][-1]["content"][-1].get("text")))) == 3
    calls = rows(
        agent,
        "SELECT purpose, cache_read_tokens AS read, cache_write_5m_tokens + cache_write_1h_tokens AS written"
        " FROM llm_calls WHERE purpose IN ('work', 'reflect') ORDER BY id",
    )
    assert [c["purpose"] for c in calls] == ["work"] * 4 + ["reflect"]
    for before, after in itertools.pairwise(calls):
        assert after["read"] == before["read"] + before["written"] > before["read"], calls


# --- 3. a journal's closing tags, in linear time ---


def test_closing_tags_are_taken_off_in_linear_time() -> None:
    spec, spaces = tools.SPECS["write_journal"], " " * 200_000
    after_a_mark = {"summary": "s", "entry": f'Did it.</entry>\n<parameter name="next">{spaces}Go on.'}
    before_a_mark = {"summary": "s", "entry": f'Did it.{spaces}</entry>\n<parameter name="next">Go on.'}
    a_call = {
        "summary": "s",
        "entry": f'Did it.</entry>\n</invoke>\n<invoke name="workspace_list">\n<parameter name="path">notes{spaces}',
    }
    leaked: list[tuple[str, dict[str, object]]] = []
    started = time.perf_counter()
    moved = [tools.unleaked(spec, raw, [], leaked) for raw in (after_a_mark, before_a_mark, a_call)]
    assert time.perf_counter() - started < 1  # before: about a minute for the first
    assert [(m["entry"], m.get("next")) for m in moved[:2]] == [("Did it.", "Go on."), ("Did it.", "Go on.")]
    assert leaked == [("workspace_list", {"path": "notes"})]
    # as the pattern took them off: the closing tags at the end and the spaces before them, else nothing
    assert tools._unclosed("Did it. </entry>\n </parameter>\n") == "Did it."
    assert tools._unclosed("Did it.  ") == "Did it.  " and tools._unclosed("a </b> c") == "a </b> c"
    assert tools._unclosed("</next>") == "" and tools._unclosed(f"x{spaces}") == f"x{spaces}"


# --- From 0.32.0 (formerly test_fixes_0320.py) ---


def test_the_guides_say_how_often_a_cycle_may_use_a_tool_and_a_cut_off_call_hears_what_fits() -> None:
    # the fixed prompt has no room for every tool's cap: the guides of the ones the agent met mid-plan say theirs
    assert "A cycle makes\n  3 spreadsheets at most" in tools.guide_text("spreadsheets")
    assert "At most 2 posts a cycle." in tools.guide_text("bluesky")
    assert "At most 2 pins a cycle." in tools.guide_text("pinterest")
    assert all("{CAP:" not in tools.guide_text(topic) for topic in tools.GUIDES)
    assert loop.cut_call("workspace_write") == loop.CUT_CALL and "Write a long file in parts" in loop.CUT_CALL
    assert loop.cut_call("venture_case") == loop.CUT_OTHER and "file" not in loop.CUT_OTHER  # live: told of a file
