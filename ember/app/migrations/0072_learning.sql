-- 0.18.0: the learning loop (vision/learning.md). First, marketing before parking (agent/gates.py, agent/reach.py). A
-- product line that misses its day-14 views bar with less reach than reach.ENOUGH (blog posts, pins, listing edits for its listings) owes a push to bring buyers
-- instead of a park, and gets one more views bar two weeks later: 'retry_views'. The table is rebuilt (SQLite can't
-- change a CHECK) with its rows and their ids; its trigger stays as it was.
CREATE TABLE listing_gates_new (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session      INTEGER NOT NULL,
    project_id   INTEGER NOT NULL REFERENCES projects (id),
    gate         TEXT NOT NULL CHECK (
        gate IN ('day7_views', 'day14_views', 'retry_views', 'day14_favorites', 'day21_sale', 'scale')
    ),
    milestone_id INTEGER NOT NULL UNIQUE REFERENCES milestones (id),
    started_on   TEXT NOT NULL CHECK (started_on GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    created_at   TEXT NOT NULL,
    UNIQUE (mode, session, project_id, gate)
);
INSERT INTO listing_gates_new (id, mode, session, project_id, gate, milestone_id, started_on, created_at)
SELECT id, mode, session, project_id, gate, milestone_id, started_on, created_at FROM listing_gates;
DROP TABLE listing_gates;
ALTER TABLE listing_gates_new RENAME TO listing_gates;
CREATE TRIGGER listing_gates_no_delete BEFORE DELETE ON listing_gates
BEGIN SELECT RAISE(ABORT, 'listing_gates: rows cannot be deleted'); END;

-- 0.18.0: the learning loop (vision/learning.md, phases 2 and 3).
-- A bet: what the agent expects of a change to a project, settled by Ember's code against the project's funnel
-- (agent/bets.py). Its baseline is the metric when it was placed; won once it gained ``gain`` by ``due``.
CREATE TABLE bets (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    cycle_id    INTEGER REFERENCES cycles (id),
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    metric      TEXT NOT NULL CHECK (metric IN ('views', 'favorites', 'orders')),
    gain        INTEGER NOT NULL CHECK (gain BETWEEN 1 AND 100000),
    baseline    INTEGER NOT NULL CHECK (baseline >= 0),
    reach       INTEGER NOT NULL CHECK (reach >= 0),  -- the project's reach actions when it was placed
    expect      TEXT NOT NULL CHECK (length(expect) BETWEEN 1 AND 200),
    placed_at   TEXT NOT NULL,
    due         TEXT NOT NULL CHECK (length(due) = 10),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'won', 'lost', 'no_reach', 'void')),
    final       INTEGER CHECK (final IS NULL OR final >= 0),  -- the metric when it was settled
    settled_at  TEXT,
    CHECK ((status = 'open') = (settled_at IS NULL))
);
CREATE INDEX bets_by_scope ON bets (mode, session, status, due);
CREATE TRIGGER bets_no_delete BEFORE DELETE ON bets
BEGIN SELECT RAISE(ABORT, 'bets: rows cannot be deleted'); END;

-- A case: the daily review's retrospective of something that settled (a bet, a listing bar, a closed project or
-- venture, a rejected request): what was expected, what happened, why, and its cause (agent/learning.py).
CREATE TABLE cases (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    review_id   INTEGER REFERENCES reviews (id),
    subject     TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 80),  -- e.g. "bet #3", "milestone #12"
    project_id  INTEGER REFERENCES projects (id),
    venture_id  INTEGER REFERENCES ventures (id),
    expected    TEXT NOT NULL CHECK (length(expected) <= 300),
    happened    TEXT NOT NULL CHECK (length(happened) <= 300),
    why         TEXT NOT NULL CHECK (length(why) BETWEEN 1 AND 300),
    cause       TEXT NOT NULL CHECK (
        cause IN ('worked', 'wrong_idea', 'weak_execution', 'no_reach', 'too_early', 'outside')
    ),
    sure        TEXT NOT NULL CHECK (sure IN ('low', 'medium', 'high')),
    lesson      TEXT NOT NULL DEFAULT '' CHECK (length(lesson) <= 300),
    created_at  TEXT NOT NULL
);
CREATE INDEX cases_by_scope ON cases (mode, session, id);
CREATE TRIGGER cases_no_update BEFORE UPDATE ON cases
BEGIN SELECT RAISE(ABORT, 'cases: history cannot change'); END;
CREATE TRIGGER cases_no_delete BEFORE DELETE ON cases
BEGIN SELECT RAISE(ABORT, 'cases: history cannot change'); END;

-- The playbook: principles drawn from cases by the weekly look, with the cases for and against each; Ember's code sets
-- the confidence from them and retires a principle nothing confirmed for long (agent/learning.py).
CREATE TABLE principles (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
    supports      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(supports)),  -- case ids
    against       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(against)),
    confidence    TEXT NOT NULL CHECK (confidence IN ('hypothesis', 'established', 'disputed')),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at    TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL,  -- when a case last supported it
    retired_at    TEXT,
    retired_why   TEXT CHECK (retired_why IS NULL OR length(retired_why) <= 200)
);
CREATE INDEX principles_by_scope ON principles (mode, session, status);
CREATE TRIGGER principles_no_delete BEFORE DELETE ON principles
BEGIN SELECT RAISE(ABORT, 'principles: rows cannot be deleted'); END;

-- The weekly look at the whole business (agent/weekly.py): the view Ember's code built and what came of it.
CREATE TABLE weekly_reviews (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    cycle_id    INTEGER REFERENCES cycles (id),
    created_at  TEXT NOT NULL,
    day         TEXT NOT NULL CHECK (length(day) = 10),
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    view        TEXT NOT NULL CHECK (length(view) <= 16000),
    answer      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(answer)),
    outcome     TEXT NOT NULL DEFAULT '' CHECK (length(outcome) <= 1000),  -- what Ember's code did with it
    note        TEXT CHECK (note IS NULL OR length(note) <= 300)
);
CREATE INDEX weekly_reviews_by_scope ON weekly_reviews (mode, session, day);
CREATE TRIGGER weekly_reviews_no_delete BEFORE DELETE ON weekly_reviews
BEGIN SELECT RAISE(ABORT, 'weekly_reviews: rows cannot be deleted'); END;

-- The quality critic (agent/quality.py): an independent score of a product line's live listings against its market.
CREATE TABLE quality_checks (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    llm_call_id INTEGER REFERENCES llm_calls (id),
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    score       INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 10),
    verdict     TEXT CHECK (verdict IS NULL OR verdict IN ('pass', 'improve')),
    fixes       TEXT NOT NULL DEFAULT '' CHECK (length(fixes) <= 600),
    note        TEXT CHECK (note IS NULL OR length(note) <= 300),
    CHECK (status = 'failed' OR (score IS NOT NULL AND verdict IS NOT NULL))
);
CREATE INDEX quality_checks_by_project ON quality_checks (mode, session, project_id, id);
CREATE TRIGGER quality_checks_no_delete BEFORE DELETE ON quality_checks
BEGIN SELECT RAISE(ABORT, 'quality_checks: rows cannot be deleted'); END;

-- Every version of the memory files gains a source: 'weekly', the weekly look's rewrite of the strategy. The table is
-- rebuilt (SQLite can't change a CHECK) with its rows and their ids; its index and triggers stay as they were.
CREATE TABLE memory_versions_new (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    file       TEXT NOT NULL CHECK (file IN ('strategy', 'identity', 'lessons')),
    cycle_id   INTEGER REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('seed', 'agent', 'external', 'consolidation', 'weekly')),
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
