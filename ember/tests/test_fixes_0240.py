"""0.24.0: fixes from the diagnostics of 2026-10-06 (0.23.2, cycles #102 to #113).

The quality critic judged a product line's newest listing without saying which, so its verdicts on a €39 licence
bundle were taken for verdicts on the line's cover letter listing for three days. Two reflections lost their journal,
handoff and lessons to another tool's call the model wrote inside the entry, and every cycle wrote its journal in a work
step only to have it refused. A post that repeated a live one reached the owner; a kept script run again drew a cover
from numbers it made up; a lesson naming a tool was dropped by the call that added it. Promises, bets, milestones the
code set, channels that wait for the owner, knock-outs and owner messages each cost refused calls or a wrong plan.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import bets, lines, loop, memory, obligations, prompts, quality, roadmap, tools, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import bluesky  # noqa: E402
from app.products import make  # noqa: E402
from tests.test_agent import make_agent, plan, rows, text  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_bluesky import WORDS, a_post, post_context, posted  # noqa: E402
from tests.test_etsy import call, shop_context  # noqa: E402
from tests.test_fixes_0140_channels import not_set_up  # noqa: E402
from tests.test_listing_gates import started  # noqa: E402
from tests.test_loop_shapes import JOURNAL, PLAN, run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_pinterest import PINNING  # noqa: E402
from tests.test_products import jail  # noqa: E402
from tests.test_roadmap import plan as fake_plan  # noqa: E402
from tests.test_ventures import NUMBERS  # noqa: E402
from tests.test_workshop import png, result, workshop_answer, workshop_cycle  # noqa: E402


def now(agent: Any) -> str:
    return to_iso(agent.clock.now())


# --- the quality critic judges each listing, and names it ---------------------------------------------------------


def two_listings(data_dir: Path) -> tuple[Any, int, int, int]:
    """A product line with two live listings, as project #4 had: its first one and a licence bundle made later.
    (agent, project, first listing, second listing)"""
    agent, project = started(data_dir)
    [first] = [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    second = first + 1
    with agent.db.transaction() as conn:
        approval = dict(conn.execute("SELECT * FROM approvals WHERE executor = 'etsy_listing'").fetchone())
        approval.pop("id")
        approval["payload_sha256"] = "f" * 64
        made = conn.execute(
            f"INSERT INTO approvals ({', '.join(approval)}) VALUES ({', '.join('?' for _ in approval)})",
            list(approval.values()),
        ).lastrowid
        listing = dict(conn.execute("SELECT * FROM etsy_listings").fetchone())
        listing.pop("id")
        listing.update(approval_id=made, listing_id=second, title="Resume Template Commercial License Bundle")
        conn.execute(
            f"INSERT INTO etsy_listings ({', '.join(listing)}) VALUES ({', '.join('?' for _ in listing)})",
            list(listing.values()),
        )
    return agent, project, first, second


def checked(agent: Any, project: int, listing: int, fixes: str = "Fix the tags.") -> None:
    answer = {"score": 5, "verdict": "improve", "fixes": fixes}
    with agent.db.transaction() as conn:
        quality.save(conn, agent.scope(), project, None, answer, None, now(agent), listing)


def test_the_critic_checks_each_listing_of_a_line_and_the_edited_one_again(data_dir: Path) -> None:
    agent, project, first, second = two_listings(data_dir)
    scope, today = agent.scope(), agent.clock.today()
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, first)  # never checked: the oldest listing first
    checked(agent, project, first)
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, second)  # before, it judged only the newest one
        case, _ = quality.case(conn, scope, agent.roots()[0], project, second)
    assert f"LISTING: #{second}" in case and "Title: " in case
    agent.clock.advance(hours=1)
    checked(agent, project, second, "The licence lists a German Anschreiben the files lack.")
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) is None
    agent.clock.advance(hours=1)
    with agent.db.transaction() as conn:  # an edit of the first listing carried out: that one is checked again
        approval = conn.execute("SELECT MAX(id) AS id FROM approvals").fetchone()["id"]
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, finished_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'done')",
            (scope.mode, scope.session, approval, first, now(agent), now(agent)),
        )
    with agent.db.connection() as conn:
        assert quality.due(conn, scope, today) == (project, first)


def test_every_verdict_names_the_listing_it_judged(data_dir: Path) -> None:
    agent, project, first, second = two_listings(data_dir)
    checked(agent, project, first, "Add the phrase bank.")
    checked(agent, project, second, "The cover letter is PDF only: add the .docx.")
    scope = agent.scope()
    with agent.db.connection() as conn:
        named = quality.label(conn, scope, second)
        older = quality.label(conn, scope, first)
        said = quality.review_text(conn, scope, project)
        items = lines.fixes(conn, scope, project)  # 0.28.0: the line's jobs in READY (slack.py's improve items)
        assert quality.verdict(conn, scope, project) == "improve"
    assert named == f'listing #{second} "Resume Template Commercial License Bundle"'
    assert said.startswith(f"quality 5/10, improve ({agent.clock.today().isoformat()}, {named}): The cover letter")
    assert f"{older}): Add the phrase bank." in said
    assert items == [
        f"the quality critic said of {named}: The cover letter is PDF only: add the .docx.",
        f"the quality critic said of {older}: Add the phrase bank.",
    ]


def test_a_verdict_older_than_a_change_of_its_listing_waits_for_the_next_check(data_dir: Path) -> None:
    """Live, the cover letter listing's check of 10-03 (upload the phrase bank) was done by an edit on 10-04."""
    agent, project, first, second = two_listings(data_dir)
    checked(agent, project, first, "Upload the phrase bank.")
    checked(agent, project, second, "Swap the tags 'resell rights' and 'white label'.")
    agent.clock.advance(hours=1)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        approval = conn.execute("SELECT MAX(id) AS id FROM approvals").fetchone()["id"]
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, finished_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'done')",
            (scope.mode, scope.session, approval, first, now(agent), now(agent)),
        )
    with agent.db.connection() as conn:
        ready = lines.fixes(conn, scope, project)
        said = quality.review_text(conn, scope, project)
        assert quality.due(conn, scope, agent.clock.today()) == (project, first)
    assert ready == [
        f"the quality critic said of {quality_label(agent, second)}: Swap the tags 'resell rights' and 'white label'."
    ]  # noqa: E501
    assert f"{quality_label(agent, first)}, changed at Etsy since): Upload the phrase bank." in said


def quality_label(agent: Any, listing: int) -> str:
    with agent.db.connection() as conn:
        return quality.label(conn, agent.scope(), listing)


def test_a_line_whose_work_the_owner_s_park_stopped_gets_no_check_and_nothing_in_ready(data_dir: Path) -> None:
    """With 0.23.3 nothing may change, pin, post or recommend such a line's listings: no paid check, no READY item."""
    agent, project = started(data_dir)
    with agent.db.transaction() as conn:
        leg = ventures.create(conn, agent.scope(), title="A", pitch="p.", stage="building", now=now(agent))
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = ?", (leg, project))
    [listing] = [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    checked(agent, project, listing)
    with agent.db.connection() as conn:
        assert lines.fixes(conn, agent.scope(), project)
    assert owner(agent).decide_venture(leg, {"action": "park", "comment": "Stop."}, "Stefan").status == 200
    agent.clock.advance(days=quality.RECHECK_DAYS + 1)  # due again by its age, if it weren't stopped
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) is None
        items = lines.ready(conn, agent.scope(), today=agent.clock.today(), explore=False, markets=False)
        items += lines.marketing(conn, agent.scope(), printify_links=True)
    assert not [i for i in items if i.project_id == project]


def test_the_upgrade_names_the_listing_each_earlier_check_judged(data_dir: Path) -> None:
    """Before 0.24.0 the critic judged the newest listing of the line live by then: 0080 fills that in."""
    agent, project, first, second = two_listings(data_dir)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        live = conn.execute("SELECT finished_at FROM etsy_listings WHERE listing_id = ?", (second,)).fetchone()[0]
        before = to_iso(agent.clock.now() - timedelta(days=1))
        conn.execute("UPDATE etsy_listings SET finished_at = ? WHERE listing_id = ?", (before, first))
        for made in (before, live):  # one check before the bundle was live, one after
            conn.execute(
                "INSERT INTO quality_checks (mode, session, project_id, created_at, status, score, verdict, fixes)"
                " VALUES (?, ?, ?, ?, 'ok', 5, 'improve', 'x')",
                (scope.mode, scope.session, project, made),
            )
        sql = (paths.APP_DIR / "migrations" / "0080_quality_listing.sql").read_text(encoding="utf-8")
        conn.execute(sql[sql.index("UPDATE quality_checks") : sql.index("CREATE INDEX")])
    assert [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM quality_checks ORDER BY id")] == [
        first,
        second,
    ]


# --- the journal: a work step's is a draft, and another tool's call written inside a text runs on its own ----------


def test_a_journal_written_while_working_is_kept_when_the_reflection_s_is_refused(data_dir: Path) -> None:
    draft = {"summary": "Rebuilt the cover", "entry": "Made v13 and asked for it.", "next": "Check request #56 first."}
    refused = {"summary": "s", "entry": "e", "x": 1}  # its own journal refused (live: "unknown field 'content'")
    agent, transport = make_agent(
        data_dir, [plan(), calls(("write_journal", draft)), calls(("write_journal", refused))]
    )
    assert agent.run_cycle("schedule").status == "completed"
    journal = rows(agent, "SELECT phase, status, result FROM tool_calls WHERE tool = 'write_journal' ORDER BY id")
    assert [(j["phase"], j["status"]) for j in journal] == [("act", "ok"), ("reflect", "error")]
    assert journal[0]["result"] == tools.JOURNAL_DRAFT
    assert rows(agent, "SELECT author, summary, entry, handoff FROM journal") == [
        {"author": "agent", "summary": draft["summary"], "entry": draft["entry"], "handoff": draft["next"]}
    ]
    told = transport.sent[-1]["messages"][-1]["content"][-1]["text"]
    assert prompts.JOURNAL_DRAFTED in told and prompts.JOURNAL_FIRST not in told


def test_the_reflection_writes_the_journal_first_without_a_draft() -> None:
    assert prompts.JOURNAL_FIRST in prompts.reflect_prompt("done")
    assert prompts.JOURNAL_DRAFTED in prompts.reflect_prompt("done", drafted=True)


def test_a_lesson_written_inside_the_journal_s_entry_is_appended(data_dir: Path) -> None:
    """Cycle #113: the model wrote memory_update inside write_journal's entry, its fields beside the journal's."""
    leaked = {
        "summary": "Rebuilt the tracker's first photo",
        "entry": 'Looked at both covers.</entry>\n<parameter name="next">Check request #56.</parameter>\n</invoke>\n'
        '<invoke name="memory_update">\n<parameter name="file">lessons',
        "content": "- For a first photo, show the table large enough to read.",
        "mode": "append",
    }
    agent, _ = make_agent(data_dir, [plan(), text("Done."), calls(("write_journal", leaked))])
    assert agent.run_cycle("schedule").status == "completed"
    assert rows(agent, "SELECT author, entry, handoff FROM journal") == [
        {"author": "agent", "entry": "Looked at both covers.", "handoff": "Check request #56."}
    ]
    made = rows(agent, "SELECT tool, phase, status, result FROM tool_calls ORDER BY id")
    assert [(m["tool"], m["phase"], m["status"]) for m in made[-2:]] == [
        ("write_journal", "reflect", "ok"),
        ("memory_update", "reflect", "ok"),
    ]
    assert "took the memory_update call you wrote inside entry out of it" in made[-2]["result"]
    assert "For a first photo, show the table large enough to read." in agent.memory().read("lessons")


def test_a_project_update_written_inside_a_work_step_s_journal_is_made(data_dir: Path) -> None:
    """Cycle #104's shape: project_update inside the entry, its project_id as text, its other fields beside."""
    agent, project = started(data_dir)
    ctx = shop_context(agent)
    leaked = {
        "summary": "Found the cover's maths",
        "entry": 'Checked the cover.</entry>\n<parameter name="next">Fix the cover.</parameter>\n</invoke>\n'
        f'<invoke name="project_update">\n<parameter name="project_id">{project}',
        "next_step": "Rebuild the cover with the building's numbers.",
        "note": "Cover maths checked.",
    }
    made = call(ctx, "write_journal", leaked)
    assert made.ok and made.text.startswith("Kept as your journal's draft")
    ran = "Ember's code ran the project_update call you wrote inside write_journal as a call of its own"
    assert f"{ran}: Project #{project}: updated." in made.text
    assert ctx.state.journal_draft == {
        "summary": "Found the cover's maths",
        "entry": "Checked the cover.",
        "next": "Fix the cover.",
    }  # noqa: E501
    [row] = rows(agent, f"SELECT next_step, notes FROM projects WHERE id = {project}")
    assert row["next_step"] == leaked["next_step"] and row["notes"].endswith("Cover maths checked.")


def test_an_unknown_tool_written_inside_a_text_takes_nothing_out() -> None:
    raw = {"summary": "s", "entry": 'e</invoke>\n<invoke name="nope">\n<parameter name="a">1', "content": "c"}
    taken: list[tuple[str, dict[str, Any]]] = []
    assert tools.unleaked(tools.SPECS["write_journal"], raw, [], taken) == raw and taken == []


# --- Bluesky: never the same post twice ---------------------------------------------------------------------------


def test_a_post_that_repeats_a_live_one_is_refused(data_dir: Path) -> None:
    agent, _, _ = posted(data_dir)
    ctx = post_context(agent)
    again = a_post(agent, ctx, text=f"{WORDS} #Neu")  # live, #50 was #47 with a hashtag more
    assert not again.ok and "it says what your post " in again.text and "never the same post twice" in again.text
    assert a_post(agent, ctx, text="Bewerbungstipp: Nach zwei Wochen freundlich nachfragen. #Bewerbung").ok
    assert bluesky.same_words(
        "Ein Tipp für Vermieter.\n\n🤖 Written by an AI, approved by a human.", "Ein Tipp für Vermieter!"
    )
    assert not bluesky.same_words("Ein Tipp für Vermieter.", "Ein Tipp für Mieter, ganz anders.")


# --- the workshop: a kept script gets its files again; a file the task names must be handed over ----------------------


def test_a_kept_script_run_again_without_files_gets_its_first_run_s_files(data_dir: Path) -> None:
    agent, fake = workshop_cycle(
        data_dir,
        {"script.py": b"print(1)\n", "chart.png": png()},
        {"task": "Make a price chart: chart.png.", "files": "data/prices.csv"},
        before=lambda agent: agent.roots()[0].write("data/prices.csv", "shop,price\nA,4.5\n"),
    )
    [first] = rows(agent, "SELECT script_path FROM workshop_runs")
    ids = [fake._new_file("chart.png", png(), True), fake._new_file("script.py", b"print(2)\n", True)]
    again = {"task": "Again, with the new prices.", "script": first["script_path"]}
    fake.script.extend([Plan(PLAN), ToolCalls([("workshop", again)]), workshop_answer(ids), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    second = rows(agent, "SELECT inputs FROM workshop_runs ORDER BY id")[1]
    assert json.loads(second["inputs"]) == ["data/prices.csv", first["script_path"]]
    answer = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'workshop' ORDER BY id")[1]["result"]
    assert "Handed over with the script, as its first run had them: data/prices.csv." in answer


def test_a_task_that_names_a_file_it_doesn_t_hand_over_is_refused_before_it_is_paid(data_dir: Path) -> None:
    task = "Match the look of 'shop/cover.png' (light grey, navy title): cover-v2.png, 1000 x 750 pixels."
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", {"task": task})]), Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake, before=lambda agent: agent.roots()[0].write_bytes("shop/cover.png", png()))
    answer = result(agent)
    assert answer["status"] == "error"
    assert "the task names shop/cover.png, which the run can't see: hand it over in files" in answer["result"]
    assert rows(agent, "SELECT COUNT(*) AS n FROM workshop_runs") == [{"n": 0}]
    assert not [r for r in fake.sent if r.get("tools") == [prompts.CODE_TOOL]]


# --- lessons: the one just added stays ------------------------------------------------------------------------------


def test_a_lesson_just_added_isn_t_dropped_by_its_own_call_and_the_answer_names_what_went(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[fake_plan(steps=[])]))
    new = "Before any propose_bluesky_post, call bluesky_posts first to see what is live."
    old, i = "# Lessons\n\n", 0
    while len(old.encode()) + len(new) + 12 <= memory.CAPS["lessons"]:  # full: the new lesson doesn't fit
        i += 1
        old += f"- [#c{i}] A lesson about buyers number {i}, kept for a while longer.\n"
    agent.roots()[1].write("lessons.md", old)
    mem = agent.memory()
    with agent.db.transaction() as conn:
        said = mem.update(conn, "lessons", "append", new, 1, now(agent), tuple(tools.SPECS))
    assert new in mem.read("lessons")
    assert "line(s) dropped to make room (" in said and '"A lesson about buyers number 1, kept' in said


def test_the_lessons_just_added_go_last() -> None:
    text = "# Lessons\n\n- [#c1] Buyers want A4.\n- [#c2] Use propose_bluesky_post only after bluesky_posts.\n"
    cap = len(text.encode()) - 10
    gone: list[str] = []
    fresh = {memory.lesson_key("Use propose_bluesky_post only after bluesky_posts.")}
    kept, dropped = memory._drop_oldest(text, cap, set(), tuple(tools.SPECS), fresh, gone)
    assert dropped == 1 and gone == ["- [#c1] Buyers want A4."] and "propose_bluesky_post" in kept
    before, _ = memory._drop_oldest(text, cap, set(), tuple(tools.SPECS))  # not fresh: the one naming a tool goes
    assert "Buyers want A4." in before and "propose_bluesky_post" not in before


# --- bets -----------------------------------------------------------------------------------------------------------


def test_a_bet_s_why_may_follow_its_day_without_a_colon() -> None:
    today = date(2026, 10, 6)
    assert bets.parse("+5 views by 10-14 on listing #4584899301 from the new readable first photo", today) == (
        5,
        "views",
        8,
        "on listing #4584899301 from the new readable first photo",
    )
    said = "+5 views by 2026-10-14 from the tag refresh; under 3 means a category change"
    assert bets.parse(said, today)[:3] == (5, "views", 8)


def test_an_open_bet_is_named_first_and_the_rest_of_the_update_is_made(data_dir: Path) -> None:
    agent, project = started(data_dir)
    ctx = shop_context(agent)
    assert call(ctx, "project_update", {"project_id": project, "bet": "+10 views in 7 days: the new title"}).ok
    made = call(ctx, "project_update", {"project_id": project, "note": "Rebuilt the photo.", "bet": "+5 views soon"})
    assert made.ok and "Your bet was not placed: project #" in made.text
    assert "has an open bet on views (bet #1, +10 views by " in made.text and "a new one on it waits" in made.text
    [row] = rows(agent, f"SELECT notes FROM projects WHERE id = {project}")
    assert row["notes"].endswith("Rebuilt the photo.")


# --- promises, and an owner's message given to obligation_done -------------------------------------------------------


def test_a_promise_that_repeats_an_open_one_makes_no_second_obligation(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    # The next day: the fake's cycles may have written to the owner today (its choices follow the request's bytes), and
    # two messages that answer none of theirs are a day's.
    agent.clock.advance(days=1)
    ctx = shop_context(agent)
    due = (agent.clock.today() + timedelta(days=5)).isoformat()
    report = "Report Bluesky reactions and Etsy view deltas for posts #43/#44"
    first = call(ctx, "message_owner", {"text": "Report on 10-11.", "commits": report, "due": due})
    assert first.ok and "Your promise is obligation #" in first.text
    again = "Bluesky reaction + view delta report for posts #43/#44; full view re-check"
    second = call(ctx, "message_owner", {"text": "As promised, on 10-11.", "commits": again, "due": due})
    assert second.ok and "Its promise repeats your open promise #" in second.text and "no new obligation" in second.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations WHERE kind = 'promise'") == [{"n": 1}]
    with agent.db.connection() as conn:  # another promise, or one due days apart, is one of its own
        assert (
            obligations.repeated_promise(conn, agent.scope(), "Re-check all listing views and report again", due)
            is None
        )
        later = (agent.clock.today() + timedelta(days=8)).isoformat()
        assert obligations.repeated_promise(conn, agent.scope(), report, later) is None


def test_an_owner_s_message_given_to_obligation_done_is_named_as_such(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    assert owner(agent).send_message({"text": "Pinterest still waiting for approval"}, "Stefan").status == 201
    [message] = rows(agent, "SELECT MAX(id) AS id FROM messages")
    done = {"numbers": str(message["id"]), "result": "Answered via message #1."}
    refused = call(shop_context(agent), "obligation_done", done)
    assert not refused.ok and f"#{message['id']} is your owner's message, not an obligation" in refused.text


# --- milestones Ember's code set, and channels that wait for the owner -----------------------------------------------


def test_a_milestone_ember_s_code_set_says_its_date_doesn_t_move(data_dir: Path) -> None:
    agent, project = started(data_dir)
    [bar] = rows(agent, f"SELECT * FROM milestones WHERE project_id = {project} AND created_by = 'code'")
    assert roadmap.owner_said(bar) == " · set by Ember's code (its date doesn't move)"
    with agent.db.connection() as conn:
        card = roadmap.review_text(conn, agent.scope(), agent.clock.today(), "2000-01-01T00:00:00Z")
    assert f"#{bar['id']} " in card and ", Ember's code checks and closes it, its date doesn't move)" in card
    assert "never extend one whose date doesn't move" in prompts.REVIEW_RULES


def test_the_daily_review_hears_which_channels_wait_for_the_owner(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=PINNING)
    not_set_up(agent)
    agent.clock.advance(days=1)
    since = len(fake.sent)
    agent.run_cycle("schedule")
    [sent] = [r for r in list(fake.sent)[since:] if request_kind(r) == "review"]
    card = sent["messages"][0]["content"][0]["text"]
    assert "CHANNELS NOT READY (no request or tool can use them until then)\n- Pinterest: Switched on, but it" in card
    assert "Your owner's dashboard shows it: don't ask them about it again." in card
    assert loop._waiting("Pinterest", ("ok", None), "ok") == ""  # set up: nothing waits


# --- ventures: the knock-outs come with the numbers ------------------------------------------------------------------


def test_a_venture_case_says_at_once_what_knocks_it_out(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    ctx = shop_context(agent)
    ctx.venture, ctx.net_runway_days = True, 32.0
    with agent.db.transaction() as conn:
        vid = ventures.create(
            conn, agent.scope(), title="Worksheets on eduki", pitch="p.", stage="idea", now=now(agent)
        )
    made = call(ctx, "venture_case", {**NUMBERS, "venture_id": vid, "first_sale_days": 30})
    assert made.ok and "Knocked out by Ember's code (it isn't proposed while one stands): " in made.text
    assert "a first sale after half the runway (its first sale in 30 days, half the runway is 16 days)" in made.text


# --- make_image: '#' before a region ----------------------------------------------------------------------------------


def test_a_picture_s_region_after_a_hash_is_read_as_its_region(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    ws.write_bytes("shop/cover.png", png((600, 450)))
    made = make.image(ws, "shop/photo.png", "shop/cover.png#@top", "Title")
    assert made.report[0].startswith("Made shop/photo.png")
    with pytest.raises(make.ProductError, match=r"\('file.png@top', 'file.xlsx#2@top'\)"):
        make.image(ws, "shop/photo.png", "shop/cover.docx", "Title")
