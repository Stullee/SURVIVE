-- 0.12.0: milestones measured by a metric, checked by Ember's code. The agent closed "3 listings live" done with
-- "Done." while none existed: its word was the only check. A milestone can now name a metric from Ember's catalogue
-- (agent/metrics.py: listings live, orders, revenue your owner recorded, ...) and a target in the metric's unit (a
-- count, micros for USD, a stage's rank). Ember's code reads it from its records after each Etsy sync and before every
-- plan, keeps where it stands (progress, checked_at), and closes it: done once met, missed once its date has passed.
-- baseline holds the views or favorites when it was set, for a metric counting what was gained since. A metric and
-- its target are final, like the title and the measure; only Ember's code (or the owner) closes such a milestone done.
ALTER TABLE milestones ADD COLUMN metric TEXT CHECK (metric IS NULL OR length(metric) BETWEEN 1 AND 40);
ALTER TABLE milestones ADD COLUMN target INTEGER;
ALTER TABLE milestones ADD COLUMN baseline INTEGER;
ALTER TABLE milestones ADD COLUMN progress INTEGER;
ALTER TABLE milestones ADD COLUMN checked_at TEXT;

CREATE TRIGGER milestones_metric_pair BEFORE INSERT ON milestones
WHEN (NEW.metric IS NULL) <> (NEW.target IS NULL)
BEGIN SELECT RAISE(ABORT, 'milestones: a metric needs a target, and a target a metric'); END;
CREATE TRIGGER milestones_metric_fixed BEFORE UPDATE ON milestones
WHEN NEW.metric IS NOT OLD.metric OR NEW.target IS NOT OLD.target OR NEW.baseline IS NOT OLD.baseline
BEGIN SELECT RAISE(ABORT, 'milestones: a metric and its target are final'); END;
CREATE TRIGGER milestones_metric_done BEFORE UPDATE OF status ON milestones
WHEN OLD.metric IS NOT NULL AND NEW.status = 'done' AND NEW.closed_by NOT IN ('code', 'owner')
BEGIN SELECT RAISE(ABORT, 'milestones: Ember''s code closes a milestone with a metric done, from its records'); END;
