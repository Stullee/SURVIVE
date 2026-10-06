-- 0.27.0: the owner's goal leads the roadmap. The owner sets one goal at its root (earn an amount a month, or in all,
-- by a date): a milestone of theirs with owner_goal = 1 and a money metric Ember's code checks from the books
-- (revenue_month_usd: the last 30 days; revenue_verified_usd: from counts_from on). Everything else on the roadmap
-- leads to it. While it stands, Ember's code keeps no money goal of its own. It is the root (it leads to nothing), only
-- the owner or Ember's code closes it, its date moves only when the owner sets a new goal (no proposal of the agent's),
-- and one is open at a time. counts_from: the day a goal counting in all starts counting (a goal the owner raised
-- keeps the day of the one it replaced); NULL counts from the day it was set.
ALTER TABLE milestones ADD COLUMN owner_goal INTEGER NOT NULL DEFAULT 0 CHECK (owner_goal IN (0, 1));
ALTER TABLE milestones ADD COLUMN counts_from TEXT
    CHECK (counts_from IS NULL OR counts_from GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]');

CREATE UNIQUE INDEX milestones_one_goal ON milestones (mode, session) WHERE owner_goal = 1 AND status = 'open';

CREATE TRIGGER milestones_goal_shape BEFORE INSERT ON milestones
WHEN NEW.owner_goal = 1 AND (NEW.created_by IS NOT 'owner' OR NEW.parent_id IS NOT NULL OR NEW.venture_id IS NOT NULL
    OR NEW.project_id IS NOT NULL OR NEW.metric IS NULL OR NEW.metric NOT IN ('revenue_month_usd', 'revenue_verified_usd'))
BEGIN SELECT RAISE(ABORT, 'milestones: the owner''s goal is theirs, leads to nothing and is measured from the books'); END;
CREATE TRIGGER milestones_goal_fixed BEFORE UPDATE OF owner_goal, counts_from ON milestones
WHEN NEW.owner_goal IS NOT OLD.owner_goal OR NEW.counts_from IS NOT OLD.counts_from
BEGIN SELECT RAISE(ABORT, 'milestones: whether a milestone is the owner''s goal, and from when it counts, is fixed'); END;
CREATE TRIGGER milestones_goal_root BEFORE UPDATE OF parent_id ON milestones
WHEN OLD.owner_goal = 1 AND NEW.parent_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'milestones: the owner''s goal leads to nothing'); END;
CREATE TRIGGER milestones_goal_close BEFORE UPDATE OF status ON milestones
WHEN OLD.owner_goal = 1 AND NEW.status <> 'open' AND NEW.closed_by NOT IN ('owner', 'code')
BEGIN SELECT RAISE(ABORT, 'milestones: Ember''s code or the owner closes the owner''s goal'); END;
CREATE TRIGGER milestones_goal_proposal BEFORE UPDATE OF proposed_due ON milestones
WHEN OLD.owner_goal = 1 AND NEW.proposed_due IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'milestones: the owner sets the date of their goal'); END;

-- A milestone Ember's code set leads to the goal at the root: its money goal (0065) or, new, the owner's goal.
DROP TRIGGER milestones_code_parent;
CREATE TRIGGER milestones_code_parent BEFORE UPDATE OF parent_id ON milestones
WHEN OLD.created_by = 'code' AND NEW.parent_id IS NOT OLD.parent_id AND NEW.parent_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM milestones g WHERE g.id = NEW.parent_id AND (g.kind = 'money_goal' OR g.owner_goal = 1))
BEGIN SELECT RAISE(ABORT, 'milestones: a milestone Ember''s code set leads to the goal or to none'); END;
