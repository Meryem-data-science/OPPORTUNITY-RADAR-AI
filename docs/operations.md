# Operations

The Python shell runs with `python -m services.collector.main`. The web
development server runs with `npm run dev` from `apps/web` after dependencies
are installed. No deployment, scheduled collection, or external integration is
currently operational.

## Local read-only opportunity API

Start the Phase 1 FastAPI service against the existing configured SQLite
database (the API does not apply migrations or collect/write data):

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m uvicorn services.api.main:app \
  --host 127.0.0.1 \
  --port 8000
```

Read up to five most recently observed visible, active opportunities:

```bash
curl "http://127.0.0.1:8000/api/opportunities?limit=5"
```

The response contains summary fields and a total count, but never the full
description. `original_url` selects the stored application URL first, then the
stored source URL, then the stored canonical URL. This is a local Phase 1
service and is not documented as a production deployment.

## Scale AI source dry-run

After installing Python dependencies, run the first source collector explicitly
in read-only dry-run mode:

```bash
python -m services.collector.cli.collect_source \
  --source scale_ai_greenhouse --limit 3 --dry-run
```

It queries the unauthenticated public Greenhouse board using the `scaleai`
token from `config/sources.yaml`, logs counts, and prints candidate summaries
without descriptions or credentials. `--limit` controls only returned/displayed
items. This dry-run command never writes to SQLite or Turso.

## Bounded source persistence

Phase 1 uses a persistent, configurable local SQLite database. Prepare it once,
then persist and list real collected offers:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
python -m services.collector.cli.persist_source \
  --source scale_ai_greenhouse --limit 1 --apply
python -m services.collector.cli.list_opportunities --limit 5
```

`--limit 1` means at most one collected candidate is written. The SQLite batch
transaction includes the source upsert, opportunity writes,
and source-occurrence writes and rolls back on any error. The listing command
is read-only, accepts limits from 1 to 100, and omits full descriptions. Remote
Turso opportunity writes are explicitly disabled in Phase 1 after failed live
transport validations; Turso read-only health checks remain available.

The real Greenhouse-to-SQLite persistence test is opt-in:

```bash
RUN_LIVE_SQLITE_OPPORTUNITY_TEST=1 \
  pytest -q tests/live/test_sqlite_greenhouse_opportunity_persistence.py -s
```

The real-network smoke test is opt-in and skipped otherwise:

```bash
RUN_LIVE_SOURCE_TEST=1 pytest -q tests/live/test_scale_ai_greenhouse.py -s
```

## Structured logging

The Python service writes one JSON object per event. Every record includes an
ISO 8601 UTC `timestamp`, `level`, producing `module`, machine-readable `event`,
human-readable `message`, and optional `source_id` and `opportunity_id` fields.
Missing optional fields are emitted as `null`.

Common credentials in structured context or obvious message patterns—such as
tokens, passwords, API keys, database URLs, secrets, and Authorization Bearer
values—are replaced with `[REDACTED]`. This is a defensive baseline rather than
a guarantee that arbitrary secret formats can be detected.

Future collectors and service modules can reuse this local standard-library
logging configuration. No external or cloud logging service is connected.

## Runtime configuration

The service reads configuration from the process environment on each explicit
`load_settings()` call. It does not load `.env` files or create database files
while loading settings. Supported variables are:

- `OPPORTUNITY_RADAR_ENV`: `development` (default), `test`, or `production`.
- `DATABASE_BACKEND`: `sqlite` (default) or `turso`.
- `SQLITE_DATABASE_PATH`: SQLite path; defaults to
  `./data/opportunity-radar.db` only in development and is required for SQLite
  in other environments.
- `TURSO_DATABASE_URL`: required when the Turso backend is selected.
- `TURSO_AUTH_TOKEN`: secret required when the Turso backend is selected and
  excluded from the settings representation.

`.env.example` documents names and local defaults but contains no credential.
Secrets must be supplied through the process environment. Turso connectivity
has been validated manually; credential contents are not otherwise validated.

## Database health and live test

`python -m services.collector.cli.db_health` checks the configured SQLite or
Turso backend with a read-only `SELECT 1` and emits a structured success or
failure event. Logs include only the backend identifier, never the Turso URL or
authentication token.

The live Turso test is disabled by default. It runs only when all real Turso
settings are present and `RUN_TURSO_LIVE_TEST=1` is explicitly set:

```bash
RUN_TURSO_LIVE_TEST=1 DATABASE_BACKEND=turso pytest tests/live
```

No remote migration or write is performed by this test.

## Web health

The Next.js server reads `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` directly
from its process environment. These values must never be committed or given a
`NEXT_PUBLIC_` prefix. Start the application and manually check the read-only
health endpoint with real credentials:

```bash
cd apps/web
TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... npm run dev
curl http://localhost:3000/api/health
```

`GET /api/health` returns HTTP 200 after connectivity, Foundation schema, and
migration checks pass; otherwise it returns HTTP 503 without driver errors or
credentials. `/health` renders the same real result. Web-to-Turso validation
must be performed manually outside Codex.

## Configured migrations

Apply pending migrations to the configured SQLite backend with:

```bash
python -m services.collector.cli.migrate_configured --apply
```

For Turso, the same command refuses to connect unless both `--apply` and
`RUN_TURSO_LIVE_MIGRATION=1` are present. Migration logs contain only the
backend, applied count, and error type—not the Turso URL or token.

`python -m services.collector.cli.db_schema` performs a read-only check for
`schema_migrations`, `sources`, `opportunities`, and `opportunity_sources`.

The write-enabled live migration test is disabled by default. Manual WSL
validation requires real settings and `RUN_TURSO_LIVE_MIGRATION_TEST=1`:

```bash
RUN_TURSO_LIVE_MIGRATION_TEST=1 DATABASE_BACKEND=turso \
  pytest tests/live/test_turso_migrations.py
```

Codex Cloud does not run this test or apply migrations to Turso.
