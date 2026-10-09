import sqlite3, sys, os, pathlib
from app.db import Database, migrate
p = pathlib.Path(os.environ["EMBER_DATA_DIR"]) / "s.db"
if p.exists(): p.unlink()
db = Database(p); migrate(db.path)
c = sqlite3.connect(p)
for name, sql in c.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('ventures','venture_cases','venture_research','evidence','principles','cases','bets','lessons','memory_pins','library_documents','library_learnings','venture_critiques','predictions','reviews','weekly_looks','quality_checks') ORDER BY tbl_name, name"):
    print("----", name); print(sql)
