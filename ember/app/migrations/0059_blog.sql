-- 0.14.0: the blog on the owner's website (products/blog.py), uploaded by Ember's code over SFTP once the owner
-- approved it (integrations/site_publisher.py; executors site_post, site_links and site_restore, the owner's Undo).
--
-- site_uploads: each file a request writes on the owner's server. A post's and the link page's row is made when the
-- agent proposes it, with the page Ember's code rendered (the owner previews and approves exactly these bytes, which
-- never change); the blog's list gets its row when it is uploaded (it is the server's list with the post added). A
-- row is committed as 'running', with the file it replaces (before), before anything is sent, so a crash never uploads
-- twice ('unclear' when it can't be known whether the server has the new file). op 'remove': the owner's Undo of a
-- new post takes the file off the server. Rows are history: none is deleted.
CREATE TABLE site_uploads (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    approval_id   INTEGER NOT NULL REFERENCES approvals (id),
    path          TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 80 AND path NOT GLOB '*[^a-z0-9./-]*'
                      AND path NOT LIKE '%..%' AND path NOT LIKE '/%'),
    op            TEXT NOT NULL DEFAULT 'write' CHECK (op IN ('write', 'remove')),
    content       BLOB CHECK (content IS NULL OR length(content) <= 200000),
    sha256        TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    before        BLOB CHECK (before IS NULL OR length(before) <= 200000),
    before_sha256 TEXT CHECK (before_sha256 IS NULL OR length(before_sha256) = 64),
    status        TEXT NOT NULL CHECK (status IN ('proposed', 'running', 'done', 'failed', 'unclear')),
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((op = 'write') = (content IS NOT NULL)),
    CHECK ((status IN ('proposed', 'running')) = (finished_at IS NULL)),
    UNIQUE (approval_id, path)
);
CREATE INDEX site_uploads_by_scope ON site_uploads (mode, session, status);
CREATE TRIGGER site_uploads_page_fixed BEFORE UPDATE ON site_uploads
WHEN NEW.content IS NOT OLD.content OR NEW.sha256 IS NOT OLD.sha256 OR NEW.path IS NOT OLD.path
    OR NEW.approval_id IS NOT OLD.approval_id OR NEW.op IS NOT OLD.op
BEGIN SELECT RAISE(ABORT, 'site_uploads: an approved page never changes'); END;
CREATE TRIGGER site_uploads_no_delete BEFORE DELETE ON site_uploads
BEGIN SELECT RAISE(ABORT, 'site_uploads: rows cannot be deleted'); END;

-- The blog's list as Ember's code last read or wrote it on the server (replaced each time): the plan shows the agent
-- which posts are online, and an update of a post keeps its first date. approval_id: the request that published it
-- (NULL: the owner uploaded it, or an older version came back with an Undo).
CREATE TABLE blog_posts (
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    slug        TEXT NOT NULL CHECK (length(slug) BETWEEN 1 AND 60 AND slug NOT GLOB '*[^a-z0-9-]*'),
    title       TEXT NOT NULL CHECK (length(title) <= 200),
    description TEXT NOT NULL CHECK (length(description) <= 340),
    day         TEXT NOT NULL CHECK (length(day) = 10),
    position    INTEGER NOT NULL,
    approval_id INTEGER REFERENCES approvals (id),
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (mode, session, slug)
);
