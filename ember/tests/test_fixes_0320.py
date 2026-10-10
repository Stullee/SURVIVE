"""0.32.0: what the diagnostics of 2026-10-07 (0.31.0, cycles #118-#129) showed: one call in five was refused.

A marketing cycle on the posters couldn't link them: a post or a pin took only the listings Ember's code listed itself,
never the ones Printify made of its products (three refusals in two days, and the line with the least reach stayed
first in READY). English posts were held to the room of the German AI line, 19 characters less than they have (four
refusals). A refused proposal of a venture threw away everything else the call carried, and named one missing piece at
a time (ten refusals in two venture cycles, the texts sent again and again until the conversation got too long).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import loop, news, obligations, tools, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.integrations import bluesky, etsy  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.products import sheets  # noqa: E402
from tests import (  # noqa: E402
    test_bluesky,
    test_knockouts,
    test_pinterest,
    test_printify,
    test_venture_stages,
    test_ventures,
)
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import call, shop_context, started  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

EVERYWHERE = ROOMY.model_copy(update={"printify_enabled": True, "bluesky_enabled": True, "pinterest_enabled": True})
POSTER_TITLE = "Minimalist mountain poster, matte print"  # test_printify.a_proposal's


def approve(agent: Any, request: int) -> None:
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200


def poster(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent with Printify, Bluesky and Pinterest on (their fake accounts), the fake shop's first listing
    live, and a poster Printify made of a product of Ember's live at Etsy: its listing's number."""
    agent, _ = run(data_dir, FakeTransport(), cycles=4, settings=EVERYWHERE, brake=False)  # test_etsy.proposed
    listing = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    approve(agent, listing)
    assert agent.execute_approved() == [(listing, "active")]
    ctx = test_printify.pod_context(agent)
    test_printify.read_catalog(ctx)
    made = test_printify.a_proposal(agent, ctx)
    assert made.ok, made.text
    product = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'printify_product'")[0]["id"]
    approve(agent, product)
    assert agent.execute_approved() == [(product, "active")]
    [row] = rows(agent, "SELECT listing_id FROM printify_products")
    return agent, int(row["listing_id"])


def channels(agent: Any) -> tools.ToolContext:
    """The tools of a cycle on the poster's line (the shop listing's: the product was proposed on it)."""
    ctx = test_printify.pod_context(agent)
    ctx.bluesky = tools.BlueskyAccess(FakeBluesky.HANDLE, 2, "")
    ctx.pinterest = tools.PinterestAccess("ember-dry-run", 3)
    return ctx


def a_post(ctx: tools.ToolContext, link: str, **args: Any) -> tools.Outcome:
    fields = {
        "text": "Bauhaus colours for a small room: two strong tones and one neutral.",
        "language": "en",
        "link": link,
        "reason": "People who decorate small rooms look for exactly this.",
        **args,
    }
    return call(ctx, "propose_bluesky_post", fields)


def a_pin(agent: Any, ctx: tools.ToolContext, listing_id: int) -> tools.Outcome:
    agent.roots()[0].write_bytes("shop/pin-poster.png", test_pinterest.picture())
    return call(
        ctx,
        "propose_pin",
        {
            "listing_id": listing_id,
            "image": "shop/pin-poster.png",
            "title": "Minimalist mountain poster",
            "description": "A calm mountain line drawing, printed on matte paper and made on order.",
            "alt_text": "A mountain line drawing in a black frame above a sofa",
            "reason": "Pinterest searchers look for calm wall art; this sends them to the poster.",
            "board_name": "Calm wall art",
        },
    )


# --- 1. a post or a pin links a poster Printify made ----------------------------------------------------------------


def test_a_post_and_a_pin_link_a_poster_printify_made_and_are_carried_out(data_dir: Path) -> None:
    agent, listing_id = poster(data_dir)
    ctx = channels(agent)
    post = a_post(ctx, etsy.listing_url(listing_id))
    assert post.ok, post.text  # live: "isn't one of your live listings"
    shown = call(ctx, "etsy_listing", {"listing_id": listing_id})
    assert shown.ok and "a pin or a post may link them" in shown.text
    [request] = rows(agent, "SELECT id, action FROM approvals WHERE executor = 'bluesky_post'")
    action = json.loads(request["action"])
    assert action["link"] == etsy.listing_url(listing_id)
    assert (action["link_title"], action["card_photo"]) == (POSTER_TITLE, None)  # Printify keeps its photos
    pin = a_pin(agent, ctx, listing_id)
    assert pin.ok, pin.text
    [pinned] = rows(agent, "SELECT id FROM approvals WHERE executor = 'pinterest_pin'")
    approve(agent, request["id"])
    approve(agent, pinned["id"])
    # Ember's code checks the listing again when it carries them out: still Ember's, still live
    assert sorted(agent.execute_approved()) == [(request["id"], "active"), (pinned["id"], "active")]
    assert rows(agent, "SELECT status FROM bluesky_posts") == [{"status": "active"}]
    assert rows(agent, "SELECT status FROM pinterest_pins") == [{"status": "active"}]


def test_a_poster_etsy_no_longer_shows_and_a_stranger_s_listing_stay_refused(data_dir: Path) -> None:
    agent, listing_id = poster(data_dir)
    ctx = channels(agent)
    stranger = a_post(ctx, etsy.listing_url(12))
    assert not stranger.ok and "#12 isn't one of your live listings" in stranger.text
    with agent.db.transaction() as conn:
        conn.execute("UPDATE printify_products SET state = 'inactive' WHERE listing_id = ?", (listing_id,))
    idle = a_post(ctx, etsy.listing_url(listing_id))
    assert not idle.ok and f"#{listing_id} isn't live at Etsy (deactivated)" in idle.text
    pin = a_pin(agent, ctx, listing_id)
    assert not pin.ok and f"#{listing_id} isn't live at Etsy (deactivated)" in pin.text


# --- 2. an English post has the room its own AI line leaves ----------------------------------------------------------


def test_an_english_post_has_the_room_its_ai_line_leaves_and_a_german_one_hears_by_how_much(data_dir: Path) -> None:
    assert bluesky.WORDS_CHARS == {"de": 239, "en": 258}
    [spec] = [d for d in tools.definitions(etsy=True, bluesky=True) if d["name"] == "propose_bluesky_post"]
    field = spec["input_schema"]["properties"]["text"]
    assert field["maxLength"] == 258  # the room of the English line; the guide says both
    assert "the words alone have 258 in English and 239 in German" in tools.guide_text("bluesky").replace("\n   ", " ")
    agent, _ = test_bluesky.listed(data_dir)
    ctx = test_bluesky.post_context(agent)
    words = "x" * 250
    german = test_bluesky.a_post(agent, ctx, text=words, language="de", link=None)
    assert not german.ok and "would be 311 characters with the AI line Ember's code adds" in german.text, german.text
    assert "shorten your words by 11" in german.text
    english = test_bluesky.a_post(agent, ctx, text=words, language="en")  # live: "text is too long: 250 of 239"
    assert english.ok, english.text
    too_long = test_bluesky.a_post(agent, ctx, text="y" * 259, language="en")
    assert not too_long.ok and "text is too long: 259 of 258 characters" in too_long.text, too_long.text


# --- 3. a refused proposal keeps the rest of its update, and names every gap and knock-out at once -------------------


def in_a_venture_cycle(agent: Any, tool: str, **args: Any) -> tools.Outcome:
    """A call of a venture cycle's (its tools take a venture's scores and case)."""
    return calling(agent, tool, venture=True, **args)


def calling(agent: Any, tool: str, *, venture: bool = False, **args: Any) -> tools.Outcome:
    """A call of the agent's newest cycle, an ordinary one unless ``venture``."""
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
        venture=venture,
    )
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, args, f"toolu_{tool}", llm_call, "act")


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_refused_proposal_keeps_its_case_and_names_every_gap_and_knock_out_at_once(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=test_ventures.VENTURING)
    vid = test_ventures.DROPSHIPPING
    # live (venture #23): its whole case, its scores before any research and the stage, in one call
    first = in_a_venture_cycle(
        agent, "venture_update", venture_id=vid, stage="proposed", **test_ventures.SCORES, **test_ventures.CASE
    )
    assert first.ok and first.summary == f"venture #{vid} updated (not all of it)", first.text
    assert "Not done (the rest is saved): scores come from research: research venture #3 first" in first.text
    assert (
        "Not done (the rest is saved): venture #3 can't be proposed yet: a business case needs 2 research calls for "
        "it that found something (it has 0); scores for revenue, doability, difficulty, risk, speed, cost; its numbers "
        "(venture_case). Ember's code knocks it out too: no independent source for its demand (it has no evidence "
        "yet: save an independent page's demand numbers with evidence)." in first.text
    )
    kept = test_ventures.venture(agent, vid)
    assert {name: kept[name] for name in ventures.CASE_FIELDS} == test_ventures.CASE  # live: thrown away each time
    assert (kept["stage"], kept["revenue"], kept["proposed_at"]) == ("idea", None, None)
    # what it still needs, and nothing it sent before: research, its numbers, an independent page, the scores
    for _ in range(ventures.RESEARCH_TO_PROPOSE):
        test_venture_stages.researched(agent, vid)
    test_knockouts.case(agent)
    test_knockouts.independent(agent)
    proposed = in_a_venture_cycle(agent, "venture_update", venture_id=vid, stage="proposed", **test_ventures.SCORES)
    assert proposed.ok and "Venture #3: idea → proposed." in proposed.text and "Not done" not in proposed.text
    assert test_ventures.venture(agent, vid)["stage"] == "proposed"
    # a refusal with nothing else in the call is a refusal, as before
    again = in_a_venture_cycle(agent, "venture_update", venture_id=vid, stage="live")
    assert not again.ok and "a venture goes live once your owner backed it" in again.text


# --- 4. an edit changes one passage of a file, however long it is ----------------------------------------------------


def a_spec(net: str = "=B3-B4") -> str:
    """The budget planner of cycle #129, its Summary's Net as ``net`` (live: "=B3-B4", the header less the income)."""

    def table(name: str, empty: int) -> dict[str, Any]:
        columns = [{"title": "Source", "width": 22}, {"title": "Planned", "format": "usd"}]
        columns.append({"title": "Actual", "format": "usd"})
        rows = [["Salary", 2800, 2800]]
        return {"name": name, "title": f"Monthly {name}", "columns": columns, "rows": rows, "empty_rows": empty}

    summary = {
        "name": "Summary",
        "title": "Monthly Summary",
        "columns": [{"title": "Item", "width": 24}, {"title": "Amount", "format": "usd", "width": 16}],
        "rows": [
            ["Total Income", "=SUM(Income!C4:C12)"],
            ["Total Expenses", "=SUM(Expenses!C4:C26)"],
            ["Net (Income - Expenses)", net],
        ],
    }
    notes = [f"Step {i}: type your planned and actual amounts, then read the summary and the year." for i in range(30)]
    sheets = [
        {**table("Income", 8), "totals": {"Actual": "sum"}},
        {**table("Expenses", 22), "totals": {"Actual": "sum"}},
    ]
    return json.dumps({"title": "Budget Planner", "notes": notes, "sheets": [*sheets, summary]}, indent=2) + "\n"


def test_an_edit_changes_one_passage_of_a_long_file_in_one_small_call(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=ROOMY)
    workspace = agent.roots()[0]
    spec = a_spec()
    assert len(spec) > tools.WRITE_CHARS  # live: rewritten in parts, twice, until the cycle's writes ran out
    workspace.write("drafts/budget.json", spec)
    edit = {"path": "drafts/budget.json", "mode": "edit"}
    fixed = calling(agent, "workspace_write", **edit, find='"=B3-B4"', content='"=B4-B5"')
    assert fixed.ok and fixed.text.startswith("Edited drafts/budget.json: one passage replaced"), fixed.text
    assert workspace.read("drafts/budget.json") == spec.replace('"=B3-B4"', '"=B4-B5"')
    for find, why in (
        ('"=B3-B4"', "drafts/budget.json doesn't hold find's passage"),  # it was replaced
        ('"Net (Income -   Expenses)",', "it does with other spaces or line breaks"),
        ('"usd"', f"find's passage is in drafts/budget.json {spec.count('"usd"')} times: give more of it around"),
    ):
        refused = calling(agent, "workspace_write", **edit, find=find, content='"x"')
        assert not refused.ok and why in refused.text, (find, refused.text)
    missing = calling(agent, "workspace_write", **edit, content='"x"')
    assert not missing.ok and "edit needs find" in missing.text
    stray = calling(agent, "workspace_write", path="drafts/other.json", mode="create", content="{}", find="x")
    assert not stray.ok and "find is for mode edit" in stray.text
    [spec_field] = [d for d in tools.definitions() if d["name"] == "workspace_write"]
    assert "edit" in spec_field["input_schema"]["properties"]["mode"]["enum"]
    assert "maxLength" not in spec_field["input_schema"]["properties"]["find"]  # a call's texts hold CALL_CHARS
    long = calling(agent, "workspace_write", **edit, find="x" * tools.CALL_CHARS, content="y")
    assert not long.ok and f"one call holds at most {tools.CALL_CHARS:,}" in long.text


# --- 5. a formula's cell in a title, a header or below the data is named ---------------------------------------------


def test_a_formula_s_cell_in_a_header_or_below_the_data_is_named(data_dir: Path) -> None:
    live = sheets.parse(a_spec("=B3-B4"), lambda path: "")
    assert live.warnings == ["Summary row 6: B3 is the header row of Summary, whose data are rows 4 to 6"]
    assert sheets.parse(a_spec("=B4-B5"), lambda path: "").warnings == []  # the fix, and a total, say nothing
    assert sheets.parse(a_spec("=Income!C14"), lambda path: "").warnings == [
        "Summary row 6: Income!C14 is an empty cell below the data and the total of Income, whose data are rows 4 to "
        "12 (its total row 13)"
    ]
    assert sheets.parse(a_spec("=Income!C13-Expenses!C1"), lambda path: "").warnings == [
        "Summary row 6: Expenses!C1 is the title of Expenses, whose data are rows 4 to 26 (its total row 27)"
    ]
    # make_spreadsheet says it in its Check line, beside the data rows it gives
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=ROOMY)
    agent.roots()[0].write("drafts/budget.json", a_spec())
    made = calling(agent, "make_spreadsheet", source="drafts/budget.json", output="products/budget.xlsx")
    assert made.ok, made.text
    assert "Data rows: Income 4-12 (total 13), Expenses 4-26 (total 27), Summary 4-6." in made.text
    assert "Check: Summary row 6: B3 is the header row of Summary, whose data are rows 4 to 6." in made.text


# --- 6. the release notes go on where they stopped when another version is installed ---------------------------------


def a_plan_reads(agent: Any, running: str) -> str:
    """The part of the release notes a plan shows, marked as read as a plan that showed it whole marks it."""
    with agent.db.connection() as conn:
        found = news.collect(conn, agent.db, agent.scope(), running)
    news.mark_changelog_seen(agent.db, agent.scope(), found)
    assert news._json_bytes(found.changelog) <= news.CHANGELOG_LIMIT
    return found.changelog


def test_the_release_notes_go_on_where_they_stopped_after_another_upgrade(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def notes(version: str) -> str:
        return f"## {version}\n\n" + "\n".join(
            f"- {version}: change {i}, which Ember's code now makes, said in a few more words." for i in range(40)
        )

    path = tmp_path / "CHANGELOG.md"
    path.write_text(notes("0.5.0") + "\n\n" + notes("0.4.0"), encoding="utf-8")
    monkeypatch.setattr(paths, "CHANGELOG_PATH", path)
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=ROOMY)
    seen = news.changelog_key(agent.scope().mode)
    agent.db.set_meta(seen, "0.3.0")
    first = a_plan_reads(agent, "0.4.0")
    assert first.startswith("Your software was upgraded from 0.3.0 to 0.4.0. What changed:\n\n## 0.4.0\n")
    assert first.endswith(news.MORE)
    # live: the next version began the notes again from its own, and showed 0.4.0's start again
    second = a_plan_reads(agent, "0.5.0")
    assert second.startswith(
        "Your software was upgraded to 0.5.0 meanwhile. Its notes first, then the rest of your notes since 0.3.0:\n"
        "## 0.5.0\n"
    )
    parts = [first, second]
    while agent.db.get_meta(seen) == "0.3.0":
        assert len(parts) < 12
        parts.append(a_plan_reads(agent, "0.5.0"))
    assert agent.db.get_meta(seen) == "0.5.0" and all(p.startswith(news.CONTINUED) for p in parts[2:])
    read = "\n".join(parts)
    changes = [line for version in ("0.4.0", "0.5.0") for line in notes(version).splitlines() if line.startswith("- ")]
    assert [read.count(line) for line in changes] == [1] * len(changes)  # each once: none again, none left out
    assert read.count("\n## 0.4.0 (continued)\n") == 1  # where the notes of a version go on after another's
    assert a_plan_reads(agent, "0.5.0") == ""  # all read: nothing until the next version


# --- 7. small frictions: a promise said twice, an answered message, the caps, a cut-off call --------------------------


def test_a_promise_said_in_other_words_is_one_promise_and_another_listing_s_is_its_own(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    agent.clock.advance(days=1)  # the fake's cycles wrote to the owner yesterday: today's two messages are free
    ctx = shop_context(agent)
    due = (agent.clock.today() + timedelta(days=3)).isoformat()
    first = call(
        ctx, "message_owner", {"text": "KDP next.", "commits": "Propose the Haushaltsbuch 2027 KDP book", "due": due}
    )
    assert first.ok and "Your promise is obligation #" in first.text
    again = {"text": "Coming.", "commits": "Send the Haushaltsbuch 2027 KDP proposal", "due": due}  # live: #32, #33
    second = call(ctx, "message_owner", again)
    assert second.ok and "Its promise repeats your open promise #" in second.text, second.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations WHERE kind = 'promise'") == [{"n": 1}]
    [made] = rows(agent, "SELECT cycle_id, message_id, created_at FROM obligations WHERE kind = 'promise'")
    with agent.db.transaction() as conn:  # a reference stays whole: another listing's report is a promise of its own
        what = "Report views of listing #900000101"
        obligations.promise(conn, agent.scope(), made["cycle_id"], made["message_id"], what, due, made["created_at"])
        assert obligations.repeated_promise(conn, agent.scope(), "Report views of listing #900000102", due) is None
        assert obligations.repeated_promise(conn, agent.scope(), "Report the views of listing #900000101", due)
        assert obligations._promise_words("Report views of listing #900000101") == {
            "report",
            "view",
            "listin",
            "#900000101",
        }


def test_naming_an_answered_message_of_the_owner_s_in_obligation_done_is_no_error(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    agent.clock.advance(days=1)
    assert owner(agent).send_message({"text": "How do I claim the shop?"}, "Stefan").status == 201
    [message] = rows(agent, "SELECT MAX(id) AS id FROM messages")
    with agent.db.connection() as conn:
        listed = obligations.text(conn, agent.scope(), agent.clock.today())
    assert f"message #{message['id']} (FROM YOUR OWNER; waiting since" in listed
    assert "): message_owner naming it in answers closes it." in listed
    with agent.db.transaction() as conn:  # the plan showed it (an answer names a message the agent has seen)
        conn.execute("UPDATE messages SET seen_cycle_id = (SELECT MAX(id) FROM cycles) WHERE id = ?", (message["id"],))
    ctx = shop_context(agent)
    reply = call(ctx, "message_owner", {"text": "Settings > Claimed accounts > Etsy.", "answers": str(message["id"])})
    assert reply.ok, reply.text
    [answer] = rows(agent, "SELECT MAX(id) AS id FROM messages WHERE sender = 'agent'")
    done = call(
        ctx, "obligation_done", {"numbers": str(message["id"]), "result": f"Answered via message #{answer['id']}."}
    )
    assert done.ok and done.summary == "nothing to close", done.text  # live: refused in 4 of 12 cycles
    assert (
        f"#{message['id']} is your owner's message, which your message #{answer['id']} answered: nothing to close"
        in (done.text)
    )


def test_the_guides_say_how_often_a_cycle_may_use_a_tool_and_a_cut_off_call_hears_what_fits() -> None:
    # the fixed prompt has no room for every tool's cap: the guides of the ones the agent met mid-plan say theirs
    assert "A cycle makes\n  3 spreadsheets at most" in tools.guide_text("spreadsheets")
    assert "At most 2 posts a cycle." in tools.guide_text("bluesky")
    assert "At most 2 pins a cycle." in tools.guide_text("pinterest")
    assert all("{CAP:" not in tools.guide_text(topic) for topic in tools.GUIDES)
    assert loop.cut_call("workspace_write") == loop.CUT_CALL and "Write a long file in parts" in loop.CUT_CALL
    assert loop.cut_call("venture_case") == loop.CUT_OTHER and "file" not in loop.CUT_OTHER  # live: told of a file


def test_an_older_version_installed_while_the_notes_are_read_shows_its_own_from_their_start(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text("\n\n".join(f"## 0.{m}.0\n\n" + "- A change.\n" * 300 for m in (6, 5)), encoding="utf-8")
    monkeypatch.setattr(paths, "CHANGELOG_PATH", path)
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=ROOMY)
    agent.db.set_meta(news.changelog_key(agent.scope().mode), "0.4.0")
    assert a_plan_reads(agent, "0.6.0").endswith(news.MORE)  # 0.6.0's notes, in part
    # a backup of 0.5.0 restored: what was shown of 0.6.0 isn't 0.5.0's, whose notes begin as they would have
    assert a_plan_reads(agent, "0.5.0").startswith("Your software was upgraded from 0.4.0 to 0.5.0. What changed:")
