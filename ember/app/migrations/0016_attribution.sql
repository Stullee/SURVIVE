-- 0.12.0: revenue and expenses belong to the project and venture that earned or spent them. The ledger had a
-- project_id since 0.2.0 that nothing wrote, so every project and venture showed "earned $0.00" and no project could
-- succeed. The owner names the project (its venture comes along) or the venture when recording an entry; a
-- correction belongs where the entry it corrects belongs. Only revenue and expenses are attributed.
ALTER TABLE ledger ADD COLUMN venture_id INTEGER REFERENCES ventures (id);

CREATE TRIGGER ledger_attribution_fits BEFORE INSERT ON ledger
WHEN (NEW.project_id IS NOT NULL OR NEW.venture_id IS NOT NULL) AND NEW.type NOT IN ('revenue', 'expense')
BEGIN
    SELECT RAISE(ABORT, 'ledger: only revenue and expenses belong to a project or venture');
END;
CREATE INDEX ledger_by_project ON ledger (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX ledger_by_venture ON ledger (venture_id) WHERE venture_id IS NOT NULL;
