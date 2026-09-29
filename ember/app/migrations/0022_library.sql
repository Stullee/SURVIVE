-- 0.12.0: the owner's library, and what Ember learned from it (the owner's request). The owner hands Ember reference
-- material they found (Etsy's pages on listings, titles, tags and keywords; guides; notes of their own), pasted or
-- uploaded as text. Ember studies each document once, a part at a time ('study' calls within a daily study budget the
-- owner sets), and keeps what it learned: a summary and the learnings, each naming the part it came from, so the
-- material never has to be read or analysed again. The texts are the owner's reference: a removed document loses its
-- text, and its learnings are no longer shown; its row stays as a trace.
CREATE TABLE library_documents (
    id              INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session         INTEGER NOT NULL,
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    source          TEXT NOT NULL DEFAULT '' CHECK (length(source) <= 500),  -- where it is from: a link, a book...
    note            TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),  -- the owner's word on what it is for
    venture_id      INTEGER REFERENCES ventures (id),
    project_id      INTEGER REFERENCES projects (id),
    file_name       TEXT CHECK (file_name IS NULL OR length(file_name) <= 255),  -- the uploaded file's, if any
    chars           INTEGER NOT NULL CHECK (chars > 0),
    parts           INTEGER NOT NULL CHECK (parts > 0),
    sha256          TEXT NOT NULL,  -- of the text: the same text is added once
    added_at        TEXT NOT NULL,
    added_by        TEXT,  -- the Home Assistant user (a label, never an authorization)
    -- The study: 'waiting' until every part was read ('done'); 'failed' after repeated bad answers (the owner can
    -- ask for another try). studied_parts counts the parts read, in order.
    study           TEXT NOT NULL DEFAULT 'waiting' CHECK (study IN ('waiting', 'done', 'failed')),
    studied_parts   INTEGER NOT NULL DEFAULT 0 CHECK (studied_parts >= 0 AND studied_parts <= parts),
    study_failures  INTEGER NOT NULL DEFAULT 0 CHECK (study_failures >= 0),
    study_micros    INTEGER NOT NULL DEFAULT 0 CHECK (study_micros >= 0),
    study_note      TEXT CHECK (study_note IS NULL OR length(study_note) <= 300),  -- why the last try failed
    summary         TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 600),
    studied_at      TEXT,
    removed_at      TEXT,
    removed_by      TEXT,
    CHECK ((study = 'done') = (studied_parts = parts))
);
CREATE UNIQUE INDEX library_one_copy ON library_documents (mode, session, sha256) WHERE removed_at IS NULL;
CREATE INDEX library_by_scope ON library_documents (mode, session, study);

-- A document's text, in parts of a few thousand characters (split at paragraphs), for search and reading.
CREATE TABLE library_parts (
    document_id INTEGER NOT NULL REFERENCES library_documents (id),
    part        INTEGER NOT NULL CHECK (part >= 1),
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
    PRIMARY KEY (document_id, part)
) WITHOUT ROWID;

-- What Ember learned from the library: specific, self-contained learnings (a rule, a number, a how-to, a mistake to
-- avoid), each from one part of one document. Written by Ember's study calls only; never changed.
CREATE TABLE learnings (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    document_id   INTEGER NOT NULL REFERENCES library_documents (id),
    part          INTEGER NOT NULL CHECK (part >= 1),
    topic         TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 40),
    text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
    cycle_id      INTEGER REFERENCES cycles (id),
    llm_call_id   INTEGER REFERENCES llm_calls (id),
    created_at    TEXT NOT NULL,
    seen_cycle_id INTEGER REFERENCES cycles (id)  -- the plan that first listed it as new
);
CREATE INDEX learnings_by_document ON learnings (document_id);
CREATE INDEX learnings_by_scope ON learnings (mode, session, seen_cycle_id);
CREATE TRIGGER learnings_fixed BEFORE UPDATE ON learnings
WHEN NEW.document_id IS NOT OLD.document_id OR NEW.part IS NOT OLD.part OR NEW.topic IS NOT OLD.topic
    OR NEW.text IS NOT OLD.text OR NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session
BEGIN SELECT RAISE(ABORT, 'learnings: a learning is never changed'); END;
