-- 0.13.0: the audit (agent/audit.py). The owner sees what Ember's code did (the action journal, 0049) and can undo an
-- action on a listing: an Undo is a request of the owner's, approved at once and carried out by Ember's code like any
-- change, and action_undos links it to the action it undoes. One at a time: another Undo of the same action only once
-- the last one failed with nothing made (or was cancelled). Rows are history: none changes, none is deleted.
CREATE TABLE action_undos (
    id          INTEGER PRIMARY KEY,
    journal_id  INTEGER NOT NULL REFERENCES action_journal (id),
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    by          TEXT NOT NULL CHECK (length(by) BETWEEN 1 AND 60),
    created_at  TEXT NOT NULL
);
CREATE INDEX action_undos_by_journal ON action_undos (journal_id, id);
CREATE TRIGGER action_undos_once BEFORE INSERT ON action_undos
WHEN EXISTS (
    SELECT 1 FROM action_undos u JOIN approvals a ON a.id = u.approval_id
    WHERE u.journal_id = NEW.journal_id AND NOT (
        a.status = 'failed'
        AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = u.approval_id AND j.status <> 'failed')
    )
)
BEGIN SELECT RAISE(ABORT, 'action_undos: this action is undone or being undone'); END;
CREATE TRIGGER action_undos_no_update BEFORE UPDATE ON action_undos
BEGIN SELECT RAISE(ABORT, 'action_undos: an undo never changes'); END;
CREATE TRIGGER action_undos_no_delete BEFORE DELETE ON action_undos
BEGIN SELECT RAISE(ABORT, 'action_undos: rows cannot be deleted'); END;

-- The daily digest: what Ember's code did in one of the owner's days, on their unlocks, on its own and on their
-- approvals, what the unlocks hold, what was taken back and what waited whatever was unlocked. Written once, after
-- the day ended; shown on the Approvals tab and in the sensors.
CREATE TABLE owner_digests (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL,
    session    INTEGER NOT NULL,
    day        TEXT NOT NULL,  -- the owner's day it covers (YYYY-MM-DD)
    text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    data       TEXT NOT NULL CHECK (json_valid(data)),
    created_at TEXT NOT NULL,
    UNIQUE (mode, session, day)
);
CREATE TRIGGER owner_digests_no_update BEFORE UPDATE ON owner_digests
BEGIN SELECT RAISE(ABORT, 'owner_digests: a digest never changes'); END;
CREATE TRIGGER owner_digests_no_delete BEFORE DELETE ON owner_digests
BEGIN SELECT RAISE(ABORT, 'owner_digests: rows cannot be deleted'); END;
