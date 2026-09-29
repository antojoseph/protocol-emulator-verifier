CREATE TABLE IF NOT EXISTS asic_jobs (
    id uuid PRIMARY KEY,
    request_key text UNIQUE NOT NULL,
    source_hash text NOT NULL,
    verifier_hash text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('fast', 'full')),
    seed bigint NOT NULL,
    archive bytea NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','running','completed','failed','cancelled')),
    cancel_requested boolean NOT NULL DEFAULT false,
    attempt integer NOT NULL DEFAULT 0,
    lease_token uuid,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    result jsonb,
    artifact jsonb,
    error text
);
CREATE INDEX IF NOT EXISTS asic_jobs_queue ON asic_jobs(mode, created_at)
    WHERE state IN ('queued','running');
