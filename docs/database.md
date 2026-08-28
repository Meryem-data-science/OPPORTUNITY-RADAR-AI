# Database

## Operational database

Persistent local **SQLite** is the operational Phase 2 database. Connections
enable foreign-key enforcement, and collection, qualification, duplicate
decision, and merge writes are local SQLite operations.

Turso/libSQL remains only a Foundation/read-only connectivity and schema
experiment where applicable. Although runtime Foundation utilities retain
backend selection, remote Turso opportunity writes are disabled and are not an
operational Phase 2 path. Do not enable or recommend Turso writes.

## Migrations

Ordered SQL files are applied transactionally and recorded in
`schema_migrations`. A failed file is rolled back and its version is not
recorded. The migrations currently present are:

1. `0001_opportunity_foundation.sql`: `sources`, `opportunities`, and
   `opportunity_sources` plus indexes;
2. `0002_deduplication_decisions.sql`: the explicit duplicate-review registry;
3. `0003_deduplication_merges.sql`: merge history and source-movement history;
4. `0004_opportunity_qualifications.sql`: persistent versioned qualification;
5. `0005_source_runs.sql`: persistent history of attempted source runs.

Apply every pending migration to configured local SQLite explicitly:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

Neither `RadarAgent` nor the FastAPI application applies migrations implicitly.
`python -m services.collector.cli.db_health` performs a read-only `SELECT 1`, and
`python -m services.collector.cli.db_schema` performs read-only Foundation
schema verification.

## Opportunity persistence

Each collected source batch is one transaction: source metadata is upserted,
then opportunities and their `opportunity_sources` observations are created or
refreshed. Repeat observations use `(source_id, source_url)` to find the existing
source occurrence, preserve discovery/first-seen timestamps, and refresh
last-seen data. A later `NULL` does not erase an existing optional value.

That same-source behavior is not the whole deduplication design. Cross-source
duplicate handling is deliberately review-driven:

- `audit_duplicates` examines pairs read-only and classifies retained candidates;
- a review scan may stage eligible candidates in `deduplication_decisions`, and a
  reviewer explicitly records `CONFIRMED_DUPLICATE` or `NOT_DUPLICATE`;
- candidate classifications are not `AUTO_MERGE` decisions, and similarity alone
  never changes opportunity rows;
- merge requires a confirmed pair and an explicit canonical opportunity;
- one SQLite transaction moves the merged row's `opportunity_sources` to the
  canonical opportunity, fills permitted missing canonical fields, records the
  before/after state and moves, and marks the other opportunity inactive with
  status `merged_duplicate` rather than deleting it;
- rollback is transactional and restores recorded state/source ownership only
  when LIFO and data-drift safety checks pass.

The retained tombstone, decision, merge, and movement rows make applied changes
auditable and reversible instead of destructive.

## Qualification persistence

Migration `0004` stores one current derived result per eligible opportunity,
including explanatory signals, classifier version, a SHA-256 input fingerprint,
and timestamps. The current classifier version is `qualification-rules-v1`.
Reconciliation leaves a row unchanged when both its input fingerprint and
classifier version match; otherwise it inserts or updates atomically. Merged,
inactive duplicates are excluded. `RadarAgent` runs this global reconciliation
once after its source loop; the standalone CLI remains available for audit or
maintenance.

Closed taxonomy values are:

- **Qualification:** `CORE_TARGET`, `ADJACENT_TARGET`, `OUT_OF_SCOPE`,
  `UNCERTAIN`.
- **Primary domain:** `DATA_ENGINEERING`, `DATA_SCIENCE`,
  `MACHINE_LEARNING_AI`, `GENAI_LLM`, `MLOPS_ML_PLATFORM`, `BI_ANALYTICS`,
  `DATA_QUALITY_GOVERNANCE`, `OTHER_DATA_AI`, `NON_TARGET`, `UNKNOWN`.
- **Opportunity type:** `PFE`, `INTERNSHIP`, `GRADUATE`, `APPRENTICESHIP`,
  `JOB`, `UNKNOWN`.
- **Employment type:** `FULL_TIME`, `PART_TIME`, `CONTRACT`, `TEMPORARY`,
  `UNKNOWN`.
- **Listing quality:** `NORMAL_LISTING`, `POSSIBLE_NON_JOB_PAGE`,
  `INSUFFICIENT_CONTENT`.

These classifications do not implement personalized matching or ranking.

## Source run history

Migration `0005` stores one row per attempted execution of a source by
`RadarAgent`. `started_at` is captured before collection begins and
`finished_at` when the attempt ends, so a row describes a real, closed window.

The status taxonomy is deliberately the closed pair the history needs:

- **`SUCCESS`**: collection and opportunity persistence both completed;
- **`FAILED`**: the attempt raised; `error_type` names the exception and
  `error_message` carries a redacted, diagnosable message.

There is no in-flight status. A run is written once, when it completes, so an
interrupted process cannot leave a permanently unfinished row that nothing in
this phase would reconcile. The cost of that choice is that a crash between
start and completion leaves no row at all.

`NULL` means "this run did not know it" and is never replaced by a zero, which
would read later as a real observation:

- `items_found` is the number of candidates the collector actually returned. It
  is `NULL` when collection itself failed, and a real count when collection
  succeeded and persistence then failed.
- `new_items` is the number of opportunities actually created. It is only known
  on a successful run.
- `relevant_items` is always `NULL` today: qualification runs once globally
  after the whole source loop, so per-source relevance is genuinely unknown at
  the moment a run is recorded.
- `pages_checked`, `http_status`, and `parser_version` are always `NULL` today:
  no current collector paginates, exposes a reliable transport status to the
  agent, or carries a versioned parser. A collector may opt in by exposing
  `run_metrics()`; only known, well-typed keys are stored.

`error_message` is redacted before it is stored. Credential assignments,
authorization schemes, URL user-info, JWTs, and long opaque blobs are removed
and the message is length-bounded. No token, credential, or OAuth secret is
persisted or logged.

### `sources.last_run_at`

`sources.last_run_at` is the `finished_at` of that source's most recent
**completed attempt**, successful or failed. It is not "last successful run" and
not "last time data changed". Both writes share one transaction, so the stamp
can never disagree with the run that produced it. A source that has never been
attempted keeps `NULL`.

Recording an attempt registers a never-persisted source so the run's foreign key
resolves; on a source that already exists it updates `last_run_at` only and
leaves every other column alone.

### Transaction boundaries

Each attempt is recorded in its own transaction, separate from the opportunity
batch it describes. A later source failing never rolls back an earlier
committed one, and a source that fails still records the fact that it failed.
Run history observes collection and never governs it: if recording itself fails,
the failure is logged and the source keeps the outcome it actually had.

### Not yet implemented

`source_runs` is history only. Anomaly detection is **not** implemented: nothing
scores runs, nothing detects a source returning zero items across consecutive
runs, nothing raises an alert, and there is no Source Health page. This table is
the evidence a later phase would need, not that phase.

## Read-only application API

FastAPI exposes `GET /api/opportunities` with a bounded `limit` (1–100). It
returns the count and summary fields for visible, active rows, including
`original_url` and `description_length`; it does not expose the stored full
description. The service opens existing configured storage read-only for the
request and does not collect data or alter schema.
