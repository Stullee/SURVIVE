-- 0.13.0: the stages' two new rules (agent/stages.py). Triage: an idea of the agent's is researched or parked within
-- TRIAGE_DAYS of coming up. Live: a live venture that has sold nothing LIVE_DAYS after going live is parked, and one
-- that earns more than it costs gets a decision point to scale it. rules_from: when these rules began to count for a
-- venture already in the stage (this upgrade), so that none is parked at once; scale_milestone_id: its decision point.
ALTER TABLE ventures ADD COLUMN rules_from TEXT;
ALTER TABLE ventures ADD COLUMN scale_milestone_id INTEGER REFERENCES milestones (id);
UPDATE ventures SET rules_from = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE stage IN ('idea', 'live');
