-- 0.12.0: a research budget for each venture. "Decide every venture within about $3" was a line in the prompt that
-- nothing checked. Now a venture that isn't backed has a research budget (ventures.RESEARCH_BUDGET_USD of research
-- calls, counted from venture_research): once it is spent, Ember's code refuses more research for it, and the agent
-- decides it (a business case or parked). The budget counts from the owner's last word asking for research on the
-- venture (Research next, more or again on the Ventures tab), which starts a new one: only the owner grants more.
ALTER TABLE ventures ADD COLUMN research_granted_at TEXT;
CREATE TRIGGER ventures_owner_grants_research BEFORE UPDATE OF research_granted_at ON ventures
WHEN NEW.research_granted_at IS NOT OLD.research_granted_at
    AND NOT (NEW.owner_action IS 'research' AND NEW.owner_version = OLD.owner_version + 1)
BEGIN SELECT RAISE(ABORT, 'ventures: only the owner grants more research'); END;
