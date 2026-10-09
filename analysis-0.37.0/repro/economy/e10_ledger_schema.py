"""The ledger's append-only triggers and checks after every migration."""
import sqlite3, tempfile, uuid
from pathlib import Path
from app.config import Settings
from tests.economy_helpers import make_economy, owner

e = make_economy(Path(tempfile.mkdtemp()), Settings(starting_balance_usd=20))
with e.db.connection() as conn:
    print([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='ledger'")])
    for sql in ("UPDATE ledger SET amount_micros = 1", "DELETE FROM ledger",
                "INSERT INTO ledger (ts, occurred_on, type, amount_micros, created_by, idempotency_key) VALUES ('2026-09-01T00:00:00Z','2026-09-01','revenue',5,'system','x')",
                "INSERT INTO ledger (ts, occurred_on, type, amount_micros, created_by, idempotency_key, corrects_id) VALUES ('2026-09-01T00:00:00Z','2026-09-01','owner_grant',-30000000,'owner','y',1)"):
        try:
            conn.execute(sql)
            print("ALLOWED:", sql[:60])
        except sqlite3.DatabaseError as exc:
            print("refused:", sql[:40], "->", exc)
