-- 0.39.8 (analysis-0.39.6, 3.5): a paid test of Ember's listings, kept by Ember's code. Live, the owner turned on Etsy
-- Ads at 01:03 on 10-16 for the three listings request #128 named (approved with "done starting from now"), and
-- nothing in Ember's plan knew: no bet, no step, nothing in STATUS; its own reviews had run an hour before, and its
-- keep-or-drop dates fell three days into the test. An approved spend request (spend_money) that names Ember's
-- listings becomes a test: its listings, their views on the day the owner decided it (the baseline), and the day it
-- ends (the request's "for N days", else 7 days later). Once it ends, the listings' views and favorites then are kept
-- with it (final). STATUS shows a test while it runs and for a few days after it ended (agent/paid.py).
CREATE TABLE paid_tests (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    started_on  TEXT NOT NULL CHECK (length(started_on) = 10),
    ends_on     TEXT NOT NULL CHECK (length(ends_on) = 10),
    listings    TEXT NOT NULL CHECK (json_valid(listings)),
    baseline    TEXT NOT NULL CHECK (json_valid(baseline)),
    status      TEXT NOT NULL CHECK (status IN ('running', 'ended')),
    final       TEXT CHECK (final IS NULL OR json_valid(final)),
    created_at  TEXT NOT NULL,
    ended_at    TEXT,
    CHECK ((status = 'ended') = (ended_at IS NOT NULL)),
    CHECK ((status = 'ended') = (final IS NOT NULL))
);
CREATE INDEX paid_tests_by_scope ON paid_tests (mode, session, id);
CREATE TRIGGER paid_tests_no_delete BEFORE DELETE ON paid_tests
BEGIN SELECT RAISE(ABORT, 'paid_tests: rows cannot be deleted'); END;
