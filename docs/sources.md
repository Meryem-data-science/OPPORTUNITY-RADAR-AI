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

## ReKrute (Phase 7C.3A — foundation only, not a source)

ReKrute is **not** a configured source. It has no row in `config/sources.yaml`,
no `SourceConfig` type, no entry in the collector factory and no
`production_source_id` in the Morocco PFE source map, where it stays
`CANDIDATE`. The `RadarAgent` cannot run it and nothing about it is written to
the database.

**The access audit did not succeed.** 7C.3A was required to verify ReKrute's
public access before writing a parser against it. Outbound HTTPS from the
Claude Code Cloud session is filtered, and the egress proxy answered 403 to
`CONNECT` for `www.rekrute.com:443` and `rekrute.com:443`. DNS resolved and
unrelated hosts returned 200, so the denial is that sandbox's allow-list rather
than ReKrute blocking the request: no robots.txt, no terms page, no listing
page and no detail page was ever received. No CAPTCHA, bot challenge, site 403
or forced login was observed — not because none exists, but because no response
ever arrived.

Consequently `services/collector/parsers/rekrute.py` contains **no listing
parser and no detail parser**. Extracting offer links or offer fields requires
ReKrute's real markup, and inventing it is the one thing this phase forbids.
What the module does contain owes nothing to the site's HTML:

* `RekruteOfferRecord` — a source-shaped record whose field list is the
  architect's specification, not observed evidence. It keeps `deadline` and
  `contract_type`, which `OpportunityCandidate` has no column for, so the
  evidence is not discarded; `to_opportunity_candidate` bridges only what the
  shared model already supports and smuggles nothing into another field. The
  shared model is unchanged in this slice.
* `canonical_offer_url` — deterministic canonicalization: https, canonical
  host, fragment dropped, universal tracking parameters (`utm_*`, `gclid`, …)
  dropped, remaining query sorted. Unrecognised parameters are **kept**,
  because where ReKrute puts an offer id is unverified and discarding one could
  destroy identity. Non-http(s) schemes, foreign hosts and URLs carrying
  RFC 3986-invalid characters are rejected.
* `assess_target_evidence` — the locked PFE/stage rule (below).

### Completing the audit

    python -m evaluation.morocco_pfe.cli.rekrute_access_audit
    python -m evaluation.morocco_pfe.cli.rekrute_access_audit \
        --url https://www.rekrute.com/<public listing path> --follow-links --limit 5

GET-only, sequential, with a delay and an explicit timeout, hard bounded at 10
pages, and **every page is fetched exactly once** — the target's links come
from that single response rather than a second request. It stops at any wall it
meets — 403, 429, CAPTCHA, login redirect — and reports it. It never bypasses a
CAPTCHA, rotates a proxy, impersonates a browser, drives a browser engine,
authenticates or sends a cookie; the user agent names the audit truthfully. It
writes nothing: no SQLite, no benchmark row, no HTML on disk.

`robots.txt` is fetched first, under an explicit status policy:

| robots.txt response | audit behaviour |
|---|---|
| 200 | parse and obey; a disallowed path is not fetched (exit `3`) |
| 404, 410 | file definitively absent — no explicit rule applies, audit continues |
| 401, 403, 407, 429 | we were **refused** the file — stop before the target |
| 5xx / unexpected status | unresolved — stop before the target |
| challenge page or login redirect served as robots | stop |
| transport failure or timeout | stop |

The asymmetry is the point: a robots.txt we were refused tells us nothing about
what is allowed, and an unknown rule is never read as a permissive one. A 404
means no rule exists — a statement about robots.txt only, and **not**
permission of any kind.

The audit also checks the public terms page
(`https://www.rekrute.com/conditions-utilisation.html`) once, GET-only, and
reports it structurally: URL, status, availability, any barrier, and — when
reachable — which automation-related words (`robot`, `crawler`, `scrap`,
`scraping`, `aspir`, `automatis`, `bot`) appear in its **visible text**, with
markup excluded so a `<meta name="robots">` tag cannot manufacture a finding.
No clause, sentence or page text is emitted. There is no legal classifier here:
`manual_review_required` is always true, and the absence of those words is
explicitly **not** permission. A barrier on the terms page is reported, not
bypassed; if robots disallows that path, or robots itself was unusable, the
terms page is not fetched at all.

It prints **structure, not content** — JSON-LD types and `JobPosting` key
*names*, response codes, link counts, and path shapes with digit runs collapsed
so an offer-URL grammar becomes visible without anyone having guessed it. Exit
codes: `0` completed, `1` transport/usage failure, `2` access barrier, `3`
robots.txt disallows the path. No listing URL is hardcoded, because none was
verified; an operator passes the page they can see.

Its report records what a public GET returned. It does **not** establish that
automated collection is permitted — robots.txt and ReKrute's terms are both
still unread, and this project makes no legal claim about either.

### PFE/stage targeting, and where classification lives

An offer is PFE/stage evidence only when **either** the source's own structured
contract field explicitly says stage, **or** the title/offer text carries
explicit PFE evidence ("PFE", "projet de fin d'études"). Incidental internship
wording in prose is never sufficient: a CDI whose description reads "une
première expérience ou un stage est appréciée" is a CDI.

The division of responsibility: **source parsing** reports facts ReKrute states
and answers that one narrow question from the source's own contract field;
**shared classification**
(`services/collector/qualification/classifier.py`) keeps the cross-source
questions — `OpportunityType`, `Domain`, Data/AI qualification — and stays
source- and geography-neutral. The parser imports the Phase 7B vocabulary
rather than re-declaring a taxonomy, and adds only what 7B has no notion of: a
source-published contract field. Data & AI filtering is Phase 8 and appears
nowhere here.

Phase **7C.3B** is the operational activation: finishing the audit from a
permitted network, writing the listing and detail parsers against the structure
it reports, and only then registering a collector.
