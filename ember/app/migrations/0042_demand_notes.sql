-- 0.12.0: a demand note before a new product line. Ember made generic templates without checking that anyone
-- searched for them. Now a project's first Etsy listing needs a demand note from the last 14 days: the keywords buyers
-- search and the demand the agent found (with its source: a page from its research results or the owner's library),
-- and, with the owner's Etsy market probe on, Etsy's numbers for those keywords. The probe keeps only aggregates,
-- computed as Etsy's answer is read (how many active listings match, and the quartiles of the first ones' prices),
-- never another seller's listing. Written by Ember's code; never changed.
CREATE TABLE demand_notes (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    cycle_id   INTEGER REFERENCES cycles (id),
    project_id INTEGER NOT NULL REFERENCES projects (id),
    created_at TEXT NOT NULL,
    keywords   TEXT NOT NULL CHECK (length(keywords) BETWEEN 1 AND 100),
    demand     TEXT CHECK (demand IS NULL OR length(demand) BETWEEN 1 AND 600),
    source     TEXT CHECK (source IS NULL OR length(source) BETWEEN 1 AND 300),
    -- the market probe's aggregates (NULL without one)
    listings   INTEGER CHECK (listings IS NULL OR listings >= 0),  -- active listings matching the keywords
    sampled    INTEGER CHECK (sampled IS NULL OR sampled >= 0),  -- the first ones whose prices were read
    currency   TEXT CHECK (currency IS NULL OR length(currency) BETWEEN 1 AND 3),
    low        REAL,  -- their prices' lower quartile, median and upper quartile
    median     REAL,
    high       REAL,
    CHECK (listings IS NOT NULL OR (demand IS NOT NULL AND source IS NOT NULL))  -- a probe, or the demand and its source
);
CREATE INDEX demand_notes_by_project ON demand_notes (project_id, created_at);
CREATE TRIGGER demand_notes_no_update BEFORE UPDATE ON demand_notes
BEGIN SELECT RAISE(ABORT, 'demand_notes: a note never changes'); END;
CREATE TRIGGER demand_notes_no_delete BEFORE DELETE ON demand_notes
BEGIN SELECT RAISE(ABORT, 'demand_notes: rows cannot be deleted'); END;
