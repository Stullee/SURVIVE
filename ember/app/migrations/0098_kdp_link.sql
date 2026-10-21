-- 0.40.2 (analysis-0.40.1, 3.1): the owner adds a KDP book's link at Amazon to its request marked done without one
-- (owner.add_link). Live, both Haushaltsbuch books were marked done with "published" alone, and the daily review
-- planned pins and posts "linking to Amazon" that nothing could make. A closed request stays final otherwise: only a
-- done kdp_package's missing link may be set, once.
DROP TRIGGER approvals_decision_final;
CREATE TRIGGER approvals_decision_final BEFORE UPDATE ON approvals
WHEN (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at OR NEW.decided_by IS NOT OLD.decided_by
        OR NEW.decision_comment IS NOT OLD.decision_comment OR NEW.final_payload IS NOT OLD.final_payload)
        AND NOT (OLD.status = 'approved' AND NEW.status = 'pending' AND OLD.decided_by = 'Ember''s code (your unlock)'
            AND NEW.decided_at IS NULL AND NEW.decided_by IS NULL AND NEW.final_payload IS NULL))
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.result_note IS NOT OLD.result_note
        OR (NEW.result_link IS NOT OLD.result_link AND NOT (OLD.executor = 'kdp_package' AND OLD.status = 'done'
            AND NEW.status = 'done' AND OLD.result_link IS NULL AND NEW.result_link IS NOT NULL))))
BEGIN
    SELECT RAISE(ABORT, 'approvals: a decision is final');
END;
