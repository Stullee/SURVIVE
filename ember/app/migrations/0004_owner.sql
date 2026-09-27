-- Phase 4: the owner's side of the queues. Only the owner's HTTP endpoints write these
-- columns; the database enforces which status changes are possible and that a decision,
-- once made, stays as it was.

ALTER TABLE approvals ADD COLUMN decided_at TEXT;
ALTER TABLE approvals ADD COLUMN decided_by TEXT;
ALTER TABLE approvals ADD COLUMN decision_comment TEXT CHECK (decision_comment IS NULL OR length(decision_comment) <= 2000);
ALTER TABLE approvals ADD COLUMN final_payload TEXT CHECK (final_payload IS NULL OR length(final_payload) <= 8000);
ALTER TABLE approvals ADD COLUMN closed_at TEXT;
ALTER TABLE approvals ADD COLUMN result_note TEXT CHECK (result_note IS NULL OR length(result_note) <= 2000);
ALTER TABLE approvals ADD COLUMN result_link TEXT CHECK (result_link IS NULL OR length(result_link) <= 2048);
ALTER TABLE approvals ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

CREATE TRIGGER approvals_status_flow BEFORE UPDATE OF status ON approvals
WHEN NEW.status IS NOT OLD.status AND NOT (
    (OLD.status = 'pending' AND NEW.status IN ('approved', 'approved_with_changes', 'rejected', 'withdrawn', 'expired'))
    OR (OLD.status IN ('approved', 'approved_with_changes') AND NEW.status IN ('done', 'failed'))
)
BEGIN
    SELECT RAISE(ABORT, 'approvals: not an allowed status change');
END;
CREATE TRIGGER approvals_decision_final BEFORE UPDATE ON approvals
WHEN (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at OR NEW.decided_by IS NOT OLD.decided_by
        OR NEW.decision_comment IS NOT OLD.decision_comment OR NEW.final_payload IS NOT OLD.final_payload))
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.result_note IS NOT OLD.result_note
        OR NEW.result_link IS NOT OLD.result_link))
BEGIN
    SELECT RAISE(ABORT, 'approvals: a decision is final');
END;

ALTER TABLE upgrades ADD COLUMN decided_at TEXT;
ALTER TABLE upgrades ADD COLUMN owner_note TEXT CHECK (owner_note IS NULL OR length(owner_note) <= 2000);
ALTER TABLE upgrades ADD COLUMN released_version TEXT CHECK (released_version IS NULL OR length(released_version) <= 20);

CREATE TRIGGER upgrades_status_flow BEFORE UPDATE OF status ON upgrades
WHEN NEW.status IS NOT OLD.status AND NOT (
    (OLD.status = 'new' AND NEW.status IN ('accepted', 'declined', 'released', 'withdrawn'))
    OR (OLD.status = 'accepted' AND NEW.status IN ('released', 'declined'))
)
BEGIN
    SELECT RAISE(ABORT, 'upgrades: not an allowed status change');
END;

ALTER TABLE messages ADD COLUMN entered_by TEXT CHECK (entered_by IS NULL OR length(entered_by) <= 60);
