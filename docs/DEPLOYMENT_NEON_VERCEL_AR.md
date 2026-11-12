# Wasla V1 | Neon and Vercel deployment

## Environments
- An independent Neon project, with production and preview-v1 branches.
- Vercel FastAPI root entrypoint is index.py; Python 3.12.
- Separate pooled DATABASE_URL for Vercel production and preview.
- Database migrations executed by the operator with a direct DATABASE_URL_UNPOOLED; never on cold-start.
- The default Neon database has no operator accounts and no real customer data migrated from SQLite.

## Security
- WASLA_ENV=production ensures secure cookies and HTTPS requirements.
- On Vercel, same-origin POST is checked against the requested host.
- WASLA_WORKER_SECRET (minimum 32 chars) is an independently generated secret stored only in Vercel and the Neon Function environment; never in Git.
- To reach an SSO-protected preview, the Neon scheduler uses a Vercel automation bypass secret. This secret is only for the server-to-server scheduler and must be kept private; rotate on suspicion of disclosure.
- GET /api/health is a live database health check.
- Use a trusted operator terminal and an unpooled Neon connection to invoke python -m wasla.cli bootstrap. The email and password are entered interactively; The optional /setup flow requires an expiring random capability and refuses once any provider exists; see PRODUCTION_RELEASE_AR.md.
- Configure separate webhook Meta secrets only after obtaining a real authorized Meta app. Send is intentionally disabled in V1.

## Durable event processing
- POST /api/webhooks/whatsapp verifies HMAC, validates payload and writes the raw event into PostgreSQL before acknowledging.
- Neon Function source at neon/functions/drain/index.mjs is invoked on a one-minute UTC schedule using a Neon Function Trigger.
- The Neon Function calls the Vercel /api/internal/drain-webhooks endpoint with the worker bearer secret.
- Concurrent workers use SELECT FOR UPDATE SKIP LOCKED, transactional writes, retry backoff and a dead-letter status after five failures.
- The provider can inspect failed events and retry after correcting the cause.
- If a scheduled call fails, a pending event remains persisted and is retried on a later occurrence. Alerts for prolonged queue lag are still needed before critical production traffic.

## Pre-release checklist
- [x] PostgreSQL schema and constraints on both branches.
- [x] GitHub Actions SQLite and PostgreSQL acceptance tests.
- [x] Preview HTTP health/catalog and website smoke test.
- [ ] Preview scheduler trigger observed processing a synthetic webhook.
- [ ] Production security and domain checks.
- [ ] Provider admin initialized interactively by the owner.
- [ ] Real WhatsApp business account end-to-end verification.
- [ ] External encrypted database backup and restore exercise.
