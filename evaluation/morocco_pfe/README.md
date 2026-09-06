# Morocco PFE evaluation — 7C.1 source map and gold benchmark, 7C.2A Gmail intake audit, 7C.4A Stagiaires.ma structure audit

This directory is the foundation for one question, and it does not yet answer
it:

> Over the declared universe of Morocco PFE/stage sources, what proportion of
> the real opportunities does Opportunity Radar AI actually find?

7C.1 builds the two things that question needs before it can be asked
honestly — an explicit list of the sources we want to be measured against, and
a corpus of opportunities we know were really published — and adds **no
collector, no scraper, no migration, no endpoint and no page**.

## Three things that are not the same thing

| Artefact | What it is | Where |
| --- | --- | --- |
| Operational catalogue | The sources the radar really collects | `config/sources.yaml` |
| Coverage source map | The sources we declare we want to be measured against | `evaluation/source_coverage/morocco_pfe_sources_v1.yaml` |
| Gold benchmark | Real opportunities observed on named sources | `evaluation/benchmarks/morocco_pfe_gold_v1.jsonl` |

They are kept apart on purpose. Writing a source into the coverage map turns
nothing on: only `config/sources.yaml` does that, and 7C.1 leaves it byte for
byte as it was. Writing an opportunity into the benchmark makes it evidence,
not an opportunity: nothing here is inserted into `opportunities`,
`opportunity_sources` or any other operational table, ever.

The map carries `is_production_registry: false` and the validator refuses the
file if that ever reads true — the separation is checked, not just described.

## What the current production coverage actually is

Three sources are configured and enabled today, and only two collector types
exist (`greenhouse`, `gmail_linkedin_alert`):

| Source id | Type | Morocco-specific? |
| --- | --- | --- |
| `scale_ai_greenhouse` | Greenhouse | no — a global board |
| `artefact_greenhouse` | Greenhouse | no — a global board |
| `linkedin_job_alert_email` | Gmail LinkedIn alert | not by construction |

So exactly one entry of the source map is `ACTIVE`, and the map's fourteen
entries are otherwise a statement of intent. The validator cross-checks every
`ACTIVE` entry against `config/sources.yaml` and rejects the map if an entry
claims a collector the operational catalogue does not actually run.

`ACTIVE` means exactly what `RadarAgent.run_once` means by it — the configured
source is **enabled *and* has `status: active`** — because those are the two
conditions under which a source is really collected. A source that is enabled
but inactive is never read, so calling it `ACTIVE` here would put a source in
the coverage numerator that contributes nothing to it. An `ACTIVE` entry
declaring `GMAIL_ALERT` must additionally name a production source of type
`gmail_linkedin_alert`, so a strategy and the collector behind it cannot drift
apart.

## LinkedIn is Gmail-only

The LinkedIn entry is `collection_strategy: GMAIL_ALERT` and the closed
strategy registry has no member that would describe scraping. LinkedIn
opportunities are read out of job-alert email through the read-only Gmail API,
conservatively, exactly as `services/collector/parsers/linkedin_job_alert.py`
does today; LinkedIn is never authenticated to, opened or scraped. 7C.1 changes
neither the Gmail client nor the LinkedIn parser.

Alert mail is a feed LinkedIn filters for us, so what fraction of Morocco PFE
postings it even contains is unknown. That is a measurement, and it is 7C.2's.

## The benchmark

One JSON object per line, versioned in git, with a manifest beside it. Every
row is an opportunity somebody actually saw on a named source.

Fields: `benchmark_id`, `title`, `organization`, `location`, `country_code`,
`source_name`, `source_url`, `official_application_url`, `published_at`,
`observed_at`, `pfe_cohort_year`, `expected_opportunity_type`,
`expected_data_ai`, `historical_or_live`, `source_authority`, `notes`.

Rules that make it worth trusting:

* **Nothing is generated.** No synthetic row, no invented employer, no filler.
  The validator refuses records whose identity fields read as placeholders and
  URLs on placeholder hosts. A small honest benchmark beats a padded one: a
  recall computed against invented postings measures nothing.
* **`benchmark_id` is independent of the database.** It is derived from the
  source (`stage-ma-9279`), never from `opportunities.id`. A SQLite row id is
  local to one database file and would silently rebind if the database were
  rebuilt.
* **No URL is invented.** `official_application_url` is null whenever the
  employer-side URL is unknown, which is the case for both seed rows. 7C.1
  resolves nothing over the network; a later slice may store the aggregator URL
  and the official one side by side.
* **`pfe_cohort_year` is observed, never inferred.** A publication date is not
  a cohort: a campaign published in October 2025 can be PFE 2026, and one
  published in August 2026 can be PFE 2027. The field is a reviewed gold fact,
  so it stays null unless the evidence states the cohort — it is never derived
  from `published_at`, and no code here derives it. Both seed rows are null.
* **`expected_opportunity_type` uses the existing closed registry**
  (`services/digital_twin/preferences/models.py`), imported by the validator.
  No third taxonomy is created here.
* **Historical rows stay historical.** `historical_or_live` says whether a row
  is evidence of something already gone or of something the radar could still
  be caught missing. Expired postings are evaluation evidence and are never
  re-injected as active opportunities, so the freshness rules a later slice
  adds to the operational catalogue are not being smuggled past.

### Current state: DRAFT / SEED

| | |
| --- | --- |
| status | `DRAFT` |
| rows | 2 |
| `target_minimum_rows` | 60 |
| `evaluation_ready` | `false` |

**Two rows are a seed, not an evaluation set.** No recall number computed on
this corpus today would mean anything, and none should be published. The
benchmark becomes evaluation-ready only when a corpus large enough to be worth
believing has been assembled *and* reviewed by a human — the validator enforces
that `evaluation_ready` cannot be true below the declared target, but reaching
the target does not flip it: row count is not review, and a person sets that
flag deliberately.

### Live canaries are not gold rows

Oracle's official Morocco R&D careers page publicly advertises the Morocco PFE
2027 internship programme:

    https://www.oracle.com/ma/careers/research-development/

It is recorded in the **source map** as a live canary
(`oracle_morocco_rd_careers`, `live_canary: true`), and deliberately **not** as
a benchmark row. A canary is evidence that a source publishes the kind of thing
we care about; a gold row is one specific opportunity with a title, an employer
and a URL. Turning the former into the latter is fabrication, and it is exactly
the failure mode this file exists to prevent.

## What we measure, and what we refuse to claim

We do **not** claim, and will not report, "coverage of every PFE offer on the
internet". That denominator does not exist, cannot be enumerated, and any
number computed against it is marketing.

What we report instead has an explicit, versioned denominator:

> coverage against the declared source universe
> `morocco_pfe_source_coverage v1`, measured on the gold benchmark
> `morocco_pfe_gold_v1`.

Both are files in this repository with a version in their name. When the map or
the benchmark changes, the version changes, and two numbers computed against
different versions are not comparable.

## Metrics contract (defined here, implemented later)

None of these is computed in 7C.1 and no number for any of them appears
anywhere in this slice. They are defined now so that the slice that implements
them cannot quietly choose a friendlier denominator.

**Discovery recall**

    gold opportunities discoverable on covered sources AND found by the agent
    ----------------------------------------------------------------------
    gold opportunities discoverable on covered sources

The denominator excludes gold rows whose only source is not covered by an
active collector — otherwise the metric measures the backlog rather than the
radar. The excluded count must always be reported alongside it.

**Source success rate**

    successful source runs / attempted source runs

Read from `source_runs`, per source. A source that never ran has no rate, not a
rate of zero.

**Discovery latency**

    discovered_at - published_at,  where published_at is known

Reported as a distribution, never as a single average, and only over rows whose
publication date the source actually stated.

**URL correctness**

    discovered opportunities whose canonical/application URL is correct
    ------------------------------------------------------------------
    reviewed discovered opportunities

The denominator is what a human reviewed, not what was collected.

**Dedup consolidation rate**

Whether several provenances of one real opportunity become one canonical
opportunity with several `opportunity_sources`, rather than several
opportunities. Measured against gold rows known to appear on more than one
source.

**Geography accuracy**

Agreement between Phase 7A.1's verdict and the benchmark's `country_code`.

**Type accuracy**

Agreement between the stored `opportunity_constraints.opportunity_type` and the
benchmark's `expected_opportunity_type`.

`expected_data_ai` is a gold **evaluation** label carried by the benchmark for
future use. It changes no production classifier and adds no production rule;
Data & AI classification is Phase 8 and is out of scope here.

## 7C.2A — the Gmail LinkedIn intake audit

`linkedin_gmail_audit.py` and `cli/linkedin_gmail_audit.py` are the first thing
in this directory that reads anything live. They measure one link of the chain
and nothing else:

    Gmail job-alert messages -> the existing LinkedIn parser -> counts

Run it, read-only, over a bounded window:

```
python -m evaluation.morocco_pfe.cli.linkedin_gmail_audit
python -m evaluation.morocco_pfe.cli.linkedin_gmail_audit \
    --query "newer_than:7d from:jobalerts-noreply@linkedin.com" --limit 50
```

It prints one JSON object: `query`, `message_limit`, `messages_found`,
`messages_with_candidates`, `messages_without_candidates`, `candidates_parsed`,
`unique_linkedin_jobs`, `duplicate_occurrences`, `candidates_with_location`,
`candidates_without_location`, `truncated`.

### Parser yield is not LinkedIn recall

This is the distinction the whole slice exists to keep:

| | |
| --- | --- |
| **Gmail intake coverage / parser yield** | of the messages this Gmail query returns, how many the parser reads, how many distinct jobs they describe. **This is what the audit measures.** |
| **LinkedIn recall** | of the Morocco PFE jobs really posted on LinkedIn, how many the radar sees. **The audit says nothing about this.** |

A job LinkedIn never puts in an alert email does not exist as far as this
measurement is concerned, and no alert-mail number can bound how many such jobs
there are. `messages_without_candidates == 0` means the parser read every
message it was given; it does not mean the mailbox held every job. Calling any
number produced here "LinkedIn recall" would be a false claim.

### The mailbox

The project reads **one dedicated Gmail account**, created for it, holding only
recent live mail. The owner's personal Gmail account is out of scope entirely:
it is never connected, read, imported or replayed, and no code here can reach a
mailbox other than the one `GMAIL_OAUTH_CLIENT_SECRET_PATH` and
`GMAIL_TOKEN_PATH` configure. Because that account is new, there is no
historical archive to replay — 7C.1's backlog note about replaying 2025/2026
mail describes a corpus that does not exist.

OAuth stays `gmail.readonly`, enforced by the existing client, which this slice
neither modifies nor weakens. No message is read into any file: the audit
prints counts, and never a message id, thread id, subject, snippet, body,
sender or URL.

### The one local file that does change

The audit persists nothing it reads, but "nothing is written anywhere" would be
false, and it is worth being exact about why. Authorizing any Gmail entry point
in this repository lets `services/collector/gmail/client.py` maintain its own
credential file: it tightens the permissions of `GMAIL_TOKEN_PATH` to `0600`,
and rewrites that file when a token is refreshed or a new authorization is
granted.

That is pre-existing OAuth behaviour, shared with the Gmail probe, the LinkedIn
alert probe and the production collector. 7C.2A delegates to it unchanged — no
new credential loader, no second token path, no weakening of the read-only
scope — and does not claim it away. So the accurate guarantee is:

| The audit | The OAuth client it delegates to |
| --- | --- |
| persists no Gmail message data — no subject, snippet, body or message id | may `chmod` the local token file |
| creates no evaluation dump and no business artefact | may rewrite the token file on refresh or new authorization |
| writes no SQLite or other database data | touches nothing else on disk |
| modifies no Gmail message or label | |

### `truncated`

`truncated` is true whenever `messages_found == message_limit`. The Gmail
client stops at the bound it was given and cannot prove nothing lies beyond it,
so a reached bound means **the window may be incomplete**.

`truncated: false` is a narrower statement than it looks. It says the caller's
bound did not cut the window short — nothing more. It is never evidence about
mail outside the query's own window, about mail the query does not match, or
about jobs that were never mailed at all.

### What 7C.2A deliberately does not do

* **No production change.** `config/sources.yaml` was untouched by this slice:
  LinkedIn still ran `newer_than:7d from:linkedin.com` with a limit of 50 in
  production. That query is broader than job alerts and also matches
  newsletters; correcting it to `from:jobalerts-noreply@linkedin.com` was an
  operational decision left to 7C.2B, which has since made exactly that change
  (see below). The 30-day audit query is an **evaluation** default and
  configures nothing.
* **No data write.** No SQLite write, no migration, no new table, no JSONL
  dump, no raw Gmail export, no new credential storage mechanism. Gmail →
  parser → operational database has *not* been exercised by this slice, and the
  `linkedin_job_alert_email` rows in `opportunity_sources` and `source_runs`
  remain whatever they already were. The existing OAuth client's own credential
  maintenance at `GMAIL_TOKEN_PATH` is the one exception, and it is described
  above rather than denied.
* **No parser or collector change.** The parser is reused, not reimplemented;
  the collector, the factory and `RadarAgent` are untouched.

## 7C.2B — the production query correction

7C.2B changed one production value and nothing else: the LinkedIn source's
`gmail_query` in `config/sources.yaml`.

| | |
| --- | --- |
| before | `newer_than:7d from:linkedin.com` |
| after | `newer_than:7d from:jobalerts-noreply@linkedin.com` |

`gmail_message_limit` stays **50** and the window stays **7 days**. The 30-day
query above remains a 7C.2A **evaluation** default and is never production
configuration; `tests/unit/test_sources.py` locks both facts.

Nothing else moved. No new collector, parser, persistence model, migration,
schema change, CLI or scheduler: the existing chain — `run_radar` → `RadarAgent`
→ `LinkedInJobAlertCollector` → Gmail `gmail.readonly` → the existing parser →
opportunity persistence → `source_run` finalization → the existing qualification
pass — is reused unchanged. An operational run stays the explicit

```
python -m services.collector.cli.run_radar --once --apply --source linkedin_job_alert_email
```

`--source` narrows **collection** only. After the source loop, `RadarAgent`
still runs its one qualification reconciliation over every eligible operational
opportunity, exactly as it did before; 7C.2B does not add a LinkedIn-only
qualification mode.

### The first operational SQLite run is external evidence

Before 7C.2B, `opportunity_sources` and `source_runs` held zero
`linkedin_job_alert_email` rows: Gmail → parser was proven live by 7C.2A, but
Gmail → parser → `RadarAgent` → the operational database had never been
executed. This slice does not execute it either. The first authorized run
against `data/opportunity-radar.db` is performed locally by the architect, after
a backup and after review of this branch. Its outcome is external validation
evidence: it is recorded outside the repository and is never hardcoded into
production logic or tests.

### The real numbers live outside the code

No observed count is hardcoded anywhere in this repository, and none should be.
The audit's baseline is whatever a run against the real dedicated account
printed, recorded as external validation evidence with its query, its limit and
its date. The unit tests use invented messages to prove the aggregation rules;
a synthetic fixture is never evidence of what the mailbox contains, and no test
here is a validation of live coverage.

## 7C.4A — the Stagiaires.ma public access and structure audit

Stagiaires.ma is the next collector **candidate**, and 7C.4A is the slice that
asks whether collecting it is feasible at all — before anybody writes a line of
collector. It is an audit and nothing else. When it finished, Stagiaires.ma had
exactly as much production presence as before: none.

    evaluation/morocco_pfe/stagiaires_access.py            # pure, opens no socket
    evaluation/morocco_pfe/cli/stagiaires_access_audit.py  # the only file that fetches

Run it:

```bash
python -m evaluation.morocco_pfe.cli.stagiaires_access_audit
python -m evaluation.morocco_pfe.cli.stagiaires_access_audit --limit 1
```

### The discovery chain, which is the site's own

```
robots.txt
  -> the Sitemap: line it declares      (an official sitemap index)
    -> offre-sitemap*.xml               (the public offer sitemaps)
      -> /stage-emploi-maroc/<numeric-id>-<slug>
```

Every link in that chain is *discovered*, not assumed. The sitemap index is
read from robots' own `Sitemap:` declaration — if robots declares none, the
audit stops and says so rather than guessing a URL. The offer sitemaps are read
from the index, matched as a family (`offre-sitemap*.xml`) rather than as a
hardcoded pair, so a third file is picked up the day it appears. Only same-host
entries are accepted: "the index told us to" is not a reason to fetch another
domain.

**The PFE listing is deliberately not the discovery source.** It is a
client-rendered application whose initial HTML does not carry the offer list as
ordinary links, so parsing it would mean driving a browser — which this phase
forbids and which the sitemap chain makes unnecessary. The audit fetches the
listing exactly once, to record that it is publicly reachable and what it
structurally contains, and never parses an offer list out of it.

### `lastmod` is not a publication date

The single most important invariant here. A sitemap `<lastmod>` says a *page*
changed; it does not say an *offer* was posted. A re-render, a template edit or
a counter bump all move it. So `SitemapOfferEntry` carries `sitemap_lastmod`
and has **no `published_at` field at all** — there is nothing for a later phase
to quietly map it to, and a test asserts the field set never grows one. Whether
the site states a real publication date is a question about the *detail page*,
which the detail audit answers separately and may well answer with "it does
not".

### The numeric ID is a candidate, not a contract

`/stage-emploi-maroc/<id>` exposes an integer that looks like a stable per-offer
identity, and the audit extracts it as a **candidate** `source_external_id`.
One audit cannot establish stability over time, so nothing here claims it. The
digits are kept as published (`007` stays `007`; normalizing it to `7` would
invent an equality the site never stated), and where the data contradicts the
assumption — one ID under two distinct canonical URLs — the audit **surfaces
the collision** rather than deduplicating it away. That finding is what decides
whether the ID may ever be a production key.

### The bounded detail sample

At most **three** offer pages, sequential, with a delay. The bound lives in
`select_detail_sample`, which validates the limit itself, so there is no path
through the audit that reaches the network without passing it.

Selection is deterministic and documented: **highest numeric
`source_external_id` first, ties broken by canonical URL ascending**. Newest
offers are the ones a future collector would actually meet, and "highest" is a
total order the site publishes rather than a matter of taste; the tie-break
means the choice cannot depend on dictionary or sitemap-file order.

For each sampled page the audit reports, per signal — title, organization,
location, contract type, internship type, work mode, description, publication
date, deadline, application URL — whether the public HTML carries it and
**through which generic mechanism**: JSON-LD `JobPosting`, microdata,
OpenGraph/`<meta>`, `<title>`/`<h1>`/`<time>`. Nothing site-specific is
consulted.

`application_url` is deliberately stricter than the rest. It is **not** read
from `JobPosting.url`, `itemprop="url"` or `og:url`: those name the posting —
the page you are already on — and every offer has a canonical URL, so treating
one as an application link would report "you can apply here" for every offer
ever published, including ones whose only route is an email address. It is
reported only when a control *says* it applies: an anchor, button or submit
input whose accessible name (its text, `aria-label` or `title`) carries an
application verb — `postuler`, `candidater`, `apply`. A relative href is
resolved against the detail page's own URL. The href is **reported, never
fetched**; it may legitimately point at an employer's ATS on another host, and
recording a URL is not requesting it. A signal reported as absent means the page does not publish it
through any standard mechanism, which is a structural finding, not an
instruction to go and invent a selector for it. Descriptions are reported by
**length only** — third-party job text is counted, never copied into this
repository — and JSON-LD is reported as type and key *names*, never values.

### What it writes, and what it will not do

It writes nothing: no SQLite, no benchmark row, no raw HTML on disk, no
`config/sources.yaml` change. It prints one JSON object of structural findings
to stdout. It is GET-only, sequential, identifies itself honestly as
`OpportunityRadarAI-AccessAudit/1.0`, and obeys `robots.txt` under an explicit
status policy: 200 is parsed and obeyed; 404/410 means the file is absent so no
explicit rule applies (a statement about robots.txt alone, **not** permission of
any kind); 401/403/407/429 means we were *refused* the file, and an unknown rule
is never read as a permissive one, so the audit stops before requesting anything
else. It stops at any wall — 403, 429, a CAPTCHA or bot challenge, a login
redirect — and reports it. No browser automation, no browser impersonation, no
proxy, no CAPTCHA handling, no authentication, no user cookies, no private API
taken from a JS bundle.

**It never follows a redirect off the Stagiaires.ma hosts.** Redirects are
resolved by the audit itself rather than by the HTTP client, because a client
told to follow them would take a same-host URL's `302 Location:
https://elsewhere/…` and issue a GET to a host the audit never chose to talk
to — the same-host rule would then hold only until a site decided otherwise.
Resolution is bounded to a few hops and only ever stays on the same host; an
off-domain `Location` is recorded as a finding and its target is left
unfetched.

There is also **no flag that names a URL**. An earlier `--sitemap-url` override
was removed: it let an operator point the audit at an arbitrary address, which
turns "the official discovery chain" into "whatever was typed". The sitemap is
whatever `robots.txt` declares, or the audit stops.

The terms check is deliberately incomplete: **no terms URL is hardcoded**,
because none has been evidenced. Inventing a plausible-looking
`/conditions-generales` would be exactly the fabrication this evaluation layer
exists to prevent. An operator who can see the site passes the real URL with
`--terms-url`; until then the report says the check was not attempted and why.
When a URL *is* given, the audit records its status and reachability and reads
nothing: `manual_review_required` is unconditionally true. A public HTTP 200 is
reachability, not permission, and this project makes no legal claim in either
direction.

### What 7C.4A is not

* **not production coverage** and **not an active collector.** There is no
  `config/sources.yaml` row, no `SourceConfig` type, no collector, no factory
  registration, no `RadarAgent` run, no database write, no migration, no
  scheduler, no endpoint and no frontend change for Stagiaires.ma. A test walks
  `services/`, `config/`, `migrations/` and `apps/` and fails if the word
  appears in any of them;
* **not proof of recall.** The sitemap is what the site chose to publish there.
  How much of Stagiaires.ma's real content it covers is unmeasured;
* **not proof that `lastmod` is a publication date.** See above;
* **not a Data & AI filter.** No filtering of any kind is implemented here;
* **not legal permission.** It records what public GETs returned, no more.

### Tests, and why they are offline

`tests/unit/test_stagiaires_access_audit.py` is entirely offline: every byte it
parses is a tiny synthetic fixture written in the test file. A test that needs
the real site fails when the site is slow, when a marketing team edits a
template, or when CI has no egress — none of which says anything about this
code. The fixtures imitate the *shapes* the audit must survive (a namespaced
sitemap, a `<lastmod>`, an off-domain `<loc>`, a malformed ID, a JSON-LD
`JobPosting`), never real Stagiaires.ma content. No observed count is asserted
anywhere: live numbers change, and a test pinned to today's total would be a
false failure tomorrow.

## Validation

`validator.py` is offline and deterministic. It opens no socket — no
`requests`, no `urllib` retrieval, no Playwright, no Selenium — because a
benchmark whose validity depends on the network fails for reasons that have
nothing to do with the benchmark. Live reachability checking is a later slice.

It enforces:

* the JSONL parses, one JSON object per non-blank line;
* every record has exactly the declared field set — no missing key, no extra;
* `benchmark_id` is a non-empty trimmed string, and unique across the file;
* `source_url` is an absolute `http`/`https` URL;
* `official_application_url` is null or an absolute `http`/`https` URL;
* `country_code == "MA"`;
* `historical_or_live` ∈ {`HISTORICAL`, `LIVE`};
* `source_authority` ∈ {`OFFICIAL`, `JOB_BOARD`, `AGGREGATOR`, `LINKEDIN_ALERT`};
* `expected_opportunity_type` is a member of the shared `OpportunityType`
  registry;
* `expected_data_ai` ∈ {`true`, `false`, `null`};
* `published_at` is null or ISO `YYYY-MM-DD`; `observed_at` is ISO
  `YYYY-MM-DD` and is not before `published_at`;
* `pfe_cohort_year` is null or a plausible integer year — validated, never
  derived: nothing infers a cohort from `published_at`;
* no record reads as synthetic (placeholder words in identity fields,
  placeholder URL hosts);
* the manifest's `current_rows` equals the number of rows in the JSONL;
* `evaluation_ready` is false while `current_rows < target_minimum_rows`.

For the source map it enforces the closed registries for `source_class`,
`priority`, `coverage_role`, `collection_strategy`, `integration_status` and
`homepage_url_status`; `country == "MA"` on every entry; unique ids;
`is_production_registry` false; that a `LINKEDIN_ALERT` source is `GMAIL_ALERT`
and nothing else; that a null `homepage_url` is declared `UNKNOWN` rather than
left ambiguous; and that an entry may call itself `ACTIVE` only when
`config/sources.yaml` really configures the `production_source_id` it names,
with that source both `enabled` and of `status: active` — the `RadarAgent`'s
own eligibility rule — and, for a `GMAIL_ALERT` entry, of type
`gmail_linkedin_alert`.

## Out of scope in 7C.1, on purpose

* **No new collector or source type.** `greenhouse` and `gmail_linkedin_alert`
  remain the only supported types; the factory, the collectors, the parsers and
  the `RadarAgent` are untouched.
* **No migration.** The last migration is still `0024`. The benchmark and the
  map are versioned files, not tables: a benchmark in the operational database
  is one `JOIN` away from becoming an opportunity.
* **No write to the operational database.** Nothing in this slice inserts into
  `opportunities` or `opportunity_sources`, and no script here opens
  `data/opportunity-radar.db`.
* **No Phase 8 Data & AI behaviour**, no freshness or catalogue-cleanup rules,
  no frontend and no API endpoint.

## Backlog (numbers are the architect's to fix)

* **7C.2A** — LinkedIn Gmail intake audit. Implemented, above: read-only
  against Gmail, persists nothing it reads, scrapes no LinkedIn page. There is
  no historical replay and there will not be one — the dedicated account is new
  and the personal account is out of scope.
* **7C.2B** — the operational follow-up: correcting the production LinkedIn
  Gmail query, and exercising Gmail → parser → operational database. Neither is
  implemented here.
* **7C.3** — ReKrute source feasibility evaluation. **Complete, and the answer
  was no.** ReKrute is evaluated and deliberately **not selected** for
  production; the source map records it as `NOT_SELECTED`, `P2`, `AUDIT`,
  `MANUAL_BENCHMARK`, with a null `production_source_id`. There is no 7C.3B.

  What a local GET-only, bounded, robots-obeying audit actually observed:
  `robots.txt` returned HTTP 200 and did **not** disallow the target path; the
  public terms page returned HTTP 200; and an automated GET of the PFE listing
  target `/emploi-PFE` returned **HTTP 403** with a 14-byte body, while the
  same page is visible to a human in an ordinary browser. No bypass was
  attempted or implemented — no browser impersonation, Selenium, Playwright,
  proxy rotation, CAPTCHA handling, authenticated access, user cookies or
  private API.

  This is **not** a finding that ReKrute prohibits automated access, and no
  legal claim is made in either direction: robots did not disallow the path,
  and the terms were not read as a permission and still require human review
  (the audit's keyword indicators were all false, and their absence is not
  permission). The narrower, sufficient fact is that honest automated access to
  the listing is refused today.

  The decision itself is a product one: ReKrute is a general employment board
  rather than a PFE/stage-specialised source, so its expected PFE coverage
  benefit does not justify further integration effort for a product narrowed to
  Morocco PFE/stage. The investigation's parser and audit code was removed
  rather than merged, since carrying source-specific code for a source we will
  not collect is dead weight; the evaluation and its evidence live in the
  source map and in `docs/sources.md`. Nothing exists for ReKrute in
  `config/sources.yaml`, `SourceConfig`, the collector factory, the database,
  migrations or any scheduler. Reconsider only if an official or public
  integration channel suitable for this purpose becomes available, or if the
  product scope changes.
* **7C.4A** — Stagiaires.ma public access and sitemap/detail-structure
  feasibility audit. Implemented, above: read-only, GET-only, robots-obeying,
  bounded at three detail pages, writes nothing and activates nothing. The
  source map records Stagiaires.ma as `P1` / `PRIMARY` / `FUTURE_COLLECTOR` /
  `CANDIDATE` with a null `production_source_id`, and its `homepage_url_status`
  is now `EVIDENCED` because a real bounded audit reached the host.
* **7C.4B** — a Stagiaires.ma collector, **only after the Architect has
  validated 7C.4A**. Not started, and deliberately not begun inside the audit:
  a collector, a `SourceConfig` type, a factory registration and a
  `config/sources.yaml` row are that slice's work and must be reviewed as such.
* **7C.5** — Stage.ma collector.
* **7C.x** — official ATS integration, where a reusable ATS is confirmed to
  exist.

Growing the benchmark toward `target_minimum_rows` runs alongside all of these
and gates every recall number any of them might want to report.
