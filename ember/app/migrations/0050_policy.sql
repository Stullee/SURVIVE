-- 0.13.0: a policy engine. Every action waited for the owner's click (about 46 a day live), also the small, safe ones:
-- adding photos to a live listing, a price moved a little, a reply in a thread the other person started. Now the owner
-- can grant a milestone they back autonomy for such rules (agent/policy.py): manual (as before), veto_window (it runs
-- unless the owner vetoes it in time) or auto, each with a daily limit and a budget of actions. Ember's code revokes a
-- grant on an unclear result, a spent budget, a missed milestone or the owner's veto, and only proposes promotions.
-- A grant is history: the newest per milestone and rule counts, none changes, none is deleted.
CREATE TABLE policy_grants (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL,
    session      INTEGER NOT NULL,
    milestone_id INTEGER NOT NULL REFERENCES milestones (id),
    rule         TEXT NOT NULL CHECK (rule IN ('qa_fix', 'price_change', 'listing_variant', 'deactivate', 'email_reply')),
    level        TEXT NOT NULL CHECK (level IN ('manual', 'veto_window', 'auto')),
    per_day      INTEGER NOT NULL CHECK (per_day BETWEEN 1 AND 20),
    budget       INTEGER NOT NULL CHECK (budget BETWEEN 1 AND 100),  -- actions in all, under this grant
    by           TEXT NOT NULL CHECK (length(by) BETWEEN 1 AND 60),  -- the owner, or Ember's code (a revocation)
    why          TEXT CHECK (length(why) <= 300),
    created_at   TEXT NOT NULL
);
CREATE INDEX policy_grants_by_milestone ON policy_grants (milestone_id, rule, id);
CREATE TRIGGER policy_grants_no_update BEFORE UPDATE ON policy_grants
BEGIN SELECT RAISE(ABORT, 'policy_grants: a grant never changes'); END;
CREATE TRIGGER policy_grants_no_delete BEFORE DELETE ON policy_grants
BEGIN SELECT RAISE(ABORT, 'policy_grants: rows cannot be deleted'); END;

-- A request a grant carried: at once (auto) or after the veto window, unless the owner decided it first.
CREATE TABLE policy_uses (
    id          INTEGER PRIMARY KEY,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    grant_id    INTEGER NOT NULL REFERENCES policy_grants (id),
    level       TEXT NOT NULL CHECK (level IN ('veto_window', 'auto')),
    created_at  TEXT NOT NULL,
    veto_until  TEXT,  -- veto_window: when it is approved unless the owner decided first
    approved_at TEXT,  -- when the grant approved it
    CHECK ((level = 'veto_window') = (veto_until IS NOT NULL))
);
CREATE TRIGGER policy_uses_fixed BEFORE UPDATE OF approval_id, grant_id, level, created_at, veto_until ON policy_uses
BEGIN SELECT RAISE(ABORT, 'policy_uses: a use never changes'); END;
CREATE TRIGGER policy_uses_approved_once BEFORE UPDATE OF approved_at ON policy_uses
WHEN OLD.approved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'policy_uses: approved once'); END;
CREATE TRIGGER policy_uses_no_delete BEFORE DELETE ON policy_uses
BEGIN SELECT RAISE(ABORT, 'policy_uses: rows cannot be deleted'); END;

-- Every request that fits a rule, whether a grant carries it or not: what the owner's approvals without changes are
-- counted from when Ember's code proposes more autonomy.
CREATE TABLE policy_candidates (
    approval_id  INTEGER PRIMARY KEY REFERENCES approvals (id),
    rule         TEXT NOT NULL CHECK (rule IN ('qa_fix', 'price_change', 'listing_variant', 'deactivate', 'email_reply')),
    milestone_id INTEGER REFERENCES milestones (id),
    created_at   TEXT NOT NULL
);
CREATE TRIGGER policy_candidates_no_update BEFORE UPDATE ON policy_candidates
BEGIN SELECT RAISE(ABORT, 'policy_candidates: a candidate never changes'); END;
CREATE TRIGGER policy_candidates_no_delete BEFORE DELETE ON policy_candidates
BEGIN SELECT RAISE(ABORT, 'policy_candidates: rows cannot be deleted'); END;
