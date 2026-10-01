"""The books: balances, spending and the owner's entries.

Which rows count depends on the mode (see :class:`Scope`): the live economy
counts only real rows; a dry run counts real rows plus the simulated ones
(API costs of the fake model and the owner's test money) from the current
dry-run session, so a dry run exercises the whole economy without touching the
live balance.

Sign conventions (amounts in micros): grants, revenue and adjustments add to the
balance; API cost, API cost corrections and expenses subtract. A correction of a
grant, revenue or expense has the original's type and a negative amount. A
negative API cost correction is a refund.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from ..db import Database
from .clock import Clock, to_iso
from .costs import MICROS_PER_USD, micros_to_usd

MODES = ("live", "dry_run")
STARTING_GRANT_KEY = "config:starting-balance"

_BALANCE_SIGN = """CASE type
    WHEN 'owner_grant' THEN amount_micros
    WHEN 'revenue' THEN amount_micros
    WHEN 'adjustment' THEN amount_micros
    WHEN 'api_cost' THEN -amount_micros
    WHEN 'api_cost_correction' THEN -amount_micros
    WHEN 'expense' THEN -amount_micros
END"""
_API_SPEND_TYPES = "('api_cost', 'api_cost_correction')"
# Rows that mean money came in (for leaving the critical state). 0.12.0: a refund of API costs isn't one: it corrects
# what was charged, and it ended a critical state that nothing had changed.
_MONEY_IN = (
    "((type IN ('owner_grant', 'revenue') AND corrects_id IS NULL) OR (type = 'adjustment' AND amount_micros > 0))"
)
# The calls that don't count toward the cycle cap (metering.py says why; 0.15.0: one list for the guard and the books,
# the critic's and the consolidation's are the daily cap's only, as documented).
OUTSIDE_CYCLE_CAP = ("workshop", "review", "study", "consolidate", "critic")
_ENTRY_COLUMNS = (
    "id, ts, occurred_on, type, amount_micros, simulated, source, note, llm_call_id, corrects_id,"
    " orig_amount, orig_currency, fx_rate, created_by, entered_by, project_id, venture_id"
)

_AMOUNT = re.compile(r"^[0-9]{1,6}([.,][0-9]{1,2})?$")
_LOOKS_LIKE_THOUSANDS = re.compile(r"^[0-9]{1,3}[.,][0-9]{3}$")
_FX_RATE = re.compile(r"^[0-9]([.,][0-9]{1,6})?$")
_IDEMPOTENCY_KEY = re.compile(r"^[0-9a-f]{32}$")
_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
FX_MIN = Decimal("0.5")
FX_MAX = Decimal("3.0")
MAX_BACKDATE_DAYS = 366
MAX_NOTE = 500
MAX_SOURCE = 200
LARGE_FLOOR_MICROS = 100 * MICROS_PER_USD
LARGE_FACTOR = 10

CORRECTABLE_TYPES = ("owner_grant", "revenue", "expense")
FIELDS = frozenset(
    {
        "amount",
        "direction",
        "currency",
        "fx_rate",
        "note",
        "source",
        "day",
        "test_money",
        "idempotency_key",
        "confirm_state_change",
        "confirm_large",
        "project_id",
        "venture_id",
    }
)
CORRECTION_FIELDS = frozenset({"amount", "note", "idempotency_key", "confirm_state_change", "confirm_large"})


@dataclass(frozen=True)
class OwnerKind:
    type: str
    directions: dict[str, int] | None  # None: always positive
    required: tuple[str, ...]
    test_money: bool
    attributable: bool = False  # may name the project or venture it belongs to (0.12.0)


OWNER_KINDS: dict[str, OwnerKind] = {
    "grant": OwnerKind("owner_grant", None, (), True),
    "revenue": OwnerKind("revenue", None, ("source",), True, attributable=True),
    "expense": OwnerKind("expense", None, ("note",), True, attributable=True),
    "adjustment": OwnerKind("adjustment", {"add": 1, "subtract": -1}, ("note",), True),
    "api-correction": OwnerKind("api_cost_correction", {"increase": 1, "decrease": -1}, ("note",), False),
}


class EntryError(ValueError):
    """An owner entry is invalid (HTTP 422)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class DuplicateKeyMismatch(ValueError):
    """The idempotency key was used before for a different entry (HTTP 409)."""


class AlreadyRecorded(ValueError):
    """0.12.0: Ember's code recorded this Etsy order's entry from Etsy's numbers before the owner's button did (HTTP
    409): a new key must not record it twice."""

    def __init__(self, entry_id: int) -> None:
        super().__init__(f"Ember's code already recorded this from Etsy's numbers (entry #{entry_id})")
        self.entry_id = entry_id


@dataclass(frozen=True)
class Scope:
    """Which ledger rows belong to a mode's economy.

    ``sim_mark`` is the newest ledger id when the current dry-run session
    started; simulated rows up to it belong to an earlier session and no longer
    count. (An id, not a time: the clock may be set back between sessions.)
    """

    mode: str
    sim_mark: int | None = None

    @property
    def simulated(self) -> bool:
        return self.mode == "dry_run"

    def where(self, alias: str = "") -> tuple[str, tuple[Any, ...]]:
        prefix = f"{alias}." if alias else ""
        if self.mode == "live" or self.sim_mark is None:
            return f"{prefix}simulated = 0", ()
        return f"({prefix}simulated = 0 OR {prefix}id > ?)", (self.sim_mark,)


@dataclass(frozen=True)
class PreparedEntry:
    type: str
    amount_micros: int
    simulated: bool
    source: str | None
    note: str | None
    occurred_on: str
    day_given: bool
    idempotency_key: str
    corrects_id: int | None = None
    orig_amount: str | None = None
    orig_currency: str | None = None
    fx_rate: str | None = None
    entered_by: str | None = None
    project_id: int | None = None  # the project and venture it belongs to (revenue and expenses, 0.12.0)
    venture_id: int | None = None
    created_by: str = "owner"  # or, 0.12.0, 'etsy': revenue, fees and refunds from Etsy's numbers

    @property
    def balance_effect(self) -> int:
        if self.type in ("owner_grant", "revenue", "adjustment"):
            return self.amount_micros
        return -self.amount_micros


def parse_amount(value: Any, field: str = "amount") -> Decimal:
    """An owner-typed amount: text with at most 2 decimals, '.' or ',' as separator."""
    if not isinstance(value, str):
        raise EntryError(field, 'send the amount as text, for example "12.50"')
    text = value.strip()
    if _LOOKS_LIKE_THOUSANDS.match(text):
        raise EntryError(field, "use at most 2 decimals; for one thousand type 1000")
    if not _AMOUNT.match(text):
        raise EntryError(field, "enter an amount like 12.50 (digits, at most 2 decimals)")
    amount = Decimal(text.replace(",", "."))
    if amount == 0:
        raise EntryError(field, "the amount can't be zero")
    return amount


def dollars_to_micros(amount: Decimal) -> int:
    return int((amount * MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def _text(body: dict[str, Any], name: str, limit: int) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EntryError(name, f"the {name} must be text")
    value = value.strip()
    if len(value) > limit:
        raise EntryError(name, f"keep the {name} under {limit} characters")
    if any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 for ch in value):
        raise EntryError(name, f"the {name} must be a single line of text")
    return value


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise EntryError(name, f"{name} must be true or false")
    return value


def _key(body: dict[str, Any]) -> str:
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.match(key):
        raise EntryError("idempotency_key", "missing or malformed request key (32 lowercase hex characters)")
    return key


STATE_SEVERITY = {"unfunded": 1, "critical": 1, "dead": 2}


def confirmations(body: Any) -> tuple[str | None, bool]:
    """(the state change the owner accepted, confirm_large) from a request body.

    ``confirm_state_change`` echoes the ``state_after`` of the question it
    answers ("critical", "unfunded" or "dead"), so a confirmation of "critical"
    can't be used to write an entry that turns out to kill the agent.
    """
    if not isinstance(body, dict):
        return None, False
    accepted = body.get("confirm_state_change")
    if accepted in (None, False):
        accepted = None
    elif accepted not in STATE_SEVERITY:
        raise EntryError("confirm_state_change", 'confirm with the state you accept: "critical", "unfunded" or "dead"')
    return accepted, _flag(body, "confirm_large")


class Books:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # --- balances ---

    def balance(self, scope: Scope) -> int:
        where, params = scope.where()
        with self.db.connection() as conn:
            row = conn.execute(f"SELECT COALESCE(SUM({_BALANCE_SIGN}), 0) FROM ledger WHERE {where}", params).fetchone()
        return int(row[0])

    def pending(self, scope: Scope) -> int:
        """Worst-case estimates reserved by calls still in flight (any day)."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimate_micros), 0) FROM llm_calls WHERE status = 'pending' AND simulated = ?",
                (1 if scope.simulated else 0,),
            ).fetchone()
        return int(row[0])

    def provisional_excess(self, scope: Scope) -> int:
        """How much interrupted calls may have been overcharged (charged estimate minus known floor).

        Death is judged as if that money were still there, so an estimate alone
        can't kill the agent; the budget guard still counts the full charge. A
        refund the owner records after checking the Console (a negative API cost
        correction) settles that uncertainty: it is netted against the excess.
        """
        where, params = scope.where("l")
        plain, plain_params = scope.where()
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(c.cost_micros - c.floor_micros), 0) FROM ledger l"
                " JOIN llm_calls c ON c.id = l.llm_call_id"
                f" WHERE l.type = 'api_cost' AND c.billing_uncertain = 1 AND {where}"
                " AND NOT EXISTS (SELECT 1 FROM ledger k WHERE k.type = 'api_cost_correction'"
                " AND k.llm_call_id = c.id)",
                params,
            ).fetchone()
            refunds = conn.execute(
                "SELECT COALESCE(-SUM(amount_micros), 0) FROM ledger WHERE type = 'api_cost_correction'"
                f" AND amount_micros < 0 AND llm_call_id IS NULL AND {plain}",
                plain_params,
            ).fetchone()
        return max(0, int(row[0]) - int(refunds[0]))

    # --- spending ---

    def api_spend_on(self, scope: Scope, day: date) -> int:
        where, params = scope.where()
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type IN {_API_SPEND_TYPES}"
                f" AND occurred_on = ? AND {where}",
                (day.isoformat(), *params),
            ).fetchone()
        return int(row[0])

    def cap_spend_on(self, scope: Scope, day: date) -> int:
        """API spend that counts toward the daily cap: charges and cost increases, never refunds.

        A refund of earlier overcharges (a negative correction) is money back, not room to spend more today. A call
        whose bill is uncertain (a 5xx before any reply) counts with what it is known to cost, not the worst case it
        was charged (0.12.0): the balance keeps the worst case until the owner corrects it.
        """
        where, params = scope.where()
        joined, joined_params = scope.where("l")
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE (type = 'api_cost'"
                f" OR (type = 'api_cost_correction' AND amount_micros > 0)) AND occurred_on = ? AND {where}",
                (day.isoformat(), *params),
            ).fetchone()
            excess = conn.execute(
                "SELECT COALESCE(SUM(c.cost_micros - c.floor_micros), 0) FROM ledger l"
                " JOIN llm_calls c ON c.id = l.llm_call_id"
                f" WHERE l.type = 'api_cost' AND c.billing_uncertain = 1 AND l.occurred_on = ? AND {joined}"
                " AND NOT EXISTS (SELECT 1 FROM ledger k WHERE k.type = 'api_cost_correction'"
                " AND k.llm_call_id = c.id)",
                (day.isoformat(), *joined_params),
            ).fetchone()
        return int(row[0]) - int(excess[0])

    def api_spend_between(self, scope: Scope, start: datetime, end: datetime, after_id: int = 0) -> int:
        """API spend in [start, end] (and after ledger row ``after_id``): the costs charged then, and the corrections of
        those days, by the day they correct (0.12.0: by the time they were recorded, so a refund of old charges wiped
        out the last days' spending and the runway jumped from 1.3 to 249 days)."""
        where, params = scope.where()
        first_day = self.clock.local_day(to_iso(start)).isoformat()
        last_day = self.clock.local_day(to_iso(end)).isoformat()
        with self.db.connection() as conn:
            charged = conn.execute(
                "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'api_cost'"
                f" AND ts >= ? AND ts <= ? AND id > ? AND {where}",
                (to_iso(start), to_iso(end), after_id, *params),
            ).fetchone()[0]
            corrected = conn.execute(
                "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'api_cost_correction'"
                f" AND occurred_on >= ? AND occurred_on <= ? AND ts <= ? AND id > ? AND {where}",
                (first_day, last_day, to_iso(end), after_id, *params),
            ).fetchone()[0]
        return int(charged) + int(corrected)

    def net_revenue_between(self, scope: Scope, start: datetime, end: datetime) -> int:
        """Revenue less expenses (their corrections included) on the owner's days from ``start`` to ``end`` (0.12.0: the
        money goal Ember's code keeps on the roadmap)."""
        where, params = scope.where()
        first_day = self.clock.local_day(to_iso(start)).isoformat()
        last_day = self.clock.local_day(to_iso(end)).isoformat()
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE type WHEN 'revenue' THEN amount_micros ELSE -amount_micros END), 0)"
                " FROM ledger WHERE type IN ('revenue', 'expense') AND occurred_on >= ? AND occurred_on <= ?"
                f" AND {where}",
                (first_day, last_day, *params),
            ).fetchone()
        return int(row[0])

    def first_api_cost_after(self, scope: Scope, mark: int) -> str | None:
        """When the first API cost after ledger row ``mark`` was recorded."""
        where, params = scope.where()
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT MIN(ts) FROM ledger WHERE type = 'api_cost' AND id > ? AND {where}", (mark, *params)
            ).fetchone()
        return row[0]

    def last_id(self) -> int:
        """The newest ledger row id: a mark to find what was recorded after this moment."""
        with self.db.connection() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM ledger").fetchone()[0])

    def money_in_after(self, scope: Scope, mark: int) -> bool:
        """Did money come in (a grant, revenue or a positive adjustment) after ledger row ``mark``?"""
        where, params = scope.where()
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT 1 FROM ledger WHERE {_MONEY_IN} AND id > ? AND {where} LIMIT 1", (mark, *params)
            ).fetchone()
        return row is not None

    def latest_grant_after(self, scope: Scope, mark: int) -> sqlite3.Row | None:
        """The newest owner grant (not a correction) recorded after ledger row ``mark``."""
        where, params = scope.where()
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT id, amount_micros FROM ledger WHERE type = 'owner_grant' AND corrects_id IS NULL"
                f" AND id > ? AND {where} ORDER BY id DESC LIMIT 1",
                (mark, *params),
            ).fetchone()

    def cycle_spend(self, cycle_id: int, outside_cap: bool = True, every_purpose: bool = False) -> tuple[int, int]:
        """(charged, reserved-and-pending) micros of one cycle; for the cycle cap without the calls that don't count
        toward it (OUTSIDE_CYCLE_CAP: ``outside_cap=False``; 0.15.0: with them all in a maintenance cycle,
        ``every_purpose``), and then with what an uncertain call is known to cost rather than its worst case (0.12.0,
        as ``cap_spend_on``)."""
        outside = ", ".join(f"'{purpose}'" for purpose in OUTSIDE_CYCLE_CAP)
        workshop = "" if outside_cap or every_purpose else f" AND purpose NOT IN ({outside})"
        charged = "cost_micros" if outside_cap else "floor_micros"
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(CASE WHEN status IN ('ok', 'interrupted') THEN {charged} ELSE 0 END), 0),"
                " COALESCE(SUM(CASE WHEN status = 'pending' THEN estimate_micros ELSE 0 END), 0)"
                f" FROM llm_calls WHERE cycle_id = ?{workshop}",
                (cycle_id,),
            ).fetchone()
        return int(row[0]), int(row[1])

    # --- reporting ---

    def totals(self, scope: Scope, since: str | None = None, until: str | None = None) -> dict[str, int]:
        where, params = scope.where()
        clauses, extra = [where], list(params)
        if since:
            clauses.append("ts >= ?")
            extra.append(since)
        if until:
            clauses.append("ts <= ?")
            extra.append(until)
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT type, COALESCE(SUM(amount_micros), 0) FROM ledger WHERE {' AND '.join(clauses)} GROUP BY type",
                extra,
            ).fetchall()
        totals = {row[0]: int(row[1]) for row in rows}
        return {
            "api_cost": totals.get("api_cost", 0) + totals.get("api_cost_correction", 0),
            "expense": totals.get("expense", 0),
            "revenue": totals.get("revenue", 0),
            "grant": totals.get("owner_grant", 0),
            "adjustment": totals.get("adjustment", 0),
        }

    def day_series(self, scope: Scope, days: int = 30) -> list[dict[str, Any]]:
        """Per owner-local day: flows and the balance at the end of the day, oldest first."""
        today = self.clock.today()
        first = today - timedelta(days=days - 1)
        where, params = scope.where()
        with self.db.connection() as conn:
            before = conn.execute(
                f"SELECT COALESCE(SUM({_BALANCE_SIGN}), 0) FROM ledger WHERE occurred_on < ? AND {where}",
                (first.isoformat(), *params),
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT occurred_on, type, SUM(amount_micros) FROM ledger WHERE occurred_on >= ? AND {where}"
                " GROUP BY occurred_on, type",
                (first.isoformat(), *params),
            ).fetchall()
        flows: dict[str, dict[str, int]] = {}
        for day_text, kind, amount in rows:
            flows.setdefault(day_text, {})[kind] = int(amount)
        balance = int(before)
        series = []
        for offset in range(days):
            day = (first + timedelta(days=offset)).isoformat()
            f = flows.get(day, {})
            api = f.get("api_cost", 0) + f.get("api_cost_correction", 0)
            balance += (
                f.get("owner_grant", 0) + f.get("revenue", 0) + f.get("adjustment", 0) - api - f.get("expense", 0)
            )
            series.append(
                {
                    "date": day,
                    "api_cost_usd": micros_to_usd(api),
                    "expense_usd": micros_to_usd(f.get("expense", 0)),
                    "revenue_usd": micros_to_usd(f.get("revenue", 0)),
                    "grant_usd": micros_to_usd(f.get("owner_grant", 0)),
                    "adjustment_usd": micros_to_usd(f.get("adjustment", 0)),
                    "balance_usd": micros_to_usd(balance),
                }
            )
        return series

    def entries(self, scope: Scope, limit: int = 50, before: int | None = None) -> list[dict[str, Any]]:
        """The newest ``limit`` entries, or those older than entry ``before`` (0.12.0: the tab showed only 20)."""
        where, params = scope.where()
        older = " AND id < ?" if before is not None else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger WHERE {where}{older} ORDER BY id DESC LIMIT ?",
                (*params, *((before,) if before is not None else ()), limit),
            ).fetchall()
            corrections = self._corrections(conn, [row["id"] for row in rows])
        return [self._entry_json(row, corrections.get(row["id"], 0)) for row in rows]

    def entry(self, entry_id: int) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(f"SELECT {_ENTRY_COLUMNS} FROM ledger WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                return None
            return self._entry_json(row, self._corrections(conn, [entry_id]).get(entry_id, 0))

    @staticmethod
    def _corrections(conn: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT corrects_id, SUM(amount_micros) FROM ledger WHERE corrects_id IN ({marks}) GROUP BY corrects_id",
            ids,
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    @staticmethod
    def _entry_json(row: sqlite3.Row, corrected: int) -> dict[str, Any]:
        correctable = row["type"] in CORRECTABLE_TYPES and row["corrects_id"] is None
        remaining = row["amount_micros"] + corrected
        return {
            "id": row["id"],
            "ts": row["ts"],
            "occurred_on": row["occurred_on"],
            "type": row["type"],
            "amount_usd": micros_to_usd(row["amount_micros"]),
            "simulated": bool(row["simulated"]),
            "source": row["source"],
            "note": row["note"],
            "llm_call_id": row["llm_call_id"],
            "corrects_id": row["corrects_id"],
            "corrected_usd": micros_to_usd(corrected),
            "remaining_usd": micros_to_usd(remaining) if correctable else None,
            "orig_amount": row["orig_amount"],
            "orig_currency": row["orig_currency"],
            "fx_rate": row["fx_rate"],
            "created_by": row["created_by"],
            "entered_by": row["entered_by"],
            "project_id": row["project_id"],
            "venture_id": row["venture_id"],
            "can_correct": correctable and remaining > 0,
        }

    def typical_amount(self, ledger_type: str, simulated: bool = False) -> int | None:
        """Median of the owner's earlier entries of this type, real or test money (for the 'unusually large' check)."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT ABS(amount_micros) FROM ledger WHERE type = ? AND created_by = 'owner'"
                " AND corrects_id IS NULL AND idempotency_key <> ? AND simulated = ?",
                (ledger_type, STARTING_GRANT_KEY, 1 if simulated else 0),
            ).fetchall()
        values = [int(row[0]) for row in rows]
        return int(statistics.median(values)) if values else None

    def unusually_large(self, prepared: PreparedEntry) -> tuple[bool, int | None]:
        """(is it far above what the owner usually enters, the typical amount)."""
        typical = self.typical_amount(prepared.type, prepared.simulated)
        threshold = max(LARGE_FLOOR_MICROS, LARGE_FACTOR * (typical or 0))
        return abs(prepared.amount_micros) > threshold, typical

    # --- validating owner entries ---

    def prepare(self, kind: str, body: Any, mode: str, entered_by: str | None = None) -> PreparedEntry:
        """Validate a new owner entry from a JSON body. Raises EntryError."""
        spec = OWNER_KINDS.get(kind)
        if spec is None:
            raise EntryError("kind", "unknown kind of entry")
        if not isinstance(body, dict):
            raise EntryError("body", "send a JSON object")
        unknown = sorted(set(body) - FIELDS)
        if unknown:
            raise EntryError(unknown[0], f"unknown field {unknown[0]!r}")
        amount = parse_amount(body.get("amount"))
        sign = 1
        if spec.directions is not None:
            direction = body.get("direction")
            if not isinstance(direction, str) or direction not in spec.directions:
                raise EntryError("direction", f"choose {' or '.join(repr(d) for d in spec.directions)}")
            sign = spec.directions[direction]
        elif body.get("direction") not in (None, ""):
            raise EntryError("direction", "this kind of entry has no direction")

        currency = body.get("currency") or "USD"
        orig_amount = orig_currency = fx_text = None
        if currency == "EUR":
            rate = self._fx_rate(body.get("fx_rate"))
            usd = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if usd == 0:
                raise EntryError("amount", "the amount is too small")
            orig_amount, orig_currency, fx_text = f"{amount:.2f}", "EUR", decimal_text(rate)
            amount = usd
        elif currency != "USD":
            raise EntryError("currency", "choose USD or EUR")
        elif body.get("fx_rate") not in (None, ""):
            raise EntryError("fx_rate", "an exchange rate is only used for EUR")

        note = _text(body, "note", MAX_NOTE)
        source = _text(body, "source", MAX_SOURCE)
        values = {"note": note, "source": source}
        for name in spec.required:
            if not values[name]:
                raise EntryError(name, f"please fill in the {name}")

        test_money = _flag(body, "test_money")
        if test_money:
            if not spec.test_money:
                raise EntryError("test_money", "this kind of entry can't be test money")
            if mode != "dry_run":
                raise EntryError("test_money", "test money only exists in dry run")

        day, day_given = self._day(body.get("day"))
        project_id, venture_id = self._attribution(body, spec, mode)
        micros = sign * dollars_to_micros(amount)
        if spec.type == "api_cost_correction" and micros < 0:
            # A refund corrects a day's recorded cost; it can't turn a day's API spend negative.
            recorded = self.api_spend_on(Scope("live"), day)
            if -micros > recorded:
                raise EntryError(
                    "amount",
                    f"the API cost recorded for {day.isoformat()} is only ${micros_to_usd(max(recorded, 0)):.2f};"
                    " choose the day the cost was charged, or use Other correction",
                )
        return PreparedEntry(
            type=spec.type,
            amount_micros=micros,
            simulated=test_money,
            source=source or None,
            note=note or None,
            occurred_on=day.isoformat(),
            day_given=day_given,
            idempotency_key=_key(body),
            orig_amount=orig_amount,
            orig_currency=orig_currency,
            fx_rate=fx_text,
            entered_by=entered_by,
            project_id=project_id,
            venture_id=venture_id,
        )

    def _attribution(self, body: dict[str, Any], spec: OwnerKind, mode: str) -> tuple[int | None, int | None]:
        """The project and venture a revenue or expense belongs to (0.12.0): a project brings its venture along."""
        ids: dict[str, int | None] = {}
        for name in ("project_id", "venture_id"):
            value = body.get(name)
            if value in (None, ""):
                ids[name] = None
            elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise EntryError(name, f"{name} must be the number of a {name[:-3]}")
            elif not spec.attributable:
                raise EntryError(name, "only revenue and expenses belong to a project or venture")
            else:
                ids[name] = value
        project_id, venture_id = ids["project_id"], ids["venture_id"]
        with self.db.connection() as conn:
            if project_id is not None:
                project = conn.execute(
                    "SELECT venture_id FROM projects WHERE id = ? AND mode = ?", (project_id, mode)
                ).fetchone()
                if project is None:
                    raise EntryError("project_id", f"there is no project #{project_id}")
                if venture_id is None:
                    venture_id = project["venture_id"]
                elif project["venture_id"] not in (None, venture_id):
                    raise EntryError("venture_id", f"project #{project_id} belongs to venture #{project['venture_id']}")
            if venture_id is not None and ids["venture_id"] is not None:
                found = conn.execute("SELECT 1 FROM ventures WHERE id = ? AND mode = ?", (venture_id, mode)).fetchone()
                if found is None:
                    raise EntryError("venture_id", f"there is no venture #{venture_id}")
        return project_id, venture_id

    def prepare_correction(
        self, target_id: int, body: Any, scope: Scope, entered_by: str | None = None
    ) -> PreparedEntry:
        """Validate a correction of an earlier grant, revenue or expense. Raises EntryError."""
        if not isinstance(body, dict):
            raise EntryError("body", "send a JSON object")
        unknown = sorted(set(body) - CORRECTION_FIELDS)
        if unknown:
            raise EntryError(unknown[0], f"unknown field {unknown[0]!r}")
        where, params = scope.where()
        with self.db.connection() as conn:
            target = conn.execute(
                "SELECT id, type, amount_micros, simulated, occurred_on, corrects_id, project_id, venture_id"
                f" FROM ledger WHERE id = ? AND {where}",
                (target_id, *params),
            ).fetchone()
            corrected = self._corrections(conn, [target_id]).get(target_id, 0)
        if target is None:
            raise EntryError("id", "no such entry")
        if target["type"] not in CORRECTABLE_TYPES or target["corrects_id"] is not None:
            raise EntryError("id", "only grants, revenue and expenses can be corrected this way")
        amount = dollars_to_micros(parse_amount(body.get("amount")))
        remaining = target["amount_micros"] + corrected
        key_used = isinstance(body.get("idempotency_key"), str) and self._key_used(body["idempotency_key"])
        if amount > remaining and not key_used:  # a retry is compared with what was stored, not re-validated
            raise EntryError("amount", f"at most {micros_to_usd(remaining):.2f} USD of this entry is left to correct")
        note = _text(body, "note", MAX_NOTE)
        if not note:
            raise EntryError("note", "please say why this entry is corrected")
        return PreparedEntry(
            type=target["type"],
            amount_micros=-amount,
            simulated=bool(target["simulated"]),
            source=None,
            note=note,
            occurred_on=target["occurred_on"],
            day_given=True,
            idempotency_key=_key(body),
            corrects_id=target["id"],
            entered_by=entered_by,
            project_id=target["project_id"],  # a correction belongs where the entry it corrects belongs
            venture_id=target["venture_id"],
        )

    def _key_used(self, key: str) -> bool:
        with self.db.connection() as conn:
            return conn.execute("SELECT 1 FROM ledger WHERE idempotency_key = ?", (key,)).fetchone() is not None

    @staticmethod
    def _fx_rate(value: Any) -> Decimal:
        if not isinstance(value, str) or not _FX_RATE.match(value.strip()):
            raise EntryError("fx_rate", "enter the exchange rate as USD per 1 EUR, for example 1.08")
        rate = Decimal(value.strip().replace(",", "."))
        if not FX_MIN <= rate <= FX_MAX:
            raise EntryError("fx_rate", f"the rate must be between {FX_MIN} and {FX_MAX} USD per EUR")
        return rate

    def _day(self, value: Any) -> tuple[date, bool]:
        today = self.clock.today()
        if value in (None, ""):
            return today, False
        if not isinstance(value, str) or not _DAY.match(value):
            raise EntryError("day", "use the format YYYY-MM-DD")
        try:
            day = date.fromisoformat(value)
        except ValueError as exc:
            raise EntryError("day", "not a valid date") from exc
        if day > today:
            raise EntryError("day", "the day can't be in the future")
        if day < today - timedelta(days=MAX_BACKDATE_DAYS):
            raise EntryError("day", f"the day can't be more than {MAX_BACKDATE_DAYS} days ago")
        return day, True

    # --- writing ---

    def existing(self, prepared: PreparedEntry) -> dict[str, Any] | None:
        """The entry already stored under this key, or None. Raises DuplicateKeyMismatch (or AlreadyRecorded)."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id, type, amount_micros, simulated, source, note, occurred_on, corrects_id, orig_amount,"
                " orig_currency, fx_rate, project_id, venture_id, created_by FROM ledger WHERE idempotency_key = ?",
                (prepared.idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["created_by"] == "etsy" and prepared.created_by != "etsy":  # 0.12.0: an Etsy order's button, too late
            raise AlreadyRecorded(row["id"])
        same = (
            row["type"] == prepared.type
            and row["amount_micros"] == prepared.amount_micros
            and bool(row["simulated"]) == prepared.simulated
            and row["source"] == prepared.source
            and row["note"] == prepared.note
            and row["corrects_id"] == prepared.corrects_id
            and row["orig_amount"] == prepared.orig_amount
            and row["orig_currency"] == prepared.orig_currency
            and row["fx_rate"] == prepared.fx_rate
            and row["project_id"] == prepared.project_id
            and row["venture_id"] == prepared.venture_id
            # Without an explicit day, a retry after midnight is still the same entry.
            and (not prepared.day_given or row["occurred_on"] == prepared.occurred_on)
        )
        if not same:
            raise DuplicateKeyMismatch("this request key was already used for a different entry")
        return self.entry(row["id"])

    def insert(self, conn: sqlite3.Connection, prepared: PreparedEntry) -> int:
        """Store an owner entry (or, 0.12.0, one from Etsy's numbers); call inside a transaction."""
        cursor = conn.execute(
            "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, source, note, corrects_id,"
            " orig_amount, orig_currency, fx_rate, created_by, entered_by, idempotency_key, project_id, venture_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(self.clock.now()),
                prepared.occurred_on,
                prepared.type,
                prepared.amount_micros,
                1 if prepared.simulated else 0,
                prepared.source,
                prepared.note,
                prepared.corrects_id,
                prepared.orig_amount,
                prepared.orig_currency,
                prepared.fx_rate,
                prepared.created_by,
                prepared.entered_by,
                prepared.idempotency_key,
                prepared.project_id,
                prepared.venture_id,
            ),
        )
        return int(cursor.lastrowid)

    def record_starting_grant(self, amount_usd: float) -> int | None:
        """The owner's starting balance from the options, recorded exactly once. Returns the new row id."""
        amount = int((Decimal(str(amount_usd)) * MICROS_PER_USD).to_integral_value(rounding=ROUND_DOWN))
        if amount <= 0:
            return None
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM ledger WHERE idempotency_key = ?", (STARTING_GRANT_KEY,)).fetchone():
                return None
            cursor = conn.execute(
                "INSERT INTO ledger (ts, occurred_on, type, amount_micros, source, note, created_by, idempotency_key)"
                " VALUES (?, ?, 'owner_grant', ?, 'config', 'Starting balance', 'owner', ?)",
                (to_iso(self.clock.now()), self.clock.today().isoformat(), amount, STARTING_GRANT_KEY),
            )
            return int(cursor.lastrowid)


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." in text else text + ".0"
