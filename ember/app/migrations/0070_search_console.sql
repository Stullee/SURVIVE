-- 0.16.0: Google Search Console (integrations/search_console.py): how the owner's website does in Google Search, read
-- by Ember's code with the owner's read-only service account. search_console_days keeps each day's numbers of the
-- property (Google revises the last days: a day is updated in place); search_console_top the top searches and pages of
-- the last 28 days, replaced at each read.
CREATE TABLE search_console_days (
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    site        TEXT NOT NULL CHECK (length(site) BETWEEN 1 AND 300),
    day         TEXT NOT NULL CHECK (length(day) = 10),
    clicks      INTEGER NOT NULL CHECK (clicks >= 0),
    impressions INTEGER NOT NULL CHECK (impressions >= 0),
    position    REAL NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (mode, session, site, day)
);

CREATE TABLE search_console_top (
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('query', 'page')),
    rank        INTEGER NOT NULL CHECK (rank >= 1),
    key         TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 500),
    clicks      INTEGER NOT NULL CHECK (clicks >= 0),
    impressions INTEGER NOT NULL CHECK (impressions >= 0),
    position    REAL NOT NULL,
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (mode, session, kind, rank)
);
