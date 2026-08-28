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

local SQLite ─ read-only FastAPI GET /api/opportunities
             └─────────────────────────────── Next.js home page
```

The two Greenhouse sources use public board JSON. The LinkedIn source reads Job
Alert messages via the Gmail API and parses canonical LinkedIn job URLs without
opening or scraping LinkedIn pages. Gmail access is restricted to
`https://www.googleapis.com/auth/gmail.readonly`.

The FastAPI service reads visible, active opportunities from the existing
configured SQLite database. It performs no collection or migration. The Next.js
home page calls that API server-side using `OPPORTUNITY_API_BASE_URL` (default
`http://127.0.0.1:8000`) and renders real opportunity summaries and original
source links.

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
  an invented ending; deciding that a run is abnormal — a stale `RUNNING` row
  included — is a separate concern that does not exist yet.

## Outside Phase 2

There is no production scheduler or continuous deployment path, authenticated
LinkedIn-session scraper, automatic application flow, personalized ranking,
CV-to-offer recommendation engine, Digital Twin, or ML recommendation model.
There is also no source-health anomaly engine, no consecutive-zero alerting, no
stale-`RUNNING` detection, no notification path, and no Source Health page;
`source_runs` records history and nothing reads it back to raise an alert. Those possible later capabilities must
not be inferred from the implemented qualification taxonomy, the recorded run
history, or reserved package names.
