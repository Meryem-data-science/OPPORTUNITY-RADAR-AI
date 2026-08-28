# Architecture

## Current Phase 2 path

`RadarAgent` is the normal orchestration boundary. It loads the source catalogue,
selects enabled sources whose status is `active`, and, for each source, builds a
collector and persists that collector's candidates in an independent
transaction. An exception from one source is recorded and the remaining sources
continue, so it cannot roll back earlier committed sources.

Every attempt is instrumented before it runs. The agent persists a `RUNNING`
`source_runs` row, then collects, then closes that same row as `SUCCESS` or
`FAILED` and stamps `sources.last_run_at`. A source whose run cannot be started
is not collected at all, because that work could never be audited; it is
reported as failed for this run and the remaining sources continue. A
finalization that fails is logged and leaves the row `RUNNING`, so an attempt
that was never closed stays visible rather than being erased or misreported.

An interrupted process therefore leaves `RUNNING` rows. `RadarAgent` isolates
`Exception` only, so an interruption propagates untouched instead of being
disguised as a collection failure.

After the complete source loop, the agent invokes qualification persistence
exactly once across all eligible opportunities. Qualification success or failure
is represented separately from each source result; the overall run succeeds only
when no source failed and global qualification succeeded.

```text
public Greenhouse boards ─┐
                         ├─ collectors ─ RadarAgent ─ local SQLite
Gmail LinkedIn alerts ───┘                         ├─ qualification
                                                   └─ duplicate review tools

local SQLite ─┬─ read-only FastAPI GET /api/opportunities
              │                     └─ Next.js home page
              └─ read-only FastAPI GET /api/source-health
                                      └─ Next.js /source-health page
```

The two Greenhouse sources use public board JSON. The LinkedIn source reads Job
Alert messages via the Gmail API and parses canonical LinkedIn job URLs without
opening or scraping LinkedIn pages. Gmail access is restricted to
`https://www.googleapis.com/auth/gmail.readonly`.

## Source health

`services/collector/database/source_health.py` derives, for each known source,
its `enabled` flag and the status and metrics of its most recent `source_runs`
row. It writes nothing, adds no table, and creates no row for a source that has
never run.

The status shown for a source stays the real status of that latest run —
`RUNNING`, `SUCCESS`, `FAILED`, or nothing at all for a source that never ran.
No second health taxonomy is layered on top of it. The one derived signal is
`zero_result_streak`, with the `anomaly_code` and `anomaly_message` it produces
past its threshold, and it is exposed beside the run status rather than folded
into it. The rule and its `NULL` handling are specified in
[database.md](database.md).

Both the streak and the anomaly are decided once, in the backend. The
`/source-health` page presents that verdict and never recomputes it.

## Read-only application API

The FastAPI service reads visible, active opportunities from the existing
configured SQLite database. It performs no collection or migration. The Next.js
home page calls that API server-side using `OPPORTUNITY_API_BASE_URL` (default
`http://127.0.0.1:8000`) and renders real opportunity summaries and original
source links.

The `original_url` each item exposes is chosen at read time from every
`opportunity_sources` observation of that opportunity, joined to `sources`, not
from the canonical row alone. `services/api/link_priority.py` holds that policy
in one place: a `greenhouse` observation is an official career page / ATS link
and is preferred over a `gmail_linkedin_alert` job-board observation, so the
canonical row a reviewer kept for deduplication never decides which link the
user is shown. Within one observation the order is `application_url`, then
`source_url`, then `canonical_url`; between several official observations the
tie-break is the most preferred official source type, then the smallest
`opportunity_sources.id`, which is the earliest recorded observation. With no
official observation, the previous canonical-row fallback is unchanged. The
selection reads only; it moves, rewrites, and deletes nothing.

`GET /api/source-health` is read-only more strictly still. It opens the
operational SQLite database through a `mode=ro` URI and also sets `query_only`,
so the request cannot create the database file it was pointed at, let alone a
schema or a row; a non-SQLite backend is refused locally rather than dialled, so
the request makes no network call at all. It merges the validated
`config/sources.yaml` catalogue with the sources the database already holds, so
a configured source that has never run is still listed, and returns one entry
per source. The `/source-health` page renders those entries as a table.

## Digital Twin root

`services/digital_twin/` is a separate business package: the collector keeps
owning the SQLite infrastructure — the same connection factory and the same
migration runner are reused, not replaced — while the Digital Twin owns its own
domain. Nothing about it lives in `services/collector/models/`.

Phase 3.1A persists two rows and no more:

```text
users  (identity / ownership root)
  1 ─── 1
profiles  (stable Digital Twin root)
```

`users` is who owns data; `profiles` is the stable anchor every later Digital
Twin table will point at. One user owns at most one profile, enforced by
`UNIQUE(user_id)` in the schema. There is no ORM: a small typed layer
(`models.py`, `identity.py`, `repository.py`) talks to SQLite directly, as the
rest of the project does, and a local CLI initialises or reads the real profile
without echoing or logging the address.

This slice implements the root only. CV parsing, `cv_versions`, `profile_facts`,
skills, education, experiences, projects, certifications, languages,
preferences, eligibility, matching, scoring, personal notifications, a personal
frontend, and any public profile API are **not** implemented — neither Phase 3.1
as a whole nor Phase 3 is complete. No LLM, no external API, and no remote
write path is introduced: the operational database stays local SQLite.

## Data-processing boundaries

- Opportunity persistence provides repeat-observation idempotence for a source.
- Cross-source duplicate audit is read-only and heuristic. Its
  `STRONG_CANDIDATE`, `POSSIBLE_CANDIDATE`, and `WEAK_CANDIDATE` labels are review
  aids, never automatic merge decisions.
- Human decisions are persisted separately. Physical merge requires an explicit
  `CONFIRMED_DUPLICATE` decision and an explicit canonical opportunity; merge and
  rollback are transactional and preserve audit history.
- Qualification is deterministic, explainable, versioned derived data. It is not
  personalized matching, ranking, or a recommendation model, and geography is
  metadata rather than an exclusion rule.
- Source run history is recorded evidence, not a judgement. A run row states
  what one attempt observed, with unknown metrics left `NULL` rather than
  filled with zeros, and an unfinished attempt left `RUNNING` rather than given
  an invented ending.
- The user/profile root records ownership, not knowledge. It stores who owns
  a Digital Twin and the stable profile that owns nothing yet, and it asserts
  no fact about the person. Reading an unknown address returns an explicit
  absence and never invents a profile; a user and its profile are created only
  by the explicit `ensure_user_profile` operation behind `init-profile`.
- Source health reads that evidence back without adding to it. It derives one
  entry per known source at read time, and the only judgement it makes is the
  repeated-zero anomaly described below. Deciding that a `RUNNING` row is stale
  remains a separate concern that does not exist yet.

## Outside Phase 2

There is no production scheduler or continuous deployment path, authenticated
LinkedIn-session scraper, automatic application flow, personalized ranking,
CV-to-offer recommendation engine, or ML recommendation model. The Digital
Twin exists only as the empty `users`/`profiles` root added by Phase 3.1A,
described above; no profile content, matching, or scoring is derived from it.
Source health detects exactly one anomaly, read-only, and does nothing with it
beyond returning and displaying it. There is no alerting of any kind: no email,
no web push, no notification path, no scheduler and no GitHub Actions schedule,
no stale-`RUNNING` detection, no automatic retry, and no self-healing collector.
Those possible later capabilities must not be inferred from the implemented
qualification taxonomy, the recorded run history, the source health read model,
or reserved package names.
