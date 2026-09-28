-- R5: revert PR with a human approval gate. The click handler re-checks the gate from the
-- database, never from the Slack payload, so the verdict's suggested action must be stored.
ALTER TABLE commit_candidates ADD COLUMN IF NOT EXISTS llm_suggested_action text;

-- NULL -> requested -> proposed | failed. The claim is one conditional UPDATE, which is what
-- makes a double click a no-op.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS revert_pr_url text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS revert_pr_state text
    CHECK (revert_pr_state IN ('requested', 'proposed', 'failed'));
