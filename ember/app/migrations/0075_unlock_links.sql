-- 0.21.0: an unlock covers what its milestone is linked to (approvals_scope, 0063), and the agent could re-link the
-- milestone: after one milestone_update, the owner's unlock to take the old CV templates off Etsy carried taking the
-- best seller off at once. While an unlock of a milestone stands (its newest grant for a rule isn't manual), its project
-- and venture stay as they are, whoever changes them; the owner takes the unlock back first.
CREATE TRIGGER milestones_links_keep_unlocks BEFORE UPDATE OF project_id, venture_id ON milestones
WHEN (OLD.project_id IS NOT NEW.project_id OR OLD.venture_id IS NOT NEW.venture_id)
    AND EXISTS (
        SELECT 1 FROM policy_grants g
        WHERE g.milestone_id = OLD.id AND g.level <> 'manual'
            AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
    )
BEGIN SELECT RAISE(ABORT, 'milestones: a milestone keeps its links while an unlock of it stands'); END;
