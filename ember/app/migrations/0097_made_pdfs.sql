-- 0.40.1 (analysis-0.40.0, 3.2): the file each PDF make_document made was made from, so a KDP check can say which
-- file the book is and whether it changed since. Live, the Haushaltsbuch's PDFs v19 to v22 were all made from v18.md,
-- the spec still named v15.pdf (the book the owner rejected), and each cycle on the book spent its first steps finding
-- out which file the book was. Kept here, not in the PDF: the files Ember makes go to buyers and to KDP as they are.
CREATE TABLE made_pdfs (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    path          TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 500),  -- the PDF, in Ember's workspace
    sha256        TEXT NOT NULL CHECK (length(sha256) = 64),  -- the PDF's bytes as make_document wrote them
    source        TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 500),  -- the .md (or .txt) file it was made from
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),  -- that file's text then
    cycle_id      INTEGER REFERENCES cycles (id),
    made_at       TEXT NOT NULL
);
CREATE INDEX made_pdfs_by_pdf ON made_pdfs (mode, session, sha256);
