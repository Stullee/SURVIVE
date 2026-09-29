-- 0.11.0: the roadmap, the agent's plan for the weeks and months ahead. A milestone is a dated target with a measure
-- of done: goals for the next months, the milestones that lead to them (parent_id) and this week's steps, each linked
-- to the venture or project it serves if any. The agent plans every cycle against its roadmap and keeps it honest:
-- what it says it will reach (title) and how it will know (measure) can't change once written; a date can move, and
-- the move is counted (moves, first_due); a milestone ends done (with the evidence), missed (why, and what now) or
-- dropped (why), and is final then. The owner adds milestones, leaves notes and drops them on the Roadmap tab; the
-- agent can't drop one the owner added. Nothing is deleted.
CREATE TABLE milestones (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    parent_id        INTEGER REFERENCES milestones (id),  -- the bigger milestone it leads to
    venture_id       INTEGER REFERENCES ventures (id),
    project_id       INTEGER REFERENCES projects (id),
    created_cycle_id INTEGER REFERENCES cycles (id),  -- NULL when the owner added it
    created_by       TEXT NOT NULL CHECK (created_by IN ('agent', 'owner')),
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
    owner_action     TEXT CHECK (owner_action IS NULL OR owner_action IN ('added', 'note', 'drop')),
    owner_comment    TEXT CHECK (owner_comment IS NULL OR length(owner_comment) <= 1000),
    owner_at         TEXT,
    owner_by         TEXT,
    owner_version    INTEGER NOT NULL DEFAULT 0,
    seen_cycle_id    INTEGER REFERENCES cycles (id),
    CHECK ((status = 'open') = (closed_at IS NULL))
);
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

-- The milestone a cycle worked toward (the plan's focus_milestone_id).
ALTER TABLE cycles ADD COLUMN milestone_id INTEGER REFERENCES milestones (id);

-- The daily review's read of the roadmap (0.11.0); older reviews have none.
ALTER TABLE reviews ADD COLUMN roadmap TEXT NOT NULL DEFAULT '' CHECK (length(roadmap) <= 600);
