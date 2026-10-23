-- 0.47.0 (vision/generations.md, "The estate"): what a generation hands on of the world's products, and what the
-- owner decided for each. A product passes to the next mind, its line never does: the next generation gets the listing,
-- the book, the poster, not the hypothesis, history and money spent on it, which stay in its maker's books.
--
-- One row per product and hand-over: ``from_session`` is the mind that hands it on, ``to_session`` the mind it went
-- to (set by the generation change). Its numbers are as the hand-over read them, beside the verdict Ember's code's rule
-- suggests, an evaluation's proposal (it reads the same data) and the owner's verdict, which decides:
--   adopt    becomes a line of the next mind, with a first test and its date (``test_*``), filed at her first cycle;
--   passive  stays live and is synced and its sales recorded, but isn't hers to work on;
--   retire   is taken down before the change.
-- A verdict can change later (a passive product that sold, adopted after all); what a hand-over read never does.
CREATE TABLE estate (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,  -- the world's (AgentScope.world_session)
    from_session  INTEGER NOT NULL,  -- the mind that hands it on
    to_session    INTEGER,           -- the mind it went to, once the generation change was made
    kind          TEXT NOT NULL CHECK (kind IN ('etsy_listing', 'printify_product', 'kdp_book')),
    ref           TEXT NOT NULL CHECK (length(ref) BETWEEN 1 AND 40),  -- Etsy's listing, Printify's product, the book's request
    listing_id    INTEGER,  -- its Etsy listing (an Etsy listing's own, the one Printify made)
    approval_id   INTEGER NOT NULL REFERENCES approvals (id),  -- the request that made it
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    link          TEXT CHECK (link IS NULL OR length(link) <= 2048),
    numbers       TEXT NOT NULL CHECK (json_valid(numbers) AND length(numbers) <= 2000),
    suggested     TEXT NOT NULL CHECK (suggested IN ('adopt', 'passive', 'retire')),
    suggested_why TEXT NOT NULL CHECK (length(suggested_why) BETWEEN 1 AND 300),
    proposed      TEXT CHECK (proposed IS NULL OR proposed IN ('adopt', 'passive', 'retire')),
    proposed_why  TEXT CHECK (proposed_why IS NULL OR length(proposed_why) BETWEEN 1 AND 600),
    proposed_by   TEXT CHECK (proposed_by IS NULL OR length(proposed_by) BETWEEN 1 AND 60),
    verdict       TEXT CHECK (verdict IS NULL OR verdict IN ('adopt', 'passive', 'retire')),
    decided_by    TEXT CHECK (decided_by IS NULL OR length(decided_by) <= 60),
    decided_at    TEXT,
    test_metric   TEXT CHECK (test_metric IS NULL OR test_metric IN ('views', 'favorites', 'orders')),
    test_target   INTEGER CHECK (test_target IS NULL OR test_target BETWEEN 1 AND 100000),
    test_due      TEXT CHECK (test_due IS NULL OR test_due GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    test_result   TEXT CHECK (test_result IS NULL OR test_result IN ('met', 'missed')),
    project_id    INTEGER REFERENCES projects (id),  -- the line it became in the mind it went to
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK ((verdict IS NULL) = (decided_at IS NULL)),
    CHECK ((proposed IS NULL) = (proposed_why IS NULL)),
    CHECK (verdict IS NOT 'adopt' OR (test_metric IS NOT NULL AND test_target IS NOT NULL AND test_due IS NOT NULL)),
    CHECK ((test_metric IS NULL) = (test_target IS NULL) AND (test_metric IS NULL) = (test_due IS NULL)),
    CHECK (test_result IS NULL OR test_metric IS NOT NULL),
    CHECK (project_id IS NULL OR to_session IS NOT NULL),
    UNIQUE (mode, from_session, kind, ref)
);
CREATE INDEX estate_by_world ON estate (mode, session, to_session);
CREATE TRIGGER estate_handed_over_fixed BEFORE UPDATE ON estate
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.from_session IS NOT OLD.from_session
    OR NEW.kind IS NOT OLD.kind OR NEW.ref IS NOT OLD.ref OR NEW.approval_id IS NOT OLD.approval_id
    OR NEW.created_at IS NOT OLD.created_at
    OR (OLD.to_session IS NOT NULL AND (NEW.to_session IS NOT OLD.to_session OR NEW.numbers IS NOT OLD.numbers
        OR NEW.suggested IS NOT OLD.suggested OR NEW.suggested_why IS NOT OLD.suggested_why))
    OR (OLD.project_id IS NOT NULL AND NEW.project_id IS NOT OLD.project_id)
BEGIN SELECT RAISE(ABORT, 'estate: what a hand-over read cannot change'); END;
CREATE TRIGGER estate_no_delete BEFORE DELETE ON estate
BEGIN SELECT RAISE(ABORT, 'estate: rows cannot be deleted'); END;
