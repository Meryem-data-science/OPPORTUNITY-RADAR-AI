# Operations

Phase 2 is an explicitly invoked local workflow. There is no scheduled or
continuous production execution and no automatic application behavior.

## Prepare local SQLite and Gmail

From the repository root:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db

export GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
export GMAIL_TOKEN_PATH=.secrets/gmail-token.json

python -m services.collector.cli.migrate_configured --apply
```

Create a Google Cloud OAuth 2.0 **Desktop app** client after enabling the Gmail
API. Put the downloaded client JSON and generated token in `.secrets/` (or
another ignored local directory); never commit credentials. The first Gmail use
starts local user authorization and requests exactly
`https://www.googleapis.com/auth/gmail.readonly`. Existing tokens with missing
or broader scopes are rejected. The client performs Gmail message `list` and
`get` operations only and does not change labels, messages, or read state.

The optional generic read-only diagnostic is:

```bash
export GMAIL_ALERT_QUERY='label:Opportunity-Radar'
python -m services.collector.cli.gmail_probe --limit 5
```

Its summaries omit full bodies and credentials. The operational LinkedIn source
instead takes its fixed query and message bound from `config/sources.yaml`.

## Run the radar

Run all enabled, active configured sources and then global qualification:

```bash
python -m services.collector.cli.run_radar --once --apply
```

`RadarAgent` handles each collection/persistence batch independently. A failed
source is reported but does not undo earlier successful source commits. Once the
complete source loop finishes, the agent runs exactly one qualification
reconciliation over the operational database. Qualification failure is reported
separately and makes the overall command fail without rewriting source results.
Neither this command nor any collector applies migrations implicitly.

For single-source debugging:

```bash
python -m services.collector.cli.run_radar \
  --once --apply --source linkedin_job_alert_email
```

`--source` is repeatable. Unknown, disabled, or inactive requested sources are
refused. The manual `collect_source`, `persist_source`, and
`list_opportunities` CLIs remain useful for bounded diagnostics, but
`run_radar` is the normal orchestration entry point.

The LinkedIn collector reads only alert email content. It never uses a LinkedIn
authenticated session or opens job pages. Gmail bodies, snippets, message and
thread IDs, and tracking data do not become persisted opportunity data.

## Qualification checks

Qualification normally runs automatically with `RadarAgent`. The standalone
write command remains explicit and local-SQLite-only:

```bash
python -m services.collector.cli.persist_qualifications \
  --database .data/opportunity-radar.db --apply
```

The read-only audit can compare persisted classifications with current rules:

```bash
python -m services.collector.cli.audit_qualification \
  --database .data/opportunity-radar.db --format human
```

The classifier is deterministic `qualification-rules-v1`; unchanged fingerprints
and versions are not rewritten. Geography is accepted as metadata and is not an
exclusion rule. Results are categorical qualification, not user-specific
matching or ranking.

## Cross-source duplicate review

Audit an existing SQLite file without changing it:

```bash
python -m services.collector.cli.audit_duplicates \
  --database .data/opportunity-radar.db --format human
```

The report's candidate classes are preliminary heuristics and never
`AUTO_MERGE`. Review workflow commands are available under:

```bash
python -m services.collector.cli.review_duplicates --help
python -m services.collector.cli.merge_duplicates --help
```

`review_duplicates scan` is dry-run unless explicitly given `--apply`; a human
must persist a `CONFIRMED_DUPLICATE` decision before a physical merge can pass
preflight. The merge command also requires an explicit canonical opportunity
and is dry-run unless explicitly authorized. Applied merges move source
occurrences transactionally, preserve a `merged_duplicate` tombstone and audit
history, and can be rolled back only when LIFO and drift checks consider that
safe. Similarity output alone never merges records.

## Run the API and web application

The API reads an existing migrated SQLite database. It does not run collection,
qualification, or migrations:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

Inspect a bounded response:

```bash
curl "http://127.0.0.1:8000/api/opportunities?limit=5"
```

`GET /api/opportunities` returns visible, active opportunity summaries and an
`original_url`, not the stored full description. It accepts limits from 1 to
100.

Inspect source health, which takes no parameter:

```bash
curl "http://127.0.0.1:8000/api/source-health"
```

`GET /api/source-health` returns one entry per known source — those configured
in `config/sources.yaml` and those already persisted — with the status and
metrics of each source's most recent run, plus `zero_result_streak`,
`anomaly_code` and `anomaly_message`. Unknown values come back as `null`, never
as zero, and a source that has never run comes back with a `null` status rather
than an invented one.

The read is strictly read-only: the database is opened `mode=ro` and set
`query_only`, so pointing `SQLITE_DATABASE_PATH` at a missing or not-yet-migrated
file answers `503` and leaves that path missing instead of creating an empty
database there. Source health reads the operational SQLite database only; with
`DATABASE_BACKEND=turso` it answers the same `503` without contacting anything.

In another terminal, start Next.js:

```bash
cd apps/web
OPPORTUNITY_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

`OPPORTUNITY_API_BASE_URL` is server-side and defaults to
`http://127.0.0.1:8000`. The home page fetches the API without caching, displays
the real results, and uses “Voir l’offre originale” links to open their
`original_url`. This is a local integration, not a production deployment.

`/source-health` renders the source health entries as a table of Source,
Activée, Dernière exécution, Statut, Éléments trouvés, Nouveaux éléments,
Éléments pertinents and Erreurs. It displays the backend's own verdict: the
streak and the anomaly are decided by the API, and the page never recomputes
them. A `FAILED` run shows its `error_type` and its already-redacted
`error_message`; a repeated-zero anomaly shows the deterministic message the
backend produced. Reading the page changes nothing and alerts no one — no email,
no push, no scheduled check.

The separate Next.js `/health` and `GET /api/health` surfaces retain the earlier
read-only Turso/Foundation experiment. They are not the opportunity-data path
and do not imply that remote Turso writes are supported.

## Configuration and database boundary

Important process variables are:

- `OPPORTUNITY_RADAR_ENV`: `development` (default), `test`, or `production`;
- `DATABASE_BACKEND`: use `sqlite` for the Phase 2 operational workflow;
- `SQLITE_DATABASE_PATH`: operational SQLite path (the development default is
  `./data/opportunity-radar.db`);
- `GMAIL_OAUTH_CLIENT_SECRET_PATH`: ignored local Desktop OAuth client JSON;
- `GMAIL_TOKEN_PATH`: ignored local user OAuth token JSON;
- `GMAIL_ALERT_QUERY`: optional query for the generic `gmail_probe` only;
- `OPPORTUNITY_API_BASE_URL`: server-side Next.js URL for FastAPI.

Settings loading does not read `.env` files. Secrets must come from the process
environment and local ignored files, never source control. Structured Python
logs redact common credential-shaped values, but callers must still avoid
putting sensitive values into log messages.

Persistent SQLite is operational. Turso/libSQL is retained only for read-only
Foundation experiments where applicable; remote opportunity, qualification,
duplicate-decision, and merge writes must not be enabled or recommended.

## Validation commands

```bash
pytest

cd apps/web
npm test
npm run lint
npm run build
```

Live Gmail and source tests are opt-in because they require local credentials or
network state. The repository test suite skips them unless their documented
`RUN_LIVE_*` flags are deliberately supplied.

The final disposable Phase 2 validation on 2026-08-12 exercised three real
sources, 373 opportunities and qualifications, 373 classifier comparisons with
zero mismatches, 37,711 cross-source pairs with zero retained candidates in that
dataset, and the FastAPI-to-Next.js display/original links. Those values are a
dated snapshot, not stable production counts because upstream sources change.

## CV parser (Phase 3.2A)

Parse a local CV PDF without writing anything anywhere:

```bash
python -m services.digital_twin.cv.cli parse /path/to/cv.pdf
```

The default output is privacy-safe: parser version, the file's SHA-256, the
page count, the empty pages, the canonical section types found, and the
warnings. It prints no CV text, no heading as written, and therefore no name,
email address, phone number or postal address — and neither does the structured
log event, which carries counts, canonical types and warning codes only. The
file path is never echoed back, not even in an error message, because a CV
filename usually carries the person's name.

Add `--json-out <path>` to write the detailed result — page texts and section
contents included — to a path you name explicitly. Nothing is written without
that flag, an existing file is never overwritten, and the destination belongs
outside the repository: keep your real CV and any detailed export out of git.

The command needs nothing but the file. No database connection, no environment
variable, no network access, no OCR engine and no API key are involved.

Expected failures, each reported explicitly with exit code 1:

| Situation | Error |
| --- | --- |
| The path does not exist | `PdfFileNotFoundError` |
| The path is a directory | `PdfNotAFileError` |
| The file is empty, or not a readable PDF | `InvalidPdfError` |
| The PDF is encrypted | `EncryptedPdfError` |
| The PDF has no text layer (a scan) | `EmptyPdfTextError` |

A scanned CV is refused rather than guessed at: `cv-parser-v1` performs no OCR.

What this command does **not** do: it stores no `profile_facts`, writes no row
in the Digital Twin, creates no validated Master CV, offers no accept, correct
or reject workflow, derives no skill or skill level, and computes no match,
eligibility or score. Its output is a description of a document, not a set of
verified facts about a person.

## CV candidate extraction (Phase 3.2B)

Read the unverified candidates one local CV PDF yields, still without writing
anything anywhere:

```bash
python -m services.digital_twin.cv.candidates.cli extract /path/to/cv.pdf
```

The default output is privacy-safe: the extractor version, the parser version,
the file's SHA-256, how many candidates of each type were produced, the rule
identifiers that produced them, and the warnings. It prints no candidate text,
so no name, email address, phone number, URL, employer, school or skill mention
reaches the terminal. The file path is never echoed back, not even in an error
message.

Add `--json-out <path>` to write the detailed candidates — `raw_text` included —
to a path you name explicitly. The guarantees are the ones the parser CLI
already makes and are implemented once, in `services/digital_twin/cv/export.py`:
nothing is written without the flag, an existing file is never overwritten, the
file is created with mode `0600`, and no path appears in an error. The
destination belongs outside the repository.

The command reads one PDF and returns. It opens no database connection, writes
no row, applies no migration, needs no environment variable, no network access
and no API key, and calls no model. The failures it can report are exactly the
Phase 3.2A parser failures in the table above, because it parses the file first.

What a candidate is: one named deterministic rule found this text on this page
of this section. What it is **not**: a fact about the person. Nothing here is
verified, accepted, corrected or rejected, no candidate carries a level, a
proficiency, a confidence or a score, and the result lives in memory until you
export it yourself.

Phase 3.3A now provides a `profile_facts` table (see the next section), but
this command does not write to it: no candidate is imported, automatically or
otherwise, and there is no review workflow that would let you accept one. That
import is Phase 3.3B, the advanced business normalization is Phase 3.4, and
neither exists, so Phase 3.2 is still not a validated Master CV.

## Profile facts (Phase 3.3A)

Migration `0007` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `profile_facts` and `profile_fact_provenance` and inserts no row.

**There is no command, interface or endpoint for reviewing facts.** This slice
is persistence and its validation rules, nothing else: no CLI subcommand, no
page, no HTTP route, no authentication, and no import of the Phase 3.2B CV
candidates. Extracting a CV and recording what it found are still two separate
things, and nothing connects them — that connection is Phase 3.3B. The only way
to record or decide a fact today is to call
`services/digital_twin/facts/repository.py` from Python against a database you
name explicitly.

What the operations layer guarantees:

- a fact is created only together with its evidence, in one transaction; if the
  evidence is refused, no fact is left behind;
- a fact is verified when, and only when, its status is `ACCEPTED`.
  `list_verified_profile_facts` returns those and nothing else, filtering in
  SQL, so a proposal, a rejection or a correction cannot leak into a later
  reading;
- a correction never overwrites. It writes a new `ACCEPTED` fact with a
  `USER_INPUT` provenance, marks the previous one `CORRECTED` and links the two,
  in one transaction that either commits whole or rolls back whole, so the
  previous value is still readable afterwards;
- accepting an accepted fact and rejecting a rejected one are no-ops that keep
  the original decision date; every other move raises an explicit error;
- every mutation is scoped by `profile_id`, so a fact id belonging to another
  profile is reported as missing rather than changed.

The rules, the taxonomies and the exact schema are in
[database.md](database.md#profile-facts-and-their-provenance).

What this slice does **not** do: it generates no Master CV, cover letter,
application, form or CV adaptation; it computes no eligibility, match, ranking
or score; it derives no skill level, alias, employer, institution or date; and
it opens no network connection and calls no model. Phase 3.4 has not started
and no matching of any kind exists.
