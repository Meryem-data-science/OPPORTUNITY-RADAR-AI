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
`RadarAgent`. The row is written **before** collection begins and the same row
is closed when the attempt ends, so one attempt is always exactly one row and an
attempt that started always leaves proof that it started.

The status taxonomy is the closed set that lifecycle needs:

| status | `finished_at` | `error_type` | `error_message` |
| --- | --- | --- | --- |
| `RUNNING` | `NULL` | `NULL` | `NULL` |
| `SUCCESS` | set | `NULL` | `NULL` |
| `FAILED` | set | set | set, or `NULL` if redaction empties it |

- **`RUNNING`**: the attempt was persisted and began; no ending has been
  recorded yet.
- **`SUCCESS`**: collection and opportunity persistence both completed.
- **`FAILED`**: the attempt raised; `error_type` names the exception and
  `error_message` carries a redacted, diagnosable message.

The schema enforces exactly those three shapes, and the persistence layer allows
only `RUNNING → SUCCESS` and `RUNNING → FAILED`. A row that already reached a
terminal state can never be finalized again.

A crash or an interruption therefore leaves a `RUNNING` row rather than no row
at all — that surviving row is the evidence, and `finished_at` is never invented
for an attempt that did not end. Deciding when a `RUNNING` row has been left
behind too long is a separate concern and is deliberately not implemented here.

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
not "last time data changed".

Starting a run does **not** touch it: an attempt that has only started has not
finished, and an interrupted `RUNNING` row must never look like a completed run.
Only finalization stamps it, in the same transaction that closes the run, so the
stamp can never disagree with the run that produced it. A source that has never
completed an attempt keeps `NULL`.

Starting a run registers a never-persisted source so the run's foreign key
resolves; a source that already exists is left entirely untouched.

### Transaction boundaries

Starting and finalizing are each one transaction, both separate from the
opportunity batch they describe. A later source failing never rolls back an
earlier committed one, and a source that fails still records the fact that it
failed.

The two ends are treated differently on purpose:

- if the **start** cannot be persisted, the source is not collected at all —
  running it would produce work that could never be audited. It is reported as a
  failed source for that run and the remaining sources continue;
- if the **finalization** fails after the row exists, the failure is logged and
  the row is left `RUNNING`. It is never deleted, and never rewritten into a
  state the attempt did not reach, so the unfinalized attempt stays visible.

### Deleting a source

`source_runs.source_id` uses `ON DELETE RESTRICT`, like the other audit tables.
A source that carries run history cannot be deleted, so its audit trail cannot
be discarded as a side effect of removing the source.

## Source health

Source health is **derived at read time** from `sources` and `source_runs`. It
adds no migration, no `anomalies` table, and no persisted health column: nothing
about it is stored, so the answer cannot drift from the runs it describes.
`services/collector/database/source_health.py` holds the read model; nothing in
it writes, and reading health for a source the database has never seen creates
no row for it.

Each entry carries `source_id`, `enabled`, `last_run_at`, the latest run's
`status`, `items_found`, `new_items`, `relevant_items`, `error_type` and
`error_message`, plus the derived `zero_result_streak`, `anomaly_code` and
`anomaly_message`.

`last_run_at` is the `started_at` of the most recent run, so it always describes
the very run whose status and metrics are shown beside it. For a source that has
never run it is `NULL`: `sources.last_run_at` is deliberately not substituted,
because a stamp with no run behind it would claim an execution the model cannot
show.

A source is listed when the validated `config/sources.yaml` catalogue configures
it or when the database already holds it, once either way. The configured
`enabled` flag wins over the persisted one, because a source row is registered
at that source's first run and is never rewritten afterwards.

### The consecutive-zero rule

Exactly one anomaly is detected: **three consecutive successful runs that each
found exactly zero items**, counted from the newest run backwards.

- the constant is `ZERO_RESULT_STREAK_THRESHOLD = 3`;
- `items_found IS NULL` means *unknown* and is never read as a zero;
- a `FAILED` run is not a successful zero;
- a `RUNNING` run has not finished and is not a completed zero;
- a `SUCCESS` with `items_found > 0` ends the streak;
- a run whose `items_found` is unknown ends the streak;
- any run that is not a zero-result success ends the streak, so
  `zero_result_streak` is 0 whenever the latest run failed, is still running, or
  did not measure `items_found`.

Past the threshold the entry carries `anomaly_code = "ZERO_RESULTS_STREAK"` and
a deterministic `anomaly_message` built from a fixed template and the observed
count. No language model, no heuristic phrasing, no severity beyond that count.

This distinguishes the two situations the run history would otherwise conflate:
a source that genuinely has nothing new (one or two zero runs, no anomaly) and a
collector or parser that may have broken (three or more, anomaly).

### Deliberately not a state machine

The truth about a source stays the real `source_runs.status` of its latest run —
`RUNNING`, `SUCCESS` or `FAILED`, or nothing at all. No `HEALTHY`/`DEGRADED`/
`CRITICAL` taxonomy is introduced, persisted, or derived. The anomaly is exposed
beside the status, never folded into it.

`relevant_items` stays `NULL` for every current collector and is reported as
unknown rather than invented.

### Not yet implemented

No alert is raised from any of this: nothing emails, notifies, retries, reruns,
or repairs a source, nothing is scheduled, and no `RUNNING` row is judged stale
against any age threshold — no such threshold is defined. Source health is a
read model and a page, not an alerting system.

## Read-only application API

FastAPI exposes `GET /api/opportunities` with a bounded `limit` (1–100). It
returns the count and summary fields for visible, active rows, including
`original_url` and `description_length`; it does not expose the stored full
description. The service opens existing configured storage read-only for the
request and does not collect data or alter schema.

`GET /api/source-health` takes no parameter and returns
`{"items": [...], "returned": n}` with one entry per known source, ordered by
`source_id`. Unknown values are serialized as `null` and never as zero. The
service opens SQLite in `query_only` mode; it writes nothing, migrates nothing,
and contacts nothing external.
