"""Triggers across migrations: every trigger a migration created must still exist at the end, unless a later
migration dropped it on purpose (DROP TRIGGER) — a table rebuild (DROP TABLE + RENAME) silently drops the old
table's triggers. Also lists the final guard triggers per table."""

import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")
from app import db as dbmod  # noqa: E402

base = Path(os.environ["EMBER_DATA_DIR"])
base.mkdir(parents=True, exist_ok=True)
migrations = dbmod.discover_migrations()
created: dict[str, int] = {}
dropped_explicitly: dict[str, int] = {}
for m in migrations:
    text = m.path.read_text()
    for name in re.findall(r"(?i)CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)", text):
        created[name] = m.version
    for name in re.findall(r"(?i)DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?([A-Za-z0-9_]+)", text):
        dropped_explicitly[name] = m.version

# apply step by step and watch triggers disappear
path = base / "trig.db"
if path.exists():
    path.unlink()
lost = []
previous: dict[str, str] = {}
for m in migrations:
    dbmod.migrate(path, [x for x in migrations if x.version <= m.version], backup_dir=base / "bk")
    conn = sqlite3.connect(path)
    now = {r[0]: r[1] for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'")}
    conn.close()
    text = m.path.read_text()
    for name, table in previous.items():
        if name not in now:
            explicit = re.search(rf"(?i)DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?{name}\b", text)
            if not explicit:
                lost.append((m.version, m.name, name, table))
    previous = now
print("triggers created in all:", len(created), " at the end:", len(previous))
print("triggers that vanished without a DROP TRIGGER (table rebuilt or dropped):")
for row in lost:
    recreated = row[2] in previous
    print("  ", row, "-> recreated later" if recreated else "-> GONE at the end")
by_table: dict[str, list[str]] = {}
for name, table in previous.items():
    by_table.setdefault(table, []).append(name)
for table in ("ledger", "ventures", "milestones", "events", "llm_calls", "tool_calls", "approvals", "messages",
              "schema_migrations", "call_texts", "journal", "lives", "life_transitions", "action_journal",
              "policy_grants", "rules", "plan_nodes"):
    print(f"{table:18}", sorted(by_table.get(table, [])))
