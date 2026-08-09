# Opportunity sources

Phase 1.1 configures one real public source: `scale_ai_greenhouse`, a Greenhouse
job board for Scale AI with board token `scaleai`. The collector constructs and
reads `https://boards-api.greenhouse.io/v1/boards/scaleai/jobs?content=true`.
This public GET requires no authentication.

The collector preserves official `absolute_url` values and produces typed
`OpportunityCandidate` objects in memory. Phase 1.1 performs no database
persistence; connecting these candidates to Turso belongs to a later step.
