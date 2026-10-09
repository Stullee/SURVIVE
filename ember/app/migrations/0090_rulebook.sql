-- 0.36.0: the owner's rulebook, in place of the standing instructions. Live, the owner rewrote the instructions for one
-- week's order of work (2026-10-07), and the rules they had set before (money, their time, blog posts in Markdown)
-- dropped out with the old text. Now each rule stands on its own until the owner removes it; what comes next is the
-- plan's (their pins, holds, worths and freezes). A rule never changes: an edit is a new rule in the old one's place
-- (replaces), and a removed rule stays, marked removed, so the history shows what the owner told the agent, and when.
CREATE TABLE rules (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    place      INTEGER NOT NULL CHECK (place >= 1),  -- its place in the rulebook (an edit keeps it)
    created_at TEXT NOT NULL,
    entered_by TEXT CHECK (entered_by IS NULL OR length(entered_by) <= 60),
    text       TEXT NOT NULL CHECK (length(trim(text)) >= 1 AND length(text) <= 1500),
    replaces   INTEGER REFERENCES rules (id),  -- the rule an edit replaced
    removed_at TEXT,
    removed_by TEXT CHECK (removed_by IS NULL OR length(removed_by) <= 60)
);
CREATE INDEX rules_by_scope ON rules (mode, session, place, id);
CREATE TRIGGER rules_fixed BEFORE UPDATE OF id, mode, session, place, created_at, entered_by, text, replaces ON rules
BEGIN SELECT RAISE(ABORT, 'rules: a rule never changes (an edit is a new rule in its place)'); END;
CREATE TRIGGER rules_removed_once BEFORE UPDATE OF removed_at, removed_by ON rules WHEN OLD.removed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'rules: a removed rule stays removed'); END;
CREATE TRIGGER rules_no_delete BEFORE DELETE ON rules
BEGIN SELECT RAISE(ABORT, 'rules: the history cannot change'); END;

-- The current standing instructions of each mode and session become its first rules: one a paragraph, and one a line
-- that starts with "- " or "* " (Keep as instruction added a message that way). The history stays in its table.
WITH RECURSIVE
current (mode, session, created_at, entered_by, text) AS (
    SELECT s.mode, s.session, s.created_at, s.entered_by,
           replace(replace(replace(s.text, char(13), ''), char(10) || '- ', char(10) || char(10)),
                   char(10) || '* ', char(10) || char(10))
    FROM standing_instructions s
    WHERE s.id = (SELECT MAX(t.id) FROM standing_instructions t WHERE t.mode = s.mode AND t.session = s.session)
),
parts (mode, session, created_at, entered_by, part, rest, n) AS (
    SELECT mode, session, created_at, entered_by, '', text || char(10) || char(10), 0 FROM current
    UNION ALL
    SELECT mode, session, created_at, entered_by,
           trim(substr(rest, 1, instr(rest, char(10) || char(10)) - 1), char(32, 9, 10)),
           substr(rest, instr(rest, char(10) || char(10)) + 2),
           n + 1
    FROM parts
    WHERE instr(rest, char(10) || char(10)) > 0
),
cleaned (mode, session, created_at, entered_by, text, n) AS (
    SELECT mode, session, created_at, entered_by,
           CASE WHEN part LIKE '- %' OR part LIKE '* %' THEN trim(substr(part, 3), char(32, 9, 10)) ELSE part END, n
    FROM parts
    WHERE n > 0
)
INSERT INTO rules (mode, session, place, created_at, entered_by, text)
SELECT mode, session, ROW_NUMBER() OVER (PARTITION BY mode, session ORDER BY n), created_at, entered_by, text
FROM cleaned
WHERE text <> ''
ORDER BY mode, session, n;
