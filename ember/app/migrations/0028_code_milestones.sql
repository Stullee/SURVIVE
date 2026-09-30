-- 0.12.0: the roadmap is never empty. Ember's code puts a money goal at its root (earn at least what you spend, over
-- the last 30 days) with two decision points under it, and settles the goal from the books. Such a milestone is
-- created_by 'code': its date never moves, the agent never drops it, and only Ember's code or the owner closes the
-- goal itself (the agent closes a decision point with its decision). The table is rebuilt to allow 'code', as 0021
-- did for the owner's actions; its columns, indexes and triggers stay as they were.
CREATE TABLE milestones_new (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    parent_id        INTEGER REFERENCES milestones (id),  -- the bigger milestone it leads to
    venture_id       INTEGER REFERENCES ventures (id),
    project_id       INTEGER REFERENCES projects (id),
    created_cycle_id INTEGER REFERENCES cycles (id),  -- NULL when the owner or Ember's code added it
    created_by       TEXT NOT NULL CHECK (created_by IN ('agent', 'owner', 'code')),
    entered_by       TEXT,  -- the Home Assistant user who added it (a label, never an authorization)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 100),
    measure          TEXT NOT NULL CHECK (length(measure) BETWEEN 1 AND 300),
    -- Dates are the owner's local days (YYYY-MM-DD): first_due as first planned, due as planned now.
    first_due        TEXT NOT NULL CHECK (first_due GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    due              TEXT NOT NULL CHECK (due GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    moves            INTEGER NOT NULL DEFAULT 0 CHECK (moves >= 0),
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'missed', 'dropped')),
    result           TEXT NOT NULL DEFAULT '' CHECK (length(result) <= 600),
    closed_at        TEXT,
    closed_cycle_id  INTEGER REFERENCES cycles (id),  -- NULL when the owner dropped it
    notes            TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
    -- The owner's latest word on it: news for the agent until a plan has shown it (seen_cycle_id), like a decision.
    owner_action     TEXT CHECK (owner_action IS NULL OR owner_action IN ('added', 'note', 'drop', 'accept', 'reject')),
    owner_comment    TEXT CHECK (owner_comment IS NULL OR length(owner_comment) <= 1000),
    owner_at         TEXT,
    owner_by         TEXT,
    owner_version    INTEGER NOT NULL DEFAULT 0,
    seen_cycle_id    INTEGER REFERENCES cycles (id),
    closed_by        TEXT CHECK (closed_by IS NULL OR closed_by IN ('agent', 'owner', 'code')),
    -- The agent's proposed new date for an owner's milestone, until the owner accepts or rejects it.
    proposed_due     TEXT CHECK (proposed_due IS NULL OR proposed_due GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    proposed_note    TEXT CHECK (proposed_note IS NULL OR length(proposed_note) <= 300),
    proposed_at      TEXT,
    proposed_cycle_id INTEGER REFERENCES cycles (id),
    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK ((proposed_due IS NULL) = (proposed_at IS NULL)),
    CHECK (proposed_due IS NULL OR (status = 'open' AND created_by = 'owner'))
);
INSERT INTO milestones_new (
    id, mode, session, life_id, parent_id, venture_id, project_id, created_cycle_id, created_by, entered_by,
    created_at, updated_at, title, measure, first_due, due, moves, status, result, closed_at, closed_cycle_id, notes,
    owner_action, owner_comment, owner_at, owner_by, owner_version, seen_cycle_id, closed_by
)
SELECT
    id, mode, session, life_id, parent_id, venture_id, project_id, created_cycle_id, created_by, entered_by,
    created_at, updated_at, title, measure, first_due, due, moves, status, result, closed_at, closed_cycle_id, notes,
    owner_action, owner_comment, owner_at, owner_by, owner_version, seen_cycle_id, closed_by
FROM milestones;
DROP TABLE milestones;
ALTER TABLE milestones_new RENAME TO milestones;

CREATE INDEX milestones_by_scope ON milestones (mode, session, status, due);
CREATE INDEX milestones_by_parent ON milestones (parent_id);

CREATE TRIGGER milestones_no_delete BEFORE DELETE ON milestones
BEGIN SELECT RAISE(ABORT, 'milestones: rows cannot be deleted'); END;
CREATE TRIGGER milestones_fixed BEFORE UPDATE ON milestones
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.created_cycle_id IS NOT OLD.created_cycle_id OR NEW.created_by IS NOT OLD.created_by
    OR NEW.entered_by IS NOT OLD.entered_by OR NEW.created_at IS NOT OLD.created_at OR NEW.title IS NOT OLD.title
    OR NEW.measure IS NOT OLD.measure OR NEW.first_due IS NOT OLD.first_due
BEGIN SELECT RAISE(ABORT, 'milestones: identity is fixed'); END;
CREATE TRIGGER milestones_final BEFORE UPDATE ON milestones
WHEN OLD.status <> 'open' AND (NEW.status IS NOT OLD.status OR NEW.due IS NOT OLD.due OR NEW.moves IS NOT OLD.moves
    OR NEW.result IS NOT OLD.result OR NEW.closed_at IS NOT OLD.closed_at
    OR NEW.closed_cycle_id IS NOT OLD.closed_cycle_id OR NEW.parent_id IS NOT OLD.parent_id
    OR NEW.venture_id IS NOT OLD.venture_id OR NEW.project_id IS NOT OLD.project_id)
BEGIN SELECT RAISE(ABORT, 'milestones: a closed milestone is final'); END;
CREATE TRIGGER milestones_closed_by_named BEFORE UPDATE OF status ON milestones
WHEN NEW.status <> 'open' AND NEW.closed_by IS NULL
BEGIN SELECT RAISE(ABORT, 'milestones: a closed milestone names who closed it'); END;
CREATE TRIGGER milestones_closed_by_final BEFORE UPDATE OF closed_by ON milestones
WHEN OLD.closed_by IS NOT NULL AND NEW.closed_by IS NOT OLD.closed_by
BEGIN SELECT RAISE(ABORT, 'milestones: who closed a milestone is final'); END;

-- (0021) A date moves by one counted move (moves), never quietly; an owner's milestone moves only when the owner
-- accepts a proposed date (their new word, 'accept'), and only the owner drops it.
CREATE TRIGGER milestones_moves_counted BEFORE UPDATE OF due, moves ON milestones
WHEN (NEW.due IS NOT OLD.due AND NEW.moves IS NOT OLD.moves + 1) OR (NEW.due IS OLD.due AND NEW.moves IS NOT OLD.moves)
BEGIN SELECT RAISE(ABORT, 'milestones: every move of a date is counted, once'); END;
CREATE TRIGGER milestones_owner_date BEFORE UPDATE OF due ON milestones
WHEN OLD.created_by = 'owner' AND NEW.due IS NOT OLD.due
    AND NOT (NEW.owner_action IS 'accept' AND NEW.owner_version = OLD.owner_version + 1)
BEGIN SELECT RAISE(ABORT, 'milestones: only the owner moves the date of their milestone'); END;
CREATE TRIGGER milestones_owner_drop BEFORE UPDATE OF status ON milestones
WHEN OLD.created_by = 'owner' AND NEW.status = 'dropped' AND NEW.closed_by IS NOT 'owner'
BEGIN SELECT RAISE(ABORT, 'milestones: only the owner drops their milestone'); END;

-- New: Ember's code's milestones.
CREATE TRIGGER milestones_code_date BEFORE UPDATE OF due ON milestones
WHEN OLD.created_by = 'code' AND NEW.due IS NOT OLD.due
BEGIN SELECT RAISE(ABORT, 'milestones: the date of a milestone Ember''s code set never moves'); END;
CREATE TRIGGER milestones_code_drop BEFORE UPDATE OF status ON milestones
WHEN OLD.created_by = 'code' AND NEW.status = 'dropped' AND NEW.closed_by NOT IN ('owner', 'code')
BEGIN SELECT RAISE(ABORT, 'milestones: only the owner or Ember''s code drops a milestone Ember''s code set'); END;
CREATE TRIGGER milestones_code_goal BEFORE UPDATE OF status ON milestones
WHEN OLD.created_by = 'code' AND OLD.parent_id IS NULL AND NEW.status <> 'open' AND NEW.closed_by NOT IN ('owner', 'code')
BEGIN SELECT RAISE(ABORT, 'milestones: Ember''s code or the owner closes the money goal'); END;
