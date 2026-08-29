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
5. `0005_source_runs.sql`: persistent history of attempted source runs;
6. `0006_user_profile_foundation.sql`: the `users` identity root and the
   `profiles` Digital Twin root;
7. `0007_profile_facts.sql`: the validated `profile_facts` and their
   `profile_fact_provenance` evidence.

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

## User and profile root

Migration `0006` adds the two roots Phase 3 builds on, and nothing else.

`users` is the identity and ownership root:

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `email` | `NOT NULL`, `UNIQUE COLLATE NOCASE`, and a `CHECK` that refuses an empty or untrimmed value |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`profiles` is the stable Digital Twin root:

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `user_id` | `NOT NULL UNIQUE`, `FOREIGN KEY → users(id) ON DELETE CASCADE` |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`UNIQUE(user_id)` is what makes **one user own at most one profile** a database
rule rather than an application convention, and the cascade expresses that a
profile exists only for as long as its owner does. The migration creates the
two tables and inserts no row: an identity is created by an explicit local
command, never by applying a migration.

`profiles` deliberately carries no factual column, and `0007` adds none. A
headline, a location, an education level, mobility, or availability are facts
with a provenance, and provenance is the subject of `profile_facts` below.
Putting them here would make the root itself the place where facts are
overwritten without any record of where they came from.

`services/digital_twin/repository.py` owns the two operations on these tables.
`ensure_user_profile` normalizes the address, then creates or returns the pair
inside one explicit transaction, so a first call creates one user and one
profile and any later call with the same address — in any case, with any
surrounding spaces — returns the same ids and creates nothing. Any failure
rolls the whole transaction back, so a user is never left without the profile it
owns. `get_user_profile_by_email` reads the pair back and answers an explicit
absence for an unknown address; a user row without its profile is refused as a
broken invariant rather than answered with an invented profile.

Address normalization lives in `services/digital_twin/identity.py`: trim, then
lowercase the whole address, with conservative rejection of a manifestly
malformed value rather than an RFC 5322 validator. `COLLATE NOCASE` on the
column keeps the same guarantee at the database level.

Initialise or read the local profile without putting an address in the shell
history:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.digital_twin.cli init-profile   # prompts, no echo
python -m services.digital_twin.cli show-profile
```

The command prints `user_id` and `profile_id` only, never the address, and
never logs it. A non-SQLite backend is refused locally, before any connection.

Not implemented by that slice: `cv_versions`, skills, preferences,
eligibility, matching, scoring, and any profile HTTP API. `profile_facts`
arrives with `0007` below.

The Phase 3.2A CV parser adds no table and no migration. It reads a local PDF
and returns a result in memory; nothing it extracts is written to the database,
and no `profile_facts` row exists yet. See
[operations.md](operations.md#cv-parser-phase-32a).

The Phase 3.2B candidate extractor adds no table and no migration either. It
turns that in-memory parse into unverified candidates that also stay in memory:
no candidate table, no `cv_versions`, no SQLite write, and no row in `users`,
`profiles` or `profile_facts`. Migration `0007` below creates the fact tables,
but nothing in the CV packages writes to them: no candidate is imported, and
that mapping is Phase 3.3B. See
[operations.md](operations.md#cv-candidate-extraction-phase-32b).

## Profile facts and their provenance

Migration `0007` adds the persistent anti-hallucination foundation, and nothing
else: two tables, no column on `profiles`, no seeded row.

### `profile_facts`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_type` | `NOT NULL`, non-empty and already trimmed |
| `value` | `NOT NULL`, non-blank; the source's own wording, kept verbatim |
| `normalized_value` | nullable; set only where a technical normal form is unambiguous, and non-blank when present |
| `status` | `NOT NULL`, one of `PROPOSED`, `ACCEPTED`, `CORRECTED`, `REJECTED` |
| `replaced_by_fact_id` | nullable, `FOREIGN KEY → profile_facts(id) ON DELETE RESTRICT` |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |
| `decided_at` | nullable: when a human decided |

A table `CHECK` makes the four shapes of a fact a database rule rather than an
application convention:

| status | `decided_at` | `replaced_by_fact_id` |
| --- | --- | --- |
| `PROPOSED` | `NULL` | `NULL` |
| `ACCEPTED` | set | `NULL` |
| `REJECTED` | set | `NULL` |
| `CORRECTED` | set | set |

A proposal has been decided by nobody, so it carries no decision date; a
decision is always dated; and only a corrected fact carries a replacement — and
a corrected fact always carries one, which is what keeps the previous value
reachable instead of lost. A fact cannot replace itself, `decided_at` cannot
precede `created_at`, and a partial unique index on `replaced_by_fact_id` makes
a replacement replace exactly one fact, so a correction chain stays a chain.

There is deliberately **no `verified` column**, and no column named anything
like it. "Verified" has exactly one definition — `status = 'ACCEPTED'` — because
two places to say the same thing is one place too many, and the one that drifts
is the one that gets believed. `ProfileFact.is_verified` is a computed Python
property over the status, never a stored second answer. There is no confidence,
no score, no proficiency and no skill level anywhere in the slice either.

No factual column is added to `profiles`. A fact belongs beside the evidence
that produced it, never in the root that owns it, where it could be overwritten
without a trace.

### `profile_fact_provenance`

Evidence, stored separately from the decision it supports. Every fact the
repository creates gets at least one provenance row in the same transaction: a
fact with no evidence would be an assertion nobody could check.

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `fact_id` | `NOT NULL`, `FOREIGN KEY → profile_facts(id) ON DELETE CASCADE` |
| `source_type` | `NOT NULL`, one of `CV`, `GITHUB`, `USER_INPUT`, `OTHER_ACCEPTED_EVIDENCE` |
| `provenance_key` | `NOT NULL`, non-empty, `UNIQUE (fact_id, provenance_key)` |
| `source_locator` | nullable |
| `cv_sha256` | nullable; exactly 64 lowercase hex characters when present |
| `parser_version`, `extractor_version` | nullable |
| `candidate_fingerprint` | nullable; hexadecimal when present |
| `rule_id` | nullable |
| `page_numbers` | nullable; canonical JSON array of ascending page numbers written without spaces, `'[1,2]'` |
| `section_type`, `section_index` | nullable |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

Those columns are exactly what one Phase 3.2B `ExtractedCandidate` already
carries — `cv_sha256`, `parser_version`, `extractor_version`, `fingerprint`,
`rule_id`, the pages, the section and its index — so a candidate's provenance
can later be stored field for field, without loss. **That import is not
implemented**: nothing maps an `ExtractedCandidate` to a `profile_facts` row,
and building it is Phase 3.3B.

`UNIQUE (fact_id, provenance_key)` is what stops the same proof from being
recorded twice for one fact, so a duplicate can never look like corroboration.
The key may be given explicitly; left unset, the repository derives it as a pure
function of the evidence fields, with no clock and no counter in it, so the same
proof always resolves to the same key. Evidence accumulates and is never
rewritten: a second, genuinely different proof is added beside the first.

A provenance row never invents what its source did not carry. An absent page,
rule or version stays `NULL` and is never defaulted to a plausible value, and
`NULL` means "this source did not carry it", never zero.

### The validation cycle

`services/digital_twin/facts/repository.py` owns every operation, in the style
of the user/profile root: plain SQLite, no ORM, and one explicit
`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around each multi-step write, so a
failure rolls the whole operation back.

| from | to | how |
| --- | --- | --- |
| — | `PROPOSED` | `propose_profile_fact`, with its first provenance |
| `PROPOSED` | `ACCEPTED` | `accept_profile_fact` |
| `PROPOSED` | `REJECTED` | `reject_profile_fact` |
| `PROPOSED` | `CORRECTED` | `correct_profile_fact` |
| `ACCEPTED` | `REJECTED` | `reject_profile_fact` |
| `ACCEPTED` | `CORRECTED` | `correct_profile_fact` |

`REJECTED` and `CORRECTED` are terminal. Accepting an already-accepted fact and
rejecting an already-rejected one are idempotent no-ops that leave the original
`decided_at` alone, so re-clicking never restamps the moment the real decision
was taken. Every other move — accepting a rejection, correcting a correction —
raises `InvalidFactTransitionError` rather than being silently absorbed.

Every mutation is scoped by `profile_id` as well as by `fact_id`. A fact id from
another profile is reported as missing rather than mutated, so no cross-profile
write is possible even with a valid id.

### Correction never overwrites

A correction is one transaction that does five things and commits or rolls back
as a whole:

1. the new value is written as a **new** `profile_facts` row, `ACCEPTED`,
   because a person typing the right value is an explicit human validation and
   not another proposal to review;
2. that new fact receives a `USER_INPUT` provenance;
3. the previous fact becomes `CORRECTED`;
4. its `replaced_by_fact_id` points at the replacement;
5. its own `value` is left exactly as it was.

There is no `UPDATE profile_facts SET value = ...` anywhere in the package, and
a test asserts that on the SQL the module actually executes. Successive
corrections therefore build a chain in which every value ever proposed stays
readable, and only the last link is `ACCEPTED`.

### The verified reading

`list_verified_profile_facts(connection, profile_id)` is the one reading later
phases are meant to build on. It filters on `status = 'ACCEPTED'` in SQL, so a
`PROPOSED`, `REJECTED` or `CORRECTED` fact cannot leak into it and a caller
cannot forget the filter. `list_profile_facts` is the separate review and audit
reading that deliberately shows every status.

### Not implemented by this slice

The Phase 3.2B candidates are **not** imported: no candidate-to-fact mapping,
no CV review command, no interface, no HTTP endpoint and no authentication
exist, so the only way to record a fact today is to call the repository from
Python. Nothing here normalizes an institution, an employer, a date, a canonical
role or a skill alias, and no skill, `user_skills`, alias, preference,
eligibility or matching table exists — that is Phase 3.4 and beyond, and it has
not started. No Master CV, cover letter, application, form or CV adaptation is
generated from these facts, and no future application flow may write to them
directly.

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
the very run whose status and metrics are shown beside it, and a run that is
still `RUNNING` shows its own start rather than an older run's end. For a source
that has never run it is `NULL`.

This is deliberately **not** the column `sources.last_run_at`, which keeps its
own meaning from the run-history slice: the `finished_at` of the most recent
*completed* attempt. That column is not substituted here — a stamp with no run
behind it would claim an execution the read model cannot show, and it would
disagree with the status displayed next to it whenever the latest attempt has
not finished.

A source is listed when the validated `config/sources.yaml` catalogue configures
it or when the database already holds it, once either way. The configured
`enabled` flag wins over the persisted one: the current configuration is the
most direct authority on whether a source is enabled, whereas `sources.enabled`
records what a run observed. Opportunity persistence does refresh that row —
`type`, `enabled`, `category`, `country`, `frequency_minutes` and `status` are
upserted with `ON CONFLICT(id) DO UPDATE` — but the row is absent for a source
that has never run and lags the configuration until a run refreshes it.

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
`source_id`. Unknown values are serialized as `null` and never as zero.

The read is read-only in the strong sense. The database is opened through a
`mode=ro` SQLite URI (`connect_readonly_database`) and then also set
`query_only`, so the request cannot create the database file, a schema, a table,
or a row: a missing or not-yet-migrated path answers `503` and stays missing
rather than becoming an empty database. Source health is defined over the
operational SQLite database only; any other configured backend is refused
locally, before any connection is attempted, so no remote connector is called
and no network call is made. Validated settings are still loaded first, as for
any request, so a configured remote URL and token are read from the environment
like any other setting; source health never uses them to open or contact
anything, and they reach neither the response nor the logs.
