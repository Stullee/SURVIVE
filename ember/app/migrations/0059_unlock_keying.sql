-- 0.14.0: unlocks keyed to what a request acts on; NEVER's words read in what a request says; a veto window that ends
-- with its milestone; an unclear Undo of a pin or product that can be repeated.
--
-- An unlock followed the plan's focus: a request was carried by the grant of the milestone its cycle aimed at, whatever
-- it touched (an unlock for "retire the old CV templates" could deactivate another line's best seller). Now a
-- milestone's unlock carries only what belongs to it (approvals_scope): a change to a listing of its project (a
-- milestone of a venture: of the venture's projects) or a new listing in it. An email reply belongs to no product line,
-- so only a milestone of no project and no venture carries one. Ember's code looks the grant up from what the request
-- acts on (agent/policy.py), and the database checks the same.
CREATE VIEW approvals_scope AS
SELECT a.id AS approval_id, m.id AS milestone_id
FROM approvals a
JOIN milestones m ON m.mode = a.mode AND m.session = a.session
LEFT JOIN approvals t ON t.id = CASE a.executor
    WHEN 'etsy_listing' THEN a.id
    WHEN 'etsy_edit' THEN (
        SELECT l.approval_id FROM etsy_listings l WHERE l.mode = a.mode AND l.session = a.session
            AND l.listing_id = json_extract(a.action, '$.listing_id') ORDER BY l.id DESC LIMIT 1
    )
END
WHERE CASE
    WHEN a.executor IS 'email' THEN m.project_id IS NULL AND m.venture_id IS NULL
    WHEN m.project_id IS NOT NULL THEN m.project_id = t.project_id
    ELSE m.venture_id = COALESCE((SELECT p.venture_id FROM projects p WHERE p.id = t.project_id), t.venture_id)
END;

-- NEVER's words of tax, VAT, a Gewerbe and contracts were read in a request's title, text and payload with A to Z
-- lowered: a listing's product copy tripped them ("Mietvertrag" in a disclaimer), while "§ 19 UStG", "Rechnung
-- folgt", other languages, look-alike letters and invisible characters passed. Now they are read in what a request
-- says or sends (an email's subject and text): as Ember's code normalised it when it stored the request (agent/never.py:
-- NFKC, invisible characters out, Cyrillic and Greek look-alikes folded, casefolded), and as the database lowers it.
CREATE TABLE act_words (
    approval_id INTEGER PRIMARY KEY REFERENCES approvals (id),
    words       TEXT NOT NULL
);
CREATE TRIGGER act_words_no_update BEFORE UPDATE ON act_words
BEGIN SELECT RAISE(ABORT, 'act_words: what a request says never changes'); END;
CREATE TRIGGER act_words_no_delete BEFORE DELETE ON act_words
BEGIN SELECT RAISE(ABORT, 'act_words: rows cannot be deleted'); END;

-- The view and the triggers that read it or the grants are made again: NEVER with the new words, an unlock's use only
-- for what its milestone covers, and Ember's code approving only while the milestone is open and the veto window has
-- passed, for what the milestone covers (a request held when its milestone closed as done waits for the owner, and
-- so does one an unlock of 0.13.0 held for a milestone that doesn't cover it). The NEVER checks are made last, so
-- they are the first to answer, as before.
DROP TRIGGER approvals_never_on_unlock;
DROP TRIGGER approvals_unlock_carries;
DROP TRIGGER policy_uses_never;
DROP TRIGGER policy_uses_standing;
DROP VIEW approvals_never;

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
        (r.executor IS 'etsy_listing' AND NOT EXISTS (
            SELECT 1 FROM etsy_listings l WHERE l.mode = r.mode AND l.session = r.session AND l.status = 'active'
        )) OR (r.executor IS 'pinterest_pin' AND NOT EXISTS (
            SELECT 1 FROM pinterest_boards b WHERE b.mode = r.mode AND b.session = r.session AND b.status = 'active'
        )) OR (r.executor IS 'printify_product' AND NOT EXISTS (
            SELECT 1 FROM printify_products p WHERE p.mode = r.mode AND p.session = r.session
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
CREATE TRIGGER policy_uses_never BEFORE INSERT ON policy_uses
WHEN (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: an unlock never carries this request (NEVER)'); END;
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

-- One Undo at a time (0052, 0054), except that an Undo deleting a pin or a product that ended unclear (the app stopped
-- while deleting it) may be repeated: deleting what is gone already counts as done, so a repeat can't do harm. Before,
-- such an Undo stayed "under way" for good.
DROP TRIGGER action_undos_once;
CREATE TRIGGER action_undos_once BEFORE INSERT ON action_undos
WHEN EXISTS (
    SELECT 1 FROM action_undos u JOIN approvals a ON a.id = u.approval_id
    WHERE u.journal_id = NEW.journal_id AND NOT (
        a.status = 'failed'
        AND NOT EXISTS (
            SELECT 1 FROM action_journal j WHERE j.approval_id = u.approval_id AND j.status <> 'failed'
                AND NOT (j.status = 'unclear' AND j.class IN ('pinterest.delete_pin', 'printify.delete_product'))
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'action_undos: this action is undone or being undone'); END;

-- An unlock's photo fix compared only the number of photos at Etsy with Ember's record: photos the owner replaced
-- there, as many as before, were still deleted. Ember's code now keeps the photo numbers Etsy gave a listing when it
-- last set its photos, and an unlock's photo fix waits for the owner unless Etsy still has exactly those.
CREATE TABLE etsy_photo_ids (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL,
    session     INTEGER NOT NULL DEFAULT 0,
    listing_id  INTEGER NOT NULL,
    ids         TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX etsy_photo_ids_listing ON etsy_photo_ids (mode, session, listing_id, id);
