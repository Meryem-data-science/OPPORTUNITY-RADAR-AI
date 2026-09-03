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

A scanned CV is refused rather than guessed at: `cv-parser-v5` performs no OCR.

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
by hand. There are two shapes of the same review — fact by fact, which is the
default and has not changed, and group by group, which is the same decisions
asked about a printed group at a time:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db

# one fact, one question, one keystroke — the original review
python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf

# the same review, one printed group at a time
python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf --grouped
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

### Reviewing by group (`--grouped`)

A real CV yields dozens of proposals, most of them skills, and answering each
one separately means typing `a` forty times at a list you took in at a glance.
`--grouped` changes how many facts one answer covers, and **nothing else**: the
same facts are asked about, the same decisions are available, and nothing is
accepted that you were not shown.

The review prints one group in full — its facts numbered, each with its value,
its normalized form where there is one, and its provenance — and then waits:

```text
GROUP 1/7 — IDENTITY / CONTACT
6 of 6 facts still PROPOSED

[1] NAME (fact_id=1)
    value: Example Person
    pages=1 section=UNCLASSIFIED#0 rule=HEADER_FIRST_LINE_NAME_SHAPE

[2] PROFESSIONAL_TITLE (fact_id=2)
    ...
```

| Command | Effect |
| --- | --- |
| `a` | Accept the facts of **this printed group** that are still `PROPOSED`. Nothing else, ever. |
| `a 1,3-5` | Accept only these facts of this group. Ranges are inclusive. |
| `r 2,4` | Reject only these facts of this group. Rejecting requires indices; there is no bare `r`. |
| `c 3` | Correct exactly one fact, as in the default review. Corrections are never batched. |
| `s` | Skip: every fact still `PROPOSED` in this group stays `PROPOSED`, and the next group is shown. |
| `q` | Quit. Everything still `PROPOSED` stays `PROPOSED`. |
| `?` | Print the commands. |

**`a` is not an auto-accept.** It accepts the facts of the group that is on
screen, all of which were printed before you typed it, and it can reach nothing
else: not a group that was not displayed, not a fact of another group, and not a
fact somebody already decided. There is no command that accepts the whole CV, no
yes-to-all, no accept-by-type, no score, no confidence and no threshold; the
largest scope any single command has is one displayed group. Accepting more than
twenty facts at once asks once more — `Accept all 42 facts still proposed in the
group above? [y/N]:` — and only an exact `y` confirms; anything else, an empty
line and a closed stdin included, decides nothing.

**A partial decision keeps you in the group.** After `r 17,21` the group is
printed again with those two marked as decided, so you can see what happened
before typing `a` for the rest. Refusing two skills out of forty-three and
accepting the other forty-one is two commands and one confirmation.

The groups are walked in a fixed order — identity/contact, education,
experience, projects, skills, certifications, languages, then anything else —
and inside a group the facts keep the document order the extraction produced.
A group is built from the canonical fact type alone: no value is ever read to
decide where a fact is shown, and a fact type this version does not know lands
in `OTHER` rather than disappearing.

Commands are matched **exactly**, like the five answers of the default review:
one action, optionally one space and one selection, and nothing else. `A`,
`a ` and `a  1` are not commands; `a banana`, `a 1--4`, `a 3-1`, `a 01` and
`c 1,2` are not commands either. An index that is out of range, or that names a
fact already decided, refuses the whole command rather than the part of it that
happened to be valid. Every refusal prints a notice and asks again, and **no
input error ever ends in an acceptance**.

A batch is atomic. `a` on a group of forty-two either moves all forty-two or
none of them: they are decided in one transaction, so an interruption partway
cannot leave you unable to tell what you actually answered.

The grouped run adds three counters to the closing summary — `groups_shown`,
`groups_completed`, `batch_actions` — next to the ones the default run prints.
They are counters like the others: no group carries a value into the summary or
the log.


**This is the one command in the project that prints CV content**, because a
person cannot decide about a value they are not shown. That exception is bounded
to the review prompt itself. The closing summary is counters only —
`candidates`, `newly_proposed`, `already_imported`, `accepted_this_run`,
`rejected_this_run`, `corrected_this_run`, `skipped_this_run`,
`remaining_proposed`, `quit_requested`, plus `groups_shown`, `groups_completed`
and `batch_actions` on a `--grouped` run — the structured log carries counters,
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

# 3. answer the proposals by hand (add --grouped to answer them by group)
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
project moved off — one step behind what the checkout produces, so they move
with each bump:

| Option | Default |
| --- | --- |
| `--old-parser-version` | `cv-parser-v4` |
| `--old-extractor-version` | `cv-candidates-v5` |

Reconciling from a campaign older than the defaults — `cv-parser-v1` and
`cv-candidates-v1`, say — means naming it explicitly; nothing else changes,
since how two campaigns are compared never depends on which versions they are.
Both are technical version strings and never CV content. The command also
refuses to run if the checkout does not itself produce a *newer* campaign than
the one named, so a stale reading can never retire a good fact.

For parser-v5/candidates-v6, equality remains exact: profile, CV digest,
canonical fact type, and raw value must match byte for byte. An unchanged
accepted reading keeps its human decision. A former `SKILL` now read as
`LANGUAGE` or `CERTIFICATION` is changed, becomes a new `PROPOSED` fact needing
human review, and never inherits the old decision; the wrong-type historical
fact remains auditable rather than being deleted.

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

What this slice does **not** do: no administrable alias table, no preference,
availability, mobility or career objective; no opportunity constraint, no
eligibility rule, no skill extraction from an offer, no TF-IDF, no cosine
similarity, no matching, no match score, no ranking, no recommendation, no
notification, no CV adaptation and no auto-apply. Structured experiences,
projects, education, certifications and languages are the separate Phase 3.4B
projection below, which this command never runs and never reads.
It opens no network connection and calls no model, and there is no web
interface or HTTP endpoint for any of it.

## Structured profile entries (Phase 3.4B1 and 3.4B2)

Migrations `0009` and `0010` are applied by the ordinary explicit command, like
every other one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

`0009` creates `profile_experiences` and `profile_projects`; `0010` creates
`profile_educations`, `profile_certifications` and `profile_languages`. Neither
inserts a row.

Project the accepted experience, project, education, certification and language
facts of one existing profile onto structured rows — one command, one
transaction, five tables, so a run never leaves a profile half-projected:

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
it. It reads
`fact_type IN ('EXPERIENCE', 'PROJECT', 'EDUCATION', 'CERTIFICATION',
'LANGUAGE') AND status = 'ACCEPTED'` and nothing else, so a `PROPOSED`,
`REJECTED` or `CORRECTED` fact and an `ACCEPTED` fact of any other type never
reach a row, and it never writes to `profile_facts` or
`profile_fact_provenance`.

**Nothing is invented, and a `NULL` is worth more than a guess.** A fragment is
written only where the document itself delimited it: a pipe header for an
experience or a diploma, a colon after an optional list marker for a project,
an explicit label for a certification, an explicit separator for a language,
and a period only in a closed set of explicit temporal forms. Where the wording
is not unambiguous the fragment stays `NULL` and the row records the type's
`*_UNPARSED_V1` rule — the accepted fact is still projected, never dropped.

Punctuation proves that segments exist; it never proves what they are about. So
a pipe-delimited diploma line becomes a school and a programme only when each
of the two roles carries its own exclusive proof in a closed registry — one
segment marked as an institution and not as a programme, the other marked as a
programme and not as an institution. Never by position, and whether the line
carries two segments or three. One marker is not enough: in
`University Diploma in AI | Sorbonne` the marked segment is the programme, so
both fields stay `NULL` rather than being filled the wrong way round. When both
proofs are there and no date was written, the row records
`EDUCATION_PIPE_INSTITUTION_PROGRAM_V1` and `period_text` stays `NULL`; when a
date is certain but a proof is missing, only the period is kept, with
`EDUCATION_PIPE_PERIOD_ONLY_V1` on the row. A language line has at most one
list marker removed before it is read, so `• Anglais : C1` stores `Anglais`
and never `• Anglais`. No employer is deduced
from a sentence, no role from a technology, no seniority from the word "stage",
no duration, no calendar date from a school year, no skill from a project
description, no diploma from a school, no `Bac+N` from the word "Master", no
obtained certification from a stated intention such as "préparation à" or
"objectif", no issuer, no obtention or expiry date, no language from a text
written in one, no CEFR level from "courant" or "fluent", and no level of any
kind.

**It never touches the skills.** `profile_skills` and `profile_skill_evidence`
are left exactly where the [skill projection](#profile-skills-phase-34a) put
them; this command imports no skill module and infers no skill from an
experience, a project, a diploma, a certification or a language.

Re-running is expected and is the normal way to apply a decision. The run is a
reconciliation, not an append: rows the current rules would write identically
keep their ids and timestamps, missing ones are created, a row whose fact was
corrected or rejected is dropped, and a row the rules now read differently is
replaced whole. A second run on unchanged facts writes nothing and reports
`changed=false`.

The output is counters, rule tallies and a version, and it names **no value**:

| key | meaning |
| --- | --- |
| `accepted_experience_facts` / `accepted_project_facts` / `accepted_education_facts` / `accepted_certification_facts` / `accepted_language_facts` | how many `ACCEPTED` facts of each type the run read |
| `experience_rows` / `project_rows` / `education_rows` / `certification_rows` / `language_rows` | how many rows each table holds afterwards; always equal to the counts above |
| `structured_experiences` / `unparsed_experiences` | how many experience rows a closed rule named, and how many stayed `UNPARSED_V1` |
| `structured_projects` / `unparsed_projects` | the same tally for projects |
| `structured_educations` / `unparsed_educations` | the same tally for education. `EDUCATION_PIPE_INSTITUTION_PROGRAM_V1` and `EDUCATION_PIPE_PERIOD_ONLY_V1` both count as structured: each named a fragment |
| `structured_certifications` / `unparsed_certifications` | the same tally for certifications |
| `structured_languages` / `unparsed_languages` | the same tally for languages |
| `created` / `removed` | rows added / dropped, a replacement counting as one of each |
| `structurer_version` | `structured-profile-v1` |
| `changed` | `false` when the run found the projection already correct |

An `unparsed` count is a property of how the document was written, never a
judgement about the person and never a score: a diploma nobody typed with pipes
is exactly as real as one that was.

Like the skill command, this one prints no role, organization, period, title,
description, institution, programme, certification, language or level at all —
not on stdout, not in the structured log, not in an error message — and it has
no flag that would print one. Reading the projection back is a Python call
against a database you name explicitly.

The failures it can report are a refused backend, a missing profile and an
unmigrated database, each with exit code 1.

What this slice does **not** do: no availability, mobility, preference or
career objective — those are [Phase 3.4C](#explicit-profile-input-phase-34c)
and come from the person, never from a document; no eligibility rule, no
opportunity constraint, no skill inference, no skill level, no study level, no
CEFR computation, no matching, no match score, no TF-IDF, no cosine similarity,
no ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. It opens no network connection and calls no model, and there is no
web interface or HTTP endpoint for any of it.

## Explicit profile input (Phase 3.4C)

Availability, mobility, preferences and career objectives. These four are the
things a CV cannot say, so **none of them is ever read from one**: they are
typed by the person, recorded as `USER_INPUT` facts, and projected.

Migration `0011` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `profile_availability`, `profile_mobility`, `profile_preferences`
and `profile_career_objectives`, and inserts no row.

Four commands state the four things, one command re-derives the projection, and
one reports what is known:

```bash
python -m services.digital_twin.preferences.cli set-availability \
    --status AVAILABLE_FROM --available-from 2026-09-01
python -m services.digital_twin.preferences.cli set-availability --status AVAILABLE_NOW

python -m services.digital_twin.preferences.cli set-mobility --scope OPEN
python -m services.digital_twin.preferences.cli set-mobility --scope RESTRICTED \
    --location "Casablanca" --location "Rabat"

python -m services.digital_twin.preferences.cli set-preferences \
    --opportunity-type PFE --opportunity-type ALTERNANCE \
    --work-mode HYBRID --work-mode REMOTE \
    --preferred-domain "Data engineering" \
    --constraint "Pas de déplacement le week-end" \
    --convention-status AVAILABLE --visa-sponsorship-required NO

python -m services.digital_twin.preferences.cli set-career-objectives \
    --objective "Rejoindre une équipe data en alternance"

python -m services.digital_twin.preferences.cli sync
python -m services.digital_twin.preferences.cli status
```

Four commands rather than one, because the four are four statements: somebody
can say where they will go without having decided when they are free, and a
single command taking every flag at once would make each run look like a
statement about all four.

Each command asks for the address without echo, so it never enters the shell
history; `--email` stays available for tests and automation. The profile must
already exist — create it with `python -m services.digital_twin.cli init-profile`
first. These commands create no user and no profile, run no migration, and
refuse a non-SQLite backend before connecting and before asking for anything.

**Closed registries.** `--opportunity-type` is one of `PFA`, `PFE`,
`SUMMER_INTERNSHIP`, `PRE_HIRE_INTERNSHIP`, `ALTERNANCE`, `INTERNSHIP`,
`FIRST_JOB`, `JUNIOR_ROLE`; `--work-mode` one of `ON_SITE`, `HYBRID`, `REMOTE`;
`--convention-status` one of `UNKNOWN`, `AVAILABLE`, `NOT_AVAILABLE`;
`--visa-sponsorship-required` one of `UNKNOWN`, `YES`, `NO`. A value outside a
registry is refused, never mapped onto the nearest member. `UNKNOWN` is the
default for the last two and a perfectly valid answer.

**Free text stays the person's own.** Locations, preferred domains, constraints
and objectives are trimmed and exactly de-duplicated, and nothing else happens
to them: the case, the wording and the order are kept, nothing is geocoded,
expanded or corrected, and "Data" and "data" stay two entries because deciding
they are one would be fuzzy matching.

**Stating the same thing twice writes nothing.** Each domain is a singleton, so
a `set-*` call resolves to exactly one of `CREATED` (the first statement),
`UNCHANGED` (the same statement again — no second fact, no correction, not even
a touched timestamp) or `CORRECTED` (a different statement: a new `ACCEPTED`
fact, with the previous one kept as `CORRECTED` and pointing at its
replacement, so the whole chain stays readable). Restating a preference with
the same values typed in another order is `UNCHANGED`, because the encoding is
canonical.

**An absence is `UNKNOWN`, never a "no".** A person who never stated whether
they need visa sponsorship has no `PREFERENCE` fact saying they do not need
one: they have no statement at all. `status` reports that, and reports **no
value**:

```
availability=KNOWN
mobility=KNOWN
preferences=KNOWN
career_objectives=UNKNOWN
mobility_location_count=2
opportunity_type_count=3
work_mode_count=2
preferred_domain_count=2
constraint_count=1
career_objective_count=0
```

A `set-*` or `sync` run prints the reconciliation summary instead:

| key | meaning |
| --- | --- |
| `fact_type` / `action` / `fact_id` / `previous_fact_id` | on a `set-*` only: what was stated, whether it was `CREATED`, `UNCHANGED` or `CORRECTED`, the fact holding it now, and the one it replaced |
| `fact_changed` | on a `set-*` only: whether the statement moved at all |
| `accepted_availability_facts` / `accepted_mobility_facts` / `accepted_preference_facts` / `accepted_career_objective_facts` | how many `ACCEPTED` facts of each type the run read — 0 or 1; 2 is refused |
| `availability_rows` / `mobility_rows` / `preferences_rows` / `career_objectives_rows` | how many rows each table holds afterwards |
| `created` / `removed` | rows added / dropped, a replacement counting as one of each |
| `input_version` | `explicit-profile-input-v1` |
| `changed` | `false` when the run found the projection already correct |

Re-running is expected. The run is a reconciliation, not an append: a row the
current facts would write identically keeps its timestamp, a missing one is
created, a row whose fact was corrected or rejected is dropped, and a row the
facts now decode differently is replaced whole. A second run on unchanged facts
writes nothing and reports `changed=false`.

**Only what the person stated is projected.** The projection reads facts that
are `ACCEPTED`, of the right type, **and carry `USER_INPUT` provenance**. An
`ACCEPTED` fact of one of these four types evidenced only by a CV, a GitHub
page or a derivation from other accepted evidence is not a statement by the
person, so it is never projected and never corrected in place — the person's
first statement is a fact of its own.

**Two stated facts of one domain stop the run.** A person has one
availability, not two. If a domain ever holds two `ACCEPTED` facts the whole
synchronization is refused and rolled back — the other three domains included —
rather than the most recent one being picked: the newest statement is not the
truest one, and choosing between two things somebody said is not a decision
this command gets to make. Resolve it through the facts lifecycle first.

**It never touches the neighbours.** `profile_facts` and
`profile_fact_provenance` are read and never written by the projection, and the
skill, experience, project, education, certification and language rows are left
exactly where the earlier commands put them.

The command prints **no value read back out of the database** — no objective,
no constraint, no location, no domain, no availability date and no address —
not on stdout, not in the structured log, not in an error message, and it has
no flag that would print one. Reading the projection back is a Python call
against a database you name explicitly.

The failures it can report are a refused backend, a missing profile, an
unmigrated database, an invalid statement (a malformed date, a `RESTRICTED`
mobility naming nowhere, an empty objective, a value outside a registry) and an
ambiguous domain, each with exit code 1. An invalid statement is refused before
anything is opened, so nothing is written and no database is contacted.

What this slice does **not** do: no opportunity constraint is parsed or stored,
no eligibility rule, no `ELIGIBLE`/`NOT_ELIGIBLE` verdict, no comparison of a
profile's location or date against an offer's, no matching, no match score, no
TF-IDF, no cosine similarity, no ranking, no recommendation and no
notification. This is the profile side of Phases 3.5 and 3.6, and those are not
implemented. It opens no network connection and calls no model, and there is no
web interface or HTTP endpoint for any of it.

## Opportunity constraints (Phase 3.5A)

What a posting itself requires, extracted and projected. This reads listings;
it compares them to nobody.

Migration `0012` is applied by the ordinary explicit command, like every other
one:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `opportunity_constraints`, `opportunity_constraint_locations`,
`opportunity_education_requirements`, `opportunity_constraint_evidence`,
`opportunity_constraint_conflicts` and `opportunity_skill_requirements`, and
inserts no row.

Three commands:

```bash
python -m services.collector.extractors.opportunity_constraints.cli status
python -m services.collector.extractors.opportunity_constraints.cli sync
python -m services.collector.extractors.opportunity_constraints.cli \
    extract-one --opportunity-id 42
```

`status` reads what is stored and counts it; `sync` reconciles every posting in
scope — active, not a merged duplicate, the same filter the qualification
pipeline uses; `extract-one` does the same for a single posting, which is the
command to reach for when checking why one listing reads the way it does. Both
`status` and `sync` accept `--limit`. The commands run no migration and refuse
a non-SQLite backend before connecting.

**What it extracts.** The kind of opportunity (the Phase 3.4C registry, shared
with the profile side), the education levels named and whether they are floors,
**every** experience asked for — a posting wanting seven years of engineering
and two of ML states two requirements, not one contradiction — in months and
each with its own optional `REQUIRED`/`PREFERRED`, the duration, the start at the precision it was written, the places named, the work
mode, and what the posting says about visa sponsorship, work authorization and
internship agreements.

**What it refuses to extract.** Skills and languages — those are Phase 3.5B,
below.

**UNKNOWN is the answer whenever a posting was silent, and UNKNOWN is never a
"no".** A posting that never mentions visas has not refused to sponsor. A
posting with an address has not required attendance. A posting written in
English has not required English. A title reading "Senior" states no number of
years. An internship does not imply a school agreement, a duration or a
student. A salary paragraph saying "minimum and maximum target" states no
required experience. "This isn't a research internship", "prior internship
experience" and "mentoring junior consultants" describe something other than
this offer, and none of them sets its type. `#LI-Onsite`, "onsite solutions"
and "significant time on-site with customers" do not state a work mode: only a
declaration attached to the role does. A date written as `01/02/2027` is two different days depending on the
reader's country, so it is not read at all; a month with no year keeps no year,
never the current one.

**A PFE is an internship, named precisely.** A posting calling itself a
`stage` in one line and a `PFE` in another is not disagreeing with itself, so
the precise kind wins and nothing is recorded as a conflict. That one relation
holds between the generic `INTERNSHIP` and the four specific kinds — `PFE`,
`PFA`, `SUMMER_INTERNSHIP`, `PRE_HIRE_INTERNSHIP` — and nowhere else: a posting
claiming both a PFE and a PFA, or a PFE and an alternance, still conflicts.

**Contradictions stop an assertion rather than being settled.** "Fully remote"
three lines above "fully on-site" leaves `work_mode` unset and writes a row to
`opportunity_constraint_conflicts` naming both values and both rules. A conflict
is keyed by the slot that disagreed, so a posting asking for "minimum 3 years
required" and "at least 5 years preferred" records two conflicts — one about
the quantity, one about the obligation — instead of colliding on one row. The one
exception is the opportunity type, where the title outranks the description,
exactly as the Phase 2 classifier already decides it.

**Everything asserted is explainable.** Each value carries the rule that fired,
the field it was read from, and the minimal fragment matched — capped at 200
characters, so evidence stays a pointer into the posting rather than a copy of
it. There is no confidence and no score.

Re-running is expected and cheap. `extractor_version` is fixed by the code and
cannot be passed in: a label a caller could choose per run would stop naming
the rules that produced the rows. Idempotence is `(source_fingerprint,
extractor_version)`: an unchanged posting is skipped entirely — no delete, no
insert, no timestamp moved — an edited description is re-extracted whole, and a
new extractor version re-extracts even identical text. A second run reports
`changed=false`.

The output is counters and versions, and it names **no posting's words** — no
title, no description, no evidence fragment, no location — on stdout, in the
structured log or in an error message, and there is no flag that would print
one:

| key | meaning |
| --- | --- |
| `total_opportunities` / `processed` | postings in scope, and postings read |
| `unchanged` / `created` / `replaced` | skipped, first-projected, re-projected |
| `projected` / `not_projected` | `status` only: how many hold a stored reading |
| `known_opportunity_type` … `known_convention` | how many postings **stated** each thing. Never how many are suitable: there is nobody to be suitable for. `known_experience` counts postings holding at least one requirement, not requirements |
| `conflicts` | how many contradictions were recorded rather than settled |
| `extractor_version` | `opportunity-constraints-v3` |
| `changed` | `false` when the run found every projection already correct |

A `known_*` count is a property of how postings are written. A low
`known_convention` means employers rarely say it, not that agreements are
rarely needed.

The failures it reports are a refused backend, an unmigrated database, an
absent or out-of-scope posting, and `extract-one` called without
`--opportunity-id`, each with exit code 1.

What this slice does **not** do: no profile is read, no opportunity is compared
to one, no eligibility result, no `ELIGIBLE`/`NOT_ELIGIBLE` verdict, no
`match_score`, no TF-IDF, no cosine similarity, no ranking, no recommendation,
no notification and no auto-apply. It opens no network connection and calls no
model, and there is no web interface or HTTP endpoint for any of it.

### The table 3.5A reserves

`opportunity_skill_requirements` is created by `0012`, left empty by every 3.5A
run, and filled by Phase 3.5B below. It exists in `0012` only to pin the offer
side to the `skills` vocabulary Phase 3.4A created, so 3.5B extends one
catalogue instead of starting a second, incompatible one.

## Opportunity skill and language requirements (Phase 3.5B)

Which technologies and which languages a posting **explicitly asks for**, and
how hard it asks. This still reads listings and still compares them to nobody.

Migration `0013` is applied by the ordinary explicit command:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

It creates `opportunity_skill_requirement_evidence`,
`opportunity_language_requirements`,
`opportunity_language_requirement_evidence`,
`opportunity_requirement_ambiguities` and
`opportunity_requirement_extraction_state`, and **inserts no row** — not a
skill, not a language, not a catalogue entry.

Three commands, same shape as 3.5A's:

```bash
python -m services.collector.extractors.opportunity_constraints.requirements.cli status
python -m services.collector.extractors.opportunity_constraints.requirements.cli sync
python -m services.collector.extractors.opportunity_constraints.requirements.cli \
    extract-one --opportunity-id 42
```

`status` reads what is stored and counts it, writing nothing and extracting
nothing; `sync` reconciles every posting in scope — active, not a merged
duplicate, the same filter 3.5A and the qualification pipeline use;
`extract-one` does the same for a single posting. Both `status` and `sync`
accept `--limit`. The commands run no migration and refuse a non-SQLite backend
before connecting.

**Run Phase 3.5A first.** Every 3.5B row hangs off `opportunity_constraints`, so
a posting with no 3.5A projection is refused with an explicit error naming what
to do, before anything is written. 3.5B never runs the 3.5A sync for you: two
phases that trigger each other are two phases nobody can reason about
separately. A 3.5A re-synchronization also cascades the 3.5B reading away, so
the usual order after a collection is: `migrate`, then 3.5A `sync`, then 3.5B
`sync`.

**What it extracts.** A technology or a language named under a heading that
makes it a demand (`Required Qualifications`, `Must Have`, `Prérequis`, `Profil
recherché`) or a preference (`Nice to Have`, `Preferred Qualifications`,
`Atouts`), or named in a sentence that says so itself (`Must have…`, `…is
required`, `…is a plus`, `…preferred`). Each requirement carries the fragment
that stated it, the rule that fired, the heading it sat under, and the level
that mention stated. `REQUIRED` outranks `PREFERRED` when one posting says
both, and the weaker mention survives as evidence.

**Levels are read per clause.** "Python required and Spark preferred" stores one
demand and one preference, and "No Python experience required, but SQL is
required" stores SQL alone — one skill's cancelling words never delete another's
demand. Terms joined by a bare connector still share one level, so "Python and
SQL required" is two requirements.

**What it refuses to extract, and this is the point.** A mention is not a
requirement:

```text
Our stack includes Python, Spark and Kafka.   -> nothing
You will build pipelines using Python.        -> nothing
We use SQL across the company.                -> nothing
Training in Python will be provided.          -> nothing
No prior Python experience is required.       -> nothing
```

French prose is not a technology either: token boundaries are Unicode-aware, so
`Réseaux`, `Régression`, `Réalisation` and `Câblage` never produce the `R` or
`C` skills, in composed or decomposed spelling.

A slash is not an `or` either. `AI/ML engineering` and `ML/LLM-powered system`
name one field, so they are refused under their own reason,
`COMPOUND_SKILL_EXPRESSION_UNSUPPORTED`, rather than reported as a choice the
posting never offered — and neither half is stored. The registry of such
expressions is closed; `Python/R`, `C/C++` and `TensorFlow/PyTorch` stay refused
as choices. `Bilingualism (English/French)` names both languages, like
`Bilingual English/French`.

Identical refusals of one sentence are stored once: the row holds no terms, so a
second copy would carry nothing. Expect `ambiguity_rows` to stay substantial —
real postings really do offer choices, and refusing them is the point.

An `or` is not an `and`: "Python or R required" stores neither, and writes a
row in `opportunity_requirement_ambiguities` instead — so an operator can see
that the extractor understood an explicit demand and refused to corrupt it,
rather than finding a silence indistinguishable from a posting that never
mentioned either. "Python and SQL required" still stores both.

A language is never inferred from a country, a city, a nationality or the
language the advertisement is written in, and a level is stored exactly as
written: `B2` stays `B2`, `Fluent` stays `Fluent`, and nothing maps `Fluent` to
`C1`. When one posting demands one language at two incompatible levels, the
language stays `REQUIRED` and only the level becomes UNKNOWN, with a
`CONFLICTING_LANGUAGE_PROFICIENCY` ambiguity.

**The vocabulary grows only from what was observed.** A `skills` row appears the
first time a real posting is found to require that term, inside the same
transaction; an existing row is reused and never renamed; a term no posting
requires any more is left in place, because it is vocabulary and not a claim
about anybody.

**Idempotence.** `source_fingerprint` is over the description and nothing else,
and `extractor_version` is `opportunity-requirements-v3`. A second `sync` writes
nothing and reports `changed=false`, including for a posting that requires
nothing — `opportunity_requirement_extraction_state` records that it was read.
An edited description recomputes; a new extractor version recomputes even when
the text is identical.

**The output is counters.** No description, no title, no evidence fragment, no
heading, and not even the name of a technology or a language reaches stdout or
the structured log, and there is no flag that would print one. Inspecting a
particular reading is a read-only SQL query, not something a routine command
dumps. The failures reported are a refused backend, an unmigrated database, a
missing 3.5A projection, an absent or out-of-scope posting, and `extract-one`
called without `--opportunity-id`, each with exit code 1.

What this slice does **not** do: no profile is read — no query in it names
`profiles`, `profile_skills` or `profile_languages` — no opportunity is compared
to one, no eligibility result, no `ELIGIBLE`/`NOT_ELIGIBLE` verdict, no skill
gap, no `match_score`, no TF-IDF, no cosine similarity, no embedding, no fuzzy
matching, no ranking, no recommendation, no notification and no auto-apply. It
opens no network connection, downloads no model and calls no LLM, and there is
no web interface or HTTP endpoint for any of it.

## Eligibility (Phase 3.6)

The first slice that reads both sides of the database, and the only one that
may. It answers one narrow question about one person and one posting:

    given what this posting explicitly demands, and what this person's reliable
    facts state, is there a KNOWN reason they could not apply?

Three answers, and the distance between two of them is the whole point:

| verdict | means |
| --- | --- |
| `ELIGIBLE` | nothing known stands in the way |
| `INELIGIBLE` | a hard requirement was **contradicted** by a reliable fact |
| `UNKNOWN` | a hard requirement applies and the fact needed to answer is missing |

`ELIGIBLE` does not mean the employer will accept the candidate, and `UNKNOWN`
is a question rather than a soft refusal. A thin advertisement demanding nothing
this engine checks is `ELIGIBLE`, because nothing is known to stand in the way.

Apply the migration first, like every other:

```bash
python -m services.collector.cli.migrate --database data/opportunity-radar.db
# or, against the configured backend:
python -m services.collector.cli.migrate_configured --apply
```

It creates `opportunity_eligibilities` and `eligibility_rule_results`, and
**inserts no row**.

Three commands:

```bash
python -m services.eligibility.cli status --email you@example.com
python -m services.eligibility.cli sync   --email you@example.com
python -m services.eligibility.cli audit  --email you@example.com --show-reasons 20
```

`status` reads what is stored and counts it, deciding nothing and writing
nothing; `sync` brings every in-scope posting's decision up to date; `audit`
explains the refusals and the open questions and re-checks the phase's
invariants against the rows on disk. `status` and `sync` accept `--limit`. The
commands run no migration and refuse a non-SQLite backend before connecting.

**Run Phases 3.5A and 3.5B first.** A verdict over a posting whose requirements
nobody has read would be a verdict over silence, and silence and "requires
nothing" are exactly the two things `opportunity_requirement_extraction_state`
exists to tell apart — so a corpus with any unread posting is refused as one
error, before anything is written. The usual order after a collection is
`migrate`, 3.5A `sync`, 3.5B `sync`, then this.

**The person must already exist.** `--email` names a Digital Twin created by the
digital-twin commands; the eligibility run never creates one. Bringing a user
into existence as a side effect would produce an empty profile and then a page
of UNKNOWNs about it, which reads like an answer and is an accident.

### What can block, and what cannot

Six dimensions may produce a contradiction, and only on an explicit `REQUIRED`
demand contradicted by a reliable fact: **education**, **current enrolment**,
**experience**, **a mandatory language**, **work authorization / sponsorship**,
and **a mandatory internship agreement**.

Everything else is reported and decides nothing:

```text
a REQUIRED skill the profile does not hold  -> NOT_EVALUATED, never a refusal
a PREFERRED demand of any kind              -> NOT_EVALUATED
an ambiguity Phase 3.5B refused to store    -> NOT_EVALUATED
mobility, location, availability, duration,
start date, work mode                       -> NOT_EVALUATED
```

**A required skill never blocks, and that is deliberate.** "AWS, Azure or GCP"
written as three bullets leaves three `REQUIRED` rows that look like three
obligations and are one choice, and nothing in the stored shape tells them
apart. Rejecting a candidate over the two the posting did not insist on is the
one mistake this phase is built to avoid, so a skill is evidence and never a
verdict until alternative groups are representable without loss.

The invariants are also CHECK constraints in `0014`, so a row breaking one
cannot be stored at all — see
[database.md](database.md#opportunity-eligibility).

### What it compares, and what it refuses to compare

Only where both sides already speak one normalized vocabulary:

```text
CEFR written as CEFR         B2 vs C1   -> compared
a level written in words     "courant"  -> not comparable -> UNKNOWN
levels on one ladder         BAC_PLUS_3 vs BAC_PLUS_5 -> compared
levels across two ladders    BAC_PLUS_5 vs MASTER     -> not comparable -> UNKNOWN
```

No mapping turns `fluent` into `C1` or `native` into `C2`, here or anywhere in
this project, and no visa conclusion is reachable from a nationality, a country
of residence or a posting's location — none of those is an input to any rule.

Three dimensions currently answer UNKNOWN or NOT_APPLICABLE for every real
profile, and the `audit` command prints why:

* **education** — Phase 3.4 stores an education as institution, programme and
  period, verbatim, and normalizes no degree level;
* **current enrolment** — neither phase represents it, on either side;
* **experience** — Phase 3.4 normalizes no duration, and Phase 3.5 stores a
  quantity without the field it qualified, so "three years of data engineering"
  has nothing comparable to answer it.

None of the three is guessed at. A missing datum produces UNKNOWN, and the
engine's rules for all three are written and tested against the day the Digital
Twin records them.

### Idempotence

`input_fingerprint` is a SHA-256 over the canonical JSON of exactly what the
rules read — both sides — plus `eligibility-rules-v1`. A second `sync` over
unchanged inputs writes nothing, moves no timestamp, and reports:

```text
created=0
replaced=0
unchanged=N
changed=false
```

A changed education requirement, required language, stated sponsorship need or
gained skill recomputes the decisions it affects. A changed telephone number,
GitHub URL, portfolio link, availability date or mobility recomputes nothing:
none of them is read by a rule, so none of them is in the digest.

**The output is counters, ids, versions and reason codes.** No posting's words
and no person's reach stdout or the structured log — not a description, not an
evidence fragment, not a skill or language name, and not the address the command
was given. There is no flag that prints a verdict as a percentage, and nothing
to build one from: the answer is one of three words.

What this slice does **not** do: no match score, no skill-similarity score, no
TF-IDF, no cosine similarity, no embedding, no fuzzy matching, no ranking, no
priority, no recommendation, no notification and no auto-apply. It writes to
nothing upstream — not `opportunities`, not any `profile_*` table, not the
Phase 3.5 projections — and it leaves the historical
`opportunities.eligibility_score` column untouched, because a categorical
verdict is not a number. It opens no network connection, downloads no model and
calls no LLM, and there is no web interface or HTTP endpoint for any of it.

## Daily Gmail digest (Phase 5.4A)

One command materializes one deterministic daily email per profile, from the
audited Portfolio snapshot, into `gmail_digest_outbox` (migration `0022`):

```bash
python -m services.gmail_digest.digest_cli \
  --database ./data/opportunity-radar.db \
  --profile-id 1 \
  --timezone Africa/Casablanca \
  --recipient you@example.com \
  --dry-run
```

`--dry-run` opens the database through SQLite's `mode=ro` URI with
`query_only` pinned, reports the decision, and leaves the file byte-identical —
which is what makes it safe against a real operational database. Dropping
`--dry-run` runs the same decision and, when the policy says so, writes exactly
one frozen `PENDING` row.

**5.4A does not touch Gmail.** There is no OAuth flow, no Gmail API call, no
SMTP, no mailbox read, no label management and no scheduler anywhere in this
slice. A materialized row is a message that has been decided, not one that has
been sent; Phase 5.4B adds delivery against exactly these columns, and Phase 7
Gmail Intelligence — email classification, rejection and interview detection,
application tracking — remains out of scope entirely.

### The content is the Portfolio's, not the digest's

The digest shows only the assessments the current audited Portfolio run marked
`INCLUDED`, and shows each one's persisted bucket, priority category and
eligibility status verbatim. Nothing is recomputed: not Eligibility, not
Matching, not Priority, not Portfolio. `UNKNOWN` stays `UNKNOWN`, a missing
location is rendered as an absence rather than filled in, and no global match
percentage appears because no authoritative per-opportunity one is persisted.
The outward link is chosen by `services.api.link_priority` — the same policy
the Portfolio page uses — and an `INCLUDED` opportunity that cannot be resolved
to a real absolute `http`/`https` URL fails the whole digest closed rather than
shipping one dead link. A Portfolio that is `NOT_SYNCED`, or that fails its
audit, produces no digest and no row.

Items are ordered `TARGET`, `SAFE`, `AMBITIOUS`; inside a bucket by priority
`URGENT`, `HIGH`, `MEDIUM`, `LOW`, `IGNORE`; then by `opportunity_id`. The
plain-text and HTML bodies carry the same items in the same order. The HTML is
inert: every dynamic value is escaped, including inside `href`, and there is no
script, no external image and no tracking pixel of any kind.

### The first digest is not a silent baseline

Unlike the Web Push policy, whose first sync deliberately plants a baseline and
announces nothing, a profile with actionable opportunities and no prior digest
**is** eligible for one: the user has not been told this anywhere else, so
staying silent would lose information rather than avoid noise. After that:

| situation | result | writes |
| --- | --- | --- |
| Portfolio `NOT_SYNCED` or audit not clean | error | nothing |
| no `INCLUDED` opportunity | `EMPTY` | nothing |
| a digest already exists for this profile, local day and version | `ALREADY_MATERIALIZED` | nothing — byte-level no-op |
| a later day, and the content equals the one this recipient was last **sent** | `UNCHANGED` | nothing |
| anything else | `CREATED` | one frozen `PENDING` row |

There is no placeholder row for `EMPTY` or `UNCHANGED`: a row in this table is
a message someone will receive. A repeated pass does not move a timestamp to
record that it looked. Web Push remains responsible for telling the user
immediately when something genuinely moves; the digest is the daily summary.

### The content fingerprint excludes provenance

`content_fingerprint` covers only what a reader sees — per item the opportunity
id, title, organization, location, authoritative URL, bucket, priority category
and eligibility status, plus the digest version and the reading order. It
deliberately excludes `digest_date`, `created_at`, the row id, the Portfolio run
id and the Portfolio run fingerprint, which are persisted alongside it as
provenance. So a Portfolio run that changed only `EXCLUDED` opportunities has a
new run fingerprint and the same content fingerprint, and cannot trigger a new
email.

That provenance is a real reference, not a recorded number: `(portfolio_run_id,
profile_id)` is a composite foreign key onto `portfolio_runs(id, profile_id)`,
which 0017 already keys, so a digest can neither name a run that does not exist
nor claim to summarize another profile's Portfolio. It is `ON DELETE RESTRICT`,
because the run is the evidence for what the frozen message says — deleting the
profile still removes both, but deleting the run alone out from under a digest
is refused.

The status and the error category are one statement, and the schema keeps them
agreeing: `SENT` carries no error at all; `PERMANENT_FAILURE` names a code and
the category `PERMANENT`; a `PENDING` or `IN_FLIGHT` digest either carries no
error or carries a code with the category `RETRYABLE`, because a permanent
cause makes the row terminal on the spot. A row saying "give up" and "try
again" at once is not a state this table can hold.

### The day is chosen, never inherited

`--timezone` is required and must be an IANA name resolved through `zoneinfo`.
A missing, blank or unknown zone fails closed rather than falling back to UTC or
to the machine's own setting, and the zone that decided a day is persisted in
the row beside the date. Row timestamps stay in UTC; only the business day is
local.

### The recipient is a fingerprint, not an address

`--recipient` is configuration. Only `recipient_fingerprint` — a SHA-256 over
the normalized address — is persisted, so a copy of the database carries no
mailbox, and the "already sent this" decision is made per recipient. The
address never reaches stdout, the structured log or an exception message: a
failure prints only the class of error, and the success output carries counters,
dates, identities and ids. No rendered subject or body is ever printed.

### Concurrency

Reading the audited Portfolio, resolving the content, applying the four rules
and inserting the row happen inside one `BEGIN IMMEDIATE`. Two materializers
racing on the same profile and day produce exactly one `PENDING` row: SQLite
serializes them, the loser sees the winner's row and reports
`ALREADY_MATERIALIZED`, and `UNIQUE (profile_id, digest_date, digest_version)`
is the database's own copy of that promise. No network I/O happens inside the
transaction, because no network I/O happens in this slice at all.

## Gmail delivery (Phase 5.4B)

One command materializes today's digest and sends what is due, in one pass:

```bash
python -m services.gmail_digest.delivery_cli \
  --database ./data/opportunity-radar.db \
  --profile-id 1 \
  --timezone Africa/Casablanca \
  --recipient you@example.com
```

It loads and validates the Gmail authorization **first**, then materializes
using the unchanged 5.4A policy, then drains the digests that are due, then
exits. There is no daemon, no cron entry, no GitHub Actions workflow and no
scheduler of any kind: something outside this process decides when a pass
happens. `--limit N` bounds how many digests one pass attempts.

5.4B is transport and only transport. It sends the `subject`, `body_text` and
`body_html` already frozen in `gmail_digest_outbox` — it does not re-render,
re-order, add or remove an opportunity, rewrite a URL, or re-read the
Portfolio, and it recomputes nothing upstream. It adds no migration: `0022`
already carries the lifecycle, the attempt counter, the claim, the Gmail
message id and the error vocabulary it writes.

### One scope: `gmail.send`

Authorization is a Desktop/Installed App OAuth flow requesting exactly
`https://www.googleapis.com/auth/gmail.send`, run once by a person:

```bash
python -m services.gmail_digest.oauth_bootstrap        # add --force to replace
```

It opens a consent screen asking only to *send email on your behalf*, and it
refuses the result if Google returns anything wider. There is no
`gmail.readonly`, no `gmail.modify`, no `gmail.metadata` and no
`mail.google.com` anywhere in this phase; the only Gmail call ever made is
`users().messages().send`. **Nothing here reads a mailbox.** Email
classification, interview and rejection detection, label management,
application tracking and follow-up automation are Phase 7 Gmail Intelligence,
which needs its own consent rather than a scope widened here.

The stored credential is checked for exact send-only scope every time it is
loaded, not only when it is created, so a token that gained a mailbox scope
elsewhere is refused rather than used narrowly.

### Two local files, and neither is in the database

| file | what it is |
| --- | --- |
| `.secrets/gmail-oauth-client.json` | the Desktop OAuth client, downloaded from Google Cloud. Read, never written. |
| `.secrets/gmail-token.json` | the authorization this deployment obtained. Written by the bootstrap, refreshed by delivery. |

Both paths can be overridden with `--client-secret` and `--token`. `.secrets/`
is ignored by Git. Neither file ever enters SQLite, a log line, an exception
message or a `repr`: the token, the refresh token and the client secret are
never printed, and a failure reports the class of error rather than quoting
what failed. Token writes are atomic — a temporary file in the same directory,
owner-only, renamed over the destination — so a crash mid-write cannot leave
half a token, and the bootstrap refuses to replace an existing authorization
without `--force`.

Credentials are loaded, refreshed and scope-checked **before the database is
opened for writing**. A missing, corrupt, expired, revoked or over-scoped
credential fails there, so an OAuth problem can never move a `PENDING` digest
to `IN_FLIGHT`.

### The recipient guard

`--recipient` stays runtime configuration and is never persisted. It is
normalized and fingerprinted with the same 5.4A logic, and the fingerprint is
part of the claim predicate: a digest frozen for another mailbox is never
claimed, never sent and never modified. It is not silently skipped either —
the drain counts it and reports `recipient_mismatched`, and `--dry-run`
reports `recipient_matching`, `recipient_mismatched` and
`due_recipient_mismatched`, because a due message for an address that is no
longer configured is something an operator has to be told.

### Claim, lease and retry

One digest at a time: a short `BEGIN IMMEDIATE` claims the single oldest due
row under a fresh 32-hex token, the transaction commits, Gmail is called with
**no transaction open**, and a second short transaction settles that row only
if it is still `IN_FLIGHT` under that exact token. Two passes therefore never
send the same digest, and a result whose claim was recovered in the meantime is
discarded rather than applied.

| policy | value |
| --- | --- |
| claim lease | 300 s — an older claim belonged to a dead process and is released |
| attempts | 5 in total |
| backoff after attempts 1–4 | 60 s, 300 s, 900 s, 3600 s |

A real send result increments `attempt_count`; a stale-claim recovery never
does, because a claim nobody settled is not evidence that anything was
attempted. On the fifth retryable failure the digest becomes
`PERMANENT_FAILURE` with the category `PERMANENT` — the decision is terminal
even though the cause was transient — and the cause survives inside the code as
`RETRY_LIMIT_EXHAUSTED_<cause>`.

Classification is deterministic: no HTTP status at all (DNS, refused
connection, TLS, timeout) is retryable, as are `429`, `408` and every `5xx`;
`401` and every other non-`403` `4xx` are permanent, because the credential was
already validated and refreshed before the claim. A `403` is read rather than
guessed at — Google's structured reason decides, so `rateLimitExceeded` or
`quotaExceeded` is retryable while `forbidden`, `insufficientPermissions` or
`accessNotConfigured` is permanent, and an unrecognised `403` is treated as a
refusal. Only bounded, normalized codes are stored: no Google response body,
message or address ever reaches the database.

### At-least-once, not exactly-once

Gmail accepting a message and SQLite recording `SENT` cannot be made one atomic
act. A process that dies between them leaves a claim it will never settle; the
lease releases that claim and the digest is sent again — a duplicate email,
bounded by the lease and by the attempt limit. The alternative, leaving the
claim held forever, loses the digest outright. So:

* concurrent passes never collide, because a claim is exclusive;
* a digest that reached `SENT` is never re-sent, by anyone, ever;
* after a crash in that one window, delivery is **at-least-once**.

Nothing in this phase claims exactly-once delivery.

### `--dry-run`

```bash
python -m services.gmail_digest.delivery_cli ... --dry-run
```

Opens the database through SQLite's `mode=ro` URI with `query_only` pinned,
requires no OAuth file, refreshes no credential, opens no socket, claims
nothing, recovers nothing and materializes nothing — the file is byte-identical
afterwards, which is what makes it safe against a real operational database.
It reports what today's materialization *would* decide and the counts: pending,
due, scheduled, in flight, stale claims, sent, permanent failures, and the
three recipient counters. It prints no address, no subject, no body, no MIME
payload and nothing from an OAuth file.

Run the command twice in one day and the second run is quiet by construction:
materialization reports `ALREADY_MATERIALIZED` and writes nothing, the drain
finds nothing due because the digest is already `SENT`, and no second email is
sent.
