"""0.15.0, the planner package: the plan's section budgets share what they leave unused (FIX NOW 13), the steering comes
first where a cut takes the end (the day's review, a venture's FOCUS, a missed bar's obligation), the Inbox and WAITING
FOR YOUR OWNER are lists rather than windows, the release notes are read in full over the next plans (FIX NOW 24), an
ordinary step's fixed prompt holds every channel of the owner's within 0.11.1's size (X15), and one call's texts fit a
reply (X9)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from app import paths
from app.agent import context, news, review, store, tools, ventures
from app.agent.news import News
from app.economy.clock import to_iso
from tests.test_agent import make_agent, plan, rows
from tests.test_agent import tools as calls
from tests.test_owner_loop import owner
from tests.test_owner_news import first_text, section, snapshot_with

# --- FIX NOW 13: the planner's section budgets ---


def test_what_the_sections_leave_unused_goes_to_the_cut_ones_in_order() -> None:
    budgets = {"review": 100, "roadmap": 100, "journal": 100, "strategy": 300}
    long = "\n".join(f"line {i} " + "x" * 20 for i in range(40))  # about 1,200 bytes
    shown = context._allot({"review": long, "roadmap": long, "journal": "", "strategy": "short"}, budgets, 0)
    assert shown["strategy"] == "short" and shown["journal"] == ""
    sizes = {key: context.json_bytes(text) for key, text in shown.items() if text}
    assert sizes["review"] > 300 and shown["review"].endswith("bytes cut]")  # first in SPARE_ORDER: the spare room
    assert sizes["roadmap"] <= 100  # nothing was left for it
    assert sum(sizes.values()) <= sum(budgets.values())  # the plan's bound holds
    full = context._allot(dict.fromkeys(budgets, long), budgets, 0)  # every section full: the same bound
    assert sum(context.json_bytes(text) for text in full.values()) <= sum(budgets.values())


def test_the_days_review_reaches_the_plan_whole_while_other_sections_leave_room() -> None:
    # Live 0.13.0: TODAY'S REVIEW lost 1,262 bytes (its focus, lesson and advice) while 8.6 KB of budget went unused.
    snap = snapshot_with([])
    snap.review = "\n".join(f"- #{i} Project {i} [active]: continue: " + "why " * 40 for i in range(12))
    assert context.json_bytes(snap.review) > context.PLANNER_BUDGETS["review"]
    planner, _ = context.planner_context(snap, False)
    assert section(planner, "TODAY'S REVIEW") == snap.review


def review_row(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, title TEXT, status TEXT)")
    verdicts = []
    for i in range(1, 13):
        conn.execute("INSERT INTO projects VALUES (?, ?, 'active')", (i, f"Product line {i} " + "ä" * 40))
        verdicts.append({"project_id": i, "verdict": "stop" if i == 4 else "continue", "why": "w" * 160})
    return {
        "day": "2026-09-30",
        "verdicts": json.dumps(verdicts),
        "focus": "Sleep longer, about 1 cheap check a day, to hold cost near $0.30 a day.",
        "lesson": "Each extra wake cycle burns runway.",
        "ventures": "Park #4 print on demand until the digital listings show views.",
        "roadmap": "",
    }


def test_the_reviews_advice_comes_before_its_verdicts() -> None:
    # Live: '…[1262 bytes cut]', and Focus today, Lesson and Act on it never reached a plan.
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        text = review.planner_text(conn, review_row(conn))  # type: ignore[arg-type]
    kept = context.cut(text, context.PLANNER_BUDGETS["review"])
    for advice in ("Focus today: Sleep longer", "Lesson: Each extra", "Ventures: Park #4", "Act on it: "):
        assert advice in kept, advice
    assert "- #4 Product line 4" in text and ": stop: " in text  # a stop keeps its line
    assert text.endswith("- continue: #1, #2, #3, #5, #6, #7, #8, #9, #10, #11, #12")  # the rest on one line


def test_a_ventures_focus_keeps_its_knock_outs_critic_evidence_and_pitch() -> None:
    # The 0.13.0 FOCUS put the knock-outs, the evidence and the pitch after the scores, and the brief's 1,900 bytes
    # cut them once a case and a critique existed.
    row = {
        "id": 4,
        "parent_id": 1,
        "stage": "researching",
        "title": "Print on demand posters for German hobby cyclists",
        "pitch": "Minimalist route posters of famous German cycling climbs, sold as Printify posters. " * 3,
        "next_question": "Do route posters of German climbs sell on Etsy at EUR 25 or more?",
        "notes": "Parked the mug variant: the margins after shipping were under 10%.",
        "demand": "Etsy search 'radsport poster' 1,200 results; top sellers 300+ sales at EUR 19-29.",
        "economics": "Price EUR 24.90, Printify cost EUR 11.20 incl. shipping, Etsy fees EUR 2.50.",
        "setup": "Printify account (owner has it), 5 designs made with make_image, 2 hours of the owner's time.",
        "first_euro": "Two to four weeks after the first listing, if the listings get views.",
        "risks": "Map data licences (OpenStreetMap ODbL: attribution), trademarks of race names.",
        "first_test": "List 3 posters for 21 days: 10 views each by day 7 and 1 order by day 21, or stop.",
        **{score.name: 3 for score in ventures.SCORES},
        "scores_by": "research",
        "owner_action": "note",
        "owner_comment": "I like the idea, but keep the cost of samples under EUR 30.",
        "owner_at": "2026-09-29T08:00:00Z",
        "created_by": "agent",
        "created_at": "2026-09-27T08:00:00Z",
        "researched": 3,
        "research_from": "2026-09-27T08:00:00Z",
    }
    numbers = (
        "Numbers (case #7, 2026-09-29): A sale at EUR 24.90 keeps EUR 9.34 (fees EUR 3.36, cost EUR 12.20); "
        "break-even at 0.0 sales a month. A month at 0/4/12 sales nets EUR 0 / 37 / 112; expected EUR 41 a month."
    )
    critic = (
        "Critic (a separate call on case #7): test; fatal flaw: Route posters are a crowded niche with free "
        "alternatives; the case assumes 4 sales a month without a single view yet",
        "Critic's numbers: a sale keeps EUR 8.10, expected EUR 22 a month (yours: EUR 41); it would change its mind "
        "if: one listing gets 30 views and a favourite in its first week",
    )
    knocked = (
        "Knock-outs (Ember's code; it isn't proposed while one stands): slow (first sale in 2 months, more than "
        "your net runway of 18 days); cash (samples EUR 45 over your owner's EUR 30)"
    )
    evidence = "Evidence: 4 claims (1 independent, 2 marketing, 1 unchecked); the newest: #12 1,200 listings"
    text = ventures.focus_text(
        row,  # type: ignore[arg-type]
        ventures.Money(),
        2_300,
        [],
        ["ventures/4-print-on-demand.md"],
        evidence=evidence,
        numbers=numbers,
        knocked=knocked,
        critic=critic,
    )
    text = f"Decision desk: you took appraise #4: knocked out: fix its case (cash, slow)\n{text}"
    kept = context.cut(text, context.VENTURE_FOCUS_BUDGET)
    for line in ("Knock-outs (", "fatal flaw: Route posters", "Evidence: 4 claims", "Pitch: ", "Numbers (case #7"):
        assert line in kept, line
    assert kept.index("Knock-outs (") < kept.index("First test: ")  # what rules it out comes first


def test_a_ventures_focus_keeps_its_pitch_with_every_field_at_its_longest() -> None:
    # Review: with the owner's word, the first test and the next question at 220 characters, the knock-outs at 440
    # and a 300-character flaw, the 1,900-byte cut still dropped the numbers, the evidence and the pitch.
    row = {
        "id": 4,
        "parent_id": 1,
        "stage": "researching",
        "title": "T" * 80,
        "pitch": "p " * 200,
        "next_question": "q " * 200,
        "notes": "n " * 200,
        **{name: "c " * 200 for name, _, _ in ventures.CASE},
        **{score.name: 3 for score in ventures.SCORES},
        "scores_by": "research",
        "owner_action": "note",
        "owner_comment": "o " * 200,
        "owner_at": "2026-09-29T08:00:00Z",
        "created_by": "agent",
        "created_at": "2026-09-27T08:00:00Z",
        "researched": 3,
        "research_from": "2026-09-27T08:00:00Z",
    }
    text = ventures.focus_text(
        row,  # type: ignore[arg-type]
        ventures.Money(),
        2_300,
        [],
        ["ventures/4-t.md"],
        evidence="Evidence: 9 claims (3 independent, 3 marketing, 3 unchecked); the newest: " + "e " * 200,
        numbers="Numbers (case #7, 2026-09-29): " + "n " * 150,
        knocked="Knock-outs (Ember's code; it isn't proposed while one stands): " + "k " * 300,
        critic=("Critic (a separate call on case #7): test; fatal flaw: " + "f " * 150, "Critic's numbers: x"),
    )
    text = f"Decision desk: you took appraise #4: knocked out: fix its case (cash, slow)\n{text}"
    kept = context.cut(text, context.VENTURE_FOCUS_BUDGET)
    for line in ("Owner: ", "Knock-outs (", "fatal flaw: f", "First test: ", "Numbers (case", "Evidence: 9", "Pitch: "):
        assert line in kept, line
    assert (
        context.json_bytes(text[: text.index("\nKnowledge file")]) < context.VENTURE_FOCUS_BUDGET - 50
    )  # with room to spare


def test_a_missed_bars_action_survives_the_obligation_line(data_dir: Path) -> None:
    # 0.13.0: "milestone #11 '…' was missed (Ember's code checked it…: views_total 0 views…, target a…": the action
    # came after the numbers and the line's 160 characters.
    pytest.importorskip("httpx2")
    from app.agent import obligations
    from tests.test_listing_gates import close, keep, started

    agent, project = started(data_dir)
    close(agent, "day7_views", "missed")
    keep(agent)
    with agent.db.connection() as conn:
        shown = obligations.text(conn, agent.scope(), agent.clock.today())
    # 0.33.0: day 7's action is a push to bring buyers
    assert f"project #{project}: bring buyers to its listings with pins, posts or a blog post: unseen listings" in shown


# --- FIX NOW 24: the Inbox, WAITING FOR YOUR OWNER and the release notes ---


def test_an_old_message_can_be_read_checked_and_removed(data_dir: Path) -> None:
    # 0.13.0: only the newest 30 messages were shown; an older one (a pasted password) had no Remove button.
    agent, _ = make_agent(data_dir, [])
    who = owner(agent)
    secret = who.send_message({"text": "my password is hunter2"}, "Stefan").body["id"]
    answered = who.send_message({"text": "an old question"}, "Stefan").body["id"]
    for n in range(33):
        assert who.send_message({"text": f"message {n}"}, "Stefan").status == 201
    with agent.db.transaction() as conn:  # answered, all but the first
        conn.execute("UPDATE messages SET answered_by = ? WHERE id <> ?", (secret, secret))
    board = agent.dashboard()
    shown = [m["id"] for m in board["inbox"]]
    assert len(shown) == 31 and secret in shown  # the newest 30, and the unanswered one however old
    assert answered not in shown and board["inbox_before"] == min(m for m in shown if m != secret)
    older = agent.inbox_page(board["inbox_before"], 30)
    assert [m["id"] for m in older["messages"]] == [5, 4, 3, answered, secret] and older["before"] is None
    assert who.remove_message(secret, "Stefan").status == 200
    assert secret not in [m["id"] for m in agent.dashboard()["inbox"]]  # removed: on its older page again
    assert next(m for m in agent.inbox_page(board["inbox_before"], 30)["messages"] if m["id"] == secret)["removed"]


def test_older_messages_page_through_the_web(ingress_client: Any) -> None:
    from tests.test_owner_api import post

    for n in range(35):
        assert post(ingress_client, "api/inbox", {"text": f"message {n}"}).status_code == 201
    board = ingress_client.get("api/dashboard").json()
    assert board["inbox_before"] is not None
    older = ingress_client.get(f"api/inbox?limit=30&before={board['inbox_before']}").json()
    assert older["messages"] and all(m["id"] < board["inbox_before"] for m in older["messages"])
    assert ingress_client.get("api/inbox").status_code == 422  # before= is required
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert '"api/inbox?limit=" + INBOX_PAGE + "&before="' in script and "Show older messages" in script


def test_every_new_upgrade_request_is_listed(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    now = to_iso(agent.clock.now())
    fields = {"problem": "p", "proposed_change": "c", "expected_benefit": "b", "priority": "low"}
    with agent.db.transaction() as conn:
        for n in range(35):
            store.insert_upgrade(conn, agent.scope(), 1, now, title=f"#{n}", **fields)
        conn.execute("UPDATE upgrades SET status = 'declined' WHERE id > 3")
    listed = [u["id"] for u in agent.dashboard()["upgrades"]]
    assert listed[:30] == list(range(35, 5, -1)) and listed[30:] == [3, 2, 1]


def test_waiting_for_your_owner_lists_every_pending_request(data_dir: Path) -> None:
    # 0.13.0: the plan looked for pending requests among the newest 20 only, and said "None." with 3 waiting.
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600)] * 2)
    agent.run_cycle("schedule")
    now = to_iso(agent.clock.now())
    fields = {"type": "other", "description": "d", "expected_cost": "none", "expected_benefit": "b"}
    with agent.db.transaction() as conn:
        for n in range(35):
            store.insert_approval(conn, agent.scope(), 1, now, title=f"Request {n}", payload=f"p{n}", **fields)
        conn.execute("UPDATE approvals SET status = 'rejected', decided_at = ? WHERE id > 3", (now,))
    agent.run_cycle("schedule")
    waiting = section(first_text(transport.sent[-1]), "WAITING FOR YOUR OWNER") or ""
    assert [line.split(" ")[0] for line in waiting.split("\n") if line.startswith("#")] == ["#1", "#2", "#3"]


def test_the_release_notes_are_read_in_full_over_the_next_plans(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.12.0 -> 0.13.0: the plan saw 1,937 of 8,456 characters, and the version was then marked read.
    path = tmp_path / "CHANGELOG.md"
    lines = [f"- Change {i}: Ember's code now does a thing it didn't do before, in more words." for i in range(100)]
    path.write_text("## 0.4.0\n\n" + "\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(paths, "CHANGELOG_PATH", path)
    monkeypatch.setattr("app.agent.loop.app_version", lambda: "0.4.0")
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600)] * 8)
    agent.db.set_meta(news.changelog_key("dry_run"), "0.3.0")
    parts = []
    while agent.db.get_meta(news.changelog_key("dry_run")) == "0.3.0":
        assert len(parts) < 7
        agent.run_cycle("schedule")
        part = section(first_text(transport.sent[-1]), "YOUR SOFTWARE") or ""
        assert part and context.json_bytes(part) <= news.CHANGELOG_LIMIT
        parts.append(part)
    assert len(parts) >= 4 and parts[0].startswith("Your software was upgraded from 0.3.0 to 0.4.0")
    assert all(p.startswith(news.CONTINUED) and p for p in parts[1:])
    assert all(p.endswith(news.MORE) for p in parts[:-1]) and not parts[-1].endswith(news.MORE)
    read = "\n".join(p.removeprefix(news.CONTINUED).removesuffix(news.MORE) for p in parts)
    assert [line for line in lines if line in read] == lines  # every line, once and in order
    agent.run_cycle("schedule")
    assert "YOUR SOFTWARE" not in first_text(transport.sent[-1])


def test_a_part_of_the_notes_that_isnt_shown_whole_comes_again() -> None:
    notes = "Your software was upgraded.\n\n## 0.4.0\n" + "\n".join(f"- Change {i}." for i in range(400))
    first, after = news.changelog_part(notes)
    assert after is not None and first.endswith(news.MORE)
    second, _ = news.changelog_part(notes, after)
    assert second.startswith(news.CONTINUED) and first.removesuffix(news.MORE).split("\n")[-1] not in second
    assert news.changelog_part(notes, len(notes) + 5)[0] == first  # the notes changed: from the start again
    snap = snapshot_with([])
    snap.news = News(changelog=second, running_version="0.4.0", changelog_next=after)
    assert not context.planner_context(snap, False, 0.5)[1].changelog  # cut: not read, shown again


# --- X15: an ordinary step's fixed prompt with every channel on ---


def test_scores_and_the_business_case_are_a_venture_cycles(data_dir: Path) -> None:
    ordinary = {d["name"]: d for d in tools.definitions(etsy=True)}["venture_update"]["input_schema"]["properties"]
    venture = {d["name"]: d for d in tools.definitions(etsy=True, venture=True)}["venture_update"]["input_schema"]
    assert set(ordinary) == set(tools.ORDINARY_VENTURE_FIELDS)
    assert {"revenue", "demand", "first_test"} <= set(venture["properties"])
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["score it"]),
            calls(("venture_update", {"venture_id": 1, "revenue": 3})),
            calls(("write_journal", {"summary": "Tried", "entry": "Refused."})),
        ],
    )
    agent.run_cycle("schedule")
    [refused] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'venture_update'")
    assert refused["status"] == "error"
    assert "revenue is set in a venture cycle; here venture_update takes venture_id, learned" in refused["result"]
    # The owner's news of an added idea, which any cycle can see, no longer asks for a score.
    added = {
        "id": 9,
        "title": "Grant finder",
        "pitch": "Grants.",
        "owner_action": "added",
        "parent_id": None,
        "owner_comment": "",
    }
    assert ventures.news_line(added).endswith('"Grants.". Research it; a venture cycle scores it.')


# --- X9: one call's texts fit a reply ---


def test_a_call_whose_texts_dont_fit_a_reply_is_refused_with_their_lengths() -> None:
    # FIX NOW 32 of the first analysis: each field fit, but request_approval's together could reach 5,240 characters.
    raw = {
        "type": "other",
        "title": "A long request",
        "description": "d" * 2_000,
        "payload": "p" * 2_000,
        "expected_cost": "none",
        "expected_benefit": "b" * 200,
    }
    with pytest.raises(tools.ToolError) as refused:
        tools.validate(tools.SPECS["request_approval"], raw)
    said = str(refused.value)
    assert "texts total 4,223 characters (description 2,000, payload 2,000, expected_benefit 200)" in said
    assert f"at most {tools.CALL_CHARS:,}" in said
    raw["payload"] = "p" * 700
    assert tools.validate(tools.SPECS["request_approval"], raw)["payload"] == "p" * 700  # one that fits passes
    # What a reply holds (1.7 characters a token) keeps room for the call's JSON and a line of text.
    assert tools.ONE_REPLY_CHARS < tools.CALL_CHARS <= tools.WORK_MAX_TOKENS * 1.7 - 400


def test_the_cycle_rules_state_the_call_limit() -> None:
    from app.agent import prompts

    assert f"{tools.CALL_CHARS:,} characters of text in one call" in " ".join(prompts.operating_rules(True).split())
