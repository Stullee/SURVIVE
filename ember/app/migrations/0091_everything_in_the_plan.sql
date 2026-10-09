-- 0.37.0: everything Ember does is a step of the plan, weighed like any other. The owner: "integrate potential
-- ventures as a sub goal into the plan" and "shouldn't promises just move into the plan and be evaluated for weight?".
-- Each venture being explored is a node of the Ventures project ('venture' level, venture_id), its next decision a step
-- under it (in place of 0.36.0's one Explore step and the decision desk's READY list); the promises and the owner's
-- decisions of no product line are steps of the Owner project ('owner' platform). A venture may hold the owner's
-- worth, as a product does. The table is rebuilt (SQLite can't change a CHECK) with its rows and their ids; its indexes
-- and triggers are made again, the trigger that fixes a node's place now also fixing its venture.
CREATE TABLE plan_nodes_new (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    parent_id     INTEGER REFERENCES plan_nodes (id),
    level         TEXT NOT NULL CHECK (level IN ('project', 'product', 'venture', 'stage', 'step')),
    platform      TEXT CHECK (platform IN ('etsy', 'kdp', 'printify', 'website', 'channels', 'other', 'ventures', 'owner')),
    project_id    INTEGER REFERENCES projects (id),  -- the product's line (a product, its stages and steps)
    venture_id    INTEGER REFERENCES ventures (id),  -- 0.37.0: a venture's node
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
    CHECK ((level = 'venture') = (venture_id IS NOT NULL)),
    CHECK (level NOT IN ('product', 'stage') OR project_id IS NOT NULL),
    CHECK (level NOT IN ('project', 'product', 'venture') OR stage IS NULL AND kind IS NULL),
    CHECK (level <> 'stage' OR stage IS NOT NULL AND kind IS NULL),
    CHECK ((level = 'step') = (kind IS NOT NULL)),
    CHECK (level <> 'step' OR stage IS NOT NULL OR project_id IS NULL),  -- a product's step names its stage
    CHECK (pinned = 0 OR level = 'step'),
    CHECK (level = 'product' OR could_earn IS NULL AND audience IS NULL),
    -- a worth is a product's, a venture's (0.37.0) or all the ventures' (the Ventures project's); a hold a product's
    -- or the Ventures project's
    CHECK (level IN ('product', 'venture') OR level = 'project' AND platform = 'ventures' OR owner_worth IS NULL),
    CHECK (level = 'product' OR level = 'project' AND platform = 'ventures' OR hold_reason IS NULL),
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
    audience, hold_reason, hold_by, created_at, updated_at, closed_at, closed_by, result, wait_ref, wait_until, wait_why,
    live_since, decide_by, pushed_until
FROM plan_nodes;
DROP TABLE plan_nodes;
ALTER TABLE plan_nodes_new RENAME TO plan_nodes;
CREATE INDEX plan_nodes_by_scope ON plan_nodes (mode, session, status, level);
CREATE INDEX plan_nodes_by_parent ON plan_nodes (parent_id, seq);
CREATE UNIQUE INDEX plan_nodes_one_product ON plan_nodes (mode, session, project_id) WHERE level = 'product';
CREATE UNIQUE INDEX plan_nodes_one_platform ON plan_nodes (mode, session, platform) WHERE level = 'project';
-- a venture has one open node (a parked one taken up again gets a new one)
CREATE UNIQUE INDEX plan_nodes_one_venture ON plan_nodes (mode, session, venture_id)
    WHERE level = 'venture' AND status = 'open';
CREATE TRIGGER plan_nodes_no_delete BEFORE DELETE ON plan_nodes
BEGIN SELECT RAISE(ABORT, 'plan_nodes: rows cannot be deleted'); END;
CREATE TRIGGER plan_nodes_fixed BEFORE UPDATE ON plan_nodes
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.parent_id IS NOT OLD.parent_id
    OR NEW.level IS NOT OLD.level OR NEW.platform IS NOT OLD.platform OR NEW.project_id IS NOT OLD.project_id
    OR NEW.venture_id IS NOT OLD.venture_id OR NEW.stage IS NOT OLD.stage OR NEW.kind IS NOT OLD.kind
    OR NEW.title IS NOT OLD.title OR NEW.check_kind IS NOT OLD.check_kind OR NEW.check_spec IS NOT OLD.check_spec
    OR NEW.source IS NOT OLD.source OR NEW.obligation_id IS NOT OLD.obligation_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'plan_nodes: a node''s place, title and check are fixed'); END;
CREATE TRIGGER plan_nodes_closed BEFORE UPDATE ON plan_nodes
WHEN OLD.status <> 'open'
    AND (NEW.status IS NOT OLD.status OR NEW.closed_at IS NOT OLD.closed_at OR NEW.closed_by IS NOT OLD.closed_by
        OR NEW.result IS NOT OLD.result)
BEGIN SELECT RAISE(ABORT, 'plan_nodes: a closed node is final'); END;

-- 0.36.0's one Explore step gives way to each venture's own (Ember's code lays them out at the next cycle).
UPDATE plan_nodes
SET status = 'dropped', closed_by = 'code', closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), pinned = 0,
    result = '0.37.0: each venture''s next decision is a step of its own'
WHERE template = 'explore@1' AND status = 'open';
