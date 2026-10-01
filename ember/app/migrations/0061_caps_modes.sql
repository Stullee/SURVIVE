-- 0.15.0: the burn mode a wake cycle opened in (economy/burn.py), written once when the row is made, like its cap. A
-- maintenance cycle's cap bounds every call in it (workshop runs, the daily review, the library's study and the
-- lessons' consolidation too), so the budget guard reads the mode the cycle opened in, not the mode now: a cycle's
-- rules don't change halfway through it. Rows from before stay empty (their calls are judged as before).
ALTER TABLE cycles ADD COLUMN burn_mode TEXT
    CHECK (burn_mode IS NULL OR burn_mode IN ('explore', 'focus', 'maintenance', 'dormant'));

CREATE TRIGGER cycles_burn_mode_fixed BEFORE UPDATE OF burn_mode ON cycles
WHEN NEW.burn_mode IS NOT OLD.burn_mode
BEGIN SELECT RAISE(ABORT, 'cycles: the burn mode a cycle opened in is fixed'); END;

-- 0.15.0: a document's study ends once its learnings are full (agent/library.py MAX_LEARNINGS): the study calls after
-- that kept nothing and were still paid for. Its study is then 'done' with fewer parts read than it has, and its
-- study_note says so; 'waiting' and 'failed' still mean parts are left. The table is rebuilt (SQLite can't change a
-- CHECK) with its rows and their ids; its indexes stay as they were.
CREATE TABLE library_documents_new (
    id              INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session         INTEGER NOT NULL,
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    source          TEXT NOT NULL DEFAULT '' CHECK (length(source) <= 500),
    note            TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    venture_id      INTEGER REFERENCES ventures (id),
    project_id      INTEGER REFERENCES projects (id),
    file_name       TEXT CHECK (file_name IS NULL OR length(file_name) <= 255),
    chars           INTEGER NOT NULL CHECK (chars > 0),
    parts           INTEGER NOT NULL CHECK (parts > 0),
    sha256          TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    added_by        TEXT,
    study           TEXT NOT NULL DEFAULT 'waiting' CHECK (study IN ('waiting', 'done', 'failed')),
    studied_parts   INTEGER NOT NULL DEFAULT 0 CHECK (studied_parts >= 0 AND studied_parts <= parts),
    study_failures  INTEGER NOT NULL DEFAULT 0 CHECK (study_failures >= 0),
    study_micros    INTEGER NOT NULL DEFAULT 0 CHECK (study_micros >= 0),
    study_note      TEXT CHECK (study_note IS NULL OR length(study_note) <= 300),
    summary         TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 600),
    studied_at      TEXT,
    removed_at      TEXT,
    removed_by      TEXT,
    CHECK (study = 'done' OR studied_parts < parts),
    CHECK (study <> 'done' OR studied_parts = parts OR study_note IS NOT NULL)
);
INSERT INTO library_documents_new (id, mode, session, title, source, note, venture_id, project_id, file_name, chars,
    parts, sha256, added_at, added_by, study, studied_parts, study_failures, study_micros, study_note, summary,
    studied_at, removed_at, removed_by)
SELECT id, mode, session, title, source, note, venture_id, project_id, file_name, chars, parts, sha256, added_at,
    added_by, study, studied_parts, study_failures, study_micros, study_note, summary, studied_at, removed_at,
    removed_by FROM library_documents;
DROP TABLE library_documents;
ALTER TABLE library_documents_new RENAME TO library_documents;
CREATE UNIQUE INDEX library_one_copy ON library_documents (mode, session, sha256) WHERE removed_at IS NULL;
CREATE INDEX library_by_scope ON library_documents (mode, session, study);
