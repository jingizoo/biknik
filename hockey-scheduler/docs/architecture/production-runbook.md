# Production runbook (#90)

Operational notes for deploying and running the hockey scheduler in
production. The app is stdlib-only Python; there is no build step.

## Health & readiness endpoints

Two public, non-sensitive endpoints support liveness and deployment gating.
Neither returns accounts, secrets, connection strings, or env values.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Liveness + dependencies: app up, DB reachable, migrations applied, delivery mode + worker status. |
| `GET /api/readiness` | Deployment gate: DB reachable, migrations current, (production) ≥1 active admin, cookie hardening active. Returns `ready: true/false` and a per-check breakdown. |

Point your load balancer / orchestrator liveness probe at `/api/health` and
its readiness probe at `/api/readiness`. In production `ready` is only `true`
once every check passes — notably, an active League Admin must exist.

## Required environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_MODE` | `production` disables demo conveniences (X-Demo-Role header, headerless-admin fallback, demo seeding, `/api/reset`) and enables `Secure` cookies. Anything else = demo. | `demo` |
| `DATABASE_URL` | `postgres://…` (or a SQLite path) → durable `SqlStore`. Unset → in-memory (data lost on restart). | in-memory |
| `BOOTSTRAP_ADMIN_USER` / `BOOTSTRAP_ADMIN_PASSWORD` | First League Admin, created on boot only when the store has no accounts (idempotent). | — |
| `DELIVERY_WORKER_ENABLED` / `_INTERVAL` / `_BATCH` | Opt-in background delivery worker (#79). | disabled / 30s / 50 |
| Email/push transport vars | Configure real SMTP / push; default is dry-run (nothing sent). | dry-run |

Secrets are read from the environment only — they are never returned by any
API, logged, or persisted in plaintext (passwords are PBKDF2-hashed).

## First-admin bootstrap

Production skips demo account seeding, so the first admin is provisioned from
the environment:

1. Set `APP_MODE=production`, `DATABASE_URL`, and
   `BOOTSTRAP_ADMIN_USER` / `BOOTSTRAP_ADMIN_PASSWORD`.
2. Start the app. On boot, if the store has **no** accounts, the bootstrap
   admin is created (see `hockey_scheduler/bootstrap.py`); if any account
   already exists it is a safe no-op.
3. Sign in via the login form and create the remaining accounts.
4. You can then unset the bootstrap vars — they only matter on an empty store.

## Production start checklist

- [ ] `APP_MODE=production`.
- [ ] `DATABASE_URL` points at the durable database.
- [ ] Migrations current — `GET /api/health` shows `migrations.current: true`
      (they run automatically on boot, forward-only, #75).
- [ ] At least one active League Admin — `GET /api/readiness` `active_admin` ok.
- [ ] Served over HTTPS (directly or behind a TLS-terminating proxy) so the
      `Secure` session cookie (#76) is honored.
- [ ] `GET /api/readiness` returns `ready: true`.

## Backup & restore

- **State lives entirely in the SQL database** (`DATABASE_URL`). There is no
  other durable state — the app process is stateless and sessions/tokens are
  rows, not files.
- **Backup**: use your database's native tooling (e.g. `pg_dump` for
  PostgreSQL) on a schedule. A dump captures accounts, sessions, schedule,
  rosters, notifications, feed tokens, and preferences.
- **Restore**: load the dump into a fresh database and point `DATABASE_URL` at
  it. On boot the migration runner applies any newer migrations forward-only;
  it never drops or rewrites data (the only destructive path, `reset_schema`,
  is demo/test-only and never runs in production).
- **Never** run `/api/reset` in production — it is disabled there by design.
