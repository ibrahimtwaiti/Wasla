ALTER TABLE service_tickets ADD COLUMN next_action TEXT NOT NULL DEFAULT '';
ALTER TABLE service_tickets ADD COLUMN followup_at BIGINT;
ALTER TABLE service_tickets ADD COLUMN campaign TEXT NOT NULL DEFAULT '';
CREATE TABLE service_media_jobs(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,message_id TEXT NOT NULL UNIQUE,account_id TEXT NOT NULL,descriptor TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt BIGINT NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',media_id TEXT,FOREIGN KEY(message_id,tenant_id) REFERENCES service_messages(id,tenant_id),FOREIGN KEY(account_id,tenant_id) REFERENCES service_accounts(id,tenant_id));
CREATE INDEX service_media_due ON service_media_jobs(state,next_attempt);
CREATE TABLE service_retention_policy(tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),days INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,evidence TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE service_redactions(ticket_id TEXT PRIMARY KEY REFERENCES service_tickets(id),tenant_id TEXT NOT NULL REFERENCES tenants(id),redacted_at BIGINT NOT NULL);
CREATE TABLE service_channel_snapshots(account_id TEXT PRIMARY KEY REFERENCES service_accounts(id),tenant_id TEXT NOT NULL REFERENCES tenants(id),profile TEXT NOT NULL,metrics TEXT NOT NULL,fetched_at TEXT NOT NULL);
INSERT INTO schema_migrations(version,applied_at) VALUES(4,'2026-11-14T00:00:00+00:00');
