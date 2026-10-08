-- 0.34.0: the plan tree (Release 2, in the shadow first). Live, on 2026-10-07 twelve cycles went to eight things:
-- READY's ranking, the obligations, the spending shares and the daily review each decided a part of what Ember worked
-- on. One tree under the owner's goal now holds the plan: projects (a platform: Etsy, KDP, Printify, the website, the
-- channels), their products (a project row: a product line), each product's stages (research, create, release,
-- launch, maintain) and the small steps under them, each with a check Ember's code reads. In 0.34.0 the tree only
-- runs in the shadow: Ember's code lays it out and keeps it (agent/plan.py), and each cycle records the step the tree
-- would have taken next to what READY took (plan_picks), so a week of real cycles can tune the weights
-- (agent/weights.py) before the tree steers.
CREATE TABLE plan_nodes (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    parent_id     INTEGER REFERENCES plan_nodes (id),
    level         TEXT NOT NULL CHECK (level IN ('project', 'product', 'stage', 'step')),
    platform      TEXT CHECK (platform IN ('etsy', 'kdp', 'printify', 'website', 'channels', 'other')),
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
    owner_worth   REAL CHECK (owner_worth IS NULL OR owner_worth BETWEEN 0.5 AND 10),  -- the owner's worth (a product)
    could_earn    REAL CHECK (could_earn IS NULL OR could_earn >= 0),  -- a product: expected $ a month, from records
    audience      TEXT CHECK (audience IN ('en', 'de', 'both')),  -- a product: who its marketing speaks to
    hold_reason   TEXT CHECK (hold_reason IS NULL OR length(hold_reason) BETWEEN 1 AND 200),  -- Release 2b: on hold
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    closed_by     TEXT CHECK (closed_by IN ('code', 'owner', 'agent')),
    result        TEXT CHECK (result IS NULL OR length(result) BETWEEN 1 AND 300),
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
    CHECK (level = 'product' OR owner_worth IS NULL AND could_earn IS NULL AND audience IS NULL AND hold_reason IS NULL),
    -- a node with a check Ember's code reads is done only by the code or the owner (as a metric milestone, 0029)
    CHECK (status <> 'done' OR check_kind IS NULL OR check_kind = 'agent' OR closed_by IN ('code', 'owner'))
);
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

-- Each cycle's shadow pick: the step the tree would have taken, with its weight's parts and the ranking it came from,
-- next to what READY took (the cycle's kind and the line its plan took), so a week can be re-scored offline.
CREATE TABLE plan_picks (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    cycle_id   INTEGER NOT NULL UNIQUE REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('ordinary', 'marketing', 'venture', 'reactive')),  -- the cycle's kind
    line       INTEGER REFERENCES projects (id),  -- the line READY's plan took (NULL: none)
    node_id    INTEGER REFERENCES plan_nodes (id),  -- the step the tree would have taken
    product    INTEGER REFERENCES projects (id),  -- that step's product line
    decided    TEXT NOT NULL CHECK (decided IN ('pin', 'promise', 'venture', 'weight', 'margin', 'none')),
    weight     REAL,
    parts      TEXT CHECK (parts IS NULL OR json_valid(parts)),
    ranked     TEXT NOT NULL CHECK (json_valid(ranked) AND json_type(ranked) = 'array'),
    CHECK ((node_id IS NULL) = (decided IN ('venture', 'none'))),
    CHECK ((weight IS NULL) = (node_id IS NULL))
);
CREATE INDEX plan_picks_by_scope ON plan_picks (mode, session, id);
CREATE TRIGGER plan_picks_no_update BEFORE UPDATE ON plan_picks
BEGIN SELECT RAISE(ABORT, 'plan_picks: a pick never changes'); END;
CREATE TRIGGER plan_picks_no_delete BEFORE DELETE ON plan_picks
BEGIN SELECT RAISE(ABORT, 'plan_picks: rows cannot be deleted'); END;

-- The owner's word on the plan: a step pinned or unpinned, a product's worth set or cleared (more in Release 2c).
CREATE TABLE plan_words (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    node_id    INTEGER NOT NULL REFERENCES plan_nodes (id),
    action     TEXT NOT NULL CHECK (action IN ('pin', 'unpin', 'worth', 'clear_worth')),
    value      REAL CHECK (value IS NULL OR value BETWEEN 0.5 AND 10),
    signed     TEXT CHECK (signed IS NULL OR length(signed) BETWEEN 1 AND 60),
    created_at TEXT NOT NULL,
    CHECK ((action = 'worth') = (value IS NOT NULL))
);
CREATE INDEX plan_words_by_scope ON plan_words (mode, session, id);
CREATE TRIGGER plan_words_no_update BEFORE UPDATE ON plan_words
BEGIN SELECT RAISE(ABORT, 'plan_words: the owner''s word never changes'); END;
CREATE TRIGGER plan_words_no_delete BEFORE DELETE ON plan_words
BEGIN SELECT RAISE(ABORT, 'plan_words: rows cannot be deleted'); END;
