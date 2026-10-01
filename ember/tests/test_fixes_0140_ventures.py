"""0.14.0 (FIX NOW 18, 19; X26, X28): venture gates that can't be gamed, and an expected net that counts the months
before the first sale.

* A demand note's Library source counts only when it isn't removed, is linked to the product line or its venture (or
  is a keyword or market export the owner uploaded as a table), and the note cites a number found in it; a vendor's
  page is no source of demand.
* vendor_only stands until an independent page backs a demand number (searches, sales, orders, reviews, buyers), and
  vendors are matched by their registrable name on any country domain.
* A venture proposed before the gates (no numbers) goes back to researching, and the owner's Back on a proposal that
  has no numbers or a standing knock-out sends it back there first.
* The agent can't start a venture live: only the owner's backing and a met first test make one live.
* In an ordinary cycle that focuses a venture, research counts toward that venture's research budget.
* The expected net counts the fixed costs of the months before the first sale; the first sale is given in days (at
  least MIN_FIRST_SALE_DAYS), and the "slow" knock-out compares those days with half the net runway.
* The cold-outreach words skip what a case rules out, and read German and common English phrasings.
* The desk's "decided in 7 days" counts the agent's and the owner's decisions, not Ember's code's automatic parks.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from app.agent import demand, desk, econ, evidence, knockouts, stages, store, tools, ventures
from app.agent.fake_llm import Fail, FakeTransport, Raw, Reply, ToolCalls
from app.agent.service import Agent
from app.db import Database, discover_migrations, migrate
from app.economy.clock import to_iso
from app.economy.metering import Interrupted
from tests.test_agent import rows
from tests.test_knockouts import case, found, independent
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_research_budget import ORDINARY, research
from tests.test_ventures import (
    CASE,
    DROPSHIPPING,
    ETSY,
    JOURNAL,
    SCORES,
    VENTURING,
    WEBSITE,
    plan,
    tool_results,
)
from tests.test_ventures import found as research_found

SEO_GUIDE = "Etsy search is a powerful tool. Use all 13 tags and write titles buyers search for."
EXPORT = "Keyword;Searches a month;Competition\nNebenkostenabrechnung Vorlage;1.200;450\nMietvertrag;900;2.100\n"


# --- FIX 18a: a demand note's source ---


def product_line(agent: Agent) -> int:
    """Project #1, a product line of the Etsy leg."""
    with agent.db.transaction() as conn:
        now = to_iso(agent.clock.now())
        return store.create_project(
            conn,
            agent.scope(),
            cycle_id=1,
            title="Nebenkosten",
            hypothesis="Landlords buy a statement template.",
            next_step="List one",
            status="active",
            now=now,
            venture_id=ETSY,
        )


def document(agent: Agent, **fields: object) -> int:
    reply = owner(agent).add_document(fields, "Stefan")
    assert reply.status == 201, reply.body
    return int(reply.body["id"])


def problem(agent: Agent, source: str, said: str | None, project_id: int = 1) -> str:
    with agent.db.connection() as conn:
        return demand.source_problem(conn, agent.scope(), source, project_id, said)


def test_a_library_document_backs_a_demand_note_only_when_linked_or_an_export_with_its_number(
    data_dir: Path,
) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    project = product_line(agent)
    guide = document(agent, text=SEO_GUIDE)  # live: a general Etsy guide "backed" a product line
    said = "Landlords search 1,200 times a month for a statement template."
    assert "isn't linked to project #1 or its venture" in problem(agent, f"library #{guide}", said)
    text = SEO_GUIDE + " Statement templates: 850 searches a month in 2026, at 4.99 EUR."
    linked = document(agent, text=text, project_id=project)
    assert "cite a number from library #" in problem(agent, f"library #{linked}", "Landlords must do this by law.")
    assert "cite a number from library #" in problem(agent, f"library #{linked}", said)  # 1,200 isn't in it
    for not_demand in ("A title of 140 characters and all 13 tags.", "Searched a lot in 2026.", "499 sales a month."):
        assert "cite a number from library #" in problem(agent, f"library #{linked}", not_demand), not_demand
    assert problem(agent, f"library #{linked}", "Statement templates: 850 searches a month.") == ""
    upload = {"file_name": "keywords.csv", "file_data": base64.b64encode(EXPORT.encode()).decode()}
    export = document(agent, **upload)  # the owner's keyword export: a table, linked to nothing
    assert problem(agent, f"library #{export}", said) == ""  # 1,200 = 1.200
    of_leg = document(agent, text="Nebenkosten: 1.200 Suchanfragen im Monat.", venture_id=ETSY)
    assert problem(agent, f"library #{of_leg}", said) == ""  # linked to the product line's venture
    assert "cite a number from library #" in problem(agent, f"library #{of_leg}", None)  # a source backs a demand
    assert owner(agent).remove_document(export, "Stefan").status == 200
    assert problem(agent, f"library #{export}", said) == f"there is no document #{export} in your owner's library"
    assert problem(agent, "library #99", said) == "there is no document #99 in your owner's library"


def test_a_comma_export_keeps_its_columns_apart_and_a_count_like_2000_can_be_cited(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    product_line(agent)
    plain = "Keyword,Searches,Competition\nnebenkostenabrechnung vorlage,1200,450\nmietvertrag vorlage,2000,2100\n"
    upload = {"file_name": "keywords.csv", "file_data": base64.b64encode(plain.encode()).decode()}
    export = document(agent, **upload)
    for said in ("Buyers search nebenkostenabrechnung vorlage 1200 times a month.", "1,200 searches a month."):
        assert problem(agent, f"library #{export}", said) == "", said
    assert problem(agent, f"library #{export}", "mietvertrag vorlage: 2000 searches a month.") == ""
    assert "cite a number from library #" in problem(agent, f"library #{export}", "1200450 searches a month.")
    assert "cite a number from library #" in problem(agent, f"library #{export}", "Searched a lot in 2000.")
    tabs = document(agent, file_name="k.tsv", file_data=base64.b64encode(b"kw\tvol\nvorlage\t850\n").decode())
    assert problem(agent, f"library #{tabs}", "850 searches a month.") == ""
    assert demand.numbers("1,200 12.500,00 4.99 1200,450") == {"1200", "12500", "450"}


def test_a_demand_note_needs_an_independent_page_and_a_number(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    product_line(agent)
    vendor, forum = "https://printify.com/blog/best-sellers", "https://example.invalid/forum/planners"
    with agent.db.transaction() as conn:
        evidence.record_sources(conn, agent.scope(), None, None, [vendor, forum], to_iso(agent.clock.now()))
    assert "a vendor's or an affiliate's page" in problem(agent, vendor, "Posters sell 500 a month.")
    assert "give a number" in problem(agent, forum, "Many people buy planners.")
    assert problem(agent, forum, "Top planners show 1,000+ sales.") == ""
    assert "give a number" in problem(agent, forum, None)
    assert "source must be a page from your research results" in problem(agent, "https://x.invalid/y", "1,000 sales")


# --- FIX 18b: vendor_only ---


def claim(agent: Agent, url: str, metric: str, unit: str = "EUR") -> str:
    with agent.db.transaction() as conn:
        now = to_iso(agent.clock.now())
        evidence.record_sources(conn, agent.scope(), None, None, [url], now)
        _, grade = evidence.add(conn, agent.scope(), DROPSHIPPING, None, "A claim.", metric, 1, 2, unit, "DE", url, now)
    return grade


def test_vendors_are_matched_on_any_domain_and_only_demand_lifts_vendor_only(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    case(agent)
    for url in (
        "https://www.shopify.de/blog/dropshipping",
        "https://www.bigbuy.eu/de/dropshipping",
        "https://syncee.com/blog/trends",
        "https://support.finerworks.com/policies",
        "https://www.printful.de/blog",
        "https://printify.co.uk/blog",
    ):
        assert claim(agent, url, "sales") == "marketing", url
    assert found(agent) == [("vendor_only", False)]
    assert claim(agent, "https://example.invalid/a-policy", "policy requirement") == "independent"
    assert claim(agent, "https://www.etsy.com/listing/1/poster", "price") == "independent"
    assert claim(agent, "https://example.invalid/supplier-faq", "minimum order", "units") == "independent"
    for metric in ("average sales price", "sales tax", "minimum orders", "return policy for customers"):
        assert claim(agent, "https://example.invalid/guide", metric) == "independent"
    assert claim(agent, "https://example.invalid/faq", "shipping time to customers", "days") == "independent"
    [vendor_only] = [(k.rule, k.why) for k in check(agent)]
    assert vendor_only[0] == "vendor_only" and "no independent page shows demand" in vendor_only[1]
    assert claim(agent, "https://example.invalid/forum", "monthly searches", "searches") == "independent"
    assert found(agent) == []


def test_a_claim_graded_before_a_vendor_was_known_doesnt_lift_vendor_only(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    case(agent)
    with agent.db.transaction() as conn:  # graded independent by 0.13.0's table: kept as it was
        conn.execute(
            "INSERT INTO evidence (mode, session, venture_id, created_at, claim, metric, low, high, unit, region, url,"
            " source) VALUES (?, ?, ?, ?, 'Stores sell 200.', 'sales', 200, 200, 'orders', 'DE',"
            " 'https://www.bigbuy.eu/de/blog', 'independent')",
            (agent.scope().mode, agent.scope().session, DROPSHIPPING, to_iso(agent.clock.now())),
        )
    assert found(agent) == [("vendor_only", False)]


def check(agent: Agent, net_days: float | None = 100.0) -> list[knockouts.KnockOut]:
    with agent.db.connection() as conn:
        row = ventures.get(conn, agent.scope(), DROPSHIPPING)
        assert row is not None
        return knockouts.check(conn, row, cash_eur=20.0, net_days=net_days)


# --- FIX 18c: a proposal from before the gates ---


def test_a_proposal_from_before_the_gates_goes_back_to_researching(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    every = discover_migrations()
    mine = [m for m in every if m.name == "ventures"][-1]  # 0.14.0's (0.10.0's has the name too)
    migrate(db_file, [m for m in every if m.version < mine.version], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        insert = (
            "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch, stage,"
            " notes, owner_action) VALUES (?, 'live', 0, 1, 'owner', '2026-09-29T10:00:00Z', '2026-09-29T10:00:00Z',"
            " ?, 'p', 'idea', ?, ?)"
        )
        conn.execute(insert, (3, "Dropshipping store", "", "research"))
        conn.execute(insert, (4, "Posters", "Old note.", None))
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'completed', 'schedule', 0, 1)"
        )
        for venture_id in (4,):  # live, #3 had no research call
            for n in (1, 2):
                conn.execute(
                    "INSERT INTO venture_research (venture_id, cycle_id, question, sources, cost_micros, created_at)"
                    " VALUES (?, 1, ?, 1, 10, '2026-09-29T10:00:00Z')",
                    (venture_id, f"q{n}"),
                )
        conn.execute(
            "INSERT INTO venture_cases (venture_id, created_at, channel, price_eur, unit_cost_eur, monthly_costs_eur,"
            " sales_low, sales_mid, sales_high, setup_eur, owner_hours, first_sale_months, api_usd, usd_per_eur,"
            " fees_eur, net_eur, net_low, net_mid, net_high, ev_eur, needs) VALUES (4, 'then', 'other', 10, 9, 100,"
            " 0, 1, 2, 0, 1, 6, 0, 1.1, 0, 1, -100, -99, -98, 0, '')"
        )
        # 0.10.0 proposed #3 before the gates existed: recreate that row
        conn.execute("DROP TRIGGER ventures_proposed_numbers")
        conn.execute("DROP TRIGGER ventures_proposed_researched")
        conn.execute("UPDATE ventures SET stage = 'proposed', proposed_at = updated_at WHERE id IN (3, 4)")
    old.close()
    assert mine.version in migrate(db_file, backup_dir=tmp_path / "backups")
    new = Database(db_file)
    with new.connection() as conn:
        got = {r["id"]: dict(r) for r in conn.execute("SELECT id, stage, notes, owner_action FROM ventures")}
        [ev] = [r[0] for r in conn.execute("SELECT ev_eur FROM venture_cases WHERE venture_id = 4")]
    assert got[3]["stage"] == "researching" and got[3]["owner_action"] == "research"  # the owner's wish stays
    assert got[3]["notes"] == (
        "Back to researching by Ember's code: it was proposed before a business case needed its numbers "
        "(venture_case) and passed the knock-outs."
    )
    assert got[4]["stage"] == "proposed" and got[4]["notes"] == "Old note."  # it has its numbers
    # its expected net counts the six months of fixed costs before its first sale (it showed 0)
    assert ev == pytest.approx(-100.0)
    with new.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="a case never changes"):
        conn.execute("UPDATE venture_cases SET ev_eur = 1")


def test_the_owners_back_sends_a_knocked_out_proposal_back_to_researching_first(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)  # a first cycle, for the research rows to belong to
    now = to_iso(agent.clock.now())
    independent(agent)
    case(agent, setup_eur=50.0)  # more than the owner's EUR 20 for a venture's first test
    with agent.db.transaction() as conn:
        for sources in (1, 2):
            ventures.add_research(conn, DROPSHIPPING, 1, None, "q", None, sources, 10, now)
        ventures.update(
            conn, DROPSHIPPING, now, stage="proposed", proposed_at=now, scores_by="research", **SCORES, **CASE
        )
    stale = {"action": "back", "expected_version": 99}
    assert owner(agent).decide_venture(DROPSHIPPING, stale, "Stefan").status == 409  # nothing changes
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {DROPSHIPPING}")[0]["stage"] == "proposed"
    sent_back = owner(agent).decide_venture(DROPSHIPPING, {"action": "back"}, "Stefan")
    assert sent_back.status == 200 and sent_back.body["stage"] == "researching"
    assert sent_back.body["not_backed"].startswith("it is knocked out (cash beyond the budget: EUR 50 to start")
    [row] = rows(agent, f"SELECT stage, notes, owner_action FROM ventures WHERE id = {DROPSHIPPING}")
    assert row["stage"] == "researching" and row["owner_action"] is None
    assert "Back to researching by Ember's code: it is knocked out (cash beyond the budget: EUR 50" in row["notes"]
    # a second Back from researching is checked too: refused, nothing changes, until the owner confirms it
    refused = owner(agent).decide_venture(DROPSHIPPING, {"action": "back"}, "Stefan")
    assert refused.status == 409 and refused.body["field"] == "confirm"
    assert "wouldn't back venture" in refused.body["error"] and "cash beyond the budget" in refused.body["error"]
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {DROPSHIPPING}")[0]["stage"] == "researching"
    backed = owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "confirm": True}, "Stefan")
    assert backed.status == 200 and backed.body["stage"] == "building"  # the owner's call, knowing it


def test_the_owners_words_with_a_back_that_sends_it_to_researching_are_kept(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    with agent.db.transaction() as conn:  # a proposal without numbers (from before the gates)
        conn.execute("DROP TRIGGER ventures_proposed_numbers")
        conn.execute("DROP TRIGGER ventures_proposed_researched")
        conn.execute(f"UPDATE ventures SET stage = 'proposed' WHERE id = {DROPSHIPPING}")
    [card] = [v for v in agent.ventures()["items"] if v["id"] == DROPSHIPPING]
    assert card["backing_problem"].endswith("as it was proposed before a business case needed them")
    body = {"action": "back", "comment": "Keep the first test under 20 EUR."}
    assert owner(agent).decide_venture(DROPSHIPPING, body, "Stefan").body["stage"] == "researching"
    [row] = rows(agent, f"SELECT notes FROM ventures WHERE id = {DROPSHIPPING}")
    assert "Your owner said with their Back: Keep the first test under 20 EUR." in row["notes"]
    [card] = [v for v in agent.ventures()["items"] if v["id"] == DROPSHIPPING]
    assert card["backing_problem"] == "it has no numbers (venture_case) yet"  # not proposed: no talk of a proposal


def test_the_owners_back_on_a_venture_without_numbers_needs_their_confirmation(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    with agent.db.transaction() as conn:  # dropshipping #3 after the upgrade: researching, with no case
        conn.execute(f"UPDATE ventures SET stage = 'researching' WHERE id = {DROPSHIPPING}")
    [card] = [v for v in agent.ventures()["items"] if v["id"] == DROPSHIPPING]
    assert "no numbers" in card["backing_problem"]
    refused = owner(agent).decide_venture(DROPSHIPPING, {"action": "back"}, "Stefan")
    assert refused.status == 409 and refused.body["field"] == "confirm" and "no numbers" in refused.body["error"]
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {DROPSHIPPING}")[0]["stage"] == "researching"
    backed = owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "confirm": True}, "Stefan")
    assert backed.status == 200 and backed.body["stage"] == "building"


# --- FIX 18d: no venture starts live on the agent's word ---


def test_the_agent_cant_start_a_venture_live(data_dir: Path) -> None:
    assert "live" not in ventures.AGENT_START_STAGES
    [spec] = [d for d in tools.definitions(etsy=True, venture=True) if d["name"] == "venture_create"]
    assert "live" not in spec["input_schema"]["properties"]["stage"]["enum"]
    live = ("venture_create", {"title": "Recruiting for SMEs", "pitch": "Cold-call companies.", "stage": "live"})
    fake = FakeTransport(script=[plan(steps=["add a leg"]), ToolCalls([live]), Reply("Done."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    sale = {"amount": "4.50", "source": "Etsy order 1", "idempotency_key": "a" * 32}
    assert agent.economy.record("revenue", sale, "Stefan").status == 201  # Ember earns somewhere: no matter
    fake.script.extend([plan(steps=["add a leg"]), ToolCalls([live]), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    assert [r["status"] for r in tool_results(agent, "venture_create")] == ["error", "error"]
    assert rows(agent, "SELECT id FROM ventures WHERE title = 'Recruiting for SMEs'") == []
    refused = pytest.raises(sqlite3.IntegrityError, match="only the owner's backing makes a venture live")
    with refused, agent.db.transaction() as conn:
        ventures.create(conn, agent.scope(), title="Live", pitch="p", stage="live", now="t", cycle_id=1)


# --- FIX 18e: research in an ordinary cycle that focuses a venture ---


def test_an_ordinary_cycles_research_counts_toward_its_focus_ventures_budget(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ventures, "RESEARCH_BUDGET_USD", 0.02)  # a found() call costs about $0.013
    failing = Fail(Interrupted("stream broke", partial_usage={"input_tokens": 1_000, "output_tokens": 50}))
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([research("Who sells phone cases?"), research("At what price?"), research("How many?")]),
            research_found("https://example.invalid/sellers"),
            failing,  # charged at its worst case: it counts toward the budget, which is used now
            Reply("Done."),
            JOURNAL,
        ]
    )

    def researching(agent: Agent) -> None:
        with agent.db.transaction() as conn:
            ventures.update(conn, DROPSHIPPING, to_iso(agent.clock.now()), stage="researching")

    agent, _ = run(data_dir, fake, before=researching, settings=ORDINARY)
    assert rows(agent, "SELECT venture FROM cycles") == [{"venture": 0}]  # an ordinary cycle
    results = tool_results(agent, "research")
    assert [r["status"] for r in results] == ["ok", "error", "error"]
    assert "research failed" in results[1]["result"]
    assert f"venture #{DROPSHIPPING} has used its research budget" in results[2]["result"]
    spent = rows(agent, "SELECT venture_id, sources FROM venture_research ORDER BY id")
    assert spent == [{"venture_id": DROPSHIPPING, "sources": 1}, {"venture_id": DROPSHIPPING, "sources": 0}]


def test_a_paid_failure_of_researchs_continuation_counts_toward_the_budget(data_dir: Path) -> None:
    found_it = research_found("https://example.invalid/sellers").response
    paused = Raw({**found_it, "content": found_it["content"][:1], "stop_reason": "pause_turn"})  # a search under way
    failing = Fail(Interrupted("stream broke", partial_usage={"input_tokens": 1_000, "output_tokens": 50}))
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([research("Who sells it?")]),
            paused,
            failing,
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    calls = rows(agent, "SELECT cost_micros FROM llm_calls WHERE purpose = 'research' ORDER BY id")
    assert len(calls) == 2 and calls[1]["cost_micros"] > 0
    [spent] = rows(agent, "SELECT cost_micros FROM venture_research")
    assert spent["cost_micros"] == calls[0]["cost_micros"] + calls[1]["cost_micros"]


def test_research_in_an_ordinary_cycle_for_a_backed_venture_stays_unbound(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([research("Which suppliers ship in 3 days?")]),
            research_found("https://example.invalid/suppliers"),
            Reply("Done."),
            JOURNAL,
        ]
    )

    def backed(agent: Agent) -> None:
        assert owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "confirm": True}, "Stefan").status == 200

    agent, _ = run(data_dir, fake, before=backed, settings=ORDINARY)
    assert [r["status"] for r in tool_results(agent, "research")] == ["ok"]
    assert rows(agent, "SELECT COUNT(*) AS n FROM venture_research") == [{"n": 0}]  # a backed venture has no budget


# --- FIX 19: the expected net and the first sale in days ---


def ev(price: float, cost: float, fixed: float, sales: tuple[int, int, int], days: int, setup: float) -> float:
    return econ.compute(econ.Case("other", price, cost, fixed, sales, setup, 0.0, days, 0.0), 1.10).ev_eur


def test_the_expected_net_counts_the_fixed_costs_before_the_first_sale() -> None:
    month = econ.DAYS_A_MONTH
    # dropshipping: EUR 30, cost 18, Shopify EUR 39 and USD 5 of API a month, first sale in 3 months, EUR 20 to start
    shop = econ.Case("other", 30.0, 18.0, 39.0, (0, 5, 15), 20.0, 0.0, round(3 * month), 5.0)
    assert econ.compute(shop, 1.10).ev_eur == pytest.approx(-7.88, abs=0.1)  # it showed +13.89
    assert ev(10.0, 0.0, 500.0, (0, 1, 2), round(6 * month), 0.0) == pytest.approx(-500.0, abs=0.1)  # not -3.33
    losing = [ev(10.0, 9.0, 100.0, (0, 1, 2), round(m * month), 0.0) for m in (1, 2, 4, 6, 12)]
    assert losing == sorted(losing, reverse=True) and losing[-1] == pytest.approx(-100.0)  # later is never better
    assert ev(10.0, 0.0, 5.0, (1, 2, 3), 0, 0.0) == ev(10.0, 0.0, 5.0, (1, 2, 3), econ.MIN_FIRST_SALE_DAYS, 0.0)
    assert "first sale in 21 days" in econ.compute(shop).text(econ.Case("other", 30, 18, 39, (0, 5, 15), 20, 0, 21, 5))


def test_slow_compares_the_first_sale_in_days_with_half_the_runway(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    independent(agent)
    case(agent, first_sale_days=21)  # about three weeks
    assert found(agent, net_days=45.0) == []  # it used to count as a month: knocked out below 61 days of runway
    [slow] = [k for k in check(agent, net_days=40.0) if k.rule == "slow"]
    assert slow.why == "its first sale in 21 days, half the runway is 20 days"
    case(agent, first_sale_days=0)  # 0 is no loophole: it counts as the minimum
    assert [k.rule for k in check(agent, net_days=20.0)] == ["slow"]
    spec = next(d for d in tools.definitions(etsy=True, venture=True) if d["name"] == "venture_case")
    days = spec["input_schema"]["properties"]["first_sale_days"]
    assert (days["minimum"], days["maximum"]) == (econ.MIN_FIRST_SALE_DAYS, econ.MAX_FIRST_SALE_DAYS)
    assert "first_sale_months" not in spec["input_schema"]["properties"]


# --- X26: cold outreach in words ---


@pytest.mark.parametrize(
    ("words", "cold"),
    [
        ("No cold outreach: advertising emails are illegal in Germany (UWG 7).", False),
        ("We never email businesses; buyers find us on Etsy.", False),
        ("Cold emails are illegal in Germany, so buyers come through Etsy search.", False),
        ("Keine Kaltakquise: Käufer finden uns über Etsy.", False),
        ("Buyers who never asked are not contacted.", False),
        ("Cold outreach: none.", False),
        ("Cold calls are not needed, buyers find us on Etsy.", False),
        ("We rely on Etsy search rather than cold outreach.", False),
        ("Wir werden keine Firmen anschreiben.", False),
        ("Sell phone cases to phone shops.", False),
        ("Contact hiring managers at 30 companies directly by phone.", True),
        ("Reach out to 50 HR managers on LinkedIn.", True),
        ("Writing to shop owners in Berlin.", True),
        ("Firmen per E-Mail anschreiben und Unternehmen kontaktieren.", True),
        ("30 Personaler direkt anrufen.", True),
        ("Email businesses that never asked for it.", True),
        ("Cold emails to shop owners in Berlin", True),
        ("We have no website yet, so we cold call shops.", True),
        ("We have no website yet so we cold call shops.", True),  # a "no" far before doesn't rule it out
        ("Cold emails are illegal in Germany (UWG 7), but we call 30 firms.", True),
        # review: German main clauses, a negation of something else, words after that don't rule it out
        ("Wir rufen Firmen an.", True),
        ("Wir schreiben Unternehmen per E-Mail an.", True),
        ("Wir kontaktieren Firmen direkt.", True),
        ("Without paid ads we cold call 50 shops a day.", True),
        ("Cold calls are not expensive, so we start there.", True),
        ("Cold outreach is not optional for this model.", True),
        ("Cold emails are not illegal if they are B2B, so we send them.", True),
        ("Send emails to 100 companies.", True),
        ("Wir rufen Firmen nicht an.", False),
        ("We will not do any cold outreach.", False),
        ("Wir sprechen über Firmen an der Uni.", False),
        # review round 2: an adverb before whom, a ban said of something else, small words before "kein"
        ("Wir rufen täglich 20 Firmen an.", True),
        ("Dann kontaktieren wir lokale Firmen.", True),
        ("We cold email 200 shops (forbidden for B2C only).", True),
        ("It is illegal to cold call people, so buyers come through Etsy.", False),
        ("Kaltakquise ist bei uns kein Thema.", False),
        ("Kaltakquise ist kein Problem für uns.", True),
    ],
)
def test_cold_outreach_words_read_negations_and_german(words: str, cold: bool) -> None:
    assert bool(knockouts.cold_words(words)) is cold, words


# --- X28: the desk's decisions ---


def test_the_desk_counts_decisions_not_code_parks(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    since = to_iso(agent.clock.now())

    def decided() -> int:
        with agent.db.connection() as conn:
            return desk.decided(conn, agent.scope(), since)

    before = decided()  # the tree was planted just now, with the owner's Fiverr parked
    with agent.db.transaction() as conn:
        for vid in (DROPSHIPPING, WEBSITE):  # two ideas, timed out by code: not decisions
            row = ventures.get(conn, agent.scope(), vid)
            assert row is not None
            stages.park(conn, agent.scope(), row, to_iso(agent.clock.now()), "no one took the idea up")
    assert decided() == before
    assert owner(agent).decide_venture(ETSY, {"action": "park"}, "Stefan").status == 200  # the owner's decision
    assert decided() == before + 1
