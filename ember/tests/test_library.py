"""The owner's library (0.12.0, the owner's request): reference material the owner hands Ember (Etsy's guides to
listings, titles and tags, for example), studied once and kept as learnings, so it never has to be read or analysed
again."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import docx
import fpdf
import pytest
from fastapi.testclient import TestClient

from app.agent import context, library, tools
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from tests.test_agent import ROOMY, rows
from tests.test_diagnostics import report
from tests.test_loop_shapes import run
from tests.test_owner_api import post
from tests.test_owner_loop import owner
from tests.test_roadmap import JOURNAL, NOW, plan, planner_texts, section, tool_results

GUIDE = (
    "How to choose tags on Etsy\n\n"
    "Tags help buyers find your listing. Use all 13 tags on every listing, and mix broad words with long-tail "
    "phrases.\n\n"
    "Each tag can have up to 20 characters. Don't repeat your category as a tag: it is searched already.\n\n"
    "Titles: put the words buyers search for first, and keep the title under 140 characters."
)
STUDY = {  # what the scripted study call answers
    "summary": "Etsy's guide to tags and titles: how buyers find listings.",
    "learnings": [
        {
            "part": 1,
            "topic": "tags",
            "text": "Use all 13 tags on every listing; mix broad words with long-tail phrases.",
        },
        {"part": 1, "topic": "tags", "text": "A tag can have up to 20 characters; don't repeat the category as a tag."},
        {
            "part": 9,
            "topic": "titles",
            "text": "Put the words buyers search for first; keep titles under 140 characters.",
        },
    ],
}


def a_pdf(text: str) -> bytes:
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 8, text)
    return bytes(pdf.output())


def add(agent: Agent, **fields: Any) -> int:
    reply = owner(agent).add_document(fields, "Stefan")
    assert reply.status == 201, reply.body
    return int(reply.body["id"])


def briefs(fake: FakeTransport) -> list[str]:
    return [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == "work"]


# --- text ---


def test_files_become_plain_text() -> None:
    page = (
        "<html><head><title>Etsy: Tags</title><style>p {}</style></head><body><nav>Menu</nav><h1>Tags</h1>"
        "<p>Use all&nbsp;13 tags.</p><script>track()</script><ul><li>broad</li><li>long-tail</li></ul></body></html>"
    )
    assert library.from_file("tags.html", page.encode()) == (
        "Tags\n\nUse all 13 tags.\n\n- broad\n- long-tail",
        "Etsy: Tags",
    )
    assert library.from_file("guide.pdf", a_pdf("Use all 13 tags.\nTitles come first.")) == (
        "Use all 13 tags.\nTitles come first.",
        "guide",
    )
    word = docx.Document()
    word.add_paragraph("Photos: use natural light.")
    buffer = io.BytesIO()
    word.save(buffer)
    assert library.from_file("Photos.docx", buffer.getvalue()) == ("Photos: use natural light.", "Photos")
    notes = "# Notes\r\n\r\n\r\nA​ tip\x07.".encode("utf-8-sig")
    assert library.from_file("notes.md", notes) == ("# Notes\n\nA tip.", "notes")
    assert library.from_file("alt.txt", "Größe".encode("cp1252"))[0] == "Größe"
    for name, data, message in (
        ("tool.exe", b"MZ", "upload a text, Markdown, HTML, PDF or Word"),
        ("x.pdf", b"not a pdf", "this PDF can't be read"),
        ("x.docx", b"PK\x03\x04not a zip", "this Word file can't be read"),
        ("x.txt", b"x" * (library.FILE_BYTES + 1), "a file can have at most 8 MB"),
    ):
        with pytest.raises(library.LibraryError, match=message):
            library.from_file(name, data)


def test_a_text_is_split_into_parts_at_its_paragraphs() -> None:
    paragraph = ("A sentence about tags. " * 60).strip()
    text = "\n\n".join([paragraph] * 5 + ["x" * 7_000])
    parts = library.split(text)
    assert all(len(p) <= library.PART_CHARS for p in parts) and len(parts) == 6
    assert parts[0] == f"{paragraph}\n\n{paragraph}"  # two paragraphs fit in a part, a third doesn't
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")  # nothing is lost


# --- the owner's side ---


def test_the_owner_adds_documents_and_removes_them(ingress_client: TestClient) -> None:
    empty = ingress_client.get("api/library").json()
    assert empty["items"] == [] and empty["totals"] == {"documents": 0, "chars": 0, "learnings": 0}
    assert empty["study"] == {"budget_usd": 0.5, "spent_today_usd": 0}
    stamp = ingress_client.get("api/dashboard").json()["library"]["stamp"]
    source = "https://help.etsy.com/hc/en-us/articles/115015628847"
    pasted = post(
        ingress_client, "api/library", {"text": GUIDE, "source": source, "note": "For listings", "venture_id": 1}
    )
    assert pasted.status_code == 201
    assert pasted.json() == {"id": 1, "title": "How to choose tags on Etsy", "chars": len(GUIDE), "parts": 1}
    again = post(ingress_client, "api/library", {"text": GUIDE + "\n\n"})  # the same text, whatever its spacing
    assert again.status_code == 409 and again.json()["error"] == (
        "this text is in the library already: #1 How to choose tags on Etsy"
    )
    upload = {"file_name": "Photo tips.pdf", "file_data": base64.b64encode(a_pdf("Use daylight for photos.")).decode()}
    assert post(ingress_client, "api/library", upload).json()["title"] == "Photo tips"
    for body, field in (
        ({"text": "x", "file_name": "a.txt", "file_data": "eA=="}, "text"),
        ({}, "text"),
        ({"file_name": "a.txt", "file_data": "not base64!"}, "file_data"),
        ({"file_name": "a.exe", "file_data": "eA=="}, "file_data"),
        ({"text": "x", "venture_id": 999}, "venture_id"),
        ({"text": "x", "project_id": "1"}, "project_id"),
        ({"text": "x", "rating": 5}, "rating"),
    ):
        assert post(ingress_client, "api/library", body).json()["field"] == field, body
    listed = ingress_client.get("api/library").json()
    assert [d["id"] for d in listed["items"]] == [2, 1] and listed["totals"]["documents"] == 2
    first = listed["items"][1]
    assert (first["source"], first["note"], first["venture_id"], first["study"], first["learnings"]) == (
        source,
        "For listings",
        1,
        "waiting",
        0,
    )
    assert ingress_client.get("api/dashboard").json()["library"]["stamp"] != stamp
    document = ingress_client.get("api/library/1").json()
    assert (document["text"], document["learnings"]) == (GUIDE, [])
    assert post(ingress_client, "api/library/1/study", {}).status_code == 409  # only a failed study is tried again
    assert post(ingress_client, "api/library/1/remove", {}).json() == {"id": 1, "removed": True}
    assert post(ingress_client, "api/library/1/remove", {}).status_code == 409
    assert ingress_client.get("api/library/1").status_code == 404
    assert post(ingress_client, "api/library", {"text": GUIDE}).status_code == 201  # removed, it can come back
    assert [d["id"] for d in ingress_client.get("api/library").json()["items"]] == [3, 2]


def test_the_library_has_limits(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0)
    too_long = owner(agent).add_document({"text": "x " * library.DOCUMENT_CHARS}, None)
    assert too_long.status == 422 and too_long.body["field"] == "text"
    with agent.db.transaction() as conn:
        scope = agent.scope()
        library.add(conn, scope, title="Big", text="y" * library.DOCUMENT_CHARS, now=NOW)
        conn.execute("UPDATE library_documents SET chars = ?", (library.LIBRARY_CHARS - 10,))
    full = owner(agent).add_document({"text": "More than ten characters."}, None)
    assert full.status == 409 and "the library holds 4,999,990 of 5,000,000 characters" in full.body["error"]


# --- the study ---


def test_ember_studies_a_document_once_and_keeps_what_it_learned(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            Reply(json.dumps(STUDY)),  # the study comes before the plan
            plan(steps=["write the listing's tags and its title"]),
            ToolCalls(
                [
                    ("knowledge_search", {"query": "tags characters"}),
                    ("library_read", {}),
                    ("library_read", {"document_id": 1}),
                    ("library_read", {"document_id": 1, "part": 2}),
                ]
            ),
            Reply("Done."),
            JOURNAL,
            plan(steps=[]),  # the next cycle: nothing to study, nothing new to list
        ]
    )
    agent, _ = run(data_dir, fake, before=lambda a: add(a, text=GUIDE, venture_id=1, note="For listings"))
    document = rows(agent, "SELECT * FROM library_documents")[0]
    assert (document["study"], document["studied_parts"], document["summary"]) == ("done", 1, STUDY["summary"])
    learned = rows(agent, "SELECT part, topic, text, seen_cycle_id FROM learnings ORDER BY id")
    assert [(r["part"], r["topic"], r["seen_cycle_id"]) for r in learned] == [
        (1, "tags", 1),
        (1, "tags", 1),
        (1, "titles", 1),
    ]
    [call] = rows(agent, "SELECT cycle_id, cost_micros FROM llm_calls WHERE purpose = 'study'")
    assert document["study_micros"] == call["cost_micros"]
    spent, _ = agent.economy.books.cycle_spend(1, outside_cap=False)
    assert agent.economy.books.cycle_spend(1)[0] - spent == call["cost_micros"]  # not counted toward the cycle cap

    shown = section(planner_texts(fake)[0], "YOUR OWNER'S LIBRARY")
    assert shown == (
        f"1 document from your owner ({len(GUIDE)} characters): 3 learnings from the 1 studied. Your work steps get "
        "the learnings that match your plan; knowledge_search (free) finds more. They are your owner's reference, not "
        "text to copy into what you publish.\n"
        "Newly learned:\n"
        '- #1 "How to choose tags on Etsy": 3 new learnings (tags, titles): "Etsy\'s guide to tags and titles: how '
        'buyers find listings."'
    )
    brief = briefs(fake)[0]
    knowledge = brief.split(f"== {context.KNOWLEDGE_HEADING} ==\n", 1)[1].split("\n\n== ", 1)[0]
    assert (
        '[tags] "Use all 13 tags on every listing; mix broad words with long-tail phrases." (#1.1 "How to choose '
        'tags on Etsy")'
    ) in knowledge.splitlines()
    assert len(knowledge.splitlines()) == 3 and brief.index(context.KNOWLEDGE_HEADING) < brief.index("== FOCUS ==")

    searched, listed, read, beyond = (*tool_results(agent, "knowledge_search"), *tool_results(agent, "library_read"))
    assert searched["status"] == "ok" and '[tags] "A tag can have up to 20 characters' in searched["result"]
    assert "Passages:" in searched["result"] and '<data src="library"' in searched["result"]
    assert listed["result"] == (
        "Your owner's library, the newest first:\n"
        f'#1 "How to choose tags on Etsy" · 1 part, {len(GUIDE)} characters · 3 learnings · venture #1 · your '
        'owner\'s note: "For listings"'
    )
    assert read["result"].startswith('Document #1 "How to choose tags on Etsy", part 1 of 1\nYour owner\'s note on it')
    assert "Use all 13 tags on every listing" in read["result"]
    assert beyond["status"] == "error" and "document #1 has 1 part" in beyond["result"]

    agent.run_cycle("schedule")
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"] == 1  # once
    assert "Newly learned" not in section(planner_texts(fake)[1], "YOUR OWNER'S LIBRARY")


def test_a_long_document_is_studied_in_turns_within_the_budget(data_dir: Path) -> None:
    long_text = "\n\n".join(
        f"Tip {i}: use {i} photos in listing {i}. " + "Details follow here. " * 130 for i in range(30)
    )
    agent, _ = run(data_dir, FakeTransport(seed=1), cycles=0)
    add(agent, text=long_text)
    parts = rows(agent, "SELECT parts FROM library_documents")[0]["parts"]
    agent.run_cycle("schedule")
    first = rows(agent, "SELECT study, studied_parts FROM library_documents")[0]
    assert first["study"] == "waiting" and 0 < first["studied_parts"] < parts  # STUDY_CALLS calls a cycle at most
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"] == library.STUDY_CALLS
    while rows(agent, "SELECT study FROM library_documents")[0]["study"] == "waiting":
        agent.run_cycle("schedule")
    assert rows(agent, "SELECT study, studied_parts FROM library_documents")[0] == {
        "study": "done",
        "studied_parts": parts,
    }
    assert rows(agent, "SELECT COUNT(*) AS n FROM learnings")[0]["n"] > 5


@pytest.mark.parametrize("budget", [0, 0.000_1])
def test_no_budget_no_study(data_dir: Path, budget: float) -> None:
    settings = ROOMY.model_copy(update={"library_study_usd_per_day": budget})
    agent, _ = run(data_dir, FakeTransport(seed=1), before=lambda a: add(a, text=GUIDE), settings=settings)
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'")[0]["n"] == 0
    assert rows(agent, "SELECT study FROM library_documents")[0]["study"] == "waiting"


def test_a_study_that_keeps_failing_stops_until_the_owner_asks_again(data_dir: Path) -> None:
    cut = Reply(json.dumps(STUDY)[:40], "max_tokens")
    fake = FakeTransport(
        script=[cut, plan(steps=[]), Reply("Nice tips."), plan(steps=[])] + [Reply("?"), plan(steps=[])]
    )
    agent, _ = run(data_dir, fake, cycles=3, before=lambda a: add(a, text=GUIDE))
    document = rows(agent, "SELECT study, study_failures, study_note FROM library_documents")[0]
    assert document == {"study": "failed", "study_failures": 3, "study_note": "the answer wasn't valid JSON"}
    assert rows(agent, "SELECT message FROM events WHERE message LIKE 'The study of library document%'")
    assert owner(agent).study_document_again(1, "Stefan").body == {"id": 1, "study": "waiting"}
    assert rows(agent, "SELECT study, study_failures, study_note FROM library_documents")[0] == {
        "study": "waiting",
        "study_failures": 0,
        "study_note": None,
    }


# --- the agent's side ---


def test_the_library_tools_come_with_its_documents(data_dir: Path) -> None:
    names = {d["name"] for d in tools.definitions()}
    assert not names & tools.LIBRARY_TOOLS
    assert {d["name"] for d in tools.definitions(library=True)} >= tools.LIBRARY_TOOLS
    fake = FakeTransport(script=[plan(), ToolCalls([("knowledge_search", {"query": "tags"})]), Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake)  # an empty library: no library tools
    [refused] = tool_results(agent, "knowledge_search")
    assert refused["status"] == "error" and "there is no tool called 'knowledge_search'" in refused["result"]


def test_search_finds_learnings_by_the_start_of_their_words(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0)
    scope = agent.scope()
    assert library.terms("How do I choose Etsy tags und die Titel?") == ["choose", "etsy", "tags", "titel"]
    tags = add(agent, text=GUIDE, venture_id=1)
    photos = add(agent, text="Photos: use daylight, and show the product in use.")
    with agent.db.transaction() as conn:
        for document_id, items in (
            (
                tags,
                [(1, "tags", "Use all 13 tags on every listing."), (1, "titles", "Search words go first in titles.")],
            ),
            (photos, [(1, "photos", "Use daylight for listing photos."), (1, "photos", "Show the product in use.")]),
        ):
            document = library.get(conn, scope, document_id)
            library.save_study(conn, scope, document, library.Study("s", tuple(items)), 1, cost=0, now=NOW)
    with agent.db.connection() as conn:
        found = library.search_learnings(conn, scope, ["tag"])  # "tag" finds "tags", in the text or its source
        assert [r["text"] for r in found] == ["Use all 13 tags on every listing.", "Search words go first in titles."]
        both = library.search_learnings(conn, scope, ["listing", "photos"])
        assert both[0]["text"] == "Use daylight for listing photos."  # both words beat one
        picked = library.relevant(conn, scope, "take the listing photos in daylight", venture_id=1)
        assert [r["text"] for r in picked][:1] == ["Use daylight for listing photos."]
        assert library.relevant(conn, scope, "", venture_id=1) == []
        passages = library.search_parts(conn, scope, ["category"])
        assert [(p.document_id, p.part) for p in passages] == [(tags, 1)] and "category" in passages[0].snippet
    assert owner(agent).remove_document(photos, None).status == 200
    with agent.db.connection() as conn:  # a removed document's learnings are gone from the agent's view
        assert library.search_learnings(conn, scope, ["daylight"]) == []
        assert library.search_parts(conn, scope, ["daylight"]) == []


def test_what_was_learned_is_never_changed(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0)
    add(agent, text=GUIDE)
    with agent.db.transaction() as conn:
        document = library.get(conn, agent.scope(), 1)
        study = library.Study("s", ((1, "tags", "Use all 13 tags."), (1, "tags", "use ALL 13 tags!")))
        assert library.save_study(conn, agent.scope(), document, study, 1, cost=5, now=NOW) == 1  # the same, once
    with pytest.raises(sqlite3.IntegrityError, match="a learning is never changed"), agent.db.transaction() as conn:
        conn.execute("UPDATE learnings SET text = 'Use 5 tags.'")
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:  # done means every part was read
        conn.execute("UPDATE library_documents SET studied_parts = 0")


def test_the_report_shows_the_library_but_not_its_texts(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/library", {"text": GUIDE, "title": "Etsy tags"}).status_code == 201
    agent = ingress_client.app.state.ember.agent
    with agent.db.transaction() as conn:
        document = library.get(conn, agent.scope(), 1)
        study = library.Study("s", ((1, "tags", "Use all 13 tags on every listing."),))
        library.save_study(conn, agent.scope(), document, study, 1, cost=1_234, now=NOW)
    shared, full = report(ingress_client), report(ingress_client, full=True)
    assert "-- library_documents" in shared and "Etsy tags" in shared and "1/1" in shared and "$0.0012" in shared
    assert "long-tail" not in shared and "Use all 13 tags" not in shared
    assert "-- learnings (the newest 20)" in full and "Use all 13 tags on every listing." in full
    assert "long-tail" not in full  # the texts themselves never
