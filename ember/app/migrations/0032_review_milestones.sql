-- 0.12.0: the daily review's verdicts on milestones. The review read the roadmap in words only, and nothing followed
-- from them. Now it judges each milestone that is overdue or due this week: hit (its measure is met, with the
-- evidence), miss (past its date and not met), extend (a new date) or park (it waits a week); Ember's code applies
-- each one with the same rules as the agent's milestone_update, before the day's first plan, and keeps here what it
-- did with it (applied or why not). A review never changes afterwards.
ALTER TABLE reviews ADD COLUMN milestones TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(milestones));
