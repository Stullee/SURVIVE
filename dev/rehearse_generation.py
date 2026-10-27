"""A dress rehearsal of Ember's generation change on a copy of a live database, offline (0.53.0, evaluation 2026-10-27,
C7; vision/generations.md, "The safety net").

    cd ember && python ../dev/rehearse_generation.py /path/to/ember.db [--prepare] [--out DIR]

The database is copied into a folder of its own (``--out``, else a new temporary one) and opened there by a live-mode
Ember without an API key and without any platform's credentials: no model is called, nothing reaches Etsy, Pinterest,
Bluesky, Printify, the mailbox or the website, and the original is never written. An evaluation export's ember.db
(masked) does, as does a copy of /data/ember.db.

It reports what the change waits for (generation.checks). With ``--prepare`` it plays the owner's part on the copy: each
request waiting for their decision rejected and each approved one marked done ("rehearsal"), each product of the
hand-over given the verdict Ember's code suggests (a retire as passive: offline nothing can be taken down). Then it
begins the next generation and reports what moved: the life, the mind's session, the old mind's work closed, the
products handed on, the new mind's first plan context, and the world's rows, which a change must leave as they were.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

EMBER = Path(__file__).resolve().parent.parent / "ember"


def _world_counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    found = {}
    for table in tables:
        try:
            found[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE mode = 'live'").fetchone()[0])  # noqa: S608
        except sqlite3.OperationalError:
            continue
    return found


def _print_checks(title: str, found: list[Any]) -> bool:
    print(f"\n{title}")
    for check in found:
        print(f"  [{'ok' if check.ok else 'waits'}] {check.name}{'' if check.ok else ': ' + check.detail}")
    return all(check.ok for check in found)


def main() -> int:  # noqa: C901 - one report, in order
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", type=Path, help="a copy of Ember's ember.db (it is copied again, never written)")
    parser.add_argument("--prepare", action="store_true", help="play the owner's part on the copy, then begin")
    parser.add_argument("--out", type=Path, help="the folder to rehearse in (default: a new temporary one)")
    args = parser.parse_args()
    source = args.database.resolve()
    if not source.is_file():
        print(f"no database at {source}", file=sys.stderr)
        return 2
    out = (args.out or Path(tempfile.mkdtemp(prefix="ember-rehearsal-"))).resolve()
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    copy = data / "ember.db"
    shutil.copyfile(source, copy)
    os.environ["EMBER_DATA_DIR"] = str(data)
    os.environ["EMBER_SCHEDULER"] = "off"
    sys.path.insert(0, str(EMBER))

    from app import db as dbmod
    from app.agent import estate, generation, store
    from app.agent.owner import Owner
    from app.agent.service import Agent
    from app.config import LoadedSettings, Settings
    from app.db import Database
    from app.economy.metering import OfflineTransport
    from app.economy.service import Economy

    dbmod.migrate(copy)
    database = Database(copy)
    loaded = LoadedSettings(Settings(dry_run=False))
    economy = Economy(database, loaded)
    economy.start()
    agent = Agent(database, loaded, economy, transport=OfflineTransport(simulated=False), cycles_enabled=False)
    agent.recover()
    scope = agent.scope()
    print(f"Rehearsing in {out} (a copy of {source}): mode {scope.mode}, mind {scope.session}, world {scope.world}")
    with database.connection() as conn:
        world_before = _world_counts(conn, store.WORLD_TABLES)
    with database.transaction() as conn:
        estate.refresh(conn, scope, agent.clock)
        found = generation.checks(conn, scope, running=False)
        products = estate.handover(conn, scope)
    print(f"\nThe hand-over: {len(products)} products")
    for row in products:
        title = row["title"][:60]
        print(f"  {row['kind']} {row['ref']}: {title!r} suggested {row['suggested']} ({row['suggested_why']})")
    ready = _print_checks("What the change waits for:", found)
    if not ready and not args.prepare:
        print("\nNot begun: run with --prepare to play the owner's part on the copy.")
        return 1
    if args.prepare:
        owner = Owner(agent.db, agent.clock, agent.economy, scope, agent.settings.agent_name)
        where, params = scope.where()
        with database.connection() as conn:
            waiting = conn.execute(
                f"SELECT id, status FROM approvals WHERE {where} AND status IN ('pending', 'approved',"
                " 'approved_with_changes') ORDER BY id",
                params,
            ).fetchall()
        for row in waiting:
            if row["status"] == "pending":
                reply = owner.decide(int(row["id"]), {"decision": "reject", "comment": "rehearsal"}, "rehearsal")
            else:
                reply = owner.close(int(row["id"]), {"outcome": "done", "result_note": "rehearsal"}, "rehearsal")
            print(f"  request #{row['id']} ({row['status']}): {reply.status}")
        with database.transaction() as conn:
            estate.refresh(conn, scope, agent.clock)  # a book marked done just now is a product of the hand-over too
            for row in estate.handover(conn, scope):
                if row["verdict"] is None:
                    verdict = "passive" if row["suggested"] == "retire" else row["suggested"]
                    estate.decide(conn, scope, agent.clock, row["kind"], row["ref"], verdict, "rehearsal")
            found = generation.checks(conn, scope, running=False)
        if not _print_checks("After the owner's part:", found):
            print("\nNot begun: the checks above still wait.")
            return 1
    try:
        result = agent.begin_generation("rehearsal")
    except generation.GenerationError as exc:
        _print_checks(f"Not begun: {exc}", exc.checks)
        return 1
    print("\nBegun:")
    print(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    new = agent.scope()
    with database.connection() as conn:
        world_after = _world_counts(conn, store.WORLD_TABLES)
        lines = conn.execute(
            "SELECT id, title FROM projects WHERE mode = ? AND session = ? ORDER BY id", (new.mode, new.session)
        ).fetchall()
    changed = {t: (world_before.get(t), n) for t, n in world_after.items() if world_before.get(t) != n}
    named = [(r["id"], r["title"]) for r in lines]
    print(f"\nThe new mind: session {new.session}, life #{new.life_id}; its lines (made at its first cycle): {named}")
    print("The world's rows:", "unchanged" if not changed else f"CHANGED {changed}")
    print("\nIts first plan would see (the first 60 lines):")
    print("\n".join(agent.planner_preview().splitlines()[:60]))
    return 0 if not changed else 1


if __name__ == "__main__":
    sys.exit(main())
