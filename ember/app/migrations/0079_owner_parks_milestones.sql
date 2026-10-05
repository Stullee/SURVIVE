-- 0.23.2: the owner's park or kill stops a venture's projects (0.22.0, migration 0077), and now takes every open
-- milestone of the venture and of its projects with it, and the open steps leading to them (agent/stages.py:
-- drop_milestones). Before, it took only the venture's and the bars Ember's code set for its projects: the agent's own
-- milestones of its projects stayed open, and so did the ones the agent linked to the parked venture afterwards (0.23.1
-- allowed it). Work went on through them (the plan's focus, and the unlocks they carried). What earlier parks and kills
-- left open is dropped now; a step is found through the closed ones between it and its milestone, as open_steps does.
WITH RECURSIVE stopped_venture(id) AS (
    SELECT id FROM ventures WHERE stage = 'killed' OR (stage = 'parked' AND parked_by = 'owner')
),
stopped(id) AS (
    SELECT m.id FROM milestones m
    WHERE m.status = 'open' AND (
        m.venture_id IN (SELECT id FROM stopped_venture)
        OR m.project_id IN (SELECT p.id FROM projects p WHERE p.venture_id IN (SELECT id FROM stopped_venture))
    )
    UNION
    SELECT m.id FROM milestones m JOIN stopped s ON m.parent_id = s.id
)
UPDATE milestones
SET status = 'dropped',
    result = 'Dropped at the upgrade to 0.23.2: your owner parked or killed the venture it served, or the one a'
        || ' milestone it leads to served, and that stops its work.',
    closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    closed_by = 'owner',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL
WHERE id IN (SELECT id FROM stopped) AND status = 'open';
