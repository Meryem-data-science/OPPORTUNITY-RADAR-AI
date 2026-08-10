# Operations

The Python shell runs with `python -m services.collector.main`. The web
development server runs with `npm run dev` from `apps/web` after dependencies
are installed. No deployment, scheduled collection, or external integration is
currently operational.

## Local Gmail read-only probe

Create a Google Cloud project, enable the Gmail API, and create an OAuth 2.0
**Desktop app** client. Download its JSON file to a local ignored directory;
do not commit it. Configure the OAuth client, generated token, and generic
Gmail search query through the environment:

```bash
export GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
export GMAIL_TOKEN_PATH=.secrets/gmail-token.json
export GMAIL_ALERT_QUERY='label:Opportunity-Radar'
python -m services.collector.cli.gmail_probe --limit 5
# Or override the query for one invocation:
python -m services.collector.cli.gmail_probe \
  --query 'label:Opportunity-Radar' --limit 5
```

The first invocation opens the local user-authorization flow. It requests only
`https://www.googleapis.com/auth/gmail.readonly` and writes the resulting token
to `GMAIL_TOKEN_PATH`; subsequent invocations reuse it and refresh it when a
refresh token is available. The probe performs Gmail `list` and `get` calls
only. It neither creates labels nor modifies message/read state.

Output is one JSON summary per matching message: `message_id`, `sender`,
`subject`, UTC `received_at`, a snippet truncated to 200 characters, and the
plain-text and HTML body lengths. Full bodies, OAuth credentials, and tokens
are never printed. The limit must be between 1 and 100. `.secrets/`, common
credential JSON names, and Gmail token names are ignored by Git; keep both
OAuth files there or at another ignored location. No API endpoint exposes this
Gmail intake, and it does not persist messages or create opportunities.

The real-account smoke test is deliberately opt-in and needs matching local
mail. It fails clearly rather than fabricating a message when the query is
empty or has no results:

```bash
RUN_LIVE_GMAIL_TEST=1 pytest -q tests/live/test_gmail_readonly.py
```

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
- `GMAIL_OAUTH_CLIENT_SECRET_PATH`: local ignored Desktop OAuth client JSON.
- `GMAIL_TOKEN_PATH`: local ignored user OAuth token JSON.
- `GMAIL_ALERT_QUERY`: optional default query for `gmail_probe`.

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

## LinkedIn Job Alert radar validation

The pipeline source reads LinkedIn alert emails through Gmail, using only
`https://www.googleapis.com/auth/gmail.readonly`; it does not scrape LinkedIn
pages or apply to jobs. OAuth paths remain local environment configuration,
while the Gmail query (`newer_than:7d from:linkedin.com`) and limit (`50`) come
from `config/sources.yaml` and require no query environment override.

Prepare a fresh isolated SQLite database with the existing migrations and run
only the enabled, active LinkedIn source:

```bash
export GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
export GMAIL_TOKEN_PATH=.secrets/gmail-token.json

export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/linkedin-pipeline-validation.db

python -m services.collector.cli.migrate_configured --apply

python -m services.collector.cli.run_radar \
  --once --apply \
  --source linkedin_job_alert_email
```

The first run should report `items_created > 0`. Repeating the same command
should report `items_created=0` and `items_updated > 0`, with a stable row
count. `--source` is repeatable; omitted, all enabled and active sources run.
Unknown, disabled, and inactive requested sources are refused rather than
bypassing catalogue policy. No new migration is required for LinkedIn because
the existing source/opportunity/source-occurrence schema is reused.

The full real-account pipeline test always creates a temporary SQLite database
and is skipped unless explicitly enabled in a local WSL environment:

```bash
RUN_LIVE_LINKEDIN_PIPELINE_TEST=1 \
  pytest -q tests/live/test_linkedin_radar_pipeline.py -s
```

No real email address, OAuth credential, email content, or job data is stored
in the repository. Temporal scheduling and automatic applications are outside
this phase.

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
