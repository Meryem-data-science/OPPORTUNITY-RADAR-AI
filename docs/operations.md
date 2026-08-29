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

A scanned CV is refused rather than guessed at: `cv-parser-v2` performs no OCR.

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

This command still writes nothing. Importing those candidates as reviewable
proposals is a separate, explicit command — see
[CV review](#cv-review-phase-33b) below — and even that accepts nothing on its
own. The advanced business normalization of institutions, employers, dates and
canonical roles does not exist, so Phase 3.2 is still not a validated Master CV;
the one normalization that does exist, for skills, reads accepted facts rather
than candidates — see [Profile skills](#profile-skills-phase-34a) below.

## Profile facts (Phase 3.3A)

Migration `0007` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `profile_facts` and `profile_fact_provenance` and inserts no row.

This slice is persistence and its validation rules, nothing else: no page, no
HTTP route and no authentication. The command that reviews facts is Phase 3.3B
and is described in [CV review](#cv-review-phase-33b) below; outside it, the
only way to record or decide a fact is to call
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
it opens no network connection and calls no model. Skill aliases are resolved
by the Phase 3.4A projection built on top of these facts, which reads them and
never writes one; no matching of any kind exists.

## CV review (Phase 3.3B)

Import one local CV PDF as reviewable proposals, then decide about each of them
by hand:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf
```

The command runs the whole path in one go — parse the PDF, extract the
candidates, map each one to a profile fact type, import them as `PROPOSED`, then
ask you about every one that is still undecided. It reuses migration `0007` and
adds no table, no migration and no second registry.

It asks for the address without echo, so it never enters the shell history;
`--email` stays available for tests and automation. The profile must already
exist — create it with `python -m services.digital_twin.cli init-profile` first.
This command never creates a user or a profile, and it refuses a non-SQLite
backend before it connects and before it asks for anything.

**No candidate is ever accepted automatically.** Every fact the import creates
is `PROPOSED`, and it stays there until you answer. There is no accept-all, no
yes-to-all, no auto-accept, no confidence and no threshold, and no flag adds
one.

For each undecided proposal the command shows the fact type, the value, and the
provenance you need to judge it — the pages, the section and the rule that
produced it — and then asks:

| Answer | Effect |
| --- | --- |
| `a` | Accept. The fact becomes `ACCEPTED`; that, and only that, means verified. |
| `r` | Reject. The fact becomes `REJECTED` and the row is kept, so the refusal stays auditable. |
| `c` | Correct. You type the right value; it becomes a new `ACCEPTED` fact with `USER_INPUT` evidence, and the old fact becomes `CORRECTED` and keeps its own value. |
| `s` | Skip. Nothing is written; the fact stays `PROPOSED`. |
| `q` | Quit. The remaining facts stay `PROPOSED`. |

Those five answers are matched **exactly**, in lower case and with nothing
around them: `A`, `a ` and ` a` are not the answer `a` and accept nothing. Any
other input prints a notice and asks again, so no other character decides
anything, an empty correction decides nothing either, and a closed stdin is a
quit rather than an implicit acceptance.

Quitting is safe and re-running is expected. The import is idempotent on the
CV's own evidence, so a second run on the same file proposes nothing new,
resumes where you stopped, and never offers a fact you already accepted,
rejected or corrected. A run interrupted partway leaves every candidate it did
import complete, with its provenance.

**This is the one command in the project that prints CV content**, because a
person cannot decide about a value they are not shown. That exception is bounded
to the review prompt itself. The closing summary is counters only —
`candidates`, `newly_proposed`, `already_imported`, `accepted_this_run`,
`rejected_this_run`, `corrected_this_run`, `skipped_this_run`,
`remaining_proposed`, `quit_requested` — the structured log carries counters,
versions, canonical types and ids and never a value, the CV path is never echoed
back, not even in an error message, and the command writes no file at all. Keep
your real CV out of the repository.

The failures it can report are the Phase 3.2A parser failures in the table
above, plus a refused backend and a missing profile, each with exit code 1.

What this command does **not** do: it accepts nothing on its own, it generates
no Master CV PDF, cover letter, application or CV adaptation, it derives no
skill level, alias, employer, institution, date or canonical role, and it
computes no eligibility, match, ranking or score. It opens no network connection
and calls no model, and there is no web interface or HTTP endpoint for any of
it. Skill aliases are resolved by the separate projection below, from facts
this review has already accepted.

## CV reconciliation (Phase 3.3C)

Use this when a **newer parser or extractor** re-reads a CV that has already
been imported and reviewed. Importing it again with the review command would
propose the whole document a second time, because the import is idempotent on
the evidence and a new campaign is new evidence. This command lines the two
campaigns up instead:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db

# 1. look, without writing anything
python -m services.digital_twin.cv.reconciliation_cli plan /path/to/cv.pdf

# 2. attach the new evidence, propose what changed
python -m services.digital_twin.cv.reconciliation_cli prepare /path/to/cv.pdf

# 3. answer the proposals by hand
python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf

# 4. retire the old readings the confirmed new ones replaced
python -m services.digital_twin.cv.reconciliation_cli finalize /path/to/cv.pdf
```

It reuses migration `0007` and adds no table and no migration. It asks for the
address without echo, so it never enters the shell history; `--email` stays
available for tests and automation. The profile must already exist, this
command creates no user and no profile, and it refuses a non-SQLite backend
before it connects and before it asks for anything.

The older campaign is named by two options, which default to the campaign this
project moved off:

| Option | Default |
| --- | --- |
| `--old-parser-version` | `cv-parser-v1` |
| `--old-extractor-version` | `cv-candidates-v1` |

Both are technical version strings and never CV content. The command also
refuses to run if the checkout does not itself produce a *newer* campaign than
the one named, so a stale reading can never retire a good fact.

**What each command does.** `plan` writes nothing at all. `prepare` attaches the
current campaign's provenance to every reading that is unchanged — same fact
type, same value, byte for byte — so those facts keep their id, their value and
the decision already taken about them, and creates a `PROPOSED` fact for every
reading that changed. `finalize` marks the superseded old readings `REJECTED`.

**Nothing is ever accepted automatically, and nothing is ever deleted.** Every
changed reading is `PROPOSED` until you answer it in the review command, and
`finalize` refuses to run at all while any of them is still `PROPOSED`, was
`REJECTED`, is missing, or is `CORRECTED` with a replacement nobody accepted —
it names them by fact type, id and reason, and rejects no old fact in that
case. Retiring a reading is a status change: the row, its value, its
`normalized_value` and its own campaign's provenance all stay, so both
segmentations of the document remain auditable side by side.

Re-running any of the three is expected and safe. A second `prepare` attaches
nothing and proposes nothing; a second `finalize` rejects nothing and reports
`changed_anything=false`. A run interrupted partway is resumed by running it
again.

The output is counters, canonical fact types, version strings and the document
digest, on stdout and in the structured log alike:

| Reported by | Counters |
| --- | --- |
| all three | `historical_facts`, `new_candidates`, `unchanged_candidates`, `changed_candidates`, `superseded_old_facts`, and the same three broken down by fact type |
| `prepare` | `provenance_attached`, `provenance_already_present`, `newly_proposed`, `already_proposed`, `pending_review` |
| `finalize` | `resolved_changed`, `newly_rejected`, `already_rejected`, `left_terminal_corrected`, `left_undecided`, `changed_anything` |

**This command prints no CV content at all** — no value, no `raw_text`, no
name, employer, school, project or skill mention — and there is no flag that
would print one; the review command above is the only place values are shown.
The CV path is never echoed back either, not even in an error message.

What it does **not** do: it accepts nothing, it deletes no fact and no
provenance row, it rewrites no value, it runs no migration, and it never
touches a skill, opportunity or matching row — reconciling non-`SKILL` readings
leaves the accepted `SKILL` facts, and therefore the projection below, exactly
where they were. Run that projection yourself afterwards if a `SKILL` reading
did change. It opens no network connection and calls no model.

## Profile skills (Phase 3.4A)

Migration `0008` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `skills`, `profile_skills` and `profile_skill_evidence` and inserts
no row. No vocabulary is seeded: a skill exists because an accepted fact named
it.

Project the accepted skill facts of one existing profile onto normalized
skills:

```bash
python -m services.digital_twin.skills.cli sync
```

The command asks for the address without echo, so it never enters the shell
history; `--email` stays available for tests and automation. The profile must
already exist — create it with `python -m services.digital_twin.cli init-profile`
first. This command creates no user and no profile, and it refuses a non-SQLite
backend before it connects and before it asks for anything.

**It decides nothing.** Every fact it reads was already accepted by a person in
the [CV review](#cv-review-phase-33b); a fact nobody accepted is invisible to
it. It reads `fact_type = 'SKILL' AND status = 'ACCEPTED'` and nothing else, so
a `PROPOSED`, `REJECTED` or `CORRECTED` fact and an `ACCEPTED` fact of any other
type never reach a skill, and it never writes to `profile_facts` or
`profile_fact_provenance`.

**No level is inferred, from anything.** There is no proficiency, score,
confidence or seniority in the schema or in the output, and several accepted
facts naming one skill are several *evidences* of one association rather than
"more" of that skill. A mention like `Azure Data Platform (avancé)` is one
skill whose name still carries the parenthesis.

Re-running is expected and is the normal way to apply a decision. The run is a
reconciliation, not an append: associations still justified keep their ids and
timestamps, missing ones are created, evidence whose fact was corrected or
rejected is dropped, and an association left with no evidence at all is
deleted. A second run on unchanged facts writes nothing and reports
`changed=false`.

The output is counters and a version, and it names **no skill**:

| key | meaning |
| --- | --- |
| `verified_skill_facts` | how many `ACCEPTED` `SKILL` facts the run read |
| `profile_skills` | how many skills the profile holds afterwards |
| `skills_created` | new canonical rows added to the shared vocabulary |
| `profile_skills_created` / `profile_skills_removed` | associations added / dropped |
| `evidence_created` / `evidence_removed` | proofs added / dropped |
| `normalizer_version` | `skill-normalizer-v1` |
| `changed` | `false` when the run found the projection already correct |

Unlike the CV review, this command prints no skill value at all — not on
stdout, not in the structured log, not in an error message — and it has no flag
that would print one. Reading the projection back is a Python call against a
database you name explicitly.

The failures it can report are a refused backend and a missing profile, each
with exit code 1.

What this slice does **not** do: no administrable alias table, no structured
education, certification, language, preference, availability, mobility or
career objective; no opportunity constraint, no eligibility rule, no skill
extraction from an offer, no TF-IDF, no cosine similarity, no matching, no
match score, no ranking, no recommendation, no notification, no CV adaptation
and no auto-apply. Structured experiences and projects are the separate
Phase 3.4B1 projection below, which this command never runs and never reads.
It opens no network connection and calls no model, and there is no web
interface or HTTP endpoint for any of it.

## Structured profile entries (Phase 3.4B1)

Migration `0009` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `profile_experiences` and `profile_projects` and inserts no row.

Project the accepted experience and project facts of one existing profile onto
structured rows:

```bash
python -m services.digital_twin.structured_profile.cli sync
```

The command asks for the address without echo, so it never enters the shell
history; `--email` stays available for tests and automation. The profile must
already exist — create it with `python -m services.digital_twin.cli init-profile`
first. This command creates no user and no profile, it runs no migration, and
it refuses a non-SQLite backend before it connects and before it asks for
anything.

**It decides nothing.** Every fact it reads was already accepted by a person in
the [CV review](#cv-review-phase-33b); a fact nobody accepted is invisible to
it. It reads `fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'`
and nothing else, so a `PROPOSED`, `REJECTED` or `CORRECTED` fact and an
`ACCEPTED` fact of any other type never reach a row, and it never writes to
`profile_facts` or `profile_fact_provenance`.

**Nothing is invented, and a `NULL` is worth more than a guess.** A fragment is
written only where the document itself delimited it: a pipe header for an
experience, a colon after an optional list marker for a project, a period only
in a closed set of explicit temporal forms. Where the wording is not
unambiguous the fragment stays `NULL` and the row records `UNPARSED_V1` — the
accepted fact is still projected, never dropped. No employer is deduced from a
sentence, no role from a technology, no seniority from the word "stage", no
duration, no calendar date from a school year, no skill from a project
description, and no level of any kind.

**It never touches the skills.** `profile_skills` and `profile_skill_evidence`
are left exactly where the [skill projection](#profile-skills-phase-34a) put
them; this command imports no skill module and infers no skill from an
experience or a project.

Re-running is expected and is the normal way to apply a decision. The run is a
reconciliation, not an append: rows the current rules would write identically
keep their ids and timestamps, missing ones are created, a row whose fact was
corrected or rejected is dropped, and a row the rules now read differently is
replaced whole. A second run on unchanged facts writes nothing and reports
`changed=false`.

The output is counters, rule tallies and a version, and it names **no value**:

| key | meaning |
| --- | --- |
| `accepted_experience_facts` / `accepted_project_facts` | how many `ACCEPTED` facts of each type the run read |
| `experience_rows` / `project_rows` | how many rows each table holds afterwards; always equal to the counts above |
| `structured_experiences` / `unparsed_experiences` | how many experience rows a closed rule named, and how many stayed `UNPARSED_V1` |
| `structured_projects` / `unparsed_projects` | the same tally for projects |
| `created` / `removed` | rows added / dropped, a replacement counting as one of each |
| `structurer_version` | `structured-profile-v1` |
| `changed` | `false` when the run found the projection already correct |

An `unparsed` count is a property of how the document was written, never a
judgement about the person and never a score.

Like the skill command, this one prints no role, organization, period, title or
description at all — not on stdout, not in the structured log, not in an error
message — and it has no flag that would print one. Reading the projection back
is a Python call against a database you name explicitly.

The failures it can report are a refused backend, a missing profile and an
unmigrated database, each with exit code 1.

What this slice does **not** do: no structured education, certification or
language; no availability, mobility, preference or career objective; no
eligibility rule, no opportunity constraint, no skill inference, no skill
level, no matching, no match score, no TF-IDF, no cosine similarity, no
ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. It opens no network connection and calls no model, and there is no
web interface or HTTP endpoint for any of it.
