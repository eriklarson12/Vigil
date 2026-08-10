CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE incidents (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service           text NOT NULL,
    title             text NOT NULL,
    severity          text CHECK (severity IN ('SEV1','SEV2','SEV3','SEV4')),
    status            text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','resolved','postmortem_done')),
    slack_message_ts  text,
    resolution_source text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz
);
CREATE INDEX incidents_grouping_idx ON incidents (service, status, created_at);

CREATE TABLE alerts (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint       text NOT NULL,
    alert_name        text NOT NULL,
    service           text,
    status            text NOT NULL CHECK (status IN ('firing','resolved')),
    starts_at         timestamptz NOT NULL,
    ends_at           timestamptz,
    labels            jsonb NOT NULL,
    annotations       jsonb NOT NULL DEFAULT '{}',
    raw_payload       jsonb,
    processing_status text NOT NULL DEFAULT 'queued'
                      CHECK (processing_status IN ('queued','claimed','processed','failed','duplicate')),
    claimed_at        timestamptz,
    incident_id       uuid REFERENCES incidents(id),
    received_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fingerprint, starts_at)
);
CREATE INDEX alerts_queue_idx ON alerts (processing_status, received_at)
    WHERE processing_status IN ('queued','claimed');

CREATE TABLE incident_events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id  uuid NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_type   text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX incident_events_idx ON incident_events (incident_id, created_at);

CREATE TABLE commit_candidates (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     uuid NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    sha             text NOT NULL,
    message         text,
    author          text,
    committed_at    timestamptz,
    files           jsonb,
    heuristic_score numeric(5,4),
    feature_scores  jsonb,
    llm_rank        int,
    llm_confidence  numeric(4,3),
    llm_rationale   text,
    UNIQUE (incident_id, sha)
);

CREATE TABLE deploy_events (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service     text NOT NULL,
    commit_shas text[] NOT NULL,
    finished_at timestamptz NOT NULL
);

CREATE TABLE runbooks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         text UNIQUE NOT NULL,
    title        text NOT NULL,
    service_tags text[] NOT NULL DEFAULT '{}',
    content_hash text NOT NULL,
    raw_markdown text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runbook_chunks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    runbook_id   uuid NOT NULL REFERENCES runbooks(id) ON DELETE CASCADE,
    chunk_index  int NOT NULL,
    heading_path text NOT NULL,
    content      text NOT NULL,
    token_count  int NOT NULL,
    content_hash text NOT NULL,
    embedding    vector(768),
    tsv          tsvector GENERATED ALWAYS AS
                 (to_tsvector('english', heading_path || ' ' || content)) STORED,
    UNIQUE (runbook_id, chunk_index)
);
-- HNSW over IVFFlat: no training step, better recall at small N. (At hundreds of
-- chunks a seq scan is sub-ms; the index demonstrates production shape.)
CREATE INDEX runbook_chunks_hnsw ON runbook_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX runbook_chunks_tsv ON runbook_chunks USING gin (tsv);

CREATE TABLE postmortems (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid UNIQUE NOT NULL REFERENCES incidents(id),
    markdown    text NOT NULL,
    model_used  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_budget (
    day        date PRIMARY KEY,
    calls_used int NOT NULL DEFAULT 0
);
