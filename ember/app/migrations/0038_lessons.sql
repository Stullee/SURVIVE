-- 0.12.0: lessons the owner pins, and the consolidation that keeps the lessons file short. Appending to a full
-- lessons file dropped its oldest lines, data-backed no's among them ("Dropshipping ruled out by data: margins under
-- 5%"), and a rewrite could drop anything. Now the owner pins a lesson: it is never dropped, a rewrite must keep it,
-- and every plan shows it. Once a day, after the daily review, a call of its own merges the lessons that say the same
-- and retires those newer evidence contradicts; Ember's code checks its answer and keeps what it doesn't account for.
CREATE TABLE lesson_pins (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),  -- the lesson, without its cycle tag
    created_at  TEXT NOT NULL,
    pinned_by   TEXT,  -- who pinned it, if Home Assistant said
    unpinned_at TEXT
);
CREATE INDEX lesson_pins_by_scope ON lesson_pins (mode, session, unpinned_at);
CREATE TRIGGER lesson_pins_no_delete BEFORE DELETE ON lesson_pins
BEGIN SELECT RAISE(ABORT, 'lesson_pins: rows cannot be deleted'); END;
CREATE TRIGGER lesson_pins_fixed BEFORE UPDATE ON lesson_pins
WHEN OLD.unpinned_at IS NOT NULL OR NEW.unpinned_at IS NULL OR NEW.mode IS NOT OLD.mode
    OR NEW.session IS NOT OLD.session OR NEW.text IS NOT OLD.text OR NEW.created_at IS NOT OLD.created_at
    OR NEW.pinned_by IS NOT OLD.pinned_by
BEGIN SELECT RAISE(ABORT, 'lesson_pins: a pin is fixed, and unpinning it is final'); END;

-- Every version of the memory files gains a source: 'consolidation', the daily consolidation's rewrite of the lessons.
-- The table is rebuilt (SQLite can't change a CHECK) with its rows and their ids; its index and triggers stay as they
-- were.
CREATE TABLE memory_versions_new (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    file       TEXT NOT NULL CHECK (file IN ('strategy', 'identity', 'lessons')),
    cycle_id   INTEGER REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('seed', 'agent', 'external', 'consolidation')),
    sha256     TEXT NOT NULL,
    content    TEXT NOT NULL CHECK (length(content) <= 8000)
);
INSERT INTO memory_versions_new (id, mode, session, file, cycle_id, created_at, source, sha256, content)
SELECT id, mode, session, file, cycle_id, created_at, source, sha256, content FROM memory_versions;
DROP TABLE memory_versions;
ALTER TABLE memory_versions_new RENAME TO memory_versions;
CREATE INDEX memory_versions_by_file ON memory_versions (mode, session, file, id);
CREATE TRIGGER memory_versions_no_update BEFORE UPDATE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions: history cannot change'); END;
CREATE TRIGGER memory_versions_no_delete BEFORE DELETE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions: history cannot change'); END;
