-- 0.13.0: the connector protocol's journal. Each channel had its own executor and its own records (email_actions,
-- etsy_listings, etsy_edits), and nothing said across them what an action did or how to take it back. Now every
-- action Ember's code carries out (integrations/connectors.py) is journaled once, the same way: its action class,
-- the request it carries out (none for one Ember's code takes on its own, like turning on a sold listing's automatic
-- renewal), what it acts on, the state before and after, how it ended, and what would undo it. The executors keep
-- their own records as before. A finished entry is final; none is deleted.
CREATE TABLE action_journal (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL,
    session     INTEGER NOT NULL,
    approval_id INTEGER REFERENCES approvals (id),
    class       TEXT NOT NULL CHECK (length(class) BETWEEN 1 AND 40),
    subject     TEXT CHECK (length(subject) <= 300),  -- what it acts on: an address, a listing
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'partial', 'failed', 'unclear', 'simulated')),
    before      TEXT CHECK (before IS NULL OR json_valid(before)),
    after       TEXT CHECK (after IS NULL OR json_valid(after)),
    undo        TEXT CHECK (undo IS NULL OR json_valid(undo)),  -- what would undo it (NULL: nothing can)
    note        TEXT CHECK (length(note) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX action_journal_running ON action_journal (approval_id) WHERE status = 'running';
CREATE INDEX action_journal_by_scope ON action_journal (mode, session, id);
CREATE TRIGGER action_journal_fixed BEFORE UPDATE OF mode, session, approval_id, class, started_at, before
ON action_journal
BEGIN SELECT RAISE(ABORT, 'action_journal: what an action was never changes'); END;
-- What it acts on is known once (a new listing's number comes when it is made), then fixed.
CREATE TRIGGER action_journal_subject_once BEFORE UPDATE OF subject ON action_journal
WHEN OLD.subject IS NOT NULL AND NEW.subject IS NOT OLD.subject
BEGIN SELECT RAISE(ABORT, 'action_journal: what an action was never changes'); END;
CREATE TRIGGER action_journal_final BEFORE UPDATE ON action_journal
WHEN OLD.status <> 'running'
BEGIN SELECT RAISE(ABORT, 'action_journal: a finished action is final'); END;
CREATE TRIGGER action_journal_no_delete BEFORE DELETE ON action_journal
BEGIN SELECT RAISE(ABORT, 'action_journal: rows cannot be deleted'); END;
