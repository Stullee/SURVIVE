"""0.13.0: NEVER, in code and in a DB check. Whatever the owner unlocks, no unlock carries creating an account, money,
a first contact, Ember's first publication in the shop, a post in a third-party community, tax, VAT, a Gewerbe or a
contract, or what only the owner carries out; and only the owner unlocks. The policy engine checks it (never.py) and
the database checks it again on its own (migration 0051). These tests go through every NEVER kind with every rule and
level, take each check of the database alone, and generate requests at random to show both read a request alike."""

from __future__ import annotations

import itertools
import json
import random
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import never, policy, roadmap  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import change, price_of, work_on  # noqa: E402
from tests.test_ventures import tool_results  # noqa: E402

_numbers = itertools.count(1)
WROTE = "lena@example.org"  # she wrote to Ember
LEGAL = "tax, VAT, a Gewerbe or a contract"


def an_agent(data_dir: Path) -> Any:
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    with agent.db.transaction() as conn:
        scope, now = agent.scope(), to_iso(agent.clock.now())
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr, to_addr,"
            " subject, received_at, body) VALUES (?, ?, ?, 'in', 1, 1, '<q1@example.org>', ?, 'ember@example.org',"
            " 'A question', ?, 'Do you make A5 planners?')",
            (scope.mode, scope.session, scope.life_id, WROTE, now),
        )
    return agent


def a_milestone(agent: Any, level: str | None = None, **limits: int) -> int:
    """An open milestone; with a level, every rule unlocked for it at that level by the owner."""
    due = (agent.clock.today() + timedelta(days=30)).isoformat()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        goal = roadmap.create(
            conn, agent.scope(), title=f"Goal {next(_numbers)}", measure="10 orders", due=due, now=now
        )
        for rule in policy.RULES if level else ():
            policy.set_grant(conn, agent.scope(), goal, rule, level, now, by="Stefan", **limits)
    return goal


def request(
    agent: Any,
    milestone_id: int,
    *,
    type: str = "sell",
    executor: str | None = "etsy_edit",
    action: Any = None,
    words: str = "Sells better.",
) -> int:
    """A request stored as the tools store one, for a milestone (its shape doesn't matter to NEVER)."""
    n = next(_numbers)
    if executor is not None and action is None:
        action = {"listing_id": 900_000_001, "currency": "EUR", "price": "9.50"}
    scope = agent.scope()
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
            " payload_sha256, expected_cost, expected_benefit, executor, action, milestone_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', 'more sales', ?, ?, ?)",
            (
                scope.mode,
                scope.session,
                scope.life_id,
                cycle,
                to_iso(agent.clock.now()),
                type,
                f"Request {n}",
                words,
                f"Request {n}: {words}",
                f"sha-{n}",
                executor,
                None if action is None else json.dumps(action, ensure_ascii=False),
                milestone_id,
            ),
        )
    return int(cursor.lastrowid)


def an_email(to: str, words: str = "Yes, in A5 too.") -> dict[str, Any]:
    return {"to": to, "subject": "Re: A question", "body": words, "in_reply_to": "<q1@example.org>"}


def never_requests(agent: Any, goal: int) -> dict[str, tuple[int, list[str]]]:
    """A request of each NEVER kind, and the other ways into one, with the classes each falls in."""
    first = an_email(f"new{next(_numbers)}@example.org")
    return {
        "account": (request(agent, goal, type="create_account", executor=None), ["account", "owner_only"]),
        "account, by Ember's code": (request(agent, goal, type="create_account"), ["account"]),
        "money": (request(agent, goal, type="spend_money", executor=None), ["money", "owner_only"]),
        "money, by Ember's code": (request(agent, goal, type="spend_money"), ["money"]),
        "first contact": (request(agent, goal, type="contact", executor="email", action=first), ["first_contact"]),
        "first contact, no address": (
            request(agent, goal, type="contact", executor="email", action={"subject": "Hi"}),
            ["first_contact"],
        ),
        "first publication": (
            request(agent, goal, executor="etsy_listing", action={"title": "A5 planner"}),
            ["first_publication"],
        ),
        "community post": (
            request(agent, goal, type="publish", executor="reddit_link", action={"subreddit": "planners"}),
            ["community_post"],
        ),
        "VAT": (request(agent, goal, words="The price now includes VAT."), ["legal"]),
        "Umsatzsteuer": (request(agent, goal, words="Ohne Umsatzsteuer, Kleinunternehmer."), ["legal"]),
        "a contract in a reply": (  # the body is in the request's text too, as the email tool writes it
            request(agent, goal, type="contact", executor="email", action=an_email(WROTE), words="Sign the CONTRACT."),
            ["legal"],
        ),
        "owner only": (request(agent, goal, type="other", executor=None), ["owner_only"]),
    }


def reasons_of(conn: sqlite3.Connection, approval_id: int) -> list[str]:
    return never.reasons(conn, conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())


def carry(agent: Any, approval_id: int) -> str:
    with agent.db.transaction() as conn:
        return policy.apply(conn, agent.scope(), approval_id, agent.clock)


def fits(monkeypatch: pytest.MonkeyPatch, rule: str) -> None:
    """Whatever a request is, the policy engine takes it to fit this rule."""
    monkeypatch.setattr(policy, "match", lambda conn, scope, row: rule)


def a_use(conn: sqlite3.Connection, approval_id: int, grant: Any, now: str) -> None:
    veto = grant["level"] == "veto_window"
    conn.execute(
        "INSERT INTO policy_uses (approval_id, grant_id, level, created_at, veto_until, approved_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (approval_id, grant["id"], grant["level"], now, now if veto else None, None if veto else now),
    )


def approve_as_code(conn: sqlite3.Connection, approval_id: int, now: str) -> None:
    conn.execute(
        "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, version = version + 1 WHERE id = ?",
        (now, policy.POLICY_BY, approval_id),
    )


def test_no_unlock_carries_a_never_request_whatever_the_rule_or_level(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = an_agent(data_dir)
    scope, now = agent.scope(), to_iso(agent.clock.now())
    for level in ("veto_window", "auto"):
        goal = a_milestone(agent, level)
        kinds = never_requests(agent, goal)
        controls = {rule: request(agent, goal) for rule in policy.RULES}  # the same, with nothing NEVER in them
        replies = {
            rule: request(agent, goal, type="contact", executor="email", action=an_email(WROTE))
            for rule in policy.RULES
        }
        with agent.db.connection() as conn:
            for kind, (approval_id, classes) in kinds.items():
                assert reasons_of(conn, approval_id) == classes, kind
            for approval_id in (*controls.values(), *replies.values()):
                assert reasons_of(conn, approval_id) == []
        for rule in policy.RULES:
            fits(monkeypatch, rule)
            for kind, (approval_id, classes) in kinds.items():
                said = carry(agent, approval_id)
                assert said == (
                    f" It waits for your owner whatever they unlocked: never automatic for "
                    f"{'; '.join(never.CLASSES[c] for c in classes)}."
                ), (kind, rule, level)
            for approval_id in (controls[rule], replies[rule]):  # the checks don't stop what they shouldn't
                assert ("approved it at once" if level == "auto" else "unless your owner decides first") in carry(
                    agent, approval_id
                )
        never_ids = ",".join(str(i) for i, _ in kinds.values())
        assert rows(agent, f"SELECT DISTINCT status FROM approvals WHERE id IN ({never_ids})") == [
            {"status": "pending"}
        ]
        assert rows(agent, f"SELECT COUNT(*) AS n FROM policy_uses WHERE approval_id IN ({never_ids})") == [{"n": 0}]
        # The card says why no unlock carries it
        cards = {a["id"]: a for a in agent.dashboard()["approvals"]}
        assert cards[kinds["VAT"][0]]["never"] == [LEGAL]
        assert cards[kinds["first contact"][0]]["never"] == ["a first contact (UWG section 7)"]
        assert cards[controls["qa_fix"]]["never"] == []
        # The database refuses the same on its own: no use of a standing unlock, no approval by Ember's code
        with agent.db.connection() as conn:
            grant = conn.execute(
                "SELECT * FROM policy_grants WHERE milestone_id = ? AND rule = 'deactivate'", (goal,)
            ).fetchone()
        for approval_id, _ in kinds.values():
            with (
                pytest.raises(sqlite3.IntegrityError, match=r"an unlock never carries this request \(NEVER\)"),
                agent.db.transaction() as conn,
            ):
                a_use(conn, approval_id, grant, now)
            with (
                pytest.raises(sqlite3.IntegrityError, match="approvals: (an unlock never approves|no standing unlock)"),
                agent.db.transaction() as conn,
            ):
                approve_as_code(conn, approval_id, now)
    # What Ember's code approved (the auto controls), it approved through an unlock, and none is a NEVER request
    with agent.db.connection() as conn:
        carried = conn.execute(
            "SELECT COUNT(*), SUM(n.never), SUM(EXISTS (SELECT 1 FROM policy_uses u WHERE u.approval_id = a.id))"
            " FROM approvals a JOIN approvals_never n ON n.approval_id = a.id WHERE a.mode = ? AND a.decided_by = ?",
            (scope.mode, policy.POLICY_BY),
        ).fetchone()
    assert tuple(carried) == (2 * len(policy.RULES), 0, 2 * len(policy.RULES))


def test_each_check_of_the_database_holds_on_its_own(data_dir: Path) -> None:
    """Were the check of the uses gone, the check of the approvals would still refuse, and the other way round."""
    agent = an_agent(data_dir)
    goal = a_milestone(agent, "auto")
    vat = request(agent, goal, words="Plus VAT.")
    now = to_iso(agent.clock.now())
    with agent.db.connection() as conn:
        grant = conn.execute(
            "SELECT * FROM policy_grants WHERE milestone_id = ? AND rule = 'qa_fix'", (goal,)
        ).fetchone()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TRIGGER policy_uses_never")
            a_use(conn, vat, grant, now)
            with pytest.raises(sqlite3.IntegrityError, match=r"an unlock never approves this request \(NEVER\)"):
                approve_as_code(conn, vat, now)
        finally:
            conn.execute("ROLLBACK")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TRIGGER approvals_never_on_unlock")
            conn.execute("DROP TRIGGER approvals_unlock_carries")
            with pytest.raises(sqlite3.IntegrityError, match=r"an unlock never carries this request \(NEVER\)"):
                a_use(conn, vat, grant, now)
        finally:
            conn.execute("ROLLBACK")
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {vat}") == [{"status": "pending"}]


def test_an_unlock_taken_back_or_spent_carries_nothing_more(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = an_agent(data_dir)
    goal = a_milestone(agent, "veto_window")
    fits(monkeypatch, "price_change")
    vetoed, held = request(agent, goal), request(agent, goal)
    for approval_id in (vetoed, held):
        assert "unless your owner decides first" in carry(agent, approval_id)
    assert owner(agent).decide(vetoed, {"decision": "reject"}, "Stefan").status == 200
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()  # the veto takes the unlock back first: what it held waits for the owner
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {held}") == [{"status": "pending"}]
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == held)
    assert card["veto_until"] is None  # the card no longer says Ember's code approves it
    now = to_iso(agent.clock.now())
    with agent.db.connection() as conn:
        taken_back = conn.execute(
            "SELECT * FROM policy_grants WHERE milestone_id = ? AND rule = 'price_change' ORDER BY id LIMIT 1", (goal,)
        ).fetchone()
    later = request(agent, goal)
    with (
        pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"),
        agent.db.transaction() as conn,
    ):
        a_use(conn, later, taken_back, now)
    with (
        pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"),
        agent.db.transaction() as conn,
    ):
        approve_as_code(conn, held, now)  # it is held, but by an unlock that no longer stands
    # A budget of one: the code sees it spent, and the database refuses a second use on its own
    spent = a_milestone(agent)
    with agent.db.transaction() as conn:
        policy.set_grant(conn, agent.scope(), spent, "deactivate", "auto", now, budget=1, by="Stefan")
        grant = conn.execute("SELECT * FROM policy_grants WHERE milestone_id = ?", (spent,)).fetchone()
    fits(monkeypatch, "deactivate")
    first, second = request(agent, spent), request(agent, spent)
    assert "approved it at once" in carry(agent, first)
    assert carry(agent, second) == ""
    with pytest.raises(sqlite3.IntegrityError, match="beyond the unlock's budget"), agent.db.transaction() as conn:
        a_use(conn, second, grant, now)
    with (
        pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"),
        agent.db.transaction() as conn,
    ):
        approve_as_code(conn, second, now)  # no use: Ember's code approves nothing
    # A missed milestone's unlock carries nothing new either, before Ember's code takes it back
    missed = a_milestone(agent, "auto")
    late = request(agent, missed)
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = 'missed', closed_at = ?, closed_by = 'code', result = 'x' WHERE id = ?",
            (now, missed),
        )
        grant = conn.execute(
            "SELECT * FROM policy_grants WHERE milestone_id = ? AND rule = 'deactivate'", (missed,)
        ).fetchone()
    assert carry(agent, late) == ""
    with (
        pytest.raises(sqlite3.IntegrityError, match="no standing unlock carries this request"),
        agent.db.transaction() as conn,
    ):
        a_use(conn, late, grant, now)


def test_only_the_owner_unlocks(data_dir: Path) -> None:
    agent = an_agent(data_dir)
    goal = a_milestone(agent)
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        for by in policy.CODE:
            with pytest.raises(ValueError, match="only the owner unlocks"):
                policy.set_grant(conn, scope, goal, "price_change", "auto", now, by=by)
        policy.set_grant(conn, scope, goal, "price_change", "manual", now, by=policy.REVOKED_BY)  # taking back is fine
    for by in policy.CODE:
        with (
            pytest.raises(sqlite3.IntegrityError, match=r"only the owner unlocks \(NEVER\)"),
            agent.db.transaction() as conn,
        ):
            conn.execute(
                "INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by,"
                " created_at) VALUES (?, ?, ?, 'price_change', 'veto_window', 3, 10, ?, ?)",
                (scope.mode, scope.session, goal, by, now),
            )
    # The database names Ember's code as the code does
    with agent.db.connection() as conn:
        triggers = {
            r["name"]: r["sql"]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN"
                " ('approvals_never_on_unlock', 'approvals_unlock_carries', 'policy_grants_owner_unlocks')"
            )
        }
    assert len(triggers) == 3
    for sql in triggers.values():
        named = "(" + ", ".join("'" + by.replace("'", "''") + "'" for by in policy.CODE) + ")"
        assert f"IN {named}" in sql
    # Whatever the owner's name, their own unlock works (a name Ember's code signs with becomes "the owner")
    for who in ("Stefan", policy.REVOKED_BY):
        assert owner(agent).set_autonomy(goal, {"rule": "deactivate", "level": "auto"}, who).status == 200
    assert rows(agent, "SELECT by FROM policy_grants WHERE rule = 'deactivate' ORDER BY id") == [
        {"by": "Stefan"},
        {"by": "the owner"},
    ]


VOCABULARY = (
    "tax", "Tax.", "TAXES", "taxed", "taxonomy_id", "syntax", "Taxi", "_tax_", "tax2", "vat", "VAT-free", "(vat)",
    "vatican", "Umsatzsteuer", "USt-IdNr.", "ust", "MwSt.", "mwst", "Gewerbeanmeldung", "Kleingewerbe", "GEWERBE",
    "Finanzamt", "Kaufvertrag", "VERTRÄGE", "Verträge", "Vertrag", "contract", "Contracts", "contractor",
    "subcontract", "Steuerung", "ÜSTEUER", "planner", "printable", "A5", "ä", "Ö", "invoice", "price", "€", "USD",
)  # fmt: skip


def test_the_database_reads_a_request_as_the_code_does(data_dir: Path) -> None:
    """Random requests of every type, executor and wording, read before and after Ember's first listing went live:
    the code's NEVER classes (never.py) are exactly the database's (the view approvals_never), class by class."""
    agent = an_agent(data_dir)
    goal = a_milestone(agent)
    rng = random.Random(51)  # noqa: S311 - generated requests, not security
    types = ("publish", "contact", "create_account", "spend_money", "sell", "other")
    tos = (WROTE, WROTE.upper(), " " + WROTE, "new@example.org", "", 5, None)
    made = []
    for _ in range(300):
        executor = rng.choice((None, "email", "reddit_link", "etsy_listing", "etsy_edit"))
        action: Any = None
        if executor == "email":
            to = rng.choice(tos)
            action = {"subject": "Hi"} if to is None else {"to": to, "subject": "Hi"}
        elif executor is not None:
            action = rng.choice(({"title": "x"}, ["a", "list"], {"listing_id": 1}))
        words = "".join(
            rng.choice(VOCABULARY) + rng.choice(("", " ", " ", "-", "\n", ", ")) for _ in range(rng.randint(1, 6))
        )
        made.append(request(agent, goal, type=rng.choice(types), executor=executor, action=action, words=words))
    seen: set[str] = set()

    def agree() -> None:
        with agent.db.connection() as conn:
            for approval_id in made:
                row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
                view = conn.execute("SELECT * FROM approvals_never WHERE approval_id = ?", (approval_id,)).fetchone()
                columns = set(view.keys())
                in_db = [k for k in never.CLASSES if k in columns and view[k]]
                assert never.reasons(conn, row) == in_db, dict(row)
                assert bool(view["never"]) == bool(in_db)
                seen.update(in_db)
                seen.update(["none"] if not in_db else [])

    agree()
    with agent.db.transaction() as conn:  # Ember's first listing goes live: no etsy_listing request is first anymore
        scope, now = agent.scope(), to_iso(agent.clock.now())
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id, title)"
            " VALUES (?, ?, ?, ?, ?, 'active', 900000009, 'A5 planner')",
            (scope.mode, scope.session, made[0], now, now),
        )
    agree()
    assert seen == {
        "account",
        "money",
        "first_contact",
        "first_publication",
        "community_post",
        "legal",
        "owner_only",
        "none",
    }


def test_a_price_change_in_words_of_tax_waits_for_the_owner(data_dir: Path) -> None:
    """Through the agent's own tool: an auto unlock carries a small price change, never one whose reason is VAT."""
    agent, listing_id = listed(data_dir)
    due = (agent.clock.today() + timedelta(days=30)).isoformat()
    with agent.db.transaction() as conn:
        goal = roadmap.create(
            conn, agent.scope(), title="Ten sales", measure="10 orders", due=due, now=to_iso(agent.clock.now())
        )
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    old = price_of(agent, listing_id)
    made = work_on(agent, goal, change(listing_id, price=f"{old * Decimal('0.95'):.2f}", reason="Etsy adds VAT now."))
    assert (made[-1]["status"], made[-1]["decided_by"]) == ("pending", None)
    said = tool_results(agent, "propose_etsy_edit")[-1]["result"]
    assert f"It waits for your owner whatever they unlocked: never automatic for {LEGAL}." in said
    assert next(a for a in agent.dashboard()["approvals"] if a["id"] == made[-1]["id"])["never"] == [LEGAL]
