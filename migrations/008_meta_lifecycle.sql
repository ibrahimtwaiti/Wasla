CREATE TABLE meta_grants(account_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,app_id TEXT NOT NULL,subject_hash TEXT NOT NULL,checked_at BIGINT,health TEXT NOT NULL DEFAULT 'unchecked',FOREIGN KEY(account_id,tenant_id) REFERENCES service_accounts(id,tenant_id));
CREATE INDEX meta_grant_subject ON meta_grants(app_id,subject_hash);
CREATE TABLE meta_callback_receipts(event_hash TEXT PRIMARY KEY,kind TEXT NOT NULL,received_at BIGINT NOT NULL);
INSERT INTO schema_migrations(version,applied_at) VALUES(7,'2026-11-18T00:00:00+00:00');
