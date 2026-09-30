-- 0.14.0: the unlock loopholes closed before any unlock (agent/policy.py). An unlock taken back, by the owner, the kill
-- switch or Ember's code, stopped only what it held: what it had approved still ran, maybe a day later (an email or a
-- listing waiting for the daily limit). Now Ember's code puts such a request back to waiting for the owner, with the
-- reason, until an executor began it (its journal entry, committed before anything is sent). The owner's decisions
-- stay final: only an unlock's approval of a request nothing began goes back, and only to pending, undecided.
DROP TRIGGER approvals_status_flow;
CREATE TRIGGER approvals_status_flow BEFORE UPDATE OF status ON approvals
WHEN NEW.status IS NOT OLD.status AND NOT (
    (OLD.status = 'pending' AND NEW.status IN ('approved', 'approved_with_changes', 'rejected', 'withdrawn', 'expired'))
    OR (OLD.status IN ('approved', 'approved_with_changes') AND NEW.status IN ('done', 'failed'))
    OR (OLD.status = 'approved' AND NEW.status = 'pending' AND OLD.decided_by = 'Ember''s code (your unlock)'
        AND NEW.decided_at IS NULL AND NEW.decided_by IS NULL AND NEW.final_payload IS NULL AND OLD.closed_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = OLD.id))
)
BEGIN
    SELECT RAISE(ABORT, 'approvals: not an allowed status change');
END;
DROP TRIGGER approvals_decision_final;
CREATE TRIGGER approvals_decision_final BEFORE UPDATE ON approvals
WHEN (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at OR NEW.decided_by IS NOT OLD.decided_by
        OR NEW.decision_comment IS NOT OLD.decision_comment OR NEW.final_payload IS NOT OLD.final_payload)
        AND NOT (OLD.status = 'approved' AND NEW.status = 'pending' AND OLD.decided_by = 'Ember''s code (your unlock)'
            AND NEW.decided_at IS NULL AND NEW.decided_by IS NULL AND NEW.final_payload IS NULL))
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.result_note IS NOT OLD.result_note
        OR NEW.result_link IS NOT OLD.result_link))
BEGIN
    SELECT RAISE(ABORT, 'approvals: a decision is final');
END;

-- The unlocks of 0.13.0 were granted under looser rules (without owner_user_ids, past the kill switch, without QA):
-- every one that stands is taken back once, by Ember's code, and the owner grants again what they want. What they
-- held waits for the owner; what they approved and nothing began waits too. The same request waiting already (one
-- waits at a time): this one is closed.
INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, why, created_at)
SELECT g.mode, g.session, g.milestone_id, g.rule, 'manual', g.per_day, g.budget, 'Ember''s code',
    'the upgrade to 0.14.0', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM policy_grants g
WHERE g.level <> 'manual'
    AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule);
UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL, version = version + 1,
    seen_cycle_id = NULL, decision_comment = 'Approved by your unlock, which was taken back (the upgrade to 0.14.0)'
        || ' before Ember''s code carried it out: it waits for you.'
WHERE status = 'approved' AND decided_by = 'Ember''s code (your unlock)'
    AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)
    AND NOT EXISTS (
        SELECT 1 FROM approvals p WHERE p.mode = approvals.mode AND p.session = approvals.session
            AND p.payload_sha256 = approvals.payload_sha256 AND p.id <> approvals.id
            AND (p.status = 'pending' OR (p.status = 'approved' AND p.decided_by = 'Ember''s code (your unlock)'
                AND p.id < approvals.id AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = p.id)))
    );
UPDATE approvals SET status = 'failed', closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), closed_by = 'Ember''s code',
    version = version + 1, seen_cycle_id = NULL,
    result_note = 'Approved by your unlock, which was taken back (the upgrade to 0.14.0) before Ember''s code carried it'
        || ' out: it waits for you. The same request waits as #' || (
            SELECT p.id FROM approvals p WHERE p.mode = approvals.mode AND p.session = approvals.session
                AND p.payload_sha256 = approvals.payload_sha256 AND p.status = 'pending'
        ) || '.'
WHERE status = 'approved' AND decided_by = 'Ember''s code (your unlock)'
    AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id);
