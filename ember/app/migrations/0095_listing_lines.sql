-- 0.39.8 (analysis-0.39.6, 3.3): the product line a listing was moved to. A listing belongs to the line of the request
-- that made it (its project, or its cycle's), and nothing could change that: live, the coach licence bundle
-- #4587912058 was made by a request of line #4, so line #12 (the licence packs, whose only listing it is) had no
-- listing for Ember's code, line #4's funnel held its views, and every Bluesky post for it in a line-#12 cycle was
-- refused ("no post for project #4"). Ember moves one with project_update (listing_id); the newest row for a listing
-- says its line, for every count that reads a listing's line (ventures.listing_line, metrics.listings, the orders'
-- revenue). Rows are a history: they are never changed or deleted.
CREATE TABLE listing_lines (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    listing_id  INTEGER NOT NULL,
    project_id  INTEGER NOT NULL REFERENCES projects (id),
    moved_from  INTEGER REFERENCES projects (id),
    cycle_id    INTEGER REFERENCES cycles (id),
    created_at  TEXT NOT NULL,
    why         TEXT NOT NULL CHECK (length(why) BETWEEN 1 AND 300)
);
CREATE INDEX listing_lines_by_listing ON listing_lines (mode, session, listing_id, id);
CREATE TRIGGER listing_lines_no_update BEFORE UPDATE ON listing_lines
BEGIN SELECT RAISE(ABORT, 'listing_lines: rows are a history'); END;
CREATE TRIGGER listing_lines_no_delete BEFORE DELETE ON listing_lines
BEGIN SELECT RAISE(ABORT, 'listing_lines: rows are a history'); END;
