# Opportunity sources

Phase 1.1 configures one real public source: `scale_ai_greenhouse`, a Greenhouse
job board for Scale AI with board token `scaleai`. The collector constructs and
reads `https://boards-api.greenhouse.io/v1/boards/scaleai/jobs?content=true`.
This public GET requires no authentication.

The collector preserves official `absolute_url` values and produces typed
`OpportunityCandidate` objects. The read-only CLI leaves them in memory; the
separate persistence CLI writes a bounded batch with explicit authorization.
Configured category, country, frequency, and status metadata are stored on the
source, while `organization` and public `board_token` remain collector-only
configuration.

Repeat observations use `(source_id, source_url)` as their current identity and
refresh the existing opportunity without changing its first-seen timestamp.
This is same-source idempotence only, not multi-source deduplication.
