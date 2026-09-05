# Opportunity sources

The operational catalogue in `config/sources.yaml` currently has three enabled,
active sources:

| Source ID | Type | Intake |
| --- | --- | --- |
| `scale_ai_greenhouse` | Greenhouse | Public Scale AI board (`scaleai`) |
| `artefact_greenhouse` | Greenhouse | Public Artefact board (`artefact`) |
| `linkedin_job_alert_email` | Gmail LinkedIn alert | `newer_than:7d from:jobalerts-noreply@linkedin.com`, at most 50 messages |

## Greenhouse

Both organizations use the generic `GreenhouseCollector`. It performs an
unauthenticated GET against the public Greenhouse Job Board API, requests full
content, validates the response, and normalizes the official ID, title,
organization, location, description, publication time, and `absolute_url` into
an `OpportunityCandidate`. Public board tokens are collector configuration, not
credentials.

## LinkedIn Job Alert email

LinkedIn opportunities come from Job Alert emails through the official Gmail
API. The implementation does **not** authenticate to, open, or scrape LinkedIn
pages. Local user OAuth must grant exactly this one scope:

```text
https://www.googleapis.com/auth/gmail.readonly
```

The Gmail client exposes bounded `list` and `get` reads only; it does not mutate
labels, messages, or read state. The collector parses transient normalized MIME
content and deduplicates repeated numeric LinkedIn job IDs across messages in
one run. It emits canonical URLs of the form
`https://www.linkedin.com/jobs/view/<JOB_ID>`.

### The production query

The operational query is exactly:

```text
newer_than:7d from:jobalerts-noreply@linkedin.com
```

with a bound of 50 messages. The sender is the LinkedIn Job Alert address
specifically. The broader `from:linkedin.com` used before Phase 7C.2B also
matched newsletters and other LinkedIn mail, so it was not a job-alert intake
query. The 30-day window belongs to the read-only 7C.2A evaluation audit in
`evaluation/morocco_pfe/` and configures nothing operational.

Only normalized opportunity fields reach persistence. Email bodies, snippets,
Gmail message/thread IDs, and LinkedIn email tracking parameters are not stored
as opportunity data. OAuth client and token files stay local, should live in an
ignored directory such as `.secrets/`, and must never be committed.

## Identity and duplicate review

Repeated observations from one source refresh the occurrence identified by
`(source_id, source_url)`. Separately, Phase 2 can audit pairs whose source sets
differ and persist explicit reviewer decisions. No cross-source opportunity is
merged solely because an audit considers it similar; see
[database.md](database.md) for the confirmed-merge and rollback safeguards.

Source `frequency_minutes` values are catalogue metadata only. No scheduled or
continuous production runner is implemented: an operational run is the explicit
`python -m services.collector.cli.run_radar --once --apply` command described in
[operations.md](operations.md).

## Watching a source

Every attempt a source makes is recorded in `source_runs`, and
`GET /api/source-health` with the `/source-health` page reads that history back
read-only. A source configured here but never yet executed is listed with its
`enabled` flag and no run, rather than being hidden or given a fabricated one.

Three consecutive successful runs that each found exactly zero items raise a
deterministic anomaly on that source, which separates "nothing new was
published" from "this collector or parser may be broken". An unknown
`items_found` is never counted as a zero. The rule is specified in
[database.md](database.md). Nothing is alerted, retried, or rescheduled as a
result.
