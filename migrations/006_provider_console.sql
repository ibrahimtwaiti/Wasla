CREATE TABLE provider_configuration(key TEXT PRIMARY KEY,ciphertext TEXT NOT NULL,updated_at TEXT NOT NULL);
INSERT INTO schema_migrations(version,applied_at) VALUES(5,'2026-11-16T00:00:00+00:00');
