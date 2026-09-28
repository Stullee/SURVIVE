-- 0.8.0: Etsy. An approved listing is created by Ember's code (executor 'etsy_listing'), so the approvals table
-- is rebuilt with that executor allowed; nothing else about it changes (same columns, indexes and triggers).
CREATE TABLE approvals_new (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    cycle_id         INTEGER NOT NULL REFERENCES cycles (id),
    project_id       INTEGER REFERENCES projects (id),
    created_at       TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('publish', 'contact', 'create_account', 'spend_money', 'sell', 'other')),
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    description      TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    payload          TEXT NOT NULL CHECK (length(payload) BETWEEN 1 AND 8000),
    payload_sha256   TEXT NOT NULL,
    -- Words only: never parsed as money and never used to prefill an owner form.
    expected_cost    TEXT NOT NULL CHECK (length(expected_cost) BETWEEN 1 AND 300),
    expected_benefit TEXT NOT NULL CHECK (length(expected_benefit) BETWEEN 1 AND 300),
    status           TEXT NOT NULL DEFAULT 'pending',
    seen_cycle_id    INTEGER REFERENCES cycles (id),
    decided_at       TEXT,
    decided_by       TEXT,
    decision_comment TEXT CHECK (decision_comment IS NULL OR length(decision_comment) <= 2000),
    final_payload    TEXT CHECK (final_payload IS NULL OR length(final_payload) <= 8000),
    closed_at        TEXT,
    result_note      TEXT CHECK (result_note IS NULL OR length(result_note) <= 2000),
    result_link      TEXT CHECK (result_link IS NULL OR length(result_link) <= 2048),
    version          INTEGER NOT NULL DEFAULT 0,
    executor         TEXT CHECK (executor IS NULL OR executor IN ('email', 'reddit_link', 'etsy_listing')),
    action           TEXT CHECK (action IS NULL OR (json_valid(action) AND length(action) <= 12000)),
    closed_by        TEXT CHECK (closed_by IS NULL OR length(closed_by) <= 60)
);
INSERT INTO approvals_new (
    id, mode, session, life_id, cycle_id, project_id, created_at, type, title, description, payload, payload_sha256,
    expected_cost, expected_benefit, status, seen_cycle_id, decided_at, decided_by, decision_comment, final_payload,
    closed_at, result_note, result_link, version, executor, action, closed_by
)
SELECT
    id, mode, session, life_id, cycle_id, project_id, created_at, type, title, description, payload, payload_sha256,
    expected_cost, expected_benefit, status, seen_cycle_id, decided_at, decided_by, decision_comment, final_payload,
    closed_at, result_note, result_link, version, executor, action, closed_by
FROM approvals;
DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;

CREATE INDEX approvals_by_scope ON approvals (mode, session, status);
CREATE UNIQUE INDEX approvals_one_pending ON approvals (mode, session, payload_sha256) WHERE status = 'pending';

CREATE TRIGGER approvals_action_fixed BEFORE UPDATE ON approvals
WHEN NEW.executor IS NOT OLD.executor OR NEW.action IS NOT OLD.action
BEGIN SELECT RAISE(ABORT, 'approvals: the action to carry out cannot change'); END;

CREATE TRIGGER approvals_action_pair BEFORE INSERT ON approvals
WHEN (NEW.executor IS NULL) <> (NEW.action IS NULL)
BEGIN SELECT RAISE(ABORT, 'approvals: an executor needs an action and an action needs an executor'); END;

CREATE TRIGGER approvals_closed_by_final BEFORE UPDATE ON approvals
WHEN OLD.closed_at IS NOT NULL AND NEW.closed_by IS NOT OLD.closed_by
BEGIN SELECT RAISE(ABORT, 'approvals: a decision is final'); END;

CREATE TRIGGER approvals_decision_final BEFORE UPDATE ON approvals
WHEN (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at OR NEW.decided_by IS NOT OLD.decided_by
        OR NEW.decision_comment IS NOT OLD.decision_comment OR NEW.final_payload IS NOT OLD.final_payload))
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.result_note IS NOT OLD.result_note
        OR NEW.result_link IS NOT OLD.result_link))
BEGIN
    SELECT RAISE(ABORT, 'approvals: a decision is final');
END;

CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals: rows cannot be deleted'); END;

CREATE TRIGGER approvals_request_fixed BEFORE UPDATE ON approvals
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.project_id IS NOT OLD.project_id OR NEW.created_at IS NOT OLD.created_at
    OR NEW.type IS NOT OLD.type OR NEW.title IS NOT OLD.title OR NEW.description IS NOT OLD.description
    OR NEW.payload IS NOT OLD.payload OR NEW.payload_sha256 IS NOT OLD.payload_sha256
    OR NEW.expected_cost IS NOT OLD.expected_cost OR NEW.expected_benefit IS NOT OLD.expected_benefit
BEGIN SELECT RAISE(ABORT, 'approvals: the request itself cannot change'); END;

CREATE TRIGGER approvals_status_flow BEFORE UPDATE OF status ON approvals
WHEN NEW.status IS NOT OLD.status AND NOT (
    (OLD.status = 'pending' AND NEW.status IN ('approved', 'approved_with_changes', 'rejected', 'withdrawn', 'expired'))
    OR (OLD.status IN ('approved', 'approved_with_changes') AND NEW.status IN ('done', 'failed'))
)
BEGIN
    SELECT RAISE(ABORT, 'approvals: not an allowed status change');
END;

-- One row per approved listing Ember's code creates on Etsy, inserted as 'running' and committed before anything
-- is sent, so a crash in the middle never leads to a second listing (it becomes 'unclear'). The last sync's
-- numbers are kept with it, for the agent's plans and reviews.
CREATE TABLE etsy_listings (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'active', 'draft', 'failed', 'unclear')),
    listing_id  INTEGER,  -- Etsy's number, once the draft exists
    title       TEXT NOT NULL CHECK (length(title) <= 140),
    state       TEXT CHECK (state IS NULL OR length(state) <= 20),  -- Etsy's state at the last sync
    views       INTEGER,
    favorites   INTEGER,
    synced_at   TEXT,
    result      TEXT CHECK (result IS NULL OR length(result) <= 500),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX etsy_listings_by_scope ON etsy_listings (mode, session, id);
CREATE TRIGGER etsy_listings_no_delete BEFORE DELETE ON etsy_listings
BEGIN SELECT RAISE(ABORT, 'etsy_listings: rows cannot be deleted'); END;

-- The shop's orders (Etsy receipts) as the last sync saw them: what sold, for the agent and for the owner, who
-- records the revenue (Ember never does).
CREATE TABLE etsy_orders (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    receipt_id  INTEGER NOT NULL,
    ordered_at  TEXT NOT NULL,
    total       TEXT NOT NULL CHECK (length(total) <= 40),  -- e.g. "9.80 EUR", as Etsy reports it
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    currency    TEXT NOT NULL CHECK (length(currency) = 3),
    items       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(items) AND length(items) <= 4000),
    synced_at   TEXT NOT NULL,
    UNIQUE (mode, session, receipt_id)
);
CREATE INDEX etsy_orders_by_scope ON etsy_orders (mode, session, ordered_at);
