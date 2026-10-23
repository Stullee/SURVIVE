-- 0.46.0 (vision/generations.md): the world a request acts in is the world's, and a generation change keeps it.
-- Ember's records of the world (WORLD_TABLES in agent/store.py: the shop's listings, the boards, the Printify products,
-- the mailbox) keep the session of the world they were made in, her requests the session of the mind that made them,
-- and a mind's session is never below its world's: sessions only count up. So NEVER's "first" classes and an unlock's
-- scope read the world at or below the request's session, not at it: a later generation's first listing in a shop
-- that has listings is no first publication, nor an email to someone who wrote to her predecessor a first contact.
-- Until a generation change the two sessions are the same and nothing reads otherwise. (In dry run an earlier test
-- world's rows count too: its integrations are fake.) The views and the triggers that read them are made again, as
-- 0064 did, unchanged but for that.
DROP TRIGGER approvals_never_on_unlock;
DROP TRIGGER policy_uses_never;
DROP TRIGGER policy_uses_standing;
DROP TRIGGER approvals_unlock_carries;
DROP VIEW approvals_never;
DROP VIEW approvals_scope;
CREATE VIEW approvals_scope AS
SELECT a.id AS approval_id, m.id AS milestone_id
FROM approvals a
JOIN milestones m ON m.mode = a.mode AND m.session = a.session
LEFT JOIN approvals t ON t.id = CASE a.executor
    WHEN 'etsy_listing' THEN a.id
    WHEN 'etsy_edit' THEN (
        SELECT l.approval_id FROM etsy_listings l WHERE l.mode = a.mode AND l.session <= a.session
            AND l.listing_id = json_extract(a.action, '$.listing_id') ORDER BY l.id DESC LIMIT 1
    )
END
WHERE CASE
    WHEN a.executor IS 'email' THEN m.project_id IS NULL AND m.venture_id IS NULL
    WHEN m.project_id IS NOT NULL THEN m.project_id = t.project_id
    ELSE m.venture_id = COALESCE((SELECT p.venture_id FROM projects p WHERE p.id = t.project_id), t.venture_id)
END;
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
                SELECT 1 FROM emails e WHERE e.mode = r.mode AND e.session <= r.session
                    AND (e.direction = 'in' AND e.authenticated = 1 AND e.bulk = 0)
                    AND lower(e.from_addr) = lower(json_extract(r.action, '$.to'))
            )
        ) AS first_contact,
        (r.executor IS 'etsy_listing' AND NOT EXISTS (
            SELECT 1 FROM etsy_listings l WHERE l.mode = r.mode AND l.session <= r.session AND l.status = 'active'
        )) OR (r.executor IS 'pinterest_pin' AND NOT EXISTS (
            SELECT 1 FROM pinterest_boards b WHERE b.mode = r.mode AND b.session <= r.session AND b.status = 'active'
        )) OR (r.executor IS 'printify_product' AND NOT EXISTS (
            SELECT 1 FROM printify_products p WHERE p.mode = r.mode AND p.session <= r.session
                AND p.status IN ('publishing', 'active')
        )) AS first_publication,
        r.executor IS 'reddit_link' AS community_post,
        r.words GLOB '*steuer*' OR r.words GLOB '*gewerbe*' OR r.words GLOB '*finanzamt*' OR r.words GLOB '*vertrag*'
            OR r.words GLOB '*verträg*' OR r.words GLOB '*vertrÄg*' OR r.words GLOB '*contract*'
            OR r.words GLOB '*auftrag*' OR r.words GLOB '*aufträg*' OR r.words GLOB '*auftrÄg*'
            OR r.words GLOB '*vereinbarung*' OR r.words GLOB '*lizenz*'
            OR r.words GLOB '*widerruf*' OR r.words GLOB '*einfuhr*' OR r.words GLOB '*verzoll*'
            OR r.words GLOB '*[^a-z]invoic*' OR r.words GLOB '*[^a-z]agreement*' OR r.words GLOB '*[^a-z]licen*'
            OR r.words GLOB '*[^e]rechnung*' OR r.words GLOB '*[^a-z]ustg*' OR r.words GLOB '*[^a-z]umsatzst*'
            OR r.words GLOB '*[^a-z]mehrwertst*' OR r.words GLOB '*[^a-z]kleinunternehm*'
            OR r.words GLOB '*[^a-z]angebot*' OR r.words GLOB '*[^a-z]factur*' OR r.words GLOB '*[^a-z]fattur*'
            OR r.words GLOB '*[^a-z]faktur*' OR r.words GLOB '*[^a-z]contrat*' OR r.words GLOB '*[^a-z]impuest*'
            OR r.words GLOB '*[^a-z]impot*'
            OR r.words GLOB '*[^a-z]tax[^a-z]*' OR r.words GLOB '*[^a-z]taxes[^a-z]*'
            OR r.words GLOB '*[^a-z]taxed[^a-z]*' OR r.words GLOB '*[^a-z]taxable[^a-z]*'
            OR r.words GLOB '*[^a-z]taxation[^a-z]*' OR r.words GLOB '*[^a-z]vat[^a-z]*'
            OR r.words GLOB '*[^a-z]ust[^a-z]*' OR r.words GLOB '*[^a-z]mwst[^a-z]*'
            OR r.words GLOB '*[^a-z]gst[^a-z]*' OR r.words GLOB '*[^a-z]iva[^a-z]*'
            OR r.words GLOB '*[^a-z]tva[^a-z]*' OR r.words GLOB '*[^a-z]agb[^a-z]*' OR r.words GLOB '*[^a-z]customs[^a-z]*'
            OR r.words GLOB '*[^a-z]uid[^a-z]*' OR r.words GLOB '*[^a-z]offer[^a-z]*' OR r.words GLOB '*[^a-z]offers[^a-z]*'
            OR r.words GLOB '*[^a-z]quote[^a-z]*' OR r.words GLOB '*[^a-z]quotes[^a-z]*'
            OR r.words GLOB '*[^a-z]quotation[^a-z]*' AS legal,
        r.executor IS NULL OR r.executor NOT IN (
            'email', 'reddit_link', 'etsy_listing', 'etsy_edit', 'pinterest_pin', 'printify_product'
        ) AS owner_only
    FROM (
        SELECT id, mode, session, type, executor, action,
            ' ' || COALESCE((SELECT w.words FROM act_words w WHERE w.approval_id = approvals.id), '') || ' ' || lower(
                CASE WHEN executor IS 'email' THEN
                    (CASE WHEN json_type(action, '$.subject') = 'text' THEN json_extract(action, '$.subject') ELSE '' END)
                    || ' ' || (CASE WHEN json_type(action, '$.body') = 'text' THEN json_extract(action, '$.body') ELSE '' END)
                ELSE '' END
            ) || ' ' AS words
        FROM approvals
    ) r
);
CREATE TRIGGER policy_uses_standing BEFORE INSERT ON policy_uses
WHEN NOT EXISTS (
    SELECT 1 FROM policy_grants g JOIN approvals a ON a.id = NEW.approval_id JOIN milestones m ON m.id = g.milestone_id
    WHERE g.id = NEW.grant_id AND g.level = NEW.level AND g.mode = a.mode AND g.session = a.session
        AND m.status = 'open'
        AND EXISTS (SELECT 1 FROM approvals_scope s WHERE s.approval_id = a.id AND s.milestone_id = m.id)
        AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
)
BEGIN SELECT RAISE(ABORT, 'policy_uses: no standing unlock carries this request'); END;
CREATE TRIGGER approvals_unlock_carries BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND NOT EXISTS (
        SELECT 1 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id JOIN milestones m ON m.id = g.milestone_id
        WHERE u.approval_id = NEW.id AND g.level = u.level AND m.status = 'open'
            AND (u.level = 'auto' OR u.veto_until <= NEW.decided_at)
            AND EXISTS (SELECT 1 FROM approvals_scope s WHERE s.approval_id = NEW.id AND s.milestone_id = m.id)
            AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
    )
BEGIN SELECT RAISE(ABORT, 'approvals: no standing unlock carries this request'); END;
CREATE TRIGGER approvals_never_on_unlock BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'approvals: an unlock never approves this request (NEVER)'); END;
CREATE TRIGGER policy_uses_never BEFORE INSERT ON policy_uses
WHEN (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: an unlock never carries this request (NEVER)'); END;
