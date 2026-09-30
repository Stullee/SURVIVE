-- 0.12.0: money and time on a milestone, and waiting. A milestone had no budget, whole cycles were charged to it, and
-- a milestone waiting on the owner (or on buyers) turned overdue and asked for paid date moves. Now a milestone can
-- carry what it may cost: the API spending the agent plans for it (budget_micros), the cash it needs from the owner
-- (cash_cents, in euros) and the owner's time (owner_minutes); the plan shows what it spent of its budget (the work
-- of the cycles aimed at it; plans, reviews and brainstorms are overhead, charged to none). And it can wait: for what
-- (wait_for) and the owner's day it is checked again (check_at): a waiting milestone isn't nagged as overdue, and the
-- agent wakes on the morning its check is due (a few times a day at most).
ALTER TABLE milestones ADD COLUMN budget_micros INTEGER CHECK (budget_micros IS NULL OR budget_micros > 0);
ALTER TABLE milestones ADD COLUMN cash_cents INTEGER CHECK (cash_cents IS NULL OR cash_cents > 0);
ALTER TABLE milestones ADD COLUMN owner_minutes INTEGER CHECK (owner_minutes IS NULL OR owner_minutes > 0);
ALTER TABLE milestones ADD COLUMN wait_for TEXT CHECK (wait_for IS NULL OR length(wait_for) BETWEEN 1 AND 200);
ALTER TABLE milestones ADD COLUMN check_at TEXT
    CHECK (check_at IS NULL OR check_at GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]');

CREATE TRIGGER milestones_wait_pair BEFORE UPDATE OF wait_for, check_at ON milestones
WHEN (NEW.wait_for IS NULL) <> (NEW.check_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'milestones: a wait names what it waits for and when it is checked'); END;
-- What it may cost is set with it, like its measure: an overrun shows, it isn't quietly raised.
CREATE TRIGGER milestones_budget_fixed BEFORE UPDATE OF budget_micros, cash_cents, owner_minutes ON milestones
WHEN NEW.budget_micros IS NOT OLD.budget_micros OR NEW.cash_cents IS NOT OLD.cash_cents
    OR NEW.owner_minutes IS NOT OLD.owner_minutes
BEGIN SELECT RAISE(ABORT, 'milestones: what a milestone may cost is fixed'); END;
