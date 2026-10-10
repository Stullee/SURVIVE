-- 0.37.6: the plan's brake. Live on 2026-10-09 a step no cycle could advance took every cycle 30 minutes apart until
-- the daily cap stopped it ($5.88 of $7 by 12:35): nothing in the tree noticed that a cycle took a step and nothing
-- changed. Each pick now keeps what its step's check read when the cycle took it (mark: a digest of the numbers and the
-- requests the check reads, agent/plan.py) and the owner's day it was taken on (day), so Ember's code can tell a step
-- a cycle advanced from one it couldn't: one taken without what it reads moving waits until that moves, or the next
-- day. Picks made before have neither: they brake nothing.
ALTER TABLE plan_picks ADD COLUMN mark TEXT CHECK (mark IS NULL OR length(mark) BETWEEN 1 AND 64);
ALTER TABLE plan_picks ADD COLUMN day TEXT CHECK (day IS NULL OR length(day) = 10);
