-- 0.19.0: Bluesky, the account the owner made for Ember (integrations/bluesky.py). The agent proposes a post; the owner
-- approves it as it is; Ember's code posts it (executor 'bluesky_post', any short name since 0054) and the owner's Undo
-- deletes it ('bluesky_delete'). No rule of the policy engine names them: every post waits for the owner's click.
--
-- Ember's posts (like pinterest_pins: a row committed as 'running' before anything is sent, so a crash never posts one
-- twice; 'unclear' when it can't be known whether Bluesky took it).
CREATE TABLE bluesky_posts (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    uri         TEXT CHECK (uri IS NULL OR length(uri) <= 600),  -- Bluesky's at:// address, once it exists
    rkey        TEXT CHECK (rkey IS NULL OR length(rkey) <= 512),  -- its record key: what the Undo deletes
    did         TEXT CHECK (did IS NULL OR length(did) <= 2048),  -- the account's: its address on bsky.app
    text        TEXT NOT NULL CHECK (length(text) <= 3000),  -- the post as Bluesky shows it
    link        TEXT CHECK (link IS NULL OR length(link) <= 500),
    status      TEXT NOT NULL CHECK (status IN ('running', 'active', 'deleted', 'failed', 'unclear')),
    -- 1 once Ember's code sent it to Bluesky (the day's limit counts these); 0: its checks refused it first
    sent        INTEGER NOT NULL DEFAULT 0 CHECK (sent IN (0, 1)),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    likes       INTEGER,  -- at the last sync
    reposts     INTEGER,
    replies     INTEGER,
    quotes      INTEGER,
    labels      TEXT CHECK (labels IS NULL OR length(labels) <= 300),  -- what moderation says of it (e.g. spam)
    synced_at   TEXT,
    result      TEXT CHECK (result IS NULL OR length(result) <= 500),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX bluesky_posts_by_scope ON bluesky_posts (mode, session, id);
CREATE TRIGGER bluesky_posts_no_delete BEFORE DELETE ON bluesky_posts
BEGIN SELECT RAISE(ABORT, 'bluesky_posts: rows cannot be deleted'); END;
