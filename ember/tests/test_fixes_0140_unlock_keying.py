"""0.14.0 (FIX NOW 21, X11, X20): unlocks keyed to what a request acts on, not to the plan's focus; the end of a
milestone ends its unlock, also for what it held; NEVER's words of tax, VAT, a Gewerbe and contracts read in what a
request says or sends, normalised; a price band measured from the approved price; an unlock's change that Etsy's
listing doesn't match waits for the owner; Undo without dead ends, while paused, and after a crash; a daily digest of
what Ember's code did before the journal began and on the days nobody wrote one; promotions that count only clean
approvals and don't come back after a revocation for cause."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import never, policy, roadmap, store  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import connectors, etsy  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import change, price_of, reject, work_on  # noqa: E402
from tests.test_ventures import tool_results  # noqa: E402

LINE = 1  # the project of the fake shop's first listing
WROTE = "lena@example.org"  # she wrote to Ember


def goal(agent: Any, title: str, **links: int) -> int:
    """An open milestone, of a project or venture if given."""
    due = (agent.clock.today() + timedelta(days=30)).isoformat()
    with agent.db.transaction() as conn:
        return roadmap.create(
            conn, agent.scope(), title=title, measure="10 orders", due=due, now=to_iso(agent.clock.now()), **links
        )


def unlock(agent: Any, milestone_id: int, rule: str, level: str = "auto") -> None:
    assert owner(agent).set_autonomy(milestone_id, {"rule": rule, "level": level}, "Stefan").status == 200


def unlocked_before(agent: Any, milestone_id: int, rule: str, level: str = "auto") -> None:
    """An unlock the owner could give before 0.14.0 refused one the milestone never covers."""
    with agent.db.transaction() as conn:
        policy.set_grant(conn, agent.scope(), milestone_id, rule, level, to_iso(agent.clock.now()), by="Stefan")


def close(agent: Any, milestone_id: int, status: str) -> None:
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = ?, closed_at = ?, closed_by = 'code', result = 'x' WHERE id = ?",
            (status, to_iso(agent.clock.now()), milestone_id),
        )


def approve_as_code(agent: Any, approval_id: int) -> None:
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, version = version + 1"
            " WHERE id = ?",
            (to_iso(agent.clock.now()), policy.POLICY_BY, approval_id),
        )


def carrier_of(agent: Any, approval_id: int) -> list[dict[str, Any]]:
    return rows(
        agent,
        "SELECT g.milestone_id, g.rule FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id"
        f" WHERE u.approval_id = {approval_id}",
    )


def she_wrote(agent: Any) -> None:
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr, to_addr,"
            " subject, received_at, body) VALUES (?, ?, ?, 'in', 1, 1, '<q1@example.org>', ?, 'ember@example.org',"
            " 'A question', ?, 'Do you make A5 planners?')",
            (scope.mode, scope.session, scope.life_id, WROTE, to_iso(agent.clock.now())),
        )


def a_request(agent: Any, executor: str, action: dict[str, Any], words: str = "Sells better.", **more: Any) -> int:
    """A request made as the tools make one (and the policy engine sees it), its number."""
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        made = store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            to_iso(agent.clock.now()),
            type=more.pop("type", "contact" if executor == "email" else "sell"),
            title=more.pop("title", "A request"),
            description=words,
            payload=more.pop("payload", f"{words} {json.dumps(action, ensure_ascii=False)}"),
            expected_cost="none",
            expected_benefit="an answer",
            executor=executor,
            action=store.canonical(action),
            **more,
        )
        policy.apply(conn, agent.scope(), made, agent.clock)
    return made


def a_reply(agent: Any, body: str) -> int:
    """A reply to Lena in the thread she started."""
    action = {"to": WROTE, "subject": "Re: A question", "body": body, "in_reply_to": "<q1@example.org>"}
    return a_request(agent, "email", action, words=f"Answer Lena: {body}")


def status_of(agent: Any, approval_id: int) -> tuple[str, str | None]:
    row = rows(agent, f"SELECT status, decided_by FROM approvals WHERE id = {approval_id}")[0]
    return row["status"], row["decided_by"]


# --- FIX 21a: an unlock carries what belongs to its milestone, not what the plan named -----------------------------


def test_an_unlock_carries_only_what_belongs_to_its_milestone(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    guide = goal(agent, "Write the Pinterest guide")  # no project, no venture: no listing is its
    line = goal(agent, "Ten sales", project_id=LINE)
    unlocked_before(agent, guide, "deactivate")
    made = work_on(agent, guide, change(listing_id, state="deactivate"))[-1]  # the plan names the guide
    assert (made["status"], made["milestone_id"]) == ("pending", guide)  # before: deactivated at once
    now = to_iso(agent.clock.now())
    with (
        pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"),
        agent.db.transaction() as conn,
    ):  # the database refuses it on its own
        granted = policy.grant(conn, agent.scope(), guide, "deactivate")
        conn.execute(
            "INSERT INTO policy_uses (approval_id, grant_id, level, created_at, approved_at)"
            " VALUES (?, ?, 'auto', ?, ?)",
            (made["id"], granted["id"], now, now),
        )
    reject(agent, made["id"])
    # The same price cut on the same listing is carried by its product line's unlock, whatever milestone the plan names
    unlock(agent, line, "price_change")
    old = price_of(agent, listing_id)
    made = work_on(agent, guide, change(listing_id, price=f"{old * Decimal('0.95'):.2f}"))[-1]
    assert (made["status"], made["decided_by"]) == ("approved", policy.POLICY_BY)
    assert carrier_of(agent, made["id"]) == [{"milestone_id": line, "rule": "price_change"}]
    assert (
        f"your owner unlocked {policy.RULES['price_change'].label} for milestone #{line}"
        in (tool_results(agent, "propose_etsy_edit")[-1]["result"])
    )
    assert agent.execute_approved() == [(made["id"], "done")]
    # A milestone of a venture carries the listings of the venture's projects
    with agent.db.transaction() as conn:
        conn.execute("UPDATE projects SET venture_id = 1 WHERE id = ?", (LINE,))
    venture = goal(agent, "The first sale", venture_id=1)
    unlock(agent, venture, "deactivate")
    made = work_on(agent, guide, change(listing_id, state="deactivate"))[-1]
    assert (made["status"], carrier_of(agent, made["id"])) == (
        "approved",
        [{"milestone_id": venture, "rule": "deactivate"}],
    )
    # An email reply belongs to no product line: a milestone of no project or venture carries it
    she_wrote(agent)
    unlocked_before(agent, line, "email_reply")
    assert status_of(agent, a_reply(agent, "Yes, in A5 too.")) == ("pending", None)
    unlock(agent, guide, "email_reply")
    reply = a_reply(agent, "Yes, in A4 too.")
    assert (status_of(agent, reply), carrier_of(agent, reply)) == (
        ("approved", policy.POLICY_BY),
        [{"milestone_id": guide, "rule": "email_reply"}],
    )
    # The card says what a milestone's unlock covers
    items = {m["id"]: m for m in agent.roadmap()["items"]}
    assert (items[line]["project_id"], items[venture]["venture_id"], items[guide]["project_id"]) == (LINE, 1, None)


# --- FIX 21b: a milestone that closed ends its unlock, and what the unlock held waits for the owner ---------------


def test_a_request_held_when_its_milestone_is_done_waits_for_the_owner(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "deactivate", "veto_window")
    held = work_on(agent, line, change(listing_id, state="deactivate"))[-1]["id"]
    assert status_of(agent, held) == ("pending", None)
    with pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"):
        approve_as_code(agent, held)  # the database approves nothing of the code's before the window passed
    close(agent, line, "done")  # success: the milestone is met two hours later
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    with pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"):
        approve_as_code(agent, held)  # nor for a milestone that closed
    agent.run_policy()
    assert status_of(agent, held) == ("pending", None)  # before: approved and carried out
    assert agent.execute_approved() == []
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == etsy.LIVE_STATE
    assert rows(agent, "SELECT level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"level": "manual", "by": policy.REVOKED_BY, "why": f"milestone #{line} was done"}
    ]
    said = [e["message"] for e in rows(agent, "SELECT message FROM events ORDER BY id")]
    assert (
        f"Ember's code revoked the unlock for {policy.RULES['deactivate'].label} (milestone #{line}): milestone"
        f" #{line} was done; what it held waits for you: request #{held}"
    ) in said
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == held)
    assert card["veto_until"] is None
    assert card["unlock_ended"] == f"milestone #{line} was done, so the unlock that held it ended"


def test_a_request_held_by_a_milestone_that_doesnt_cover_it_waits_for_the_owner(data_dir: Path) -> None:
    """What an unlock of 0.13.0 held for the plan's focus, whatever it touched, waits for the owner after the upgrade:
    Ember's code doesn't approve it, nor does the database."""
    agent, listing_id = listed(data_dir)
    guide = goal(agent, "Write the Pinterest guide")
    unlocked_before(agent, guide, "deactivate", "veto_window")
    held = work_on(agent, guide, change(listing_id, state="deactivate"))[-1]["id"]
    with agent.db.connection() as conn:
        granted = policy.grant(conn, agent.scope(), guide, "deactivate")
        conn.execute("BEGIN IMMEDIATE")
        try:  # as 0.13.0 stored it
            conn.execute("DROP TRIGGER policy_uses_standing")
            conn.execute(
                "INSERT INTO policy_uses (approval_id, grant_id, level, created_at, veto_until)"
                " VALUES (?, ?, 'veto_window', ?, ?)",
                (held, granted["id"], to_iso(agent.clock.now()), to_iso(agent.clock.now())),
            )
            assert policy.run_due(conn, agent.scope(), agent.clock) == []
            assert policy.veto_until(conn, held) is None
            assert policy.ended(conn, held) == (
                f"milestone #{guide} doesn't cover what it acts on, so its unlock doesn't carry it"
            )
            with pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"):
                conn.execute(
                    "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, version = version + 1"
                    " WHERE id = ?",
                    (to_iso(agent.clock.now()), policy.POLICY_BY, held),
                )
        finally:
            conn.execute("ROLLBACK")
    assert status_of(agent, held) == ("pending", None)


# --- FIX 21c: NEVER reads what a request says or sends, normalised ------------------------------------------------

EVASIONS = (
    "Kleinunternehmer gemäß § 19 UStG.",
    "Ja, wir nehmen Ihren Auftrag über 200 Stück zu 3 € an, Rechnung folgt.",
    "Invoice to follow.",
    "Our agreement stands.",
    "All prices are taxable.",
    "Double taxation.",
    "IVA incluida.",
    "TVA non applicable.",
    "Die St\u0435uer zahlen Sie.",  # a Cyrillic e
    "Die Ste\u00aduer zahlen Sie.",  # a soft hyphen
    "Die Ste\u200buer zahlen Sie.",  # a zero-width space
    "\uff34\uff21\uff38 included.",  # full-width letters
    "We subcontract the print.",
    "Our contractual terms apply.",
    "Zzgl. Umsatzst.",
    "Zoll und Einfuhrabgaben trägt der Käufer.",
    "Mit Lizenz für gewerbliche Nutzung.",
    "A commercial licence is included.",
    "RECH\u039dUNG folgt.",  # Greek capital Nu
    "CO\u039dTRACT accepted.",
    "KLEI\u039dU\u039dTER\u039dEHMER.",
    "Die Steu-er zahlen Sie.",
    "Une facture suivra.",
    "Le contrat est sign\u00e9.",
    "Fattura in arrivo.",
    "Faktura f\u00f8lger.",
    "Ihr Angebot nehmen wir an.",
    "Schlussrechnung folgt.",  # review round 2: invoice compounds, an offer or a quote in English, a VAT number
    "Die Vorabrechnung ist angeh\u00e4ngt.",
    "Proformarechnung anbei.",
    "Our offer: 200 at 3 EUR.",
    "Quote attached.",
    "Unsere UID-Nummer steht unten.",
)
HARMLESS = ("Btw, the A5 file is attached.", "Sonderangebot: 2 f\u00fcr 1", "Your T-Shirt ships today.")
HARMLESS += ("Die Berechnung der Versandkosten steht im Shop.",)  # a Berechnung is a sum, no invoice


def legal_in_db(agent: Any, approval_id: int) -> int:
    return int(rows(agent, f"SELECT legal FROM approvals_never WHERE approval_id = {approval_id}")[0]["legal"])


def test_never_reads_what_a_request_says_normalised(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    she_wrote(agent)
    for words in EVASIONS:  # a reply that states a tax status, takes an order, promises an invoice: code and database
        reply = a_reply(agent, words)
        with agent.db.connection() as conn:
            found = never.reasons(conn, conn.execute("SELECT * FROM approvals WHERE id = ?", (reply,)).fetchone())
        assert (found, legal_in_db(agent, reply)) == (["legal"], 1), words
    for words in HARMLESS:  # 0.14.0: "btw" is "by the way", a Sonderangebot no offer of a contract
        reply = a_reply(agent, words)
        with agent.db.connection() as conn:
            found = never.reasons(conn, conn.execute("SELECT * FROM approvals WHERE id = ?", (reply,)).fetchone())
        assert (found, legal_in_db(agent, reply)) == ([], 0), words
    assert (never.normalise("\uff33t\u0435\u00adu\u0435r\u2060"), never.normalise("A\u00a0b\nc")) == ("steuer", "a b c")
    # A listing's product copy and its disclaimer are no act: "Mietvertrag" in a checklist is no contract
    copy = "Umzugs-Checkliste: Mietvertrag kündigen, Nachsendeauftrag stellen. Keine Rechts- oder Steuerberatung."
    edit = a_request(
        agent,
        "etsy_edit",
        {"listing_id": listing_id, "currency": "EUR", "description": copy},
        words=f"A clearer description. {copy}",
    )
    with agent.db.connection() as conn:
        assert never.reasons(conn, conn.execute("SELECT * FROM approvals WHERE id = ?", (edit,)).fetchone()) == []
    assert legal_in_db(agent, edit) == 0
    # The agent isn't told which words kept a reply from its unlock (the owner's card says it)
    guide = goal(agent, "Answer questions")
    unlock(agent, guide, "email_reply")
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        made = store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            to_iso(agent.clock.now()),
            type="contact",
            title="Reply to Lena",
            description="She asked.",
            payload="Prices include VAT.",
            expected_cost="none",
            expected_benefit="an answer",
            executor="email",
            action=store.canonical(
                {
                    "to": WROTE,
                    "subject": "Re: A question",
                    "body": "Prices include VAT.",
                    "in_reply_to": "<q1@example.org>",
                }
            ),
        )
        said = policy.apply(conn, agent.scope(), made, agent.clock)
    assert said == " It waits for your owner whatever they unlocked (never automatic)."
    assert next(a for a in agent.dashboard()["approvals"] if a["id"] == made)["never"] == [never.CLASSES["legal"]]


# --- FIX 21d: automatic price changes can't add up past the band --------------------------------------------------


def test_automatic_price_changes_cant_add_up_past_the_band(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert price_of(agent, listing_id) == Decimal("4.50")  # as the owner approved it
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "price_change")
    first = work_on(agent, line, change(listing_id, price="3.87"))[-1]  # 14% under 4.50
    assert (first["status"], first["decided_by"]) == ("approved", policy.POLICY_BY)
    assert agent.execute_approved() == [(first["id"], "done")]
    second = work_on(agent, line, change(listing_id, price="3.33"))[-1]  # 14% under 3.87, 26% under 4.50
    assert (second["status"], second["decided_by"]) == ("pending", None)  # before: approved (and 2.86 next)
    assert owner(agent).decide(second["id"], {"decision": "approve"}, "Stefan").status == 200
    assert agent.execute_approved() == [(second["id"], "done")]
    with agent.db.connection() as conn:  # the owner's price is where the band starts now
        assert policy.approved_price(conn, agent.scope(), listing_id) == "3.33"
    third = work_on(agent, line, change(listing_id, price="2.86"))[-1]
    assert (third["status"], third["decided_by"]) == ("approved", policy.POLICY_BY)


# --- FIX 21e: an unlock's change that Etsy's listing doesn't match goes back to the owner -------------------------


def test_an_unlock_s_change_waits_for_the_owner_when_etsy_s_listing_differs(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "qa_fix")
    unlock(agent, line, "price_change")
    shop = agent.etsy.shop()
    for rank in range(2, 6):  # the owner adds four photos of their own at Etsy; Ember's record still has one
        shop.upload_photo(listing_id, f"theirs-{rank}.jpg", b"theirs", rank)
    theirs = agent.etsy.shop().photo_ids(listing_id)
    photos = [f"shop/fix-{n}.png" for n in range(1, 6)]
    for path in photos:
        agent.roots()[0].write_bytes(path, f"{path} data".encode())
    fix = work_on(agent, line, change(listing_id, photos=", ".join(photos)))[-1]
    assert (fix["status"], fix["decided_by"]) == ("approved", policy.POLICY_BY)
    assert agent.execute_approved() == [(fix["id"], "failed")]
    assert agent.etsy.shop().photo_ids(listing_id) == theirs  # before: all five replaced by Ember's
    [back] = rows(agent, f"SELECT * FROM approvals WHERE id > {fix['id']} AND executor = 'etsy_edit'")
    assert (back["status"], back["decided_by"], back["action"]) == (
        "pending",
        None,
        rows(agent, f"SELECT action FROM approvals WHERE id = {fix['id']}")[0]["action"],
    )
    assert back["description"].startswith(
        "Sent back to you by Ember's code: Etsy's listing differs from Ember's record (its photos at Etsy aren't the"
        " ones Ember set last (5 now, 1 then))."
    )
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {fix['id']}")[0]["result_note"]
    assert f"request #{back['id']} waits for your owner" in note
    reject(agent, back["id"])
    # A price the owner set at Etsy isn't overwritten by an unlock either
    agent.etsy.shop().set_price(listing_id, "5.00")
    cut = work_on(agent, line, change(listing_id, price="4.28"))[-1]
    assert (cut["status"], cut["decided_by"]) == ("approved", policy.POLICY_BY)
    assert agent.execute_approved() == [(cut["id"], "failed")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["price_cents"] == 500
    [back] = rows(agent, f"SELECT description FROM approvals WHERE id > {cut['id']} AND executor = 'etsy_edit'")
    assert "(its price at Etsy is 5.00, 4.50 in Ember's record)" in back["description"]


# --- FIX 21f: Undo without a dead end, while paused, and after a crash --------------------------------------------


def feed(agent: Any) -> list[dict[str, Any]]:
    return agent.dashboard()["audit"]["feed"]


def undo(agent: Any, journal_id: int) -> int:
    reply = owner(agent).undo(journal_id, "Stefan")
    assert reply.status == 200, reply.body
    return int(reply.body["approval_id"])


def test_after_an_undo_the_action_before_it_can_be_undone(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "price_change")
    made = work_on(agent, line, change(listing_id, price="4.28"))[-1]
    assert agent.execute_approved() == [(made["id"], "done")]
    changed, created = feed(agent)[:2]
    request = undo(agent, changed["id"])
    assert agent.execute_approved() == [(request, "done")]
    restored, changed, created = feed(agent)[:3]
    assert (restored["undoes"], changed["undo"]["why_not"]) == (changed["id"], "it is undone")
    assert created["undo"]["why_not"] is None  # before: "a later action changed this listing (#2): undo that one first"
    request = undo(agent, created["id"])
    assert agent.execute_approved() == [(request, "done")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == etsy.STATES["deactivate"]
    deactivated, restored = feed(agent)[:2]
    assert deactivated["undoes"] == created["id"]
    assert restored["undo"]["why_not"] == (  # still one at a time, the newest first
        f"a later action changed this listing (#{deactivated['id']}): undo that one first"
    )
    assert deactivated["undo"]["why_not"] is None


def test_undoing_an_undo_of_an_undo_still_leads_back_to_the_first_action(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "price_change")
    made = work_on(agent, line, change(listing_id, price="4.28"))[-1]
    assert agent.execute_approved() == [(made["id"], "done")]
    for n in range(3):  # undo the change, undo that Undo, undo it again: the price is back as listed
        request = undo(agent, feed(agent)[0]["id"])
        assert agent.execute_approved() == [(request, "done")]
        if n == 1:  # two Undos done: the change is in effect again, and the newest Undo is the one to undo
            newest = feed(agent)[0]
            assert next(e for e in feed(agent) if e["class"] == "etsy.create_listing")["undo"]["why_not"] == (
                f"a later action changed this listing (#{newest['id']}): undo that one first"
            )
            assert newest["undo"]["why_not"] is None
            changed = next(e for e in feed(agent) if e["class"] == "etsy.edit_listing" and not e["undoes"])
            assert changed["undo"]["why_not"] == f"its Undo was undone: undo #{newest['id']} to undo it again"
    assert price_of(agent, listing_id) == Decimal("4.50")
    created = next(e for e in feed(agent) if e["class"] == "etsy.create_listing")
    assert created["undo"]["why_not"] is None  # before: "undo #2 first", and #2 "it is undone": a dead end
    # A renewal nothing can undo doesn't stand in the way either
    renewal = a_request(agent, "etsy_edit", {"listing_id": listing_id, "state": "renew"}, title="Renew it")
    assert owner(agent).decide(renewal, {"decision": "reject"}, "Owner").status == 200
    with agent.db.transaction() as conn:  # journaled as if it had been carried out (only the entry matters here)
        now = to_iso(agent.clock.now())
        connectors.begin(conn, renewal, now, subject=str(listing_id), name="etsy.renew")
        connectors.finish(conn, renewal, "done", now)
    assert feed(agent)[0]["class"] == "etsy.renew"
    created = next(e for e in feed(agent) if e["class"] == "etsy.create_listing")
    assert created["undo"]["why_not"] is None  # before: "a later action changed this listing (#6)"
    request = undo(agent, created["id"])
    assert agent.execute_approved() == [(request, "done")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == etsy.STATES["deactivate"]


def test_the_owner_s_undo_runs_while_paused_and_is_refused_once_killed(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    created = feed(agent)[0]
    agent.economy.set_paused(True)
    assert agent.executor_blocked() == "The agent is paused"
    request = undo(agent, created["id"])
    assert agent.execute_approved() == [(request, "done")]  # before: nothing until the owner resumed
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == etsy.STATES["deactivate"]
    agent.economy.set_paused(False)
    agent.economy.life.set_switch(KILLED_KEY, True)  # the kill switch stops everything Ember's code sends
    deactivated = feed(agent)[0]
    refused = owner(agent).undo(deactivated["id"], "Stefan")
    assert (refused.status, refused.body["error"]) == (
        409,
        "Ember's code carries nothing out while the kill switch is on, an Undo neither: undo it by hand",
    )
    assert rows(agent, "SELECT COUNT(*) AS n FROM action_undos") == [{"n": 1}]


def test_a_crash_while_deleting_a_pin_or_product_is_unclear_and_can_be_undone_again(data_dir: Path) -> None:
    from tests.test_pinterest import PIN, pinned  # noqa: PLC0415
    from tests.test_printify import proposed  # noqa: PLC0415

    agent, _, _ = pinned(data_dir)
    entry = next(e for e in feed(agent) if e["class"] == "pinterest.create_pin")
    request = undo(agent, entry["id"])
    with agent.db.transaction() as conn:  # the executor committed "running", then the app stopped
        connectors.begin(conn, request, to_iso(agent.clock.now()), subject=PIN)
    assert agent.pins.recover() == 1  # before: 0, and the Undo "under way" for good
    assert status_of(agent, request) == ("failed", "Stefan")
    assert rows(agent, f"SELECT status FROM action_journal WHERE approval_id = {request}") == [{"status": "unclear"}]
    entry = next(e for e in feed(agent) if e["class"] == "pinterest.create_pin")
    assert (entry["undo"]["why_not"], entry["undo"]["request"]["status"]) == (None, "failed")
    assert "press Delete the pin again" in entry["undo"]["request"]["note"]
    again = undo(agent, entry["id"])
    assert agent.execute_approved() == [(again, "done")]
    assert agent.pinterest.account().state["pins"] == {}
    # A Printify product the same way
    agent, _, made = proposed(data_dir / "pod")
    assert owner(agent).decide(made, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(made, "active")]
    entry = next(e for e in feed(agent) if e["class"] == "printify.create_product")
    request = undo(agent, entry["id"])
    with agent.db.transaction() as conn:
        connectors.begin(conn, request, to_iso(agent.clock.now()), subject=entry["subject"])
    assert agent.pod.recover() == 1
    entry = next(e for e in feed(agent) if e["class"] == "printify.create_product")
    assert entry["undo"]["why_not"] is None
    again = undo(agent, entry["id"])
    assert agent.execute_approved() == [(again, "done")]
    assert agent.printify.account().state["products"] == {}


# --- X11: the daily digest counts the day's actions ---------------------------------------------------------------


def done_before_the_journal(agent: Any, executor: str, action: dict[str, Any], when: str, status: str) -> int:
    """A request Ember's code carried out under 0.12.0, before the journal began: only the request says so."""
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        made = store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            when,
            type="sell",
            title=f"An old {executor}",
            description="Sells better.",
            payload=json.dumps(action),
            expected_cost="none",
            expected_benefit="sales",
            executor=executor,
            action=store.canonical(action),
        )
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = 'Stefan', version = version + 1"
            " WHERE id = ?",
            (when, made),
        )
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = 'Ember', result_note = 'x',"
            " version = version + 1 WHERE id = ?",
            (status, when, made),
        )
    return made


def test_the_digest_counts_what_ember_s_code_did_before_the_journal_began(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    yesterday = agent.clock.today() - timedelta(days=1)
    for n, status in enumerate(("done", "done", "failed")):
        when = to_iso(agent.clock.day_start(yesterday) + timedelta(hours=9 + n))
        done_before_the_journal(agent, "etsy_edit", {"listing_id": listing_id, "price": f"4.{n}0"}, when, status)
    agent.run_policy()
    [written] = rows(agent, "SELECT day, text FROM owner_digests")
    assert written == {
        "day": yesterday.isoformat(),
        "text": f"{yesterday.isoformat()}: Ember's code carried out 3 actions (3 you approved): 1 failed.",
    }  # before: "Ember's code carried out nothing."


def test_a_digest_is_written_for_each_day_the_rounds_didnt_run(data_dir: Path) -> None:
    agent, _ = listed(data_dir)  # Ember's code created a listing today
    today = agent.clock.today()
    agent.run_policy()
    agent.economy.set_paused(True)
    agent.clock.advance(hours=24 * 3)
    agent.run_policy()  # paused: the rounds don't write it
    agent.economy.set_paused(False)
    agent.run_policy()
    days = [today + timedelta(days=n) for n in range(-1, 3)]
    assert [r["day"] for r in rows(agent, "SELECT day FROM owner_digests ORDER BY day")] == [
        d.isoformat() for d in days
    ]  # before: the day before and the last day, not the paused ones
    text = rows(agent, f"SELECT text FROM owner_digests WHERE day = '{today.isoformat()}'")[0]["text"]
    assert text == f"{today.isoformat()}: Ember's code carried out 1 action (1 you approved)."


# --- X20: promotions count only clean approvals, and stay away after a revocation for cause ------------------------


def test_promotions_count_only_clean_approvals_and_stay_away_after_a_revocation_for_cause(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)

    def five(comment: str | None) -> None:
        for n in range(policy.PROMOTE_AFTER):
            made = work_on(agent, line, change(listing_id, price=f"4.{40 - n}"))[-1]
            body = {"decision": "approve", **({"comment": comment} if comment else {})}
            assert owner(agent).decide(made["id"], body, "Stefan").status == 200
            assert agent.execute_approved() == [(made["id"], "done")]

    five("I approve, but the pictures look alike: improve them.")
    assert agent.roadmap()["autonomy_suggestions"] == []  # before: price changes suggested at a veto window
    five(None)
    [suggested] = agent.roadmap()["autonomy_suggestions"]
    assert (suggested["milestone_id"], suggested["rule"], suggested["approved"]) == (line, "price_change", 5)
    unlock(agent, line, "price_change", "veto_window")
    held = work_on(agent, line, change(listing_id, price="4.20"))[-1]
    reject(agent, held["id"])  # the owner's veto: Ember's code takes the unlock back for cause
    agent.run_policy()
    assert rows(agent, "SELECT why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"why": f"your owner vetoed request #{held['id']}"}
    ]
    assert agent.roadmap()["autonomy_suggestions"] == []  # before: suggested again at once


# --- Review round 2 ----------------------------------------------------------------------------------------------


def test_a_photo_fix_waits_for_the_owner_when_etsy_has_other_photos_as_many(data_dir: Path) -> None:
    """21e: the owner replaced Ember's photo at Etsy with one of their own: the count is the same, the photo not."""
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "qa_fix")
    shop = agent.etsy.shop()
    [embers] = shop.photo_ids(listing_id)
    shop.upload_photo(listing_id, "theirs.jpg", b"theirs", 1)
    shop.delete_photo(listing_id, embers)
    theirs = agent.etsy.shop().photo_ids(listing_id)
    assert len(theirs) == 1
    photos = [f"shop/fix-{n}.png" for n in range(1, 6)]
    for path in photos:
        agent.roots()[0].write_bytes(path, f"{path} data".encode())
    fix = work_on(agent, line, change(listing_id, photos=", ".join(photos)))[-1]
    assert (fix["status"], fix["decided_by"]) == ("approved", policy.POLICY_BY)
    assert agent.execute_approved() == [(fix["id"], "failed")]  # before: done, and the owner's photo deleted
    assert agent.etsy.shop().photo_ids(listing_id) == theirs
    [back] = rows(agent, f"SELECT id, description FROM approvals WHERE id > {fix['id']} AND executor = 'etsy_edit'")
    assert "its photos at Etsy aren't the ones Ember set last (1 now, 1 then)" in back["description"]
    reject(agent, back["id"])
    # A listing whose photos Ember's code never saw at Etsy (set before 0.14.0) waits for the owner
    with agent.db.transaction() as conn:
        conn.execute("DELETE FROM etsy_photo_ids")
    last = work_on(agent, line, change(listing_id, photos=", ".join(photos)))[-1]
    assert agent.execute_approved() == [(last["id"], "failed")]
    [back] = rows(agent, f"SELECT id, description FROM approvals WHERE id > {last['id']} AND executor = 'etsy_edit'")
    assert "Ember has no record of the photos Etsy has" in back["description"]
    # The owner approves it: Ember's code sets the photos and notes Etsy's numbers for the next fix
    assert owner(agent).decide(back["id"], {"decision": "approve"}, "Stefan").status == 200
    assert agent.execute_approved() == [(back["id"], "done")]
    kept = rows(agent, f"SELECT ids FROM etsy_photo_ids WHERE listing_id = {listing_id} ORDER BY id DESC LIMIT 1")
    assert json.loads(kept[0]["ids"]) == agent.etsy.shop().photo_ids(listing_id)


def test_the_owner_s_own_ask_me_doesnt_keep_a_rule_from_being_suggested(data_dir: Path) -> None:
    """X20: only a take-back by Ember's code for cause holds a suggestion back, not the owner's own 'Ask me'."""
    agent, listing_id = listed(data_dir)
    line = goal(agent, "Ten sales", project_id=LINE)
    unlock(agent, line, "price_change", "manual")
    for n in range(policy.PROMOTE_AFTER):
        made = work_on(agent, line, change(listing_id, price=f"4.{40 - n}"))[-1]
        assert owner(agent).decide(made["id"], {"decision": "approve"}, "Stefan").status == 200
        assert agent.execute_approved() == [(made["id"], "done")]
    [suggested] = agent.roadmap()["autonomy_suggestions"]  # before: none for 30 days
    assert (suggested["milestone_id"], suggested["rule"]) == (line, "price_change")


def test_a_rule_a_milestone_never_covers_cant_be_unlocked_on_it(data_dir: Path) -> None:
    """21a: email replies on a product line's milestone, listing rules on one of no product line would carry nothing:
    they aren't unlocked, nor offered."""
    agent, _ = listed(data_dir)
    line, replies = goal(agent, "Ten sales", project_id=LINE), goal(agent, "Answer questions")
    for milestone_id, rule in ((line, "email_reply"), (replies, "deactivate"), (replies, "price_change")):
        reply = owner(agent).set_autonomy(milestone_id, {"rule": rule, "level": "auto"}, "Stefan")
        assert reply.status == 409, (milestone_id, rule)
        assert "this milestone never covers" in str(reply.body)
    unlock(agent, line, "deactivate")
    unlock(agent, replies, "email_reply")
    assert owner(agent).set_autonomy(line, {"rule": "email_reply", "level": "manual"}, "Stefan").status == 200
    with agent.db.connection() as conn:
        fits = {r["rule"]: r["fits"] for r in policy.view(conn, agent.scope(), agent.clock, replies)}
    assert fits == {rule: rule == "email_reply" for rule in policy.RULES}


def test_an_email_request_stored_before_the_upgrade_is_read_normalised_after_a_restart(data_dir: Path) -> None:
    """21c: a reply stored without its normalised act (before 0.14.0) gets it at startup: look-alikes count."""
    agent, _ = listed(data_dir)
    she_wrote(agent)
    reply = a_reply(agent, "RECHΝUNG folgt.")
    assert legal_in_db(agent, reply) == 1
    with agent.db.transaction() as conn:  # as stored by 0.13.0: no act_words row
        conn.execute("DROP TRIGGER act_words_no_delete")
        conn.execute("DELETE FROM act_words")
    assert legal_in_db(agent, reply) == 0
    agent.recover()
    assert legal_in_db(agent, reply) == 1
