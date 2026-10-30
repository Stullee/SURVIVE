-- 0.55.0: Ember records a published KDP book's link at Amazon that the owner sent her in a message (propose_kdp_book
-- with link; Ember's code checks the address is in one of the owner's messages). Live, the owner sent both links in
-- messages, Ember had no tool to put them on the requests, and every post linking the books was refused. link_by says
-- who recorded a link: 'Ember', or NULL for the owner. The owner may replace a link Ember recorded (she may have put it
-- on the wrong book); a link the owner recorded stays final, as 0.40.2's did.
ALTER TABLE approvals ADD COLUMN link_by TEXT CHECK (link_by IS NULL OR link_by = 'Ember');
DROP TRIGGER approvals_decision_final;
CREATE TRIGGER approvals_decision_final BEFORE UPDATE ON approvals
WHEN (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at OR NEW.decided_by IS NOT OLD.decided_by
        OR NEW.decision_comment IS NOT OLD.decision_comment OR NEW.final_payload IS NOT OLD.final_payload)
        AND NOT (OLD.status = 'approved' AND NEW.status = 'pending' AND OLD.decided_by = 'Ember''s code (your unlock)'
            AND NEW.decided_at IS NULL AND NEW.decided_by IS NULL AND NEW.final_payload IS NULL))
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS NOT OLD.closed_at OR NEW.result_note IS NOT OLD.result_note
        OR ((NEW.result_link IS NOT OLD.result_link OR NEW.link_by IS NOT OLD.link_by)
            AND NOT (OLD.executor IS 'kdp_package' AND OLD.status = 'done' AND NEW.status = 'done'
                AND NEW.result_link IS NOT NULL AND (OLD.result_link IS NULL OR OLD.link_by IS 'Ember')))))
BEGIN
    SELECT RAISE(ABORT, 'approvals: a decision is final');
END;
