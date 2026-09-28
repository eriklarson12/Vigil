-- R9: postmortem action items become GitHub issues. Filing runs outside the postmortem graph
-- (ADR-013), so a retry needs the structured items, not just the rendered markdown.
-- Postmortems written before this migration keep action_items NULL and are never filed.
ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS action_items jsonb;

-- {item index: issue url}. Written one item at a time, so a crash loses at most the item in
-- flight, and the marker in each issue body recovers that one.
ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS issue_urls jsonb NOT NULL DEFAULT '{}';
ALTER TABLE postmortems ADD COLUMN IF NOT EXISTS issue_attempts int NOT NULL DEFAULT 0;
