-- 0.12.0: a venture's stages with rules. Nothing stopped research that never reached a business case, the business
-- case's first test never became a milestone, a killed venture's milestones stayed open, and only the prompt said
-- that the owner alone backs or kills a venture. Now Ember's code keeps a rule for each stage (agent/stages.py):
-- research that brings no business case within 21 days, while nothing is built for it, is parked; when the owner
-- backs a venture, its first test becomes a milestone Ember's code sets (test_milestone_id), which it must meet before
-- it goes live, and a missed first test parks it. A venture Ember's code parks (parked_by 'code') is the owner's to
-- take up again, like one they parked. stage_at: when its stage last changed, kept by the database.
--
-- The ventures table is rebuilt to allow parked_by 'code', as 0021 did for milestones; its columns, indexes and
-- triggers stay as they were.
CREATE TABLE ventures_new (
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
    seen_cycle_id    INTEGER REFERENCES cycles (id),
    -- Who parked it (0025): the agent, the owner or, new, Ember's code by a stage's rule.
    parked_by        TEXT CHECK (parked_by IS NULL OR parked_by IN ('agent', 'owner', 'code')),
    -- New: when its stage last changed, and the milestone of its first test once the owner backed it.
    stage_at         TEXT,
    test_milestone_id INTEGER REFERENCES milestones (id)
);
INSERT INTO ventures_new (
    id, mode, session, life_id, parent_id, created_cycle_id, created_by, entered_by, created_at, updated_at, title,
    pitch, stage, demand, economics, setup, first_euro, risks, first_test, next_question, revenue, doability,
    difficulty, risk, speed, cost, scores_by, notes, proposed_at, owner_action, owner_comment, owner_at, owner_by,
    owner_version, seen_cycle_id, parked_by, stage_at
)
SELECT
    id, mode, session, life_id, parent_id, created_cycle_id, created_by, entered_by, created_at, updated_at, title,
    pitch, stage, demand, economics, setup, first_euro, risks, first_test, next_question, revenue, doability,
    difficulty, risk, speed, cost, scores_by, notes, proposed_at, owner_action, owner_comment, owner_at, owner_by,
    owner_version, seen_cycle_id, parked_by, updated_at
FROM ventures;
DROP TABLE ventures;
ALTER TABLE ventures_new RENAME TO ventures;

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
-- (0024) A new venture has no research yet; scores from research and a business case need research that found
-- something.
CREATE TRIGGER ventures_start_unresearched BEFORE INSERT ON ventures
WHEN NEW.stage = 'proposed' OR NEW.scores_by = 'research'
BEGIN SELECT RAISE(ABORT, 'ventures: a new venture has no research yet'); END;
CREATE TRIGGER ventures_scores_researched BEFORE UPDATE ON ventures
WHEN NEW.scores_by = 'research'
    AND (OLD.scores_by IS NOT 'research' OR NEW.revenue IS NOT OLD.revenue OR NEW.doability IS NOT OLD.doability
        OR NEW.difficulty IS NOT OLD.difficulty OR NEW.risk IS NOT OLD.risk OR NEW.speed IS NOT OLD.speed
        OR NEW.cost IS NOT OLD.cost)
    AND NOT EXISTS (SELECT 1 FROM venture_research WHERE venture_id = NEW.id AND sources > 0)
BEGIN SELECT RAISE(ABORT, 'ventures: scores from research need research that found something'); END;
CREATE TRIGGER ventures_proposed_researched BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage = 'proposed' AND OLD.stage IS NOT 'proposed'
    AND (SELECT COUNT(*) FROM venture_research WHERE venture_id = NEW.id AND sources > 0) < 2
BEGIN SELECT RAISE(ABORT, 'ventures: a business case needs two research calls that found something'); END;
-- (0025, now for Ember's code's park too) Only the owner's word takes a venture out of their park or the code's.
CREATE TRIGGER ventures_owner_park BEFORE UPDATE OF stage ON ventures
WHEN OLD.stage = 'parked' AND OLD.parked_by IN ('owner', 'code') AND NEW.stage IS NOT 'parked'
    AND NEW.owner_version = OLD.owner_version
BEGIN SELECT RAISE(ABORT, 'ventures: only the owner takes a venture out of their park'); END;

-- New: only the owner backs or kills a venture, a backed venture goes live once its first test is met (or the owner
-- dropped that test), and the database keeps when the stage changed.
CREATE TRIGGER ventures_owner_decides BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage IN ('building', 'killed') AND NEW.stage IS NOT OLD.stage
    AND NOT (NEW.owner_action IS (CASE NEW.stage WHEN 'building' THEN 'back' ELSE 'kill' END)
        AND NEW.owner_version = OLD.owner_version + 1)
BEGIN SELECT RAISE(ABORT, 'ventures: only the owner backs or kills a venture'); END;
CREATE TRIGGER ventures_tested_live BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage = 'live' AND OLD.stage = 'building'
    AND NOT EXISTS (
        SELECT 1 FROM milestones WHERE id = NEW.test_milestone_id
            AND (status = 'done' OR (status = 'dropped' AND closed_by = 'owner'))
    )
BEGIN SELECT RAISE(ABORT, 'ventures: a backed venture goes live once its first test is met'); END;
CREATE TRIGGER ventures_stage_at_new AFTER INSERT ON ventures
WHEN NEW.stage_at IS NULL
BEGIN UPDATE ventures SET stage_at = NEW.created_at WHERE id = NEW.id; END;
CREATE TRIGGER ventures_stage_at AFTER UPDATE OF stage ON ventures
WHEN NEW.stage IS NOT OLD.stage
BEGIN UPDATE ventures SET stage_at = NEW.updated_at WHERE id = NEW.id; END;

-- Ember's code's milestones by kind: the money goal (0028), its decision points (0028) and, new, a backed venture's
-- first test, which leads to the money goal like the agent's milestones. Only Ember's code or the owner closes the
-- money goal; the kind of a milestone is fixed.
ALTER TABLE milestones ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind IN ('money_goal', 'decision', 'first_test'));
UPDATE milestones SET kind = CASE WHEN parent_id IS NULL THEN 'money_goal' ELSE 'decision' END WHERE created_by = 'code';
CREATE TRIGGER milestones_kind_code BEFORE INSERT ON milestones
WHEN (NEW.kind IS NOT NULL) <> (NEW.created_by = 'code')
BEGIN SELECT RAISE(ABORT, 'milestones: a milestone Ember''s code sets has a kind, and only such a one'); END;
CREATE TRIGGER milestones_kind_fixed BEFORE UPDATE OF kind ON milestones
WHEN NEW.kind IS NOT OLD.kind
BEGIN SELECT RAISE(ABORT, 'milestones: the kind of a milestone is fixed'); END;
DROP TRIGGER milestones_code_goal;
CREATE TRIGGER milestones_code_goal BEFORE UPDATE OF status ON milestones
WHEN OLD.kind = 'money_goal' AND NEW.status <> 'open' AND NEW.closed_by NOT IN ('owner', 'code')
BEGIN SELECT RAISE(ABORT, 'milestones: Ember''s code or the owner closes the money goal'); END;
