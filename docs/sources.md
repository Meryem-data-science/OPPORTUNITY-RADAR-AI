# Opportunity sources

Phase 2.1 configures two real public sources: `scale_ai_greenhouse` (board token
`scaleai`) and `artefact_greenhouse` (board token `artefact`). Both use the same
generic `GreenhouseCollector`, which reads the configured public Greenhouse
board API. These public GET requests require no authentication.

The collector preserves official `absolute_url` values and produces typed
`OpportunityCandidate` objects. The read-only CLI leaves them in memory; the
separate persistence CLI writes a bounded batch with explicit authorization.
Configured category, country, frequency, and status metadata are stored on the
source, while `organization` and public `board_token` remain collector-only
configuration.

Repeat observations use `(source_id, source_url)` as their current identity and
refresh the existing opportunity without changing its first-seen timestamp.
This is same-source idempotence only, not multi-source deduplication.

LinkedIn Job Alerts are an operational pipeline source with id
`linkedin_job_alert_email`. They are collected **via alert email**, never by
scraping or opening a LinkedIn job page. The source uses official Gmail API
`list`/`get` reads under the single `gmail.readonly` scope. Its non-secret
catalogue settings are query `newer_than:7d from:linkedin.com` and message limit
`50`; the subject is not required.

The dedicated collector parses transient `GmailMessageCandidate` values, then
deduplicates repeated numeric LinkedIn job IDs across all messages in the run.
Only `OpportunityCandidate` values with canonical
`https://www.linkedin.com/jobs/view/<JOB_ID>` URLs reach RadarAgent and the
existing persistence layer. Email bodies, message/thread IDs, snippets, and
tracking query strings are not persisted. The generic API and web UI therefore
handle persisted LinkedIn opportunities like all other opportunities. There is
no automatic application, LinkedIn page scraping, or scheduled execution.
