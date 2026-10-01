-- 0.15.0: the roadmap's integrity.
--
-- What a milestone Ember's code set or checks counts is fixed. The agent moved a listing test's bar to another product
-- line (milestone_update's project_id), and Ember's code graded it by that line's views. Now the project and venture
-- of a milestone Ember's code set, or of one with a metric, never change; and a milestone Ember's code set leads only
-- where Ember's code leads it: to a money goal (the next one, once one closed), or to none.
CREATE TRIGGER milestones_links_fixed BEFORE UPDATE OF venture_id, project_id ON milestones
WHEN (OLD.created_by = 'code' OR OLD.metric IS NOT NULL)
    AND (NEW.venture_id IS NOT OLD.venture_id OR NEW.project_id IS NOT OLD.project_id)
BEGIN SELECT RAISE(ABORT, 'milestones: what a milestone Ember''s code set or checks counts is fixed'); END;
CREATE TRIGGER milestones_code_parent BEFORE UPDATE OF parent_id ON milestones
WHEN OLD.created_by = 'code' AND NEW.parent_id IS NOT OLD.parent_id AND NEW.parent_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM milestones g WHERE g.id = NEW.parent_id AND g.kind = 'money_goal')
BEGIN SELECT RAISE(ABORT, 'milestones: a milestone Ember''s code set leads to a money goal or to none'); END;

-- A backed venture goes live once its first test is met as Ember's code checked it or the owner closed it (or the
-- owner dropped it). The agent's done, or the daily review's, took a venture live on its own word (0030 accepted any
-- done). Replaced with the stricter rule.
DROP TRIGGER ventures_tested_live;
CREATE TRIGGER ventures_tested_live BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage = 'live' AND OLD.stage = 'building'
    AND NOT EXISTS (
        SELECT 1 FROM milestones WHERE id = NEW.test_milestone_id
            AND ((status = 'done' AND closed_by IN ('code', 'owner')) OR (status = 'dropped' AND closed_by = 'owner'))
    )
BEGIN SELECT RAISE(ABORT, 'ventures: a backed venture goes live once its first test is met'); END;

-- The print-on-demand venture's first test is a first order (Phase E4). One backed under 0.12.0 kept its first test in
-- words: 0055 gave it the channel, but a first test is made only when the owner backs a venture. Its open first test
-- in words is replaced by the channel's, which Ember's code checks: a buyer orders one of its Printify products
-- (pod_orders, at least 1), due on the same day, leading where it led; the open steps that led to the old one lead to
-- it. The old one is dropped by Ember's code with the reason, and the venture's first test is the new one (replaces_id
-- names the old one).
INSERT INTO milestones (
    mode, session, life_id, parent_id, venture_id, created_by, created_at, updated_at, title, measure, first_due, due,
    kind, metric, target, replaces_id
)
SELECT
    m.mode, m.session, m.life_id, m.parent_id, m.venture_id, 'code', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), m.title, 'A buyer orders one of its products (Printify''s records)',
    m.first_due, m.due, 'first_test', 'pod_orders', 1, m.id
FROM ventures v JOIN milestones m ON m.id = v.test_milestone_id
WHERE v.channel = 'printify' AND v.stage = 'building' AND m.status = 'open' AND m.kind = 'first_test'
    AND m.metric IS NULL;
UPDATE ventures SET
    test_milestone_id = (
        SELECT n.id FROM milestones n
        WHERE n.replaces_id = ventures.test_milestone_id AND n.created_by = 'code' AND n.metric = 'pod_orders'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE EXISTS (
    SELECT 1 FROM milestones n
    WHERE n.replaces_id = ventures.test_milestone_id AND n.created_by = 'code' AND n.metric = 'pod_orders'
);
UPDATE milestones SET
    parent_id = (
        SELECT n.id FROM milestones n
        WHERE n.replaces_id = milestones.parent_id AND n.created_by = 'code' AND n.metric = 'pod_orders'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE status = 'open' AND parent_id IN (
    SELECT n.replaces_id FROM milestones n
    WHERE n.created_by = 'code' AND n.metric = 'pod_orders' AND n.replaces_id IS NOT NULL
);
UPDATE milestones SET
    status = 'dropped',
    closed_by = 'code',
    closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    result = 'Replaced by #' || (
        SELECT n.id FROM milestones n
        WHERE n.replaces_id = milestones.id AND n.created_by = 'code' AND n.metric = 'pod_orders'
    ) || ': the first test of this print-on-demand venture is a first order now (Printify''s records), which '
        || 'Ember''s code checks.'
WHERE status = 'open' AND id IN (
    SELECT n.replaces_id FROM milestones n
    WHERE n.created_by = 'code' AND n.metric = 'pod_orders' AND n.replaces_id IS NOT NULL
);

-- A backed venture whose first test the agent closed done on its own word (0.13.0 let it) could now never go live,
-- as the owner can't drop a closed milestone. Ember's code sets it a new first test at the next plan, as for a backed
-- venture without one (agent/stages.py): met as Ember's code checks it, or confirmed by the owner's drop.
UPDATE ventures SET test_milestone_id = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE stage = 'building' AND test_milestone_id IN (
    SELECT id FROM milestones WHERE status = 'done' AND COALESCE(closed_by, '') NOT IN ('code', 'owner')
);
