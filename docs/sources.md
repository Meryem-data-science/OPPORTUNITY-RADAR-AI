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

Phase 2.2A adds a separate generic Gmail API intake foundation. It uses local
user OAuth with the single `gmail.readonly` scope and can normalize matching
messages into transient plain-text/HTML values for a safe CLI summary. Gmail is
not yet an `OpportunityCandidate` source: no LinkedIn/Indeed parsing, database
persistence, RadarAgent integration, or API exposure is part of this phase.

## ReKrute public discovery (Phase 2.3A)

`rekrute_public` is a Morocco-first, public web source with no configured
organization: the real employer is read from each offer page. Its dedicated
collector uses identified, read-only HTTP GET requests only. It performs no
login, candidate session, browser automation, application submission, or
database persistence.

Every run first fetches and parses ReKrute's `robots.txt` and fails closed when
the policy is unavailable, unusable, or denies either the configured search
path or any selected offer path. Discovery is limited to direct links on one
parameterized search page whose path ends in a reliable numeric offer ID.
External hosts, tracking query parameters, non-offer links, and duplicate links
are discarded. At most 20 detail pages can be selected, with a default probe
limit of three and a configurable pause between detail requests.
