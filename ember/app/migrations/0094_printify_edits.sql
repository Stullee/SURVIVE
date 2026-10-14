-- 0.39.0 (the agent's request): Ember changes one of its live Printify products after the owner approved the change
-- (executor 'printify_edit'): a new design, title, description or tags, which Printify then publishes to the Etsy
-- listing it made. One row per approved change, inserted as 'running' and committed before anything is sent, so a
-- crash in the middle never makes the change twice (it becomes 'unclear'). 'partial': Printify took the change, but
-- didn't publish it to the listing. The change itself is the request's action: the product as Printify has it is the
-- one approved, with each change made since ('done' or 'partial') in turn.
CREATE TABLE printify_edits (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    product_id  TEXT NOT NULL CHECK (length(product_id) BETWEEN 1 AND 40),
    listing_id  INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'partial', 'failed', 'unclear')),
    result      TEXT CHECK (result IS NULL OR length(result) <= 1000),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX printify_edits_by_listing ON printify_edits (mode, session, listing_id, id);
CREATE TRIGGER printify_edits_no_delete BEFORE DELETE ON printify_edits
BEGIN SELECT RAISE(ABORT, 'printify_edits: rows cannot be deleted'); END;
