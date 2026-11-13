-- Wasla V1 security upgrade. Apply separately from request handling on production.
-- Existing legacy sessions without an activity record will be asked to log in again.
CREATE TABLE IF NOT EXISTS session_activity (
    token_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE,
    issued_at BIGINT NOT NULL,
    last_seen_at BIGINT NOT NULL
);
INSERT INTO schema_migrations(version,applied_at)
VALUES(2,to_char(timezone('UTC',CURRENT_TIMESTAMP),'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
ON CONFLICT(version) DO NOTHING;
