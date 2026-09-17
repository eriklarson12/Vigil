-- R6: deploy correlation writes deploy_events on every incident, so the insert must be
-- idempotent across a resumed graph run. The roadmap named UNIQUE(service, sha, deployed_at);
-- those columns do not exist. The natural key on the table as built is the whole triple.
--
-- btree indexes text[] via array_ops, but an index row is capped near 2704 bytes: about 66
-- forty-character shas. The writer emits ARRAY[sha], one row per sha, partly for that reason.

-- De-duplicate first: repeated `vigil-sim fire` runs may already have left identical rows,
-- and CREATE UNIQUE INDEX would abort the whole migration. ctid is the physical row id.
DELETE FROM deploy_events a USING deploy_events b
 WHERE a.ctid > b.ctid AND a.service = b.service
   AND a.commit_shas = b.commit_shas AND a.finished_at = b.finished_at;

CREATE UNIQUE INDEX IF NOT EXISTS deploy_events_natural_key
    ON deploy_events (service, commit_shas, finished_at);

-- score_commits_node filters `WHERE service = %s` on a table that now grows once per
-- incident per service; before R6 it only grew when the simulator planted.
CREATE INDEX IF NOT EXISTS deploy_events_service_finished_idx
    ON deploy_events (service, finished_at DESC);
