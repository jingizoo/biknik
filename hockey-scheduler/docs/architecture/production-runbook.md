# Production runbook (#90)

Operational notes for deploying and running the hockey scheduler in
production. The app is stdlib-only Python; there is no build step.

## Health & readiness endpoints

Two public, non-sensitive endpoints support liveness and deployment gating.
Neither returns accounts, secrets, connection strings, or env values.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Liveness + dependencies: app up, DB reachable, migrations applied, delivery mode + worker status. |
| `GET /api/readiness` | Deployment gate: DB reachable, migrations current, (production) ≥1 active admin, cookie hardening active, (production) a durable store. Returns `ready: true/false` and a per-check breakdown. |

Point your load balancer / orchestrator liveness probe at `/api/health` and
its readiness probe at `/api/readiness`. In production `ready` is only `true`
once every check passes — notably, an active League Admin must exist and the
store must be durable (`persistent_store`, #143): a production deployment
with a missing or typo'd `DATABASE_URL` fails this check immediately rather
than silently running on in-memory storage that resets on every restart.
This also catches `DATABASE_URL` values that resolve to SQLite's `:memory:`
mode (or an empty path) — those produce a real `SqlStore`, but one that's
exactly as ephemeral as no `DATABASE_URL` at all; only a real Postgres
connection or an actual SQLite file counts as durable.

`GET /api/health` returns (illustrative — a running Postgres-backed instance;
`store`/`migrations.backend` reads `"memory"` with empty `applied`/`expected`
arrays for the in-memory demo store instead):

```json
{
  "status": "ok",
  "store": "postgres",
  "database_reachable": true,
  "migrations": {"backend": "postgres", "current": true,
                 "applied": ["001_initial", "002_sessions", "..."],
                 "expected": ["001_initial", "002_sessions", "..."]},
  "delivery": {"email_mode": "dry_run", "push_mode": "dry_run", "worker": {"enabled": false}}
}
```

## Required environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_MODE` | `production` disables demo conveniences (X-Demo-Role header, headerless-admin fallback, demo seeding, `/api/reset`) and enables `Secure` cookies. Anything else = demo. | `demo` |
| `DATABASE_URL` | `postgres://…` (or a real SQLite file path) → durable `SqlStore`. Unset, or a value that resolves to SQLite `:memory:`/empty (ephemeral, #143) → readiness fails in production. | in-memory |
| `HOST` / `PORT` | Bind address/port for `python3 -m hockey_scheduler.web` (also settable via `--host`/`--port`). Many container platforms inject `PORT` automatically. | `127.0.0.1` / `8000` |
| `BOOTSTRAP_ADMIN_USER` / `BOOTSTRAP_ADMIN_PASSWORD` | First League Admin, created on boot only when the store has no accounts (idempotent). | — |
| `DELIVERY_WORKER_ENABLED` / `_INTERVAL` / `_BATCH` | Opt-in background delivery worker (#79). | disabled / 30s / 50 |
| Email/push transport vars | Configure real SMTP / push; default is dry-run (nothing sent). | dry-run |
| `TRUST_PROXY_HEADERS` | `1` trusts `X-Forwarded-For` for anonymous-route rate limiting (#131). **Only set this if a real reverse proxy sits in front of the app and is configured to strip/overwrite any client-supplied `X-Forwarded-For` before appending its own** — otherwise any caller can spoof a new value per request and defeat rate limiting entirely. Unset when serving direct HTTP with no proxy. | unset (raw connecting IP) |
| `ALLOW_PRODUCTION_FACTORY_RESET` | `1`/`true`/`yes` makes the guarded production factory-reset workflow reachable (#256). Leave **unset** except during a deliberate, supervised wipe — see [Factory reset (Danger zone)](#factory-reset-danger-zone). Has no effect outside `APP_MODE=production`. | unset (disabled) |
| `DEPLOYMENT_ENV` | Free-form environment label recorded on the durable factory-reset audit event (e.g. `prod-eu`). Falls back to `APP_MODE` when unset. | `APP_MODE` |

Secrets are read from the environment only — they are never returned by any
API, logged, or persisted in plaintext (passwords are PBKDF2-hashed).

## Durable Postgres setup

The app talks to Postgres via `psycopg` (installed separately — it is not a
runtime dependency of the stdlib-only app itself, only needed when
`DATABASE_URL` points at Postgres): `pip install "psycopg[binary]>=3.1"`.

1. Create a dedicated database and role (placeholder credentials below —
   replace with real, secret values from your secrets manager, never
   committed anywhere):

   ```bash
   createuser hockey_app --pwprompt
   createdb hockey_scheduler --owner=hockey_app
   ```

2. Build the connection string and set it as `DATABASE_URL`:

   ```bash
   export DATABASE_URL="postgresql://hockey_app:<password>@<host>:5432/hockey_scheduler"
   ```

3. Start the app once with `APP_MODE=production` set. `SqlStore.__init__`
   applies every pending numbered migration automatically, forward-only —
   no separate migration step to run by hand (#75).
4. Confirm with `GET /api/health` — `store` reads `"postgres"` and
   `migrations.current` is `true`.

A raw filesystem path (or `sqlite:///path`) is also accepted for a
lighter-weight durable deployment; Postgres is the product target for
concurrent multi-instance production traffic.

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

## Smoke test after deploy

Run immediately after every deploy, before routing real traffic (or as an
automated deployment gate). All of these are safe, anonymous, read-only
checks — no credentials needed, nothing to clean up afterward.

```bash
BASE_URL="https://your-deployment"

# 1. Liveness + dependency snapshot.
curl -sf "$BASE_URL/api/health" | tee /tmp/health.json
# Expect: "status":"ok", "database_reachable":true, "migrations":{"current":true,...}

# 2. Deployment readiness gate — the authoritative go/no-go signal.
curl -sf "$BASE_URL/api/readiness" | tee /tmp/readiness.json
# Expect: "ready":true and every entry in "checks" has "ok":true

# 3. A real request through the full stack: routing, TLS, app, DB round-trip.
#    Anonymous and side-effect-free — safe to run against production.
curl -sf "$BASE_URL/api/public/schedule" | head -c 200
```

If `/api/readiness` returns `ready: false`, do not consider the deploy
complete — check `checks[].ok` for which gate failed (`database_reachable`,
`migrations_current`, `active_admin`, or `cookie_hardening`) and treat it as
a rollback trigger (below) rather than investigating live in production.

## Rollback checklist

Rollback has two independent halves — app code and data — because a bad
deploy might involve either, both, or neither:

- [ ] **App code**: redeploy the previous known-good version/image. This
      alone is often sufficient — migrations are additive and forward-only
      (#75), so an older app version keeps working against a newer schema
      unless the bad release also shipped a breaking migration.
- [ ] **Data**: only needed if the bad release corrupted or destroyed data.
      Restore the most recent backup taken before the deploy (see Backup &
      restore below). Prefer rolling back app code alone when possible —
      restoring from backup loses any writes made since that backup.
- [ ] Re-run the smoke test (above) against the rolled-back deployment
      before resuming traffic.
- [ ] Re-check `GET /api/readiness` returns `ready: true`.
- [ ] File an incident note: what broke, which check caught it (readiness
      gate vs. smoke test vs. user report), and what changed since the last
      known-good deploy.

## Data persistence & restart survival (#174)

Everything a client configures is written to the durable database
(`DATABASE_URL`) as it is created — there is no separate "save" step and no
in-memory-only state to lose. Restarting or redeploying the application
against the same database brings back the entire configuration intact:

- the facility hierarchy — Organization → League → Venue → Rink → Ice Slot,
  including the League↔owner and Venue↔league relationships (#173);
- the competition hierarchy — League → Season → Level → Division → Team →
  Player, plus Club → Team;
- officials, user accounts (with their PBKDF2-hashed credentials, so logins
  keep working after a restart), and the full setup **audit trail** with its
  original actor attribution.

Because the app process is stateless, a "restart" is simply a new process
pointed at the same `DATABASE_URL`. No migration re-run rewrites data: the
runner only applies *newer* migrations forward-only (see Backup & restore).

This guarantee is a **regression test**, not just a claim:
`backend/tests/test_production_restart.py` stands up an empty durable
database, configures the whole hierarchy plus a staff account through the
public API, drops the process, re-opens a fresh store against the same
database, and asserts every record, relationship, account, login, and audit
entry survived. It runs against a SQLite file locally and against PostgreSQL
in CI (`TEST_DATABASE_URL`), so the promise is verified on the real
production engine on every change.

To confirm persistence on a live deployment by hand: create a record in
Setup, restart the app process (the database is untouched), reload the app,
and confirm the record is still present. The health endpoint's
`persistent_store` readiness check (#143) independently guards against a
deployment accidentally running on ephemeral storage in the first place.

## Backup & restore

State lives entirely in the SQL database (`DATABASE_URL`) — the app process
itself is stateless, and sessions/tokens are rows, not files. A database
dump captures everything: accounts, sessions, schedule, rosters,
notifications, feed tokens, and preferences.

**Backup** (custom format — compressed, supports parallel restore):

```bash
pg_dump "$DATABASE_URL" --format=custom --file="hockey_scheduler_$(date +%Y%m%d%H%M%S).dump"
```

Run this on a schedule (e.g. a daily cron / managed-database automated
backup) and after any risky operation (a migration-bearing deploy, a bulk
import). Store dumps somewhere durable and access-controlled, separate from
the database host itself.

**Restore** (into a fresh, empty database — never restore over a live one
without first taking a fresh backup of its current state):

```bash
createdb hockey_scheduler_restored --owner=hockey_app
pg_restore --dbname="postgresql://hockey_app:<password>@<host>:5432/hockey_scheduler_restored" \
           --no-owner --jobs=4 hockey_scheduler_20260101120000.dump
```

Then point `DATABASE_URL` at the restored database and start the app — on
boot the migration runner applies any migrations newer than the dump
forward-only; it never drops or rewrites existing data (the only
destructive path, `reset_schema`, is demo/test-only and never runs in
production). Run migration verification (below) before routing traffic.

**Never** run `/api/reset` in production — it is disabled there by design.

### Backup/restore acceptance check (#174)

Taking a dump is only half the guarantee — a dump you cannot successfully
restore is worthless. After configuring a client (and periodically
thereafter), prove the round trip end to end with the checked-in acceptance
smoke, which backs a deployment up, restores it into a **fresh, empty**
database, starts the app against that restore, and verifies the record census
and server-derived onboarding status match the source:

```bash
# Verify a durable single-instance SQLite deployment restores faithfully:
python -m hockey_scheduler.acceptance.backup_restore --database-url ./client.db

# Or exercise the whole round trip on a throwaway sample (safe anywhere):
python -m hockey_scheduler.acceptance.backup_restore
```

It prints the restored record census plus migration/onboarding status and
exits non-zero on any divergence. The file-copy backup path is SQLite-only
(the pilot single-instance path); for PostgreSQL, back up with the
`pg_dump`/`pg_restore` commands above and then apply the same logical
acceptance — compare `GET /api/onboarding/status` and record counts on the
restored database against the source. `test_backup_restore_acceptance.py`
runs this check (and proves it *fails* on a divergent restore) in CI.

## Factory reset (Danger zone)

The **factory reset** (#256) is the supported way to return a production
installation to a clean, freshly-configured state — it deletes every business
and operational row (setup hierarchy, teams, players, registrations, venue
access, games, rosters, availability, officials, notifications, contacts,
feeds, imports) while **keeping the schema, migration ledger, and this
installation intact**. It is a row-level wipe, not `reset_schema`'s demo/test
DDL rebuild, and it is deliberately **not** `/api/demo/clear` (blocked in
production) nor ordinary per-record deletion.

It is disabled by default. To make it reachable at all, the deployment must
run in `APP_MODE=production` **and** set `ALLOW_PRODUCTION_FACTORY_RESET=true`
— the flag alone, in a non-production process, does nothing. Even then, every
one of these must hold before a single row is deleted:

- the caller is a League Admin with **both** `manage_setup` and `manage_users`
  (re-checked in the service, not just at the HTTP gate);
- a fresh **password re-authentication** succeeds;
- the operator has typed the exact phrase `DELETE ALL PRODUCTION DATA`;
- the operator has **acknowledged that a backup was taken** (the request must
  carry the JSON boolean `true`, not a truthy string);
- a short-lived, single-use **challenge token** from the preview call is
  present and still matches the actor and the previewed row counts;
- no other reset is in progress (an installation-wide lease-backed lock).

The wipe itself runs as **one transaction**: it locks the clearable tables,
re-validates the preview snapshot (rejecting as `preview_stale` if the data
changed since preview), deletes, re-inserts exactly the acting admin's account
plus a fresh installation-claim marker, and writes a durable **success** event
— so a failure at any point rolls the whole thing back with no partial
deletion. A separate append-only `factory_reset_events` row (actor, timestamp,
environment, pre-reset counts, result — no secrets or PII) survives the wipe as
the audit record. Every session and token is revoked; the operator is signed
out and must log back in as the preserved admin.

### Operator procedure

1. **Take a fresh backup first** and confirm it restores (the [Backup &
   restore](#backup--restore) round trip above). The reset is irreversible from
   inside the app — a backup is the only recovery path.
2. Set `ALLOW_PRODUCTION_FACTORY_RESET=true` on the production deployment and
   restart (the flag is read fresh per request, but restarting is the simplest
   way to set it deliberately). Optionally set `DEPLOYMENT_ENV` for the audit
   label.
3. Sign in as a League Admin, open **Pilot Readiness → Danger zone → Factory
   reset production data**, and follow the modal: review the row-count preview,
   acknowledge the backup, re-enter your password, type the confirmation
   phrase, and confirm. The button stays locked until all three inputs are
   satisfied, and a double-submit cannot fire a second wipe.
4. You are signed out on success. Sign back in as the same admin and
   reconfigure the installation (or run the one-time client-owned admin claim
   below if you preserved a fresh-install posture).
5. **Unset `ALLOW_PRODUCTION_FACTORY_RESET`** again and restart, so the Danger
   zone is not left reachable.

For an immediate, lower-risk production reset you can instead stand up a
**new, empty database**, validate it in staging, cut `DATABASE_URL` over to it,
and retain the original database until validation is complete — the factory
reset is the in-place equivalent for when a new database is not practical.

The reset engine (including a forced mid-transaction failure proving full
PostgreSQL rollback, and two two-connection concurrency regressions) is covered
by `tests/test_factory_reset.py`; the Danger-zone UI and its safety gating are
covered by the `factory-reset` browser journey.

## Migration verification

After any restore, or any deploy that shipped a new migration, confirm the
schema actually landed before trusting the deployment:

```bash
curl -sf "$BASE_URL/api/health" | python3 -c \
  'import json,sys; m=json.load(sys.stdin)["migrations"]; \
   print("current" if m["current"] else "STALE", m["applied"][-1] if m["applied"] else None)'
```

For a direct check against the database itself (useful when the app hasn't
been started yet against the restored data, e.g. mid-rollback):

```sql
SELECT version, applied_at FROM schema_migrations ORDER BY version DESC LIMIT 5;
```

Compare the latest `version` there against the migration files in
`hockey_scheduler/store/migrations/` — the restored database should be at or
behind the app version's expected set (the app will bring it current on its
own next boot; behind is fine, ahead is not and means a newer database was
restored against an older app version, which should not happen if app code
and data are rolled back together).

## One-time client-owned admin claim

For the normal client-owned setup path, do **not** configure
`BOOTSTRAP_ADMIN_PASSWORD`. Generate a separate high-entropy one-time code and
place it in the deployment secret manager:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```text
APP_MODE=production
DATABASE_URL=<durable Postgres or real SQLite file>
INITIAL_SETUP_CODE=<generated one-time code>
```

Deliver the code to the client through a secure channel. The client opens
`/setup`, enters that code, and chooses their own League Admin username and
password. The server compares the code in constant time, atomically creates one
admin, writes only non-secret claim metadata/audit, and establishes a normal
secure session. The setup code and password are never returned or persisted.

Operational checks:

```text
GET /api/bootstrap/status
```

- `claim_available: true` means the fresh durable installation is ready.
- `reason: already_claimed` means use the normal sign-in page.
- Other unavailable reasons require deployment configuration/readiness review.

After a successful claim, remove `INITIAL_SETUP_CODE` from deployment
configuration. Reusing the code cannot create another admin because the durable
single-row claim marker and existing-account check fail closed with HTTP 409.
The environment/CLI bootstrap remains an emergency path and consumes the same
atomic marker, so browser and operations bootstrap cannot race.
