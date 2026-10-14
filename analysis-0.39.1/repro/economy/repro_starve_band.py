"""ECON: an outside entry that leaves 0 < balance < the last will's cost kills the agent at its next wake, without its
last will and without the owner's decision (the guard keeps the reserve only against its own calls).

Run from the repository's ember/ folder with PYTHONPATH=. (see ../README.md).
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.ledger import PreparedEntry
from app.economy.metering import CallRefused
from app.economy.pricing import last_will_reserve
from tests.economy_helpers import FakeClock, ScriptedTransport, close_databases, make_economy, request


def fee_entry(economy, micros: int) -> PreparedEntry:
    """An Etsy listing fee as Ember's code records it (integrations/etsy_revenue.py: a fact)."""
    return PreparedEntry(
        type="expense",
        amount_micros=micros,
        simulated=False,
        source="Etsy listing 1",
        note="Etsy listing fee",
        occurred_on=economy.clock.today().isoformat(),
        day_given=True,
        idempotency_key=uuid.uuid4().hex,
        created_by="etsy",
    )


def scenario(leave_micros: int) -> None:
    root = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
    settings = Settings(dry_run=False, starting_balance_usd=1.0, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
    economy = make_economy(root, settings, FakeClock())
    model = economy.metered(ScriptedTransport(simulated=False, tokens=1_000))
    reserve = last_will_reserve(settings, economy.db, "live")
    # one ordinary cycle: the agent has spent money in this life
    cycle = model.open_cycle("schedule")
    model.call(cycle, "plan", request(max_tokens=1_200))
    model.close_cycle(cycle)
    before = economy.status()
    fee = before.balance - leave_micros
    economy.record_integration(fee_entry(economy, fee), fact=True)
    after = economy.status()
    print(f"\n-- an Etsy fact of ${micros_to_usd(fee):.4f} leaves ${micros_to_usd(after.balance):.4f}"
          f" (last-will reserve ${micros_to_usd(reserve):.4f})")
    print(f"   state after the entry: {after.state!r} ({after.reason}); paused={economy.life.flag('control.paused')}")
    if not after.can_run:
        return
    # the next wake: its plan, then (if due) the last will, as agent/service.py decides them
    cycle = model.open_cycle("schedule")
    try:
        model.call(cycle, "plan", request(max_tokens=1_200))
    except CallRefused as exc:
        print(f"   plan refused: {exc.reason} -> state {exc.state!r}")
    model.close_cycle(cycle, "refused")
    status = economy.status()
    print(f"   after the plan: state={status.state!r}, last_will_due={status.last_will_due}")
    if status.last_will_due:
        cycle = model.open_cycle("last_will")
        try:
            model.call(cycle, "last_will", request(max_tokens=1_000))
            print("   last will written")
        except CallRefused as exc:
            print(f"   last will refused: {exc.reason} -> state {exc.state!r}")
        model.close_cycle(cycle, "refused")
    final = economy.status()
    print(f"   final: state={final.state!r} ({final.reason}); last will at {final.last_will_at}")
    close_databases()


if __name__ == "__main__":
    scenario(-10_000)  # control: the fee leaves -$0.01 -> paused for the owner (0.15.0 promise)
    scenario(10_000)   # the band: the fee leaves +$0.01 -> no pause; dead at the next wake, no will


def owner_variant() -> None:
    """The owner's own expense leaving +$0.01: is the owner asked to confirm a death?"""
    root = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
    settings = Settings(dry_run=False, starting_balance_usd=1.0, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
    economy = make_economy(root, settings, FakeClock())
    model = economy.metered(ScriptedTransport(simulated=False, tokens=1_000))
    cycle = model.open_cycle("schedule")
    model.call(cycle, "plan", request(max_tokens=1_200))
    model.close_cycle(cycle)
    balance = economy.status().balance
    for amount in ("0.99", "0.98"):
        body = {"amount": amount, "note": "domain", "idempotency_key": uuid.uuid4().hex}
        reply = economy.record("expense", body)
        print(f"\n-- balance ${balance / 1e6:.4f}; owner expense of ${amount} (no confirmation sent): HTTP {reply.status},"
              f" code={reply.body.get('code')}, state_after={reply.body.get('state_after')},"
              f" economy={reply.body.get('economy')}")
    close_databases()


if __name__ == "__main__":
    owner_variant()


def model_change_variant() -> None:
    """The owner moves the worker to claude-opus-5-5 (an options change, so a restart): the last will's reserve grows
    5x; an agent between the old and the new reserve dies at its next wake without its will, never warned."""
    from tests.economy_helpers import restart

    root = Path(tempfile.mkdtemp(dir=sys.argv[1] if len(sys.argv) > 1 else None))
    sonnet = Settings(dry_run=False, starting_balance_usd=0.11, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
    economy = make_economy(root, sonnet, FakeClock())
    model = economy.metered(ScriptedTransport(simulated=False, tokens=1_000))
    cycle = model.open_cycle("schedule")
    model.call(cycle, "plan", request(max_tokens=1_200))
    model.close_cycle(cycle)
    opus = sonnet.model_copy(update={"worker_model": "claude-opus-5-5"})
    print(f"\n-- balance ${economy.status().balance / 1e6:.4f}; reserve with a Sonnet worker"
          f" ${last_will_reserve(sonnet, economy.db, 'live') / 1e6:.4f}, with an Opus worker"
          f" ${last_will_reserve(opus, economy.db, 'live') / 1e6:.4f}")
    economy = restart(economy, opus)
    print(f"   after the restart: state={economy.status().state!r}, warnings={economy.warnings()}")
    model = economy.metered(ScriptedTransport(simulated=False, tokens=1_000))
    cycle = model.open_cycle("schedule")
    try:
        model.call(cycle, "plan", request(max_tokens=1_200))
    except CallRefused as exc:
        print(f"   plan refused: {exc.reason} -> {exc.state!r}")
    model.close_cycle(cycle, "refused")
    cycle = model.open_cycle("last_will")
    will = economy.metered(ScriptedTransport(simulated=False, tokens=4_400))  # the will's prompt as counted (~4,061 rough)
    try:
        will.call(cycle, "last_will", request(max_tokens=5_000, model="claude-opus-5-5"))
        print("   last will sent")
    except CallRefused as exc:
        print(f"   last will refused: {exc.reason} -> {exc.state!r}")
    print(f"   final: {economy.status().state!r} ({economy.status().reason}); last will at {economy.status().last_will_at}")
    close_databases()


if __name__ == "__main__":
    model_change_variant()
