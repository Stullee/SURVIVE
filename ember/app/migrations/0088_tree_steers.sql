-- 0.35.0: the plan tree steers (Release 2b). Ember's code takes each cycle's step from the tree (agent/plan.py) and the
-- step decides what the cycle is. Ember may add, split and replace steps, say a step waits on a block the code can
-- check, and hold a product to work elsewhere; each change is kept with its reason, so the owner sees what changed.
-- The listing test's bars and the goal's decision points retire: each product's decide-by dates take their place.

-- A step Ember says waits: on a request to the owner (wait_ref: its number), an upgrade request (its number), another
-- step (its number) or a day (wait_until), with her words (wait_why). The code checks the block each cycle and lifts
-- the wait once it is gone.
ALTER TABLE plan_nodes ADD COLUMN wait_ref INTEGER;
ALTER TABLE plan_nodes ADD COLUMN wait_until TEXT CHECK (wait_until IS NULL OR length(wait_until) = 10);
ALTER TABLE plan_nodes ADD COLUMN wait_why TEXT CHECK (wait_why IS NULL OR length(wait_why) BETWEEN 1 AND 200);

-- Every change to the tree that isn't a check passing: by Ember (a step added, split, replaced, done or waiting, a
-- product held or resumed), by the code (a step for an owner's decision) or by the owner (a product dropped). Never
-- changed or deleted.
CREATE TABLE plan_changes (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    node_id    INTEGER NOT NULL REFERENCES plan_nodes (id),
    cycle_id   INTEGER REFERENCES cycles (id),
    actor      TEXT NOT NULL CHECK (actor IN ('agent', 'code', 'owner')),
    action     TEXT NOT NULL CHECK (action IN ('add', 'split', 'replace', 'done', 'wait', 'unwait', 'hold', 'resume',
                                               'drop', 'close')),
    why        TEXT NOT NULL CHECK (length(why) BETWEEN 1 AND 300),
    created_at TEXT NOT NULL
);
CREATE INDEX plan_changes_by_scope ON plan_changes (mode, session, id);
CREATE INDEX plan_changes_by_node ON plan_changes (node_id, id);
CREATE TRIGGER plan_changes_no_update BEFORE UPDATE ON plan_changes
BEGIN SELECT RAISE(ABORT, 'plan_changes: a change never changes'); END;
CREATE TRIGGER plan_changes_no_delete BEFORE DELETE ON plan_changes
BEGIN SELECT RAISE(ABORT, 'plan_changes: rows cannot be deleted'); END;

-- A product's decide-by dates (in place of the listing test's bars, agent/gates.py until 0.34.0): the day its test
-- began (its first listing seen live), what each date found (day7, day14, day28, day21: met, missed, retry, decide,
-- scale), and until when its marketing is urgent (a views bar missed).
ALTER TABLE plan_nodes ADD COLUMN live_since TEXT CHECK (live_since IS NULL OR length(live_since) = 10);
ALTER TABLE plan_nodes ADD COLUMN decide_by TEXT CHECK (decide_by IS NULL OR json_valid(decide_by));
ALTER TABLE plan_nodes ADD COLUMN pushed_until TEXT CHECK (pushed_until IS NULL OR length(pushed_until) = 10);

-- Each product keeps one hidden milestone (Ember's code's, of the kind first_test, linked to its line): the owner's
-- Autonomy unlocks, the costs counted toward a milestone and the digests keep working on it. The roadmap's readers
-- leave it out.
ALTER TABLE milestones ADD COLUMN product_node INTEGER REFERENCES plan_nodes (id);
CREATE UNIQUE INDEX milestones_one_per_product ON milestones (product_node) WHERE product_node IS NOT NULL;

-- The milestones open at the upgrade that the plan tree takes the place of move into it once (agent/plan.py, ``move``:
-- the owner's Autonomy unlocks on them carried to their product's milestone first, then closed, each in the owner's
-- System log): the listing test's bars and scale points, the goal's decision points and Ember's own milestones (one
-- of a product becomes a step of it). This is the newest milestone they can be; Ember has none of her own after it
-- (milestone_plan retired).
INSERT INTO meta (key, value, updated_at)
VALUES ('plan_tree.milestones_upto', CAST((SELECT COALESCE(MAX(id), 0) FROM milestones) AS TEXT),
        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
ON CONFLICT (key) DO NOTHING;
