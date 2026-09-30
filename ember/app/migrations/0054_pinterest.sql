-- 0.13.0 (Phase E2): Pinterest, the owner's account (integrations/pinterest.py). The agent proposes a pin that brings
-- buyers to one of Ember's live Etsy listings; the owner approves; Ember's code creates it (a new board first, when
-- the pin goes on one).
--
-- A new channel's requests used to need the approvals table rebuilt for its executor (0010, 0011). This rebuild is the
-- last one: an executor is now any short name, and Ember's code (connectors.EXECUTORS) and the NEVER check decide
-- which exist (an executor the view below doesn't name is what only the owner carries out: never on an unlock). The
-- view approvals_never and the triggers that read approvals are dropped first and made again, unchanged but for the
-- new executor. Nothing else about approvals changes (same columns, indexes and triggers).
DROP VIEW approvals_never;
DROP TRIGGER approvals_never_on_unlock;
DROP TRIGGER approvals_unlock_carries;
DROP TRIGGER policy_uses_never;
DROP TRIGGER policy_uses_standing;
DROP TRIGGER action_undos_once;

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
    -- 0.13.0: any short lower-case name (connectors.EXECUTORS says which exist)
    executor         TEXT CHECK (executor IS NULL OR (length(executor) BETWEEN 1 AND 40 AND executor NOT GLOB '*[^a-z_]*')),
    action           TEXT CHECK (action IS NULL OR (json_valid(action) AND length(action) <= 12000)),
    closed_by        TEXT CHECK (closed_by IS NULL OR length(closed_by) <= 60),
    venture_id       INTEGER REFERENCES ventures (id),
    milestone_id     INTEGER REFERENCES milestones (id)
);
INSERT INTO approvals_new (
    id, mode, session, life_id, cycle_id, project_id, created_at, type, title, description, payload, payload_sha256,
    expected_cost, expected_benefit, status, seen_cycle_id, decided_at, decided_by, decision_comment, final_payload,
    closed_at, result_note, result_link, version, executor, action, closed_by, venture_id, milestone_id
)
SELECT
    id, mode, session, life_id, cycle_id, project_id, created_at, type, title, description, payload, payload_sha256,
    expected_cost, expected_benefit, status, seen_cycle_id, decided_at, decided_by, decision_comment, final_payload,
    closed_at, result_note, result_link, version, executor, action, closed_by, venture_id, milestone_id
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
CREATE TRIGGER approvals_attribution_fixed BEFORE UPDATE OF venture_id, milestone_id ON approvals
WHEN NEW.venture_id IS NOT OLD.venture_id OR NEW.milestone_id IS NOT OLD.milestone_id
BEGIN SELECT RAISE(ABORT, 'approvals: what a request worked for is fixed'); END;
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

-- A venture a channel of Ember's code serves (the seeded "Pinterest for the Etsy shop"): its first test is that
-- channel's (agent/stages.py: pins that bring clicks), and its kill rule the first test's.
ALTER TABLE ventures ADD COLUMN channel TEXT CHECK (channel IS NULL OR length(channel) BETWEEN 1 AND 40);
UPDATE ventures SET channel = 'pinterest' WHERE title = 'Pinterest for the Etsy shop' AND created_by = 'owner';

-- Ember's boards and pins (like etsy_listings: a row committed as 'running' before anything is sent, so a crash never
-- makes one twice; 'unclear' when it can't be known whether Pinterest made it).
CREATE TABLE pinterest_boards (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL REFERENCES approvals (id),
    board_id    TEXT CHECK (board_id IS NULL OR length(board_id) <= 40),  -- Pinterest's, once it exists
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 50),
    status      TEXT NOT NULL CHECK (status IN ('running', 'active', 'failed', 'unclear')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX pinterest_boards_once ON pinterest_boards (approval_id);
CREATE TRIGGER pinterest_boards_no_delete BEFORE DELETE ON pinterest_boards
BEGIN SELECT RAISE(ABORT, 'pinterest_boards: rows cannot be deleted'); END;

CREATE TABLE pinterest_pins (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    pin_id      TEXT CHECK (pin_id IS NULL OR length(pin_id) <= 40),  -- Pinterest's, once it exists
    board_id    TEXT CHECK (board_id IS NULL OR length(board_id) <= 40),
    title       TEXT NOT NULL CHECK (length(title) <= 100),
    link        TEXT NOT NULL CHECK (length(link) <= 500),
    status      TEXT NOT NULL CHECK (status IN ('running', 'active', 'deleted', 'failed', 'unclear')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    impressions INTEGER,  -- at the last sync
    saves       INTEGER,
    clicks      INTEGER,  -- outbound: to the link
    synced_at   TEXT,
    result      TEXT CHECK (result IS NULL OR length(result) <= 500),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX pinterest_pins_by_scope ON pinterest_pins (mode, session, id);
CREATE TRIGGER pinterest_pins_no_delete BEFORE DELETE ON pinterest_pins
BEGIN SELECT RAISE(ABORT, 'pinterest_pins: rows cannot be deleted'); END;

-- NEVER (0051), again: a pin that makes a new board is a new public presence while Ember has no board yet (the first
-- board is the owner's decision); an executor this view doesn't name is what only the owner carries out.
CREATE VIEW approvals_never AS
SELECT
    approval_id, account, money, first_contact, first_publication, community_post, legal, owner_only,
    account OR money OR first_contact OR first_publication OR community_post OR legal OR owner_only AS never
FROM (
    SELECT
        r.id AS approval_id,
        r.type = 'create_account' AS account,
        r.type = 'spend_money' AS money,
        r.executor IS 'email' AND (
            json_type(r.action, '$.to') IS NOT 'text' OR json_extract(r.action, '$.to') = '' OR NOT EXISTS (
                SELECT 1 FROM emails e WHERE e.mode = r.mode AND e.session = r.session AND e.direction = 'in'
                    AND lower(e.from_addr) = lower(json_extract(r.action, '$.to'))
            )
        ) AS first_contact,
        (r.executor IS 'etsy_listing' AND NOT EXISTS (
            SELECT 1 FROM etsy_listings l WHERE l.mode = r.mode AND l.session = r.session AND l.status = 'active'
        )) OR (r.executor IS 'pinterest_pin' AND NOT EXISTS (
            SELECT 1 FROM pinterest_boards b WHERE b.mode = r.mode AND b.session = r.session AND b.status = 'active'
        )) AS first_publication,
        r.executor IS 'reddit_link' AS community_post,
        r.words GLOB '*steuer*' OR r.words GLOB '*gewerbe*' OR r.words GLOB '*finanzamt*' OR r.words GLOB '*vertrag*'
            OR r.words GLOB '*verträg*' OR r.words GLOB '*vertrÄg*' OR r.words GLOB '*[^a-z]tax[^a-z]*'
            OR r.words GLOB '*[^a-z]taxes[^a-z]*' OR r.words GLOB '*[^a-z]taxed[^a-z]*' OR r.words GLOB '*[^a-z]vat[^a-z]*'
            OR r.words GLOB '*[^a-z]ust[^a-z]*' OR r.words GLOB '*[^a-z]mwst[^a-z]*'
            OR r.words GLOB '*[^a-z]contract[^a-z]*' OR r.words GLOB '*[^a-z]contracts[^a-z]*' AS legal,
        r.executor IS NULL OR r.executor NOT IN ('email', 'reddit_link', 'etsy_listing', 'etsy_edit', 'pinterest_pin')
            AS owner_only
    FROM (
        SELECT id, mode, session, type, executor, action,
            ' ' || lower(title || ' ' || description || ' ' || payload) || ' ' AS words
        FROM approvals
    ) r
);
CREATE TRIGGER approvals_never_on_unlock BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'approvals: an unlock never approves this request (NEVER)'); END;
CREATE TRIGGER approvals_unlock_carries BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND NOT EXISTS (
        SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id
        WHERE u.approval_id = NEW.id AND g.level = u.level
            AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
    )
BEGIN SELECT RAISE(ABORT, 'approvals: no standing unlock carries this request'); END;
CREATE TRIGGER policy_uses_never BEFORE INSERT ON policy_uses
WHEN (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: an unlock never carries this request (NEVER)'); END;
CREATE TRIGGER policy_uses_standing BEFORE INSERT ON policy_uses
WHEN NOT EXISTS (
    SELECT 1 FROM policy_grants g JOIN approvals a ON a.id = NEW.approval_id JOIN milestones m ON m.id = g.milestone_id
    WHERE g.id = NEW.grant_id AND g.level = NEW.level AND g.mode = a.mode AND g.session = a.session
        AND g.milestone_id = a.milestone_id AND m.status = 'open'
        AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
)
BEGIN SELECT RAISE(ABORT, 'policy_uses: no standing unlock carries this request'); END;
CREATE TRIGGER action_undos_once BEFORE INSERT ON action_undos
WHEN EXISTS (
    SELECT 1 FROM action_undos u JOIN approvals a ON a.id = u.approval_id
    WHERE u.journal_id = NEW.journal_id AND NOT (
        a.status = 'failed'
        AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = u.approval_id AND j.status <> 'failed')
    )
)
BEGIN SELECT RAISE(ABORT, 'action_undos: this action is undone or being undone'); END;
