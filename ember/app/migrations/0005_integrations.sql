-- 0.4.0: integrations. Some approvals are carried out by Ember's code instead of the owner: an approved
-- email is sent by Ember itself ('email'); an approved Reddit post becomes a prefilled link the owner opens
-- ('reddit_link'). What is carried out is the request's action (canonical JSON), fixed when the agent asked.
-- Ember's own mailbox is stored here too: every email in or out, what happened to every send, and the
-- addresses that asked not to get emails. Like the rest of the agent's records, it is scoped to a mode and a
-- dry-run session, and nothing can be deleted.

ALTER TABLE approvals ADD COLUMN executor TEXT CHECK (executor IS NULL OR executor IN ('email', 'reddit_link'));
ALTER TABLE approvals ADD COLUMN action TEXT CHECK (action IS NULL OR (json_valid(action) AND length(action) <= 12000));
-- Who closed an approval: the owner (the name the dashboard reported) or "Ember" (its executor).
ALTER TABLE approvals ADD COLUMN closed_by TEXT CHECK (closed_by IS NULL OR length(closed_by) <= 60);

CREATE TRIGGER approvals_action_pair BEFORE INSERT ON approvals
WHEN (NEW.executor IS NULL) <> (NEW.action IS NULL)
BEGIN SELECT RAISE(ABORT, 'approvals: an executor needs an action and an action needs an executor'); END;
CREATE TRIGGER approvals_action_fixed BEFORE UPDATE ON approvals
WHEN NEW.executor IS NOT OLD.executor OR NEW.action IS NOT OLD.action
BEGIN SELECT RAISE(ABORT, 'approvals: the action to carry out cannot change'); END;
CREATE TRIGGER approvals_closed_by_final BEFORE UPDATE ON approvals
WHEN OLD.closed_at IS NOT NULL AND NEW.closed_by IS NOT OLD.closed_by
BEGIN SELECT RAISE(ABORT, 'approvals: a decision is final'); END;

CREATE TABLE emails (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER REFERENCES lives (id),
    direction        TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    uidvalidity      INTEGER,
    uid              INTEGER CHECK (uid IS NULL OR uid > 0),
    message_id       TEXT CHECK (message_id IS NULL OR length(message_id) <= 998),
    in_reply_to      TEXT CHECK (in_reply_to IS NULL OR length(in_reply_to) <= 998),
    references_      TEXT CHECK (references_ IS NULL OR length(references_) <= 2000),
    from_addr        TEXT NOT NULL CHECK (length(from_addr) <= 320),
    from_name        TEXT CHECK (from_name IS NULL OR length(from_name) <= 200),
    to_addr          TEXT NOT NULL CHECK (length(to_addr) <= 2000),
    subject          TEXT NOT NULL CHECK (length(subject) <= 300),
    sent_at          TEXT,
    received_at      TEXT NOT NULL,
    body             TEXT NOT NULL CHECK (length(body) <= 8000),
    body_cut         INTEGER NOT NULL DEFAULT 0 CHECK (body_cut IN (0, 1)),
    attachments      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attachments) AND length(attachments) <= 2000),
    approval_id      INTEGER REFERENCES approvals (id),
    read_by_agent_at TEXT,
    seen_cycle_id    INTEGER REFERENCES cycles (id),
    -- An incoming email is identified by its IMAP UID; an outgoing one by the approval that sent it.
    CHECK ((direction = 'in') = (uid IS NOT NULL AND uidvalidity IS NOT NULL)),
    CHECK ((direction = 'out') = (approval_id IS NOT NULL))
);
CREATE UNIQUE INDEX emails_by_uid ON emails (mode, session, uidvalidity, uid) WHERE uid IS NOT NULL;
CREATE UNIQUE INDEX emails_one_per_approval ON emails (approval_id) WHERE approval_id IS NOT NULL;
CREATE INDEX emails_by_scope ON emails (mode, session, direction, id);
-- A stored email never changes; only whether the agent has read it (once) and seen it.
CREATE TRIGGER emails_content_fixed BEFORE UPDATE ON emails
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.direction IS NOT OLD.direction OR NEW.uidvalidity IS NOT OLD.uidvalidity OR NEW.uid IS NOT OLD.uid
    OR NEW.message_id IS NOT OLD.message_id OR NEW.in_reply_to IS NOT OLD.in_reply_to
    OR NEW.references_ IS NOT OLD.references_ OR NEW.from_addr IS NOT OLD.from_addr
    OR NEW.from_name IS NOT OLD.from_name OR NEW.to_addr IS NOT OLD.to_addr OR NEW.subject IS NOT OLD.subject
    OR NEW.sent_at IS NOT OLD.sent_at OR NEW.received_at IS NOT OLD.received_at OR NEW.body IS NOT OLD.body
    OR NEW.body_cut IS NOT OLD.body_cut OR NEW.attachments IS NOT OLD.attachments
    OR NEW.approval_id IS NOT OLD.approval_id
    OR (OLD.read_by_agent_at IS NOT NULL AND NEW.read_by_agent_at IS NOT OLD.read_by_agent_at)
BEGIN SELECT RAISE(ABORT, 'emails: a stored email cannot change'); END;
CREATE TRIGGER emails_no_delete BEFORE DELETE ON emails
BEGIN SELECT RAISE(ABORT, 'emails: rows cannot be deleted'); END;

-- One row per approved email Ember tried to send, inserted as 'running' and committed before anything is sent,
-- so a crash in the middle can never lead to a second send (it becomes 'unclear').
CREATE TABLE email_actions (
    id          INTEGER PRIMARY KEY,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'sent', 'failed', 'unclear', 'simulated')),
    message_id  TEXT CHECK (message_id IS NULL OR length(message_id) <= 998),
    result      TEXT CHECK (result IS NULL OR length(result) <= 500),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE TRIGGER email_actions_start_running BEFORE INSERT ON email_actions
WHEN NEW.status <> 'running'
BEGIN SELECT RAISE(ABORT, 'email_actions: an action starts as running'); END;
CREATE TRIGGER email_actions_final BEFORE UPDATE ON email_actions
WHEN OLD.status <> 'running' OR NEW.approval_id IS NOT OLD.approval_id OR NEW.started_at IS NOT OLD.started_at
    OR NEW.message_id IS NOT OLD.message_id
BEGIN SELECT RAISE(ABORT, 'email_actions: a finished action is final'); END;
CREATE TRIGGER email_actions_no_delete BEFORE DELETE ON email_actions
BEGIN SELECT RAISE(ABORT, 'email_actions: rows cannot be deleted'); END;

-- Addresses that replied "stop" (or "unsubscribe", "abmelden"): Ember never emails them again.
CREATE TABLE email_suppressions (
    mode     TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session  INTEGER NOT NULL,
    address  TEXT NOT NULL CHECK (length(address) BETWEEN 3 AND 320),
    since    TEXT NOT NULL,
    reason   TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 300),
    email_id INTEGER REFERENCES emails (id),
    PRIMARY KEY (mode, session, address)
);
CREATE TRIGGER email_suppressions_no_update BEFORE UPDATE ON email_suppressions
BEGIN SELECT RAISE(ABORT, 'email_suppressions: an opt-out cannot change'); END;
CREATE TRIGGER email_suppressions_no_delete BEFORE DELETE ON email_suppressions
BEGIN SELECT RAISE(ABORT, 'email_suppressions: an opt-out cannot be removed'); END;
