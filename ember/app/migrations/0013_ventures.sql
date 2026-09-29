-- 0.10.0: ventures, the agent's ways to earn beyond what it does now (a new market, platform, business model or a
-- channel that brings buyers), and the legs it already stands on. They form a tree that keeps growing: an idea
-- branches from the one it came from (parent_id), and each is scored from 1 to 5 on what makes it worth doing. The
-- agent researches them in venture cycles, which get the owner's share of its spending (the venture_share option),
-- keeps what it learns in a knowledge file per venture (ventures/<id>-<name>.md in its workspace) and brings the
-- owner business cases. The owner adds ideas, backs, parks or kills them on the Ventures tab; only the owner backs a
-- venture (building) or kills it. Nothing is deleted: a parked or killed idea stays in the tree.
CREATE TABLE ventures (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    parent_id        INTEGER REFERENCES ventures (id),  -- the idea it branched from; NULL at the top of the tree
    created_cycle_id INTEGER REFERENCES cycles (id),  -- NULL when the owner added it
    created_by       TEXT NOT NULL CHECK (created_by IN ('agent', 'owner')),
    entered_by       TEXT,  -- the Home Assistant user who added it (a label, never an authorization)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
    pitch            TEXT NOT NULL CHECK (length(pitch) BETWEEN 1 AND 600),
    stage            TEXT NOT NULL
        CHECK (stage IN ('idea', 'researching', 'proposed', 'building', 'live', 'parked', 'killed')),
    -- The scorecard: words, never parsed as money. A business case (stage 'proposed') has all six filled in.
    demand           TEXT NOT NULL DEFAULT '' CHECK (length(demand) <= 400),
    economics        TEXT NOT NULL DEFAULT '' CHECK (length(economics) <= 400),
    setup            TEXT NOT NULL DEFAULT '' CHECK (length(setup) <= 400),
    first_euro       TEXT NOT NULL DEFAULT '' CHECK (length(first_euro) <= 200),
    risks            TEXT NOT NULL DEFAULT '' CHECK (length(risks) <= 400),
    first_test       TEXT NOT NULL DEFAULT '' CHECK (length(first_test) <= 400),
    next_question    TEXT NOT NULL DEFAULT '' CHECK (length(next_question) <= 300),
    -- The scores, 1 to 5 (NULL: not judged yet): what it could earn, how much of it the agent can do, how hard and
    -- how risky it is, how soon the first euro comes, and what it costs to start. scores_by says where they come
    -- from: a brainstorm's first guess or research.
    revenue          INTEGER CHECK (revenue IS NULL OR revenue BETWEEN 1 AND 5),
    doability        INTEGER CHECK (doability IS NULL OR doability BETWEEN 1 AND 5),
    difficulty       INTEGER CHECK (difficulty IS NULL OR difficulty BETWEEN 1 AND 5),
    risk             INTEGER CHECK (risk IS NULL OR risk BETWEEN 1 AND 5),
    speed            INTEGER CHECK (speed IS NULL OR speed BETWEEN 1 AND 5),
    cost             INTEGER CHECK (cost IS NULL OR cost BETWEEN 1 AND 5),
    scores_by        TEXT CHECK (scores_by IS NULL OR scores_by IN ('brainstorm', 'research')),
    notes            TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
    proposed_at      TEXT,  -- when its latest business case was made
    -- The owner's latest word on it: news for the agent until a plan has shown it (seen_cycle_id), like a decision.
    owner_action     TEXT CHECK (owner_action IS NULL OR owner_action IN ('added', 'research', 'back', 'park', 'kill', 'note')),
    owner_comment    TEXT CHECK (owner_comment IS NULL OR length(owner_comment) <= 1000),
    owner_at         TEXT,
    owner_by         TEXT,
    owner_version    INTEGER NOT NULL DEFAULT 0,
    seen_cycle_id    INTEGER REFERENCES cycles (id)
);
CREATE INDEX ventures_by_scope ON ventures (mode, session, stage);
CREATE INDEX ventures_by_parent ON ventures (parent_id);
CREATE TRIGGER ventures_no_delete BEFORE DELETE ON ventures
BEGIN SELECT RAISE(ABORT, 'ventures: rows cannot be deleted'); END;
CREATE TRIGGER ventures_fixed BEFORE UPDATE ON ventures
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.created_cycle_id IS NOT OLD.created_cycle_id OR NEW.created_by IS NOT OLD.created_by
    OR NEW.entered_by IS NOT OLD.entered_by OR NEW.created_at IS NOT OLD.created_at OR NEW.title IS NOT OLD.title
    OR NEW.parent_id IS NOT OLD.parent_id
BEGIN SELECT RAISE(ABORT, 'ventures: identity is fixed'); END;

-- A project belongs to a venture (its leg); a cycle's cost counts toward the venture it worked on.
ALTER TABLE projects ADD COLUMN venture_id INTEGER REFERENCES ventures (id);
-- A venture cycle (1) works on ventures, with the owner's share of the spending; venture_id is its focus.
ALTER TABLE cycles ADD COLUMN venture INTEGER NOT NULL DEFAULT 0 CHECK (venture IN (0, 1));
ALTER TABLE cycles ADD COLUMN venture_id INTEGER REFERENCES ventures (id);

-- The daily review's read of the venture pipeline (0.10.0); older reviews have none.
ALTER TABLE reviews ADD COLUMN ventures TEXT NOT NULL DEFAULT '' CHECK (length(ventures) <= 600);
