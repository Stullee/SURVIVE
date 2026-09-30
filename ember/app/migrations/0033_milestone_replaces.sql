-- 0.12.0: a milestone that replaces one the agent dropped or missed names it (replaces_id). Dropping a milestone and
-- putting it on the roadmap again reset its moved count and let its measure soften without a trace. Now the new one
-- keeps the old one's first date and moves (one more for a dropped one: the replacement moves its date again, and it
-- can't go beyond the limit), and the plan shows what it replaces. Ember's code asks for it when the new title is much
-- like one dropped or missed in the last 30 days. Which milestone it replaces is fixed.
ALTER TABLE milestones ADD COLUMN replaces_id INTEGER REFERENCES milestones (id);
CREATE TRIGGER milestones_replaces_fixed BEFORE UPDATE OF replaces_id ON milestones
WHEN NEW.replaces_id IS NOT OLD.replaces_id
BEGIN SELECT RAISE(ABORT, 'milestones: which milestone one replaces is fixed'); END;
