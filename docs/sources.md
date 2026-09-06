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

## Stagiaires.ma — audited in Phase 7C.4A, not collected

Stagiaires.ma is **not** a source. It has no row in `config/sources.yaml`, no
`SourceConfig` type, no entry in the collector factory and no
`production_source_id` in the Morocco PFE source map, where it stays a `P1`
`CANDIDATE` with `collection_strategy: FUTURE_COLLECTOR`. No collector, no
production parser, no `RadarAgent` run, no database row, no migration and no
scheduler exist for it.

Phase 7C.4A is a **public-access and structure feasibility audit**, and nothing
more. It answers "could this be collected, and would an offer page give us the
fields we need?" so that the decision to build a collector — Phase 7C.4B — can
be made on evidence instead of on optimism.

### The discovery chain

```
robots.txt
  -> the Sitemap: line it declares      (an official sitemap index)
    -> offre-sitemap*.xml               (the public offer sitemaps)
      -> /stage-emploi-maroc/<numeric-id>-<slug>
```

This is the site's own published chain, and it is the discovery mechanism. The
public PFE listing (`/stage-emploi-type-stage/stage-de-fin-d-etudes`) is a
client-rendered application whose initial HTML does not expose the offer list as
ordinary links; reading it would require driving a browser, which is forbidden
here and which the sitemap makes unnecessary. The audit fetches that listing
once, for reachability and structure only, and **never parses an offer list out
of it**.

### What was actually observed

A GET-only, bounded, robots-obeying audit was run locally against public pages.
It reached `robots.txt` (HTTP 200, `text/plain`, the PFE target path not
disallowed for the audit user agent), the public PFE listing (HTTP 200, no
`JobPosting` JSON-LD in the initial HTML), the official sitemap index declared
by robots (HTTP 200, a `sitemapindex`), and the offer sitemaps it declares
(HTTP 200, `urlset`). Every offer URL observed sat under `/stage-emploi-maroc/`
and carried a numeric ID, and every sitemap row carried a `lastmod`.

Those counts are evidence of **that run, at that moment**. They are not a fixed
property of the site, nothing in this repository hardcodes them, and no test
asserts them: a live total pinned into a unit test is a false failure waiting to
happen. Re-run the audit to get current numbers.

**No bypass of anything was attempted or implemented** — no browser automation,
no browser impersonation, no Selenium or Playwright, no proxy rotation, no
CAPTCHA handling, no authenticated access, no user cookies, no private API taken
from a JS bundle.

### What this evidence does and does not say

It says the site publishes its offers in a form an honest, browser-free client
can read, and that the offer URL carries a numeric identifier.

It does **not** say:

* that automated collection is permitted. A public HTTP 200 is reachability, not
  permission. No terms URL for this site has been evidenced, so the audit's
  terms check is reported as *not attempted* rather than guessing a URL, and
  when an operator supplies one with `--terms-url` the audit records only its
  status and leaves `manual_review_required` true. This project makes **no legal
  claim about Stagiaires.ma in either direction**;
* that `lastmod` is a publication date. It is page-modification metadata. The
  audit's sitemap record carries `sitemap_lastmod` and has no `published_at`
  field at all, so nothing can quietly promote one to the other;
* that the numeric ID is a stable production key. It is a **candidate**
  `source_external_id`: observed, not proven stable over time. Where one ID
  appears under two distinct canonical URLs, the audit reports the collision
  instead of silently merging the two records;
* anything about recall. The sitemap is what the site chose to publish there;
  how much of its real content that covers is unmeasured.

### The audit itself

```bash
python -m evaluation.morocco_pfe.cli.stagiaires_access_audit
```

GET-only, sequential, one page at a time with a delay, identifying itself as
`OpportunityRadarAI-AccessAudit/1.0`. It obeys `robots.txt` under an explicit
status policy — a robots we were *refused* stops the audit, because an unknown
rule is never read as a permissive one. Detail sampling is hard bounded at
**three** pages, chosen deterministically (highest numeric ID first, ties broken
by canonical URL) — an ordering picked so the sample is reproducible, not
because a higher ID is known to mean a newer offer. It is not.

It **never issues a GET to a host outside `www.stagiaires.ma` / `stagiaires.ma`**,
and never to a path `robots.txt` disallows. Redirects are resolved by the audit
itself and bounded, and every hop is re-checked against both rules before the
next request — obeying robots on the URL we asked for but not on the one we are
handed would let a site redirect an allowed path to a disallowed one. A
`Location` the audit may not follow is reported and its target left unfetched.

No option names a **discovery** URL: the sitemap comes from robots' own
declaration or the audit stops, so no invocation can redirect discovery
elsewhere. The one operator-supplied URL is `--terms-url`, which must be
same-host, is fetched at most once for terms/legal reachability metadata only,
never becomes a discovery source, and always leaves
`manual_review_required` true.

An application link is reported only on explicit evidence of an application
action: a control whose accessible text says `postuler`, `candidater` or
`apply`. The posting's own canonical URL (`JobPosting.url`, `og:url`) is never
read as one — every offer has a canonical URL, and treating it as an apply link
would claim an application route the page may not offer. A reported apply href
may point off-domain; it is recorded, never fetched.

It writes nothing: no SQLite, no raw HTML on disk, no `config/sources.yaml`
change. It prints one JSON object of structural findings — statuses, counts,
JSON-LD type and key *names*, and per-signal presence with a short excerpt for
verification. Job descriptions are reported by length only. **No Stagiaires.ma
page body is stored in this repository.**

### Next

Phase **7C.4B** — an actual collector — is the next step, and only after the
Architect has validated 7C.4A. Until then Stagiaires.ma collects nothing.

## ReKrute — evaluated in Phase 7C.3, not selected

ReKrute is **not** a source. It has no row in `config/sources.yaml`, no
`SourceConfig` type, no entry in the collector factory and no
`production_source_id` in the Morocco PFE source map, where it is recorded as
`NOT_SELECTED`. No collector, no production parser, no `RadarAgent` run, no
database row, no migration and no scheduler exist for it, and none is planned.

Phase 7C.3 is therefore complete as a **source feasibility evaluation**, with
no production activation. It is not a paused integration.

### What was actually observed

A GET-only, bounded, robots-obeying audit was run locally against public pages:

| Check | Result |
|---|---|
| `robots.txt` | HTTP 200, retrieved and parsed; the target path was **not** disallowed |
| `conditions-utilisation.html` (public terms) | HTTP 200, publicly reachable |
| `emploi-PFE` (PFE listing target), automated GET | **HTTP 403**, 14-byte body |

The same listing page is visible to a human in an ordinary browser. **No bypass
of that refusal was attempted or implemented** — no browser impersonation, no
Selenium or Playwright, no proxy rotation, no CAPTCHA handling, no
authenticated access, no user cookies, no private API.

### What this evidence does and does not say

It does **not** say that ReKrute prohibits automated access. `robots.txt` did
not disallow the target path. The terms page was reachable but was not read as
a permission and still requires human review; the audit's automated-access
keyword indicators were all false, and their absence is not permission either.
This project makes no legal claim about ReKrute in either direction.

What it does say is narrower and sufficient for the decision: honest,
non-evasive automated access to the PFE listing is refused today.

### Why it was not selected

The decision is a product one. The PFA target is narrowly Morocco + PFE/stage
(with Data & AI targeting later). ReKrute is a general employment board rather
than a PFE/stage-specialised source, so its expected PFE coverage benefit does
not justify further integration effort — particularly when automated listing
access is already refused without resorting to evasion.

Higher-value internship/PFE-specific sources remain ahead of it: Stagiaires.ma,
Stage.ma, and official employer/ATS sources. ReKrute stays in the source map as
an `AUDIT` reference for manual coverage comparison, which is what `P2` /
`AUDIT` / `MANUAL_BENCHMARK` now record.

Reconsider only if an official or public integration channel suitable for this
purpose becomes available, or if the product scope changes.

### `NOT_SELECTED` in the source map

`NOT_SELECTED` is a closed `integration_status` meaning: *the source was
evaluated and intentionally not selected for production in the current PFA
scope.* It deliberately does **not** imply that a source is legally forbidden,
permanently impossible, or fake. Keeping that distinct from
`NEEDS_VERIFICATION` matters — otherwise "we decided against it" and "we have
not looked yet" become indistinguishable a year from now.

No ReKrute page body, and no terms text, is stored in this repository.
