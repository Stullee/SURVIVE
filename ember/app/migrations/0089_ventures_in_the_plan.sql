-- 0.36.0: the plan tree decides when Ember explores (the venture share retires), and the owner's own word in it.
-- Live on 2026-10-09 the venture share forced 10 of 24 cycles into venture work that Ember, told "nothing new" by its
-- owner, left undone: 26% of the day's spending. Now a Ventures project in the tree holds one Explore step, weighed like
-- any step; the owner sets its worth or holds it ('ventures' platform; worth and hold on that project node). A hold
-- now says whose it is (hold_by): Ember lifts only its own. The table is rebuilt (SQLite can't change a CHECK) with its
-- rows and their ids; its indexes and triggers are made again as they were.
CREATE TABLE plan_nodes_new (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    parent_id     INTEGER REFERENCES plan_nodes (id),
    level         TEXT NOT NULL CHECK (level IN ('project', 'product', 'stage', 'step')),
    platform      TEXT CHECK (platform IN ('etsy', 'kdp', 'printify', 'website', 'channels', 'other', 'ventures')),
    project_id    INTEGER REFERENCES projects (id),  -- the product's line (a product, its stages and steps)
    template      TEXT CHECK (template IS NULL OR length(template) BETWEEN 1 AND 80),  -- e.g. 'kdp_book@1'
    stage         TEXT CHECK (stage IN ('research', 'create', 'release', 'launch', 'maintain')),
    kind          TEXT CHECK (kind IN ('create', 'ship', 'launch', 'fix', 'market', 'chore', 'report', 'owner', 'promise')),
    channel       TEXT CHECK (channel IN ('pinterest', 'bluesky', 'blog')),  -- a marketing step's channel
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    check_kind    TEXT CHECK (check_kind IS NULL OR length(check_kind) BETWEEN 1 AND 40),  -- agent/templates.py CHECKS
    check_spec    TEXT CHECK (check_spec IS NULL OR json_valid(check_spec)),
    seq           INTEGER NOT NULL DEFAULT 0,  -- its order among its parent's children
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    waiting       TEXT CHECK (waiting IS NULL OR waiting IN ('owner', 'channel', 'upgrade', 'date', 'step')),
    source        TEXT NOT NULL CHECK (source IN ('template', 'code', 'agent', 'owner', 'promise')),
    obligation_id INTEGER UNIQUE REFERENCES obligations (id),  -- a promise's step
    due           TEXT CHECK (due IS NULL OR length(due) = 10),
    effort        INTEGER NOT NULL DEFAULT 1 CHECK (effort BETWEEN 1 AND 3),  -- cycles it should take
    ready_since   TEXT,  -- since when it is ready and untouched: its age (none while it waits)
    pinned        INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),  -- the owner pinned the step
    owner_worth   REAL CHECK (owner_worth IS NULL OR owner_worth BETWEEN 0.5 AND 10),  -- the owner's worth
    could_earn    REAL CHECK (could_earn IS NULL OR could_earn >= 0),  -- a product: expected $ a month, from records
    audience      TEXT CHECK (audience IN ('en', 'de', 'both')),  -- a product: who its marketing speaks to
    hold_reason   TEXT CHECK (hold_reason IS NULL OR length(hold_reason) BETWEEN 1 AND 200),  -- Release 2b: on hold
    hold_by       TEXT CHECK (hold_by IS NULL OR hold_by IN ('agent', 'owner')),  -- 0.36.0: whose hold it is
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    closed_by     TEXT CHECK (closed_by IN ('code', 'owner', 'agent')),
    result        TEXT CHECK (result IS NULL OR length(result) BETWEEN 1 AND 300),
    wait_ref      INTEGER,
    wait_until    TEXT CHECK (wait_until IS NULL OR length(wait_until) = 10),
    wait_why      TEXT CHECK (wait_why IS NULL OR length(wait_why) BETWEEN 1 AND 200),
    live_since    TEXT CHECK (live_since IS NULL OR length(live_since) = 10),
    decide_by     TEXT CHECK (decide_by IS NULL OR json_valid(decide_by)),
    pushed_until  TEXT CHECK (pushed_until IS NULL OR length(pushed_until) = 10),
    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK ((status = 'open') = (closed_by IS NULL)),
    CHECK ((level = 'project') = (parent_id IS NULL)),
    CHECK ((level = 'project') = (platform IS NOT NULL)),
    CHECK (level NOT IN ('product', 'stage') OR project_id IS NOT NULL),
    CHECK (level NOT IN ('project', 'product') OR stage IS NULL AND kind IS NULL),
    CHECK (level <> 'stage' OR stage IS NOT NULL AND kind IS NULL),
    CHECK ((level = 'step') = (kind IS NOT NULL)),
    CHECK (level <> 'step' OR stage IS NOT NULL OR project_id IS NULL),  -- a product's step names its stage
    CHECK (pinned = 0 OR level = 'step'),
    CHECK (level = 'product' OR could_earn IS NULL AND audience IS NULL),
    -- a worth and a hold are a product's, and (0.36.0) the Ventures project's
    CHECK (level = 'product' OR level = 'project' AND platform = 'ventures' OR owner_worth IS NULL AND hold_reason IS NULL),
    CHECK ((hold_reason IS NULL) = (hold_by IS NULL)),
    -- a node with a check Ember's code reads is done only by the code or the owner (as a metric milestone, 0029)
    CHECK (status <> 'done' OR check_kind IS NULL OR check_kind = 'agent' OR closed_by IN ('code', 'owner'))
);
INSERT INTO plan_nodes_new (
    id, mode, session, parent_id, level, platform, project_id, template, stage, kind, channel, title, check_kind,
    check_spec, seq, status, waiting, source, obligation_id, due, effort, ready_since, pinned, owner_worth, could_earn,
    audience, hold_reason, hold_by, created_at, updated_at, closed_at, closed_by, result, wait_ref, wait_until, wait_why,
    live_since, decide_by, pushed_until
)
SELECT
    id, mode, session, parent_id, level, platform, project_id, template, stage, kind, channel, title, check_kind,
    check_spec, seq, status, waiting, source, obligation_id, due, effort, ready_since, pinned, owner_worth, could_earn,
    audience, hold_reason, CASE WHEN hold_reason IS NULL THEN NULL ELSE 'agent' END, created_at, updated_at, closed_at,
    closed_by, result, wait_ref, wait_until, wait_why, live_since, decide_by, pushed_until
FROM plan_nodes;
DROP TABLE plan_nodes;
ALTER TABLE plan_nodes_new RENAME TO plan_nodes;
CREATE INDEX plan_nodes_by_scope ON plan_nodes (mode, session, status, level);
CREATE INDEX plan_nodes_by_parent ON plan_nodes (parent_id, seq);
CREATE UNIQUE INDEX plan_nodes_one_product ON plan_nodes (mode, session, project_id) WHERE level = 'product';
CREATE UNIQUE INDEX plan_nodes_one_platform ON plan_nodes (mode, session, platform) WHERE level = 'project';
CREATE TRIGGER plan_nodes_no_delete BEFORE DELETE ON plan_nodes
BEGIN SELECT RAISE(ABORT, 'plan_nodes: rows cannot be deleted'); END;
CREATE TRIGGER plan_nodes_fixed BEFORE UPDATE ON plan_nodes
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.parent_id IS NOT OLD.parent_id
    OR NEW.level IS NOT OLD.level OR NEW.platform IS NOT OLD.platform OR NEW.project_id IS NOT OLD.project_id
    OR NEW.stage IS NOT OLD.stage OR NEW.kind IS NOT OLD.kind OR NEW.title IS NOT OLD.title
    OR NEW.check_kind IS NOT OLD.check_kind OR NEW.check_spec IS NOT OLD.check_spec OR NEW.source IS NOT OLD.source
    OR NEW.obligation_id IS NOT OLD.obligation_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'plan_nodes: a node''s place, title and check are fixed'); END;
CREATE TRIGGER plan_nodes_closed BEFORE UPDATE ON plan_nodes
WHEN OLD.status <> 'open'
    AND (NEW.status IS NOT OLD.status OR NEW.closed_at IS NOT OLD.closed_at OR NEW.closed_by IS NOT OLD.closed_by
        OR NEW.result IS NOT OLD.result)
BEGIN SELECT RAISE(ABORT, 'plan_nodes: a closed node is final'); END;

-- 0.36.0: the owner's freezes: what Ember may not change until a day, Ember's code enforcing it (live, "No title or tag
-- edits before 10-20" was a sentence in the standing instructions, which the plan and the critic never read). Each is
-- kept; lifting one sets lifted_at.
CREATE TABLE plan_freezes (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    what       TEXT NOT NULL CHECK (what IN ('titles_tags')),  -- the titles and tags of the live listings
    until      TEXT NOT NULL CHECK (length(until) = 10),  -- the last day it holds
    signed     TEXT CHECK (signed IS NULL OR length(signed) BETWEEN 1 AND 60),
    created_at TEXT NOT NULL,
    lifted_at  TEXT
);
CREATE INDEX plan_freezes_by_scope ON plan_freezes (mode, session, what, id);
CREATE TRIGGER plan_freezes_no_delete BEFORE DELETE ON plan_freezes
BEGIN SELECT RAISE(ABORT, 'plan_freezes: rows cannot be deleted'); END;
