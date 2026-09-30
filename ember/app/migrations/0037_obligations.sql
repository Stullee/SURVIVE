-- 0.12.0: what the agent owes, kept by Ember's code. Promises were only prose (a venture cycle deferred the owner's
-- quick fix; an answer that promised work for later was forgotten), and a decision of the owner's was shown once.
-- An obligation is one of:
--   promise   what the agent promised its owner in a message (message_owner commits), due on a day it named;
--   decision  a decision of the owner's the agent must react to: a request rejected, carried out by the owner, or
--             failed;
--   miss      a milestone Ember's code closed missed: the agent decides what now.
-- The agent closes one when it has met it, saying what it did (a promise only once the owner has heard from it
-- since); a miss also closes when a milestone replaces it. Obligations are shown first in the plan, never cut, and a
-- pressing one makes a wake cycle an ordinary one rather than a venture cycle. The owner's unanswered messages,
-- overdue milestones and listings with too few photos are obligations too; Ember's code reads them from their own
-- records.
CREATE TABLE obligations (
    id              INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session         INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('promise', 'decision', 'miss')),
    what            TEXT NOT NULL CHECK (length(what) BETWEEN 1 AND 400),
    due             TEXT NOT NULL CHECK (length(due) = 10),  -- the owner's day it is due
    created_at      TEXT NOT NULL,
    cycle_id        INTEGER REFERENCES cycles (id),  -- the cycle that made the promise
    message_id      INTEGER REFERENCES messages (id),  -- the message that made the promise
    approval_id     INTEGER UNIQUE REFERENCES approvals (id),  -- the decided request
    milestone_id    INTEGER UNIQUE REFERENCES milestones (id),  -- the missed milestone
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at       TEXT,
    closed_by       TEXT CHECK (closed_by IS NULL OR closed_by IN ('agent', 'code')),
    closed_cycle_id INTEGER REFERENCES cycles (id),
    result          TEXT CHECK (result IS NULL OR length(result) <= 300),
    CHECK ((kind = 'promise') = (message_id IS NOT NULL)),
    CHECK ((kind = 'decision') = (approval_id IS NOT NULL)),
    CHECK ((kind = 'miss') = (milestone_id IS NOT NULL)),
    CHECK ((status = 'open') = (closed_at IS NULL AND closed_by IS NULL))
);
CREATE INDEX obligations_by_scope ON obligations (mode, session, status, due);
CREATE TRIGGER obligations_no_delete BEFORE DELETE ON obligations
BEGIN SELECT RAISE(ABORT, 'obligations: rows cannot be deleted'); END;
CREATE TRIGGER obligations_fixed BEFORE UPDATE ON obligations
WHEN OLD.status <> 'open' OR NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.kind IS NOT OLD.kind
    OR NEW.what IS NOT OLD.what OR NEW.due IS NOT OLD.due OR NEW.created_at IS NOT OLD.created_at
    OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.message_id IS NOT OLD.message_id
    OR NEW.approval_id IS NOT OLD.approval_id OR NEW.milestone_id IS NOT OLD.milestone_id
BEGIN SELECT RAISE(ABORT, 'obligations: an obligation is fixed, and a closed one is final'); END;
