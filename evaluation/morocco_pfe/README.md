# Phase 7C.1 — Morocco PFE source map and gold benchmark foundation

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
claims a collector that is not configured and enabled there.

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
* `pfe_cohort_year` is null or a plausible integer year;
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
`config/sources.yaml` really configures and enables the `production_source_id`
it names.

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

* **7C.2** — LinkedIn Gmail coverage evaluation, including replay of historical
  2025/2026 job-alert mail. Any such replay must stay read-only against Gmail,
  write nothing to the production database, modify no message or label, and
  scrape no LinkedIn page. Not implemented here.
* **7C.3** — ReKrute collector.
* **7C.4** — Stagiaires.ma collector.
* **7C.5** — Stage.ma collector.
* **7C.x** — official ATS integration, where a reusable ATS is confirmed to
  exist.

Growing the benchmark toward `target_minimum_rows` runs alongside all of these
and gates every recall number any of them might want to report.
