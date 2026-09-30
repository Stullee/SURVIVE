-- 0.12.0: evidence, not opinions. A venture's case was prose: a number and a link in it counted the same whether the
-- page was a search result, a vendor selling the very tool it praised, or nothing the agent ever read. Now the agent
-- saves each claim as evidence (a metric, a low and a high value, a unit, a region and the page it came from), and
-- Ember's code grades the page: independent, marketing (a vendor's or an affiliate's) or unchecked (not a page from
-- the agent's research results, which Ember's code records as the research tool returns them).
CREATE TABLE research_sources (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    cycle_id    INTEGER REFERENCES cycles (id),
    llm_call_id INTEGER REFERENCES llm_calls (id),
    url         TEXT NOT NULL CHECK (length(url) BETWEEN 1 AND 300),
    url_key     TEXT NOT NULL CHECK (length(url_key) BETWEEN 1 AND 300),  -- the URL as evidence is matched to it
    created_at  TEXT NOT NULL
);
CREATE INDEX research_sources_by_key ON research_sources (mode, session, url_key);
CREATE TRIGGER research_sources_no_update BEFORE UPDATE ON research_sources
BEGIN SELECT RAISE(ABORT, 'research_sources: history cannot change'); END;
CREATE TRIGGER research_sources_no_delete BEFORE DELETE ON research_sources
BEGIN SELECT RAISE(ABORT, 'research_sources: history cannot change'); END;

CREATE TABLE evidence (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    venture_id INTEGER NOT NULL REFERENCES ventures (id),
    cycle_id   INTEGER REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    claim      TEXT NOT NULL CHECK (length(claim) BETWEEN 1 AND 300),
    metric     TEXT NOT NULL CHECK (length(metric) BETWEEN 1 AND 60),
    low        REAL NOT NULL,
    high       REAL NOT NULL CHECK (high >= low),
    unit       TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 24),
    region     TEXT NOT NULL CHECK (length(region) BETWEEN 1 AND 40),
    url        TEXT NOT NULL CHECK (length(url) BETWEEN 1 AND 300),
    source     TEXT NOT NULL CHECK (source IN ('independent', 'marketing', 'unchecked'))  -- Ember's code's grade
);
CREATE INDEX evidence_by_venture ON evidence (venture_id, id);
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence: a claim and its grade never change'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence: rows cannot be deleted'); END;
