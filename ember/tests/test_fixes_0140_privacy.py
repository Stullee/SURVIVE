"""0.15.0: the shareable report and the sensor keep private things private, building the report changes nothing, the
report shows whether observations run, and old texts are pruned.

- The shareable report printed the Library's study replies and library_read results; masking senders' names overwrote
  ordinary words ("Pinterest", "Fiverr", "mailbox") and defeated the address mask; the open sensor carried the daily
  digest, which could name the address of a person who asked to stop; agenda texts kept cut subjects, opt-out reasons
  were quoted and the owner's Home Assistant name and user ID appeared throughout.
- Building the report ran Ember's code's keepers and marked agenda events as having woken the agent.
- observations wasn't in the report at all.
- Only the event log was pruned: tool calls' inputs and results and the model's replies grew without limit.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import db as dbmod  # noqa: E402
from app import diagnostics, events, privacy  # noqa: E402
from app.agent import library, obligations, policy, scheduler  # noqa: E402
from app.agent.context import RESEARCH_CALLS  # noqa: E402
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls  # noqa: E402
from app.agent.scheduler import Scheduler  # noqa: E402
from app.agent.service import Agent, Decision  # noqa: E402
from app.agent.tools import RESEARCH_REPEAT_DAYS  # noqa: E402
from app.config import LoadedSettings  # noqa: E402
from app.economy.clock import from_iso, to_iso  # noqa: E402
from app.integrations import mailstore  # noqa: E402
from app.privacy import Masker  # noqa: E402
from app.state import AppState  # noqa: E402
from tests.test_agent import ROOMY, make_agent, reply, rows, text  # noqa: E402
from tests.test_agent import plan as scripted_plan  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_executor import REPLY  # noqa: E402
from tests.test_inquiries import inbound  # noqa: E402
from tests.test_library import add  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_mail import JOURNAL, READER, mail_cycle  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402
from tests.test_roadmap import JOURNAL as FAKE_JOURNAL  # noqa: E402
from tests.test_roadmap import plan  # noqa: E402

OWNER_ID = "8f14e45fceea167a5a36dedd4bea2543"  # Home Assistant user IDs are 32 hex digits
LABEL = f"Felix Beispiel ({OWNER_ID})"  # how web._owner labels the owner's actions


def state_of(agent: Agent) -> AppState:
    return AppState(loaded=LoadedSettings(agent.settings), db=agent.db, economy=agent.economy, agent=agent)


def shared_and_full(agent: Agent) -> tuple[str, str]:
    state = state_of(agent)
    return diagnostics.report(state), diagnostics.report(state, full=True)


def section(report: str, title: str) -> str:
    return report.split(f"\n## {title}", 1)[1].split("\n## ", 1)[0]


# --- FIX 15a: the Library's texts ---

NOTE = "My landlord at Musterweg 12 pays 3,100 EUR; I quit in March."
STUDIED = {
    "summary": "The owner's own notes: Musterweg 12 and the rent.",
    "learnings": [{"part": 1, "topic": "private", "text": "The owner lives at Musterweg 12."}],
}


def test_the_shareable_report_leaves_out_the_library_s_texts(data_dir: Path) -> None:
    """The live 0.13.0 report printed the study replies and library_read results whole."""
    fake = FakeTransport(
        script=[
            Reply(json.dumps(STUDIED)),  # the study comes before the plan
            plan(steps=["read my owner's notes"]),
            ToolCalls(
                [
                    ("knowledge_search", {"query": "landlord"}),
                    ("library_read", {}),
                    ("library_read", {"document_id": 1}),
                ]
            ),
            Reply("Done."),
            FAKE_JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, before=lambda a: add(a, text=NOTE, title="Notes", note="Private"))
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'study'") == [{"n": 1}]
    shared, full = shared_and_full(agent)
    assert "Musterweg" in full and "quit in March" in full  # the owner's own eyes
    assert "Musterweg" not in shared and "quit in March" not in shared and "3,100" not in shared
    cycles = section(shared, diagnostics.CYCLES_TITLE)
    assert "(study): [" in cycles and "characters from your library left out]" in cycles
    tool_rows = {line.split(" | ")[4] for line in cycles.splitlines() if line[:1].isdigit()}
    assert {"knowledge_search", "library_read"} <= tool_rows  # the calls stay, their results as lengths


def test_the_planner_preview_leaves_out_what_the_study_learned(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    add(agent, text=NOTE, title="Notes")
    with agent.db.transaction() as conn:
        document = library.get(conn, agent.scope(), 1)
        study = library.Study(STUDIED["summary"], ((1, "private", "The owner lives at Musterweg 12."),))
        library.save_study(conn, agent.scope(), document, study, 1, cost=1_000, now=to_iso(agent.clock.now()))
    shared, full = shared_and_full(agent)
    assert "Musterweg" in section(full, diagnostics.PLANNER_TITLE)
    preview = section(shared, diagnostics.PLANNER_TITLE)
    assert "Musterweg" not in shared and "1 document from your owner" in preview
    assert "(1 newly studied document: what it taught is left out)" in preview


# --- FIX 15b: senders' names ---


def test_only_whole_person_like_names_are_masked_and_never_inside_an_address() -> None:
    """The live 0.13.0 report: three senders, none a person, replaced about 170 ordinary words each time."""
    others = {
        "Hans Beispielmann": "[sender of email #3]",
        "Frage zu Ihrem Planer für Mieter": "[subject of email #3]",
        "Anfrage von hans@beispiel.de zum Planer": "[subject of email #4]",
    }
    masker = Masker(own_address="emberthehelper@mailbox.org", others=others)
    assert masker("Buyer wrote from john.doe@mailbox.org about it.") == "Buyer wrote from [email 1] about it."
    assert masker("emberthehelper@mailbox.org, imap.mailbox.org") == "[Ember's address], imap.mailbox.org"
    assert masker("Hans Beispielmann wrote: Frage zu Ihrem Planer für Mieter") == (
        "[sender of email #3] wrote: [subject of email #3]"
    )
    assert masker("Hans wrote; Beispielmann-Verlag") == "Hans wrote; Beispielmann-Verlag"  # whole names only
    assert masker("Re: Anfrage von hans@beispiel.de zum Planer") == "Re: [subject of email #4]"
    assert masker("Confirmed category 2078 via etsy_categories") == "Confirmed category 2078 via etsy_categories"


@pytest.mark.parametrize(
    ("name", "address", "masked"),
    [
        ("mailbox", "noreply@mailbox.org", False),  # Ember's own provider (the live report)
        ("Pinterest", "noreply@pinterest.com", False),  # a single word: a brand or a common word
        ("Fiverr", "noreply@fiverr.com", False),
        ("Etsy Support", "support@etsy.com", False),  # a company
        ("The Printify Team", "hello@printify.com", False),
        ("mailbox.org Team", "service@mailbox.org", False),
        ("Etsy No Reply", "no-reply@etsy.com", False),
        ("Etsy Transactions", "transaction@etsy.com", False),  # a word of the sender's own domain: a brand
        ("Google Workspace", "workspace-noreply@google.com", False),
        ("Canva Pro", "no-reply@canva.com", False),
        ("Hans Beispielmann", "hans@example.org", True),
        ("Dr. Hans Müller", "hm@example.org", True),
        ("Anne-Marie O'Neil", "am@example.org", True),
        ("Lena Hoffmann", READER, True),
        ("Max Mustermann", "max@mustermann.de", True),  # a person's own domain: their mailbox is no role's
        ("Hans Hans", "x@hans.dev", True),
    ],
)
def test_which_senders_names_are_masked(name: str, address: str, masked: bool, data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, from_addr, from_name, to_addr,"
            " subject, received_at, body) VALUES (?, ?, ?, 'in', 1, 1, ?, ?, 'ember@example.invalid', 'Hi', ?, 'x')",
            (scope.mode, scope.session, scope.life_id, address, name, to_iso(agent.clock.now())),
        )
        others = diagnostics._others(conn)
    assert (name in others) is masked


def test_a_name_made_of_ember_s_own_domain_is_not_a_person_s() -> None:
    assert privacy.person_like("Posteo Postmaster")
    assert not privacy.person_like("Posteo Postmaster", "ember@posteo.de")
    assert not privacy.person_like("Hans")  # one word: it would overwrite that word wherever it is written


# --- FIX 15c: the open sensor ---


def test_the_open_sensor_carries_counts_and_no_one_s_address(data_dir: Path) -> None:
    """The digest named the address of a person who asked to stop, and /api/sensors needs no user."""
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "email_reply", "level": "veto_window"}, "Stefan").status == 200
    aimed = json.dumps(
        {"assessment": "A reader asked.", "goal": "Answer", "steps": ["answer"], "focus_milestone_id": goal}
    )
    transport.outcomes.extend(
        [reply([{"type": "text", "text": aimed}], "end_turn"), calls(("propose_email", REPLY)), text("Ok."), JOURNAL]
    )
    assert agent.run_cycle("schedule").status == "completed"
    agent.clock.advance(hours=policy.VETO_HOURS)  # 0.22.0: an email reply runs at most after its veto window
    agent.run_policy()
    [made] = rows(agent, "SELECT id FROM approvals")
    assert agent.execute_approved() == [(made["id"], "simulated")]
    agent.clock.advance(minutes=30)
    with agent.db.transaction() as conn:
        mailstore.suppress(conn, agent.scope(), READER, to_iso(agent.clock.now()), "asked to stop", 1)
    agent.run_policy()
    [why] = rows(agent, "SELECT why FROM policy_grants ORDER BY id DESC LIMIT 1")
    assert why["why"] == f"the person you answered in request #{made['id']} asked to stop"
    agent.clock.advance(days=1)
    agent.run_policy()  # yesterday's digest
    [digest] = rows(agent, "SELECT day, text FROM owner_digests ORDER BY day DESC LIMIT 1")
    assert "Unlocks taken back" in digest["text"] and "@" not in digest["text"]
    fields = agent.sensor_fields()
    assert "@" not in json.dumps(fields) and "digest" not in fields
    assert fields["digest_day"] == digest["day"]
    assert (fields["digest_actions"], fields["digest_failed"], fields["digest_taken_back"]) == (1, 0, 1)


# --- FIX 15d: smaller leaks ---

LONG_SUBJECT = "Frage zu Ihrem Nebenkostenabrechnung-Tool fuer meine Wohnung in der Musterstrasse 12, Mieter Hans M."


def test_cut_subjects_opt_out_words_and_the_owner_s_identity_stay_out(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"owner_user_ids": (OWNER_ID,)})
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[])], settings)
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    agent.check_events()  # the agenda begins
    agent.clock.advance(minutes=20)
    asked = inbound(agent, "hans@example.org", subject=LONG_SUBJECT)
    agent.check_events()
    [noted] = rows(agent, "SELECT text FROM agenda WHERE kind = 'inquiry'")
    assert "Musterstrass" in noted["text"] and LONG_SUBJECT not in noted["text"]  # cut to 80 characters
    with agent.db.transaction() as conn:
        mailstore.suppress(
            conn, agent.scope(), "bo@example.org", to_iso(agent.clock.now()), 'replied "Bitte keine Mails mehr"', asked
        )
    assert owner(agent).send_message({"text": "Fiverr is on hold."}, LABEL).status == 201
    shared, full = shared_and_full(agent)
    assert "Musterstrass" in full and "Bitte keine Mails" in full and LABEL in full
    for words in ("Musterstrass", "Nebenkosten", "Bitte keine Mails", "Felix Beispiel", OWNER_ID):
        assert words not in shared, words
    assert "[the owner] sent a message to the agent" in section(shared, diagnostics.EVENTS_TITLE)
    assert "Fiverr is on hold." in shared  # the owner's own words, as they wrote them
    assert f"Email #{asked} from [email " in shared and f"[subject of email #{asked}]" in shared
    assert json.loads(section(shared, "OPTIONS (public)"))["owner_user_ids"] == ["[the owner's user ID]"]


def test_a_subject_of_one_word_is_still_masked(data_dir: Path) -> None:
    """Only a sender's name must be more than one word; a subject is someone else's text whatever its length."""
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[])])
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    agent.check_events()
    agent.clock.advance(minutes=20)
    asked = inbound(agent, "hans@example.org", subject="Mietvertragskündigung-Musterstrasse12")
    agent.check_events()
    shared, full = shared_and_full(agent)
    assert "Musterstrasse12" in full and "Musterstrasse12" not in shared
    assert f"[subject of email #{asked}]" in shared


def test_a_subject_of_one_ordinary_word_is_masked_only_where_it_is_quoted(data_dir: Path) -> None:
    """ "Rechnung" as a subject overwrote that word everywhere, in the owner's instructions too."""
    agent, _ = make_agent(data_dir, [])
    scope = agent.scope()
    with agent.db.transaction() as conn:
        for uid, subject in ((1, "Willkommen"), (2, "Rechnung")):
            conn.execute(
                "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, from_addr, from_name,"
                " to_addr, subject, received_at, body) VALUES (?, ?, ?, 'in', 1, ?, 'x@strato.de', 'Strato',"
                " 'ember@example.invalid', ?, ?, 'x')",
                (scope.mode, scope.session, scope.life_id, uid, subject, to_iso(agent.clock.now())),
            )
        masker = Masker(own_address="emberthehelper@mailbox.org", others=diagnostics._others(conn))
    text = "Willkommen bei uns. Bitte die Rechnung bezahlen."
    assert masker(text) == text
    assert masker('Email #2 from x: "Rechnung"') == 'Email #2 from x: "[subject of email #2]"'
    assert masker('{"text": "Email #1: \\"Willkommen\\""}') == '{"text": "Email #1: \\"[subject of email #1]\\""}'
    assert masker("Subject: Rechnung\n\nHallo") == "Subject: [subject of email #2]\n\nHallo"


@pytest.mark.parametrize("ids", [(), (OWNER_ID,)])
def test_the_owner_s_label_in_an_etsy_event_is_masked(ids: tuple[str, ...], data_dir: Path) -> None:
    """web.py writes the label into the etsy and pinterest events too, perhaps in no column named "by"."""
    agent, _ = make_agent(data_dir, [], ROOMY.model_copy(update={"owner_user_ids": ids}))
    events.record(agent.db, "info", "etsy", f"{LABEL} started connecting Etsy")
    shared, full = shared_and_full(agent)
    assert LABEL in full
    assert "Felix Beispiel" not in shared and OWNER_ID not in shared
    assert "[the owner] started connecting Etsy" in section(shared, diagnostics.EVENTS_TITLE)


@pytest.mark.parametrize(
    ("reason", "shown"),
    [
        ('replied "Bitte keine Mails mehr"', "replied [the sender's words, 22 characters]"),
        ('wrote "stop"', "wrote [the sender's words, 4 characters]"),  # a first message, not a reply
        ("asked to stop", "asked to stop"),
        ('asked in email #7: they wrote "Lass mich in Ruhe"', "asked in email #7 [the agent's note, 30 characters]"),
    ],
)
def test_an_opt_out_reason_keeps_only_the_length_of_their_words(reason: str, shown: str) -> None:
    assert diagnostics._their_words(reason) == shown


def test_a_user_id_without_a_name_is_masked_too(data_dir: Path) -> None:
    """web._owner labels a user without a display name by the ID alone; owner_user_ids isn't set here."""
    agent, _ = make_agent(data_dir, [])
    assert owner(agent).send_message({"text": "Hello."}, OWNER_ID).status == 201
    shared, full = shared_and_full(agent)
    assert OWNER_ID in full and OWNER_ID not in shared
    assert "[the owner's user ID] sent a message to the agent" in section(shared, diagnostics.EVENTS_TITLE)


# --- FIX 26c: building the report changes nothing ---


def everything(agent: Agent) -> dict[str, list[tuple[Any, ...]]]:
    with agent.db.connection() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}  # noqa: S608


def test_building_the_report_wakes_nothing_and_keeps_nothing(data_dir: Path) -> None:
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    agent.check_events()
    agent.clock.advance(minutes=20)
    inbound(agent, "ann@example.org", subject="Do you ship to Austria?")
    agent.check_events()
    before = everything(agent)
    shared, full = shared_and_full(agent)
    scheduler = json.loads(section(shared, "SCHEDULER"))
    assert scheduler["decision_now"]["trigger"] == "event"
    assert rows(agent, "SELECT woke_at FROM agenda WHERE kind = 'inquiry'") == [{"woke_at": None}]
    assert everything(agent) == before  # not a row, not a meta value
    assert "\n== STATUS ==\n" in section(full, diagnostics.PLANNER_TITLE)
    decision = agent.decide()  # the scheduler's decision is still the event's
    assert (decision.run, decision.trigger) == (True, "event")


def test_the_preview_wakes_for_a_waiting_message_only_if_one_is_unseen(data_dir: Path) -> None:
    """With nothing unseen, decide() clears message_waiting and goes on; the preview gives that same decision."""
    agent, _ = make_agent(data_dir, [])
    agent.message_waiting, agent.waiting_for = True, "message"
    preview = agent.decide(preview=True)
    assert "Waking up" not in preview.reason and agent.message_waiting  # the preview keeps nothing
    real = agent.decide()
    assert (preview.run, preview.trigger, preview.reason) == (real.run, real.trigger, real.reason)
    assert not agent.message_waiting
    assert owner(agent).send_message({"text": "Hello."}, LABEL).status == 201
    agent.message_waiting = True
    assert agent.decide(preview=True).reason == "Waking up to read the owner's message in a moment"


def test_the_preview_runs_no_keeper(data_dir: Path) -> None:
    """The keepers (the money goal, stages, grading, gates, predictions, obligations) run in a cycle only."""
    agent, _ = make_agent(data_dir, [])
    report = diagnostics.report(state_of(agent))
    assert "\n== STATUS ==\n" in section(report, diagnostics.PLANNER_TITLE)
    assert rows(agent, "SELECT COUNT(*) AS n FROM milestones") == [{"n": 0}]  # no money goal set
    assert agent.db.get_meta(obligations.SINCE_KEY.format(mode=agent.scope().mode)) is None
    assert agent.db.get_meta(agent._key("next_wake_at")) is None


# --- X17: observations in the report ---


def test_the_report_shows_whether_observations_run(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    scope = agent.scope()
    with agent.db.transaction() as conn:
        for day, subject, subject_id, metric in (
            ("2026-09-27", "shop", 0, "listings_live"),
            ("2026-09-28", "shop", 0, "listings_live"),
            ("2026-09-28", "listing", 900000001, "views"),
            ("2026-09-28", "listing", 900000002, "views"),
        ):
            conn.execute(
                "INSERT INTO observations (mode, session, day, observed_at, subject, subject_id, metric, value)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 7)",
                (scope.mode, scope.session, day, f"{day}T06:00:00Z", subject, subject_id, metric),
            )
    report = diagnostics.report(state_of(agent))
    assert "\n  observations: 4\n" in section(report, "DATABASE")
    shown = section(report, "AGENT RECORDS").split("-- observations", 1)[1].split("\n--", 1)[0].splitlines()
    assert shown[1:] == [
        "subject | metric | days | subjects | newest",
        "listing | views | 1 | 2 | 2026-09-28",
        "shop | listings_live | 2 | 1 | 2026-09-28",
    ]


# --- X16: pruning ---


HISTORY = ("llm_calls", "tool_calls", "call_texts")


def rows_of(database: dbmod.Database, sql: str) -> list[dict[str, Any]]:
    with database.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def insert_cycle(conn: sqlite3.Connection, when: str, texts: str = "x" * 5_000) -> tuple[int, int]:
    """A cycle at ``when`` with a paid call, its reply, a workshop call and a research call; (cycle, call)."""
    life = conn.execute("SELECT id FROM lives ORDER BY id DESC LIMIT 1").fetchone()[0]
    cycle = conn.execute(
        "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros)"
        " VALUES (?, 'test', ?, ?, 'completed', 'schedule', 1, 250000)",
        (life, when, when),
    ).lastrowid
    call = conn.execute(
        "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day, cost_micros,"
        " input_tokens, output_tokens) VALUES ('test', ?, 'work', 'scripted', 1, 'ok', ?, ?, 1234, 500, 60)",
        (cycle, when, when[:10]),
    ).lastrowid
    conn.execute("INSERT INTO call_texts (llm_call_id, text, stop_details) VALUES (?, ?, '{}')", (call, texts))
    for seq, tool in enumerate(("workshop", "research"), start=1):
        conn.execute(
            "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
            " started_at, finished_at, summary, result)"
            " VALUES (?, ?, ?, 'act', 'local', ?, ?, ?, 'ok', ?, ?, 'did', ?)",
            (cycle, call, seq, tool, f"toolu_{cycle}_{seq}", json.dumps({"task": texts}), when, when, texts),
        )
    return int(cycle), int(call)


def test_old_texts_are_pruned_and_the_money_rows_kept(data_dir: Path) -> None:
    """3.3 MB after 3 days: only the event log was pruned."""
    agent, _ = make_agent(data_dir, [])
    now = agent.clock.now()
    with agent.db.transaction() as conn:
        old = [insert_cycle(conn, to_iso(now - timedelta(days=dbmod.TEXT_DAYS + 7 - n)))[1] for n in range(6)]
        _, new = insert_cycle(conn, to_iso(now - timedelta(days=dbmod.TEXT_DAYS - 1)))
        conn.execute(  # an old call with nothing to prune: it stays as it is
            "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
            " started_at, finished_at) SELECT cycle_id, id, 3, 'act', 'local', 'email_inbox', 'toolu_x', '{}',"
            " 'interrupted', ts, ts FROM llm_calls WHERE id = ?",
            (old[0],),
        )
    money = rows(agent, "SELECT * FROM llm_calls ORDER BY id")
    # The old cycles' replies and workshop calls, and their research but the newest five of the session
    assert agent.db.prune_texts(now) == 6 + 6 + 2
    assert agent.db.prune_texts(now) == 0  # once
    assert rows(agent, "SELECT * FROM llm_calls ORDER BY id") == money  # costs, tokens and purpose stay
    texts = {r["llm_call_id"]: r for r in rows(agent, "SELECT * FROM call_texts")}
    assert {(texts[call]["text"], texts[call]["stop_details"]) for call in old} == {(dbmod.PRUNED, None)}
    assert texts[new]["text"] == "x" * 5_000
    tool_rows = {(r["llm_call_id"], r["tool"]): r for r in rows(agent, "SELECT * FROM tool_calls")}
    pruned = tool_rows[(old[0], "workshop")]
    assert (pruned["input"], pruned["result"], pruned["summary"], pruned["status"]) == ("{}", dbmod.PRUNED, "did", "ok")
    researched = [tool_rows[(call, "research")]["result"] for call in [*old, new]]
    assert researched == [dbmod.PRUNED] * 2 + ["x" * 5_000] * 5  # RECENT RESEARCH still has its five
    assert tool_rows[(new, "workshop")]["result"] == "x" * 5_000
    assert (tool_rows[(old[0], "email_inbox")]["input"], tool_rows[(old[0], "email_inbox")]["result"]) == ("{}", None)


def test_pruning_keeps_what_ember_still_reads() -> None:
    """A question asked again within tools.RESEARCH_REPEAT_DAYS is answered from its first call, and the plan's
    RECENT RESEARCH shows the newest context.RESEARCH_CALLS results."""
    assert dbmod.TEXT_DAYS >= RESEARCH_REPEAT_DAYS
    assert dbmod.RESEARCH_KEPT >= RESEARCH_CALLS


def test_the_scheduler_prunes_where_it_prunes_the_event_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In its first round and every PRUNE_EVERY_SECONDS; a failure doesn't stop the round. 0.31.0: on a machine up for
    less than PRUNE_EVERY_SECONDS too (time.monotonic counts from its boot): the first round skipped it on CI."""
    real, booted = time.monotonic, time.monotonic()
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: real() - booted + 1)  # up for a second
    pruned: list[Any] = []
    decided: list[bool] = []

    def prune_texts(now: Any) -> int:
        pruned.append(now)
        raise sqlite3.OperationalError("database is locked")

    async def main() -> None:
        now = from_iso("2026-09-30T08:00:00Z")
        agent = SimpleNamespace(
            running_cycle=False,
            stop=threading.Event(),
            run_policy=lambda: None,
            execute_approved=lambda: None,
            sync_shop=lambda: None,
            check_events=lambda: None,
            decide=lambda: decided.append(True) or Decision(False, reason="waiting"),
        )
        economy = SimpleNamespace(tick=lambda: None, clock=SimpleNamespace(now=lambda: now))
        db = SimpleNamespace(prune_events=lambda keep: 0, prune_texts=prune_texts)
        scheduler = Scheduler(db, economy, agent)  # type: ignore[arg-type]
        scheduler.start()
        for _ in range(100):
            if decided:
                break
            await asyncio.sleep(0.02)
        await scheduler.stop()

    asyncio.run(main())
    assert pruned == [from_iso("2026-09-30T08:00:00Z")] and decided
    assert "Pruning old texts failed" in caplog.text


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE tool_calls SET result = 'forged' WHERE tool = 'workshop'",
        "UPDATE tool_calls SET input = '{}', result = '[pruned]', summary = 'forged' WHERE tool = 'workshop'",
        "UPDATE tool_calls SET input = '{\"x\": 1}', result = '[pruned]' WHERE tool = 'workshop'",
        "DELETE FROM tool_calls",
        "UPDATE call_texts SET text = 'forged'",
        "UPDATE call_texts SET text = '[pruned]', stop_details = '{\"x\": 1}'",
        "DELETE FROM call_texts",
        "UPDATE llm_calls SET cost_micros = 0",
        "DELETE FROM llm_calls",
    ],
)
def test_pruning_is_the_only_change_history_allows(data_dir: Path, sql: str) -> None:
    agent, _ = make_agent(data_dir, [])
    with agent.db.transaction() as conn:
        insert_cycle(conn, to_iso(agent.clock.now()))
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute(sql)


def test_the_migration_keeps_the_history_and_its_guards(tmp_path: Path) -> None:
    """A database from before 0.15.0 with a cycle's calls: the rows stay as they were, and only pruning changes them."""
    db_file = tmp_path / "ember.db"
    ours = next(m for m in dbmod.discover_migrations() if m.name == "privacy")
    before_ours = [m for m in dbmod.discover_migrations() if m.version < ours.version]
    dbmod.migrate(db_file, before_ours, backup_dir=tmp_path / "backups")
    old = dbmod.Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        _, call = insert_cycle(conn, "2026-08-01T10:00:00Z")
        history = {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in HISTORY}  # noqa: S608
    old.close()
    assert dbmod.migrate(db_file, backup_dir=tmp_path / "backups") == [
        m.version for m in dbmod.discover_migrations() if m.version >= ours.version
    ]
    upgraded = dbmod.Database(db_file)
    with upgraded.connection() as conn:
        assert {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in HISTORY} == history  # noqa: S608
    for sql in ("UPDATE call_texts SET text = 'forged'", "UPDATE tool_calls SET result = 'forged'"):
        with pytest.raises(sqlite3.IntegrityError), upgraded.transaction() as conn:
            conn.execute(sql)
    assert upgraded.prune_texts(from_iso("2026-09-30T00:00:00Z")) == 2
    assert rows_of(upgraded, "SELECT text FROM call_texts") == [{"text": dbmod.PRUNED}]
    assert rows_of(upgraded, "SELECT tool, input, result FROM tool_calls ORDER BY id") == [
        {"tool": "workshop", "input": "{}", "result": dbmod.PRUNED},
        {"tool": "research", "input": json.dumps({"task": "x" * 5_000}), "result": "x" * 5_000},
    ]
    assert call == 1
    upgraded.close()
