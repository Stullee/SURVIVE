-- 0.13.0: NEVER, in the database. The policy engine (0050) lets the owner unlock small, safe requests; some kinds must
-- never run on an unlock, whatever is granted (agent/never.py): creating an account, moving or spending money, a first
-- contact (UWG section 7), Ember's first publication in the shop (the owner's decision and Impressum, DDG section 5), a
-- post in a third-party community, words that touch tax, VAT, a Gewerbe or a contract, what only the owner carries out
-- (no executor, or one this check doesn't know), and the policies themselves. Ember's code checks this before it holds
-- or approves a request; the database checks it again here, on its own, whatever the code does. It reads a request as
-- never.py does: lower() lowers A to Z only, and GLOB's [^a-z] marks a word's edge.
-- A migration that rebuilds approvals (as 0010 did) drops this view and the triggers below first and makes them again.
CREATE VIEW approvals_never AS
SELECT
    approval_id, account, money, first_contact, first_publication, community_post, legal, owner_only,
    account OR money OR first_contact OR first_publication OR community_post OR legal OR owner_only AS never
FROM (
    SELECT
        r.id AS approval_id,
        r.type = 'create_account' AS account,
        r.type = 'spend_money' AS money,
        r.executor IS 'email' AND (
            json_type(r.action, '$.to') IS NOT 'text' OR json_extract(r.action, '$.to') = '' OR NOT EXISTS (
                SELECT 1 FROM emails e WHERE e.mode = r.mode AND e.session = r.session AND e.direction = 'in'
                    AND lower(e.from_addr) = lower(json_extract(r.action, '$.to'))
            )
        ) AS first_contact,
        r.executor IS 'etsy_listing' AND NOT EXISTS (
            SELECT 1 FROM etsy_listings l WHERE l.mode = r.mode AND l.session = r.session AND l.status = 'active'
        ) AS first_publication,
        r.executor IS 'reddit_link' AS community_post,
        r.words GLOB '*steuer*' OR r.words GLOB '*gewerbe*' OR r.words GLOB '*finanzamt*' OR r.words GLOB '*vertrag*'
            OR r.words GLOB '*verträg*' OR r.words GLOB '*vertrÄg*' OR r.words GLOB '*[^a-z]tax[^a-z]*'
            OR r.words GLOB '*[^a-z]taxes[^a-z]*' OR r.words GLOB '*[^a-z]taxed[^a-z]*' OR r.words GLOB '*[^a-z]vat[^a-z]*'
            OR r.words GLOB '*[^a-z]ust[^a-z]*' OR r.words GLOB '*[^a-z]mwst[^a-z]*'
            OR r.words GLOB '*[^a-z]contract[^a-z]*' OR r.words GLOB '*[^a-z]contracts[^a-z]*' AS legal,
        r.executor IS NULL OR r.executor NOT IN ('email', 'reddit_link', 'etsy_listing', 'etsy_edit') AS owner_only
    FROM (
        SELECT id, mode, session, type, executor, action,
            ' ' || lower(title || ' ' || description || ' ' || payload) || ' ' AS words
        FROM approvals
    ) r
);

-- An unlock holds or approves a request only while it stands (the newest grant of its milestone and rule, at this
-- level, for that request's milestone, which is open), within its budget, and never a NEVER request.
CREATE TRIGGER policy_uses_never BEFORE INSERT ON policy_uses
WHEN (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: an unlock never carries this request (NEVER)'); END;
CREATE TRIGGER policy_uses_standing BEFORE INSERT ON policy_uses
WHEN NOT EXISTS (
    SELECT 1 FROM policy_grants g JOIN approvals a ON a.id = NEW.approval_id JOIN milestones m ON m.id = g.milestone_id
    WHERE g.id = NEW.grant_id AND g.level = NEW.level AND g.mode = a.mode AND g.session = a.session
        AND g.milestone_id = a.milestone_id AND m.status = 'open'
        AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
)
BEGIN SELECT RAISE(ABORT, 'policy_uses: no standing unlock carries this request'); END;
CREATE TRIGGER policy_uses_budget BEFORE INSERT ON policy_uses
WHEN (SELECT COUNT(*) FROM policy_uses u WHERE u.grant_id = NEW.grant_id)
    >= (SELECT g.budget FROM policy_grants g WHERE g.id = NEW.grant_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: beyond the unlock''s budget'); END;

-- Ember's code approves a request only through a standing unlock that holds it, and never a NEVER request.
CREATE TRIGGER approvals_never_on_unlock BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'approvals: an unlock never approves this request (NEVER)'); END;
CREATE TRIGGER approvals_unlock_carries BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND NOT EXISTS (
        SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id
        WHERE u.approval_id = NEW.id AND g.level = u.level
            AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
    )
BEGIN SELECT RAISE(ABORT, 'approvals: no standing unlock carries this request'); END;

-- Only the owner unlocks: Ember's code only takes an unlock back (manual).
CREATE TRIGGER policy_grants_owner_unlocks BEFORE INSERT ON policy_grants
WHEN NEW.level <> 'manual' AND NEW.by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
BEGIN SELECT RAISE(ABORT, 'policy_grants: only the owner unlocks (NEVER)'); END;
