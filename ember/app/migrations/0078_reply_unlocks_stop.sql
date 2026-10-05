-- 0.23.1: migration 0076 took back the standing "auto" unlocks of email replies, but a reply one of them had approved
-- and Ember's code hadn't sent yet (the day's email limit, or the upgrade came first) stayed approved, and the executor
-- sent it without the owner seeing it. Such a reply waits for the owner again, as when the owner takes an unlock back
-- (policy._stop); the same reply waiting already: this one is closed instead.
UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL, version = version + 1,
    seen_cycle_id = NULL, decision_comment = 'Approved by your unlock, which was taken back (email replies run at most'
        || ' with a veto window since 0.22.0) before Ember''s code carried it out: it waits for you.'
WHERE status = 'approved' AND decided_by = 'Ember''s code (your unlock)'
    AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)
    AND EXISTS (
        SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id
        WHERE u.approval_id = approvals.id AND u.level = 'auto' AND g.rule = 'email_reply'
    )
    AND NOT EXISTS (
        SELECT 1 FROM approvals p WHERE p.mode = approvals.mode AND p.session = approvals.session
            AND p.payload_sha256 = approvals.payload_sha256 AND p.id <> approvals.id
            AND (p.status = 'pending' OR (p.status = 'approved' AND p.decided_by = 'Ember''s code (your unlock)'
                AND p.id < approvals.id AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = p.id)
                AND EXISTS (
                    SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id
                    WHERE u.approval_id = p.id AND u.level = 'auto' AND g.rule = 'email_reply'
                )))
    );
UPDATE approvals SET status = 'failed', closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), closed_by = 'Ember''s code',
    version = version + 1, seen_cycle_id = NULL,
    result_note = 'Approved by your unlock, which was taken back (email replies run at most with a veto window since'
        || ' 0.22.0) before Ember''s code carried it out: it waits for you. The same request waits as #' || (
            SELECT p.id FROM approvals p WHERE p.mode = approvals.mode AND p.session = approvals.session
                AND p.payload_sha256 = approvals.payload_sha256 AND p.status = 'pending'
        ) || '.'
WHERE status = 'approved' AND decided_by = 'Ember''s code (your unlock)'
    AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)
    AND EXISTS (
        SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id
        WHERE u.approval_id = approvals.id AND u.level = 'auto' AND g.rule = 'email_reply'
    )
    AND EXISTS (
        SELECT 1 FROM approvals p WHERE p.mode = approvals.mode AND p.session = approvals.session
            AND p.payload_sha256 = approvals.payload_sha256 AND p.status = 'pending'
    );
