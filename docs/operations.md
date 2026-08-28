# Operations

Phase 2 is an explicitly invoked local workflow. There is no scheduled or
continuous production execution and no automatic application behavior.

## Prepare local SQLite and Gmail

From the repository root:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db

export GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
export GMAIL_TOKEN_PATH=.secrets/gmail-token.json

python -m services.collector.cli.migrate_configured --apply
```

Create a Google Cloud OAuth 2.0 **Desktop app** client after enabling the Gmail
API. Put the downloaded client JSON and generated token in `.secrets/` (or
another ignored local directory); never commit credentials. The first Gmail use
starts local user authorization and requests exactly
`https://www.googleapis.com/auth/gmail.readonly`. Existing tokens with missing
or broader scopes are rejected. The client performs Gmail message `list` and
`get` operations only and does not change labels, messages, or read state.

The optional generic read-only diagnostic is:

```bash
export GMAIL_ALERT_QUERY='label:Opportunity-Radar'
python -m services.collector.cli.gmail_probe --limit 5
```

Its summaries omit full bodies and credentials. The operational LinkedIn source
instead takes its fixed query and message bound from `config/sources.yaml`.

## Run the radar

Run all enabled, active configured sources and then global qualification:

```bash
python -m services.collector.cli.run_radar --once --apply
```

`RadarAgent` handles each collection/persistence batch independently. A failed
source is reported but does not undo earlier successful source commits. Once the
complete source loop finishes, the agent runs exactly one qualification
reconciliation over the operational database. Qualification failure is reported
separately and makes the overall command fail without rewriting source results.
Neither this command nor any collector applies migrations implicitly.

For single-source debugging:

```bash
python -m services.collector.cli.run_radar \
  --once --apply --source linkedin_job_alert_email
```

`--source` is repeatable. Unknown, disabled, or inactive requested sources are
refused. The manual `collect_source`, `persist_source`, and
`list_opportunities` CLIs remain useful for bounded diagnostics, but
`run_radar` is the normal orchestration entry point.

The LinkedIn collector reads only alert email content. It never uses a LinkedIn
authenticated session or opens job pages. Gmail bodies, snippets, message and
thread IDs, and tracking data do not become persisted opportunity data.

## Qualification checks

Qualification normally runs automatically with `RadarAgent`. The standalone
write command remains explicit and local-SQLite-only:

```bash
python -m services.collector.cli.persist_qualifications \
  --database .data/opportunity-radar.db --apply
```

The read-only audit can compare persisted classifications with current rules:

```bash
python -m services.collector.cli.audit_qualification \
  --database .data/opportunity-radar.db --format human
```

The classifier is deterministic `qualification-rules-v1`; unchanged fingerprints
and versions are not rewritten. Geography is accepted as metadata and is not an
exclusion rule. Results are categorical qualification, not user-specific
matching or ranking.

## Cross-source duplicate review

Audit an existing SQLite file without changing it:

```bash
python -m services.collector.cli.audit_duplicates \
  --database .data/opportunity-radar.db --format human
```

The report's candidate classes are preliminary heuristics and never
`AUTO_MERGE`. Review workflow commands are available under:

```bash
python -m services.collector.cli.review_duplicates --help
python -m services.collector.cli.merge_duplicates --help
```

`review_duplicates scan` is dry-run unless explicitly given `--apply`; a human
must persist a `CONFIRMED_DUPLICATE` decision before a physical merge can pass
preflight. The merge command also requires an explicit canonical opportunity
and is dry-run unless explicitly authorized. Applied merges move source
occurrences transactionally, preserve a `merged_duplicate` tombstone and audit
history, and can be rolled back only when LIFO and drift checks consider that
safe. Similarity output alone never merges records.

## Run the API and web application

The API reads an existing migrated SQLite database. It does not run collection,
qualification, or migrations:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

Inspect a bounded response:

```bash
curl "http://127.0.0.1:8000/api/opportunities?limit=5"
```

`GET /api/opportunities` returns visible, active opportunity summaries and an
`original_url`, not the stored full description. It accepts limits from 1 to
100.

Inspect source health, which takes no parameter:

```bash
curl "http://127.0.0.1:8000/api/source-health"
```

`GET /api/source-health` returns one entry per known source — those configured
in `config/sources.yaml` and those already persisted — with the status and
metrics of each source's most recent run, plus `zero_result_streak`,
`anomaly_code` and `anomaly_message`. Unknown values come back as `null`, never
as zero, and a source that has never run comes back with a `null` status rather
than an invented one.

The read is strictly read-only: the database is opened `mode=ro` and set
`query_only`, so pointing `SQLITE_DATABASE_PATH` at a missing or not-yet-migrated
file answers `503` and leaves that path missing instead of creating an empty
database there. Source health reads the operational SQLite database only; with
`DATABASE_BACKEND=turso` it answers the same `503` without contacting anything.

In another terminal, start Next.js:

```bash
cd apps/web
OPPORTUNITY_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

`OPPORTUNITY_API_BASE_URL` is server-side and defaults to
`http://127.0.0.1:8000`. The home page fetches the API without caching, displays
the real results, and uses “Voir l’offre originale” links to open their
`original_url`. This is a local integration, not a production deployment.

`/source-health` renders the source health entries as a table of Source,
Activée, Dernière exécution, Statut, Éléments trouvés, Nouveaux éléments,
Éléments pertinents and Erreurs. It displays the backend's own verdict: the
streak and the anomaly are decided by the API, and the page never recomputes
them. A `FAILED` run shows its `error_type` and its already-redacted
`error_message`; a repeated-zero anomaly shows the deterministic message the
backend produced. Reading the page changes nothing and alerts no one — no email,
no push, no scheduled check.

The separate Next.js `/health` and `GET /api/health` surfaces retain the earlier
read-only Turso/Foundation experiment. They are not the opportunity-data path
and do not imply that remote Turso writes are supported.

## Configuration and database boundary

Important process variables are:

- `OPPORTUNITY_RADAR_ENV`: `development` (default), `test`, or `production`;
- `DATABASE_BACKEND`: use `sqlite` for the Phase 2 operational workflow;
- `SQLITE_DATABASE_PATH`: operational SQLite path (the development default is
  `./data/opportunity-radar.db`);
- `GMAIL_OAUTH_CLIENT_SECRET_PATH`: ignored local Desktop OAuth client JSON;
- `GMAIL_TOKEN_PATH`: ignored local user OAuth token JSON;
- `GMAIL_ALERT_QUERY`: optional query for the generic `gmail_probe` only;
- `OPPORTUNITY_API_BASE_URL`: server-side Next.js URL for FastAPI.

Settings loading does not read `.env` files. Secrets must come from the process
environment and local ignored files, never source control. Structured Python
logs redact common credential-shaped values, but callers must still avoid
putting sensitive values into log messages.

Persistent SQLite is operational. Turso/libSQL is retained only for read-only
Foundation experiments where applicable; remote opportunity, qualification,
duplicate-decision, and merge writes must not be enabled or recommended.

## Validation commands

```bash
pytest

cd apps/web
npm test
npm run lint
npm run build
```

Live Gmail and source tests are opt-in because they require local credentials or
network state. The repository test suite skips them unless their documented
`RUN_LIVE_*` flags are deliberately supplied.

The final disposable Phase 2 validation on 2026-08-12 exercised three real
sources, 373 opportunities and qualifications, 373 classifier comparisons with
zero mismatches, 37,711 cross-source pairs with zero retained candidates in that
dataset, and the FastAPI-to-Next.js display/original links. Those values are a
dated snapshot, not stable production counts because upstream sources change.
