-- 0.12.0: who closed a milestone. The agent's "done" was taken at its word (any sentence closed it) and then shown
-- among the daily review's exact numbers; it is labelled self-reported now, apart from what the owner closed or
-- Ember's code checked from its records. Milestones closed before are the agent's, or the owner's if they dropped
-- them. Once a milestone is closed, who closed it stays.
ALTER TABLE milestones ADD COLUMN closed_by TEXT CHECK (closed_by IS NULL OR closed_by IN ('agent', 'owner', 'code'));
UPDATE milestones SET closed_by = CASE WHEN owner_action = 'drop' THEN 'owner' ELSE 'agent' END WHERE status <> 'open';

CREATE TRIGGER milestones_closed_by_named BEFORE UPDATE OF status ON milestones
WHEN NEW.status <> 'open' AND NEW.closed_by IS NULL
BEGIN SELECT RAISE(ABORT, 'milestones: a closed milestone names who closed it'); END;
CREATE TRIGGER milestones_closed_by_final BEFORE UPDATE OF closed_by ON milestones
WHEN OLD.closed_by IS NOT NULL AND NEW.closed_by IS NOT OLD.closed_by
BEGIN SELECT RAISE(ABORT, 'milestones: who closed a milestone is final'); END;
