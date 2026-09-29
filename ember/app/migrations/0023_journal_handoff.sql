-- 0.12.0: the reflection's handoff. What the reflection said the next cycle should do first was part of the journal
-- entry, which no plan sees (the next plan got the one-line summary only), so every cycle started from scratch. The
-- journal keeps it apart now (write_journal's next), and the next plan sees it with the last cycle's goal.
ALTER TABLE journal ADD COLUMN handoff TEXT NOT NULL DEFAULT '' CHECK (length(handoff) <= 400);
