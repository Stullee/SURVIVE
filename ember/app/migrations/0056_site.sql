-- 0.13.0 (Phase E3): the owner's website (products/site.py builds it, agent/website.py keeps it). The agent writes its
-- pages in the products' markdown; Ember's code builds a static site from them in one audited template (no script,
-- no tracker, nothing from elsewhere), with the Impressum and the privacy page made from the owner's options. The
-- owner previews it, downloads it and publishes it: Ember never does, so no executor and no NEVER class.

-- The agent's pages (a page written again replaces its text; a removed one keeps its row, and comes back when
-- written again).
CREATE TABLE site_pages (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    slug        TEXT NOT NULL CHECK (length(slug) BETWEEN 1 AND 40 AND slug NOT GLOB '*[^a-z0-9-]*'),
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 160),
    menu        TEXT NOT NULL DEFAULT '' CHECK (length(menu) <= 24),
    source      TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 20000),
    cycle_id    INTEGER REFERENCES cycles (id),
    updated_at  TEXT NOT NULL,
    removed_at  TEXT,
    UNIQUE (mode, session, slug)
);

-- Each download the owner made, with its files' SHA-256 (by name), so the plan and the dashboard can say what
-- changed since the owner last published it.
CREATE TABLE site_downloads (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    downloaded_at TEXT NOT NULL,
    files         TEXT NOT NULL CHECK (json_valid(files) AND length(files) <= 10000)
);
CREATE INDEX site_downloads_by_scope ON site_downloads (mode, session, id);
