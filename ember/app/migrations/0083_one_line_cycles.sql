-- 0.28.0: one thing a cycle. An ordinary cycle worked on several product lines, an unbacked venture and marketing at
-- once (its rules said "keep 2-3 experiments in flight ... work on another", and its tools took any project): a listing
-- went into the wrong line, files were filed under the wrong project, and a line's cost was another's. Now a wake cycle
-- is about one thing: an ordinary cycle works on one product line, a marketing cycle (a share of each day's spending,
-- the marketing_share option) brings buyers to one line's listings, a venture cycle decides one venture.
ALTER TABLE cycles ADD COLUMN marketing INTEGER NOT NULL DEFAULT 0 CHECK (marketing IN (0, 1));
-- Ember's code ranks the lines for each ordinary and marketing plan (READY, agent/lines.py), and the plan takes one or
-- says why it takes none, as a venture plan does from the decision desk: each pick is kept with the list it came from
-- in the same table (one pick a cycle), with its kind, the line it took, and whether a pressing obligation chose it.
ALTER TABLE desk_picks ADD COLUMN kind TEXT NOT NULL DEFAULT 'venture' CHECK (kind IN ('venture', 'line', 'market'));
ALTER TABLE desk_picks ADD COLUMN project_id INTEGER REFERENCES projects (id) CHECK (project_id IS NULL OR pick IS NOT NULL);
ALTER TABLE desk_picks ADD COLUMN pressed INTEGER NOT NULL DEFAULT 0 CHECK (pressed IN (0, 1));
