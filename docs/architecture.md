# Architecture

## Current Phase 2 path

`RadarAgent` is the normal orchestration boundary. It loads the source catalogue,
selects enabled sources whose status is `active`, and, for each source, builds a
collector and persists that collector's candidates in an independent
transaction. An exception from one source is recorded and the remaining sources
continue, so it cannot roll back earlier committed sources.

Every attempt is instrumented before it runs. The agent persists a `RUNNING`
`source_runs` row, then collects, then closes that same row as `SUCCESS` or
`FAILED` and stamps `sources.last_run_at`. A source whose run cannot be started
is not collected at all, because that work could never be audited; it is
reported as failed for this run and the remaining sources continue. A
finalization that fails is logged and leaves the row `RUNNING`, so an attempt
that was never closed stays visible rather than being erased or misreported.

An interrupted process therefore leaves `RUNNING` rows. `RadarAgent` isolates
`Exception` only, so an interruption propagates untouched instead of being
disguised as a collection failure.

After the complete source loop, the agent invokes qualification persistence
exactly once across all eligible opportunities. Qualification success or failure
is represented separately from each source result; the overall run succeeds only
when no source failed and global qualification succeeded.

```text
public Greenhouse boards ─┐
                         ├─ collectors ─ RadarAgent ─ local SQLite
Gmail LinkedIn alerts ───┘                         ├─ qualification
                                                   └─ duplicate review tools

local SQLite ─┬─ read-only FastAPI GET /api/opportunities
              │                     └─ Next.js home page
              └─ read-only FastAPI GET /api/source-health
                                      └─ Next.js /source-health page
```

The two Greenhouse sources use public board JSON. The LinkedIn source reads Job
Alert messages via the Gmail API and parses canonical LinkedIn job URLs without
opening or scraping LinkedIn pages. Gmail access is restricted to
`https://www.googleapis.com/auth/gmail.readonly`.

## Source health

`services/collector/database/source_health.py` derives, for each known source,
its `enabled` flag and the status and metrics of its most recent `source_runs`
row. It writes nothing, adds no table, and creates no row for a source that has
never run.

The status shown for a source stays the real status of that latest run —
`RUNNING`, `SUCCESS`, `FAILED`, or nothing at all for a source that never ran.
No second health taxonomy is layered on top of it. The one derived signal is
`zero_result_streak`, with the `anomaly_code` and `anomaly_message` it produces
past its threshold, and it is exposed beside the run status rather than folded
into it. The rule and its `NULL` handling are specified in
[database.md](database.md).

Both the streak and the anomaly are decided once, in the backend. The
`/source-health` page presents that verdict and never recomputes it.

## Read-only application API

The FastAPI service reads visible, active opportunities from the existing
configured SQLite database. It performs no collection or migration. The Next.js
home page calls that API server-side using `OPPORTUNITY_API_BASE_URL` (default
`http://127.0.0.1:8000`) and renders real opportunity summaries and original
source links.

The `original_url` each item exposes is chosen at read time from every
`opportunity_sources` observation of that opportunity, joined to `sources`, not
from the canonical row alone. `services/api/link_priority.py` holds that policy
in one place: a `greenhouse` observation is an official career page / ATS link
and is preferred over a `gmail_linkedin_alert` job-board observation, so the
canonical row a reviewer kept for deduplication never decides which link the
user is shown. Within one observation the order is `application_url`, then
`source_url`, then `canonical_url`; between several official observations the
tie-break is the most preferred official source type, then the smallest
`opportunity_sources.id`, which is the earliest recorded observation. With no
official observation, the previous canonical-row fallback is unchanged. The
selection reads only; it moves, rewrites, and deletes nothing.

`GET /api/source-health` is read-only more strictly still. It opens the
operational SQLite database through a `mode=ro` URI and also sets `query_only`,
so the request cannot create the database file it was pointed at, let alone a
schema or a row; a non-SQLite backend is refused locally rather than dialled, so
the request makes no network call at all. It merges the validated
`config/sources.yaml` catalogue with the sources the database already holds, so
a configured source that has never run is still listed, and returns one entry
per source. The `/source-health` page renders those entries as a table.

## Digital Twin root

`services/digital_twin/` is a separate business package: the collector keeps
owning the SQLite infrastructure — the same connection factory and the same
migration runner are reused, not replaced — while the Digital Twin owns its own
domain. Nothing about it lives in `services/collector/models/`.

Phase 3.1A persists two rows and no more:

```text
users  (identity / ownership root)
  1 ─── 1
profiles  (stable Digital Twin root)
```

`users` is who owns data; `profiles` is the stable anchor every later Digital
Twin table will point at. One user owns at most one profile, enforced by
`UNIQUE(user_id)` in the schema. There is no ORM: a small typed layer
(`models.py`, `identity.py`, `repository.py`) talks to SQLite directly, as the
rest of the project does, and a local CLI initialises or reads the real profile
without echoing or logging the address.

That slice implements the root only. `profile_facts` and their provenance
arrive with Phase 3.3A below; `cv_versions`, skills, preferences, eligibility,
matching, scoring, personal notifications, a personal frontend, and any public
profile API are **not** implemented — neither Phase 3.1 as a whole nor Phase 3
is complete. No LLM, no external API, and no remote write path is introduced:
the operational database stays local SQLite.

## CV parser foundation (Phase 3.2A)

`services/digital_twin/cv/` reads one local PDF and describes it. It is a pure
function of the file, deliberately kept apart from the opportunity collectors:

```text
local PDF ─ pdf.py ─ normalization.py ─ sections.py ─ parser.py ─ ParsedCv
   (bytes)  extract    conservative      lexicon       assemble    (frozen)
            + SHA-256  text clean-up     headings
```

- `models.py` holds the frozen result vocabulary: `ExtractedPage`,
  `DetectedSection`, `ParserWarning`, `ParsedCv`, and the `PARSER_VERSION`
  (`cv-parser-v2`) every result carries. `pypdf` is pinned to an exact
  version in `pyproject.toml` because the extracted text depends on it:
  changing that pin, or any extraction, normalization or segmentation rule, is
  a change of the rules `cv-parser-v2` names, so it requires deciding whether
  `PARSER_VERSION` must move with it. Without that, two different outputs could
  claim the same provenance.
- `pdf.py` extracts each page's existing text layer with `pypdf`, hashes the
  file, and raises one explicit error per failure mode: missing path, path that
  is not a file, unreadable PDF, encrypted PDF, and a PDF with no extractable
  text at all.
- `normalization.py` normalizes exactly one closed list of text-layer
  artefacts and nothing else: line-ending conventions become `\n`, space-like
  and zero-width characters are folded away, runs of spaces inside a line and
  runs of blank lines each collapse to one, and leading and trailing blank
  lines go. Those are the only characters it touches: every character carrying
  visible text comes through untouched, and nothing is reordered, reworded,
  translated, spell-checked or de-hyphenated. The separation into lines
  survives because it is all the parser has to segment on.
- `sections.py` splits the document on headings whose folded form is listed
  verbatim in one French/English lexicon. There is no model, no scoring and no
  fuzzy match, so any classification can be checked against that table. A label
  a real CV uses and the lexicon lacks is added to that table as an exact
  folded entry and moves `PARSER_VERSION` with it, which is how
  `projets selectionnes` (`PROJETS SÉLECTIONNÉS`) became a `PROJECTS` heading.
- `parser.py` composes the pipeline; `cli.py` exposes it locally.

The result is deterministic: nothing reads the clock, the environment or the
network, so the same PDF gives the same `ParsedCv`, field for field. There is
no OCR — a scanned CV raises `EmptyPdfTextError` rather than producing invented
content — and no LLM, CV-parsing service or remote call anywhere in the parse.

Ambiguity stays ambiguity. Text before the first recognised heading is kept as
one `UNCLASSIFIED` section, never given a category; a heading the lexicon does
not know is not a boundary, so its lines stay in the preceding section instead
of being classified on a guess; and warnings (`EMPTY_PAGE`,
`NO_SECTION_HEADING_DETECTED`, `UNCLASSIFIED_LEADING_CONTENT`,
`REPEATED_SECTION_TYPE`, `EMPTY_SECTION_CONTENT`) report what the parse could
not resolve.

Phase 3.2A stops there. It creates **no** `profile_facts` row, persists nothing
in the Digital Twin, adds no migration and no table, and no value it extracts is
a verified fact about the person.

`sections.py` exposes its segmentation twice, from one implementation:
`segment_document` returns each section with the page of every line it holds,
and `detect_sections` is the flattened view the `ParsedCv` carries. Phase 3.2B
reads the first, so there is exactly one answer in the codebase to "where does a
section start". `export.py` holds the one writer both CV commands use for a
detailed local export.

## Structured CV candidates (Phase 3.2B)

`services/digital_twin/cv/candidates/` takes the `ParsedCv` above and returns a
`StructuredCvExtraction`: a list of **unverified candidates**. It never re-reads
the PDF.

```text
ParsedCv ─ segment_document ─ identity ─ contact ─ entries ─ skills ─ extractor
 (3.2A)     (3.2A, lines +    header     email,    section    SKILLS   assemble
             their pages)     rules      phone,    blocks     lines    + dedup
                                         URLs                          (frozen)
```

An `ExtractedCandidate` says exactly one thing: *a named deterministic rule
found this text at this place in this document*. It carries the text as written
(`raw_text`), a technical normal form only where one is unambiguous
(`normalized_value`: a lowercased email, a phone compacted to its digits — and
`None` everywhere else), the pages it covers, the canonical section and its
index, the `rule_id` that produced it, a stable `fingerprint`, and the
`cv_sha256`, `parser_version` and `extractor_version` it was produced under.
`CANDIDATE_EXTRACTOR_VERSION` is `cv-candidates-v2` and moves independently of
`PARSER_VERSION`.

The taxonomy is `NAME_CANDIDATE`, `PROFESSIONAL_TITLE`, `EMAIL`, `PHONE`,
`GITHUB_URL`, `LINKEDIN_URL`, `PORTFOLIO_URL`, `PROFESSIONAL_URL`,
`EDUCATION_ENTRY`, `EXPERIENCE_ENTRY`, `PROJECT_ENTRY`, `CERTIFICATION_ENTRY`,
`LANGUAGE_ENTRY` and `SKILL`. Every rule is local, free and readable: regular
expressions, closed dictionaries and fixed segmentation rules. No LLM, no
external API, no CV-parsing service and no network call takes part, and
`extract_candidates` is a pure function — no clock, no environment variable, no
file and no database connection — so the same `ParsedCv` always yields the same
result, field for field, with no timestamp in it.

What the rules refuse to do is the design:

- **Identity.** Only the first non-blank line of the header block is ever
  considered, and only when its shape is that of a plain name — letters, two to
  four words, no digit, no "@", not a document label such as "Curriculum
  Vitae", and no role keyword. "Jeanne Exemple — Data Scientist" therefore
  proposes no name at all. A name is never built from an email address, never
  completed and never reordered; when no line qualifies the extractor reports
  `NO_IDENTITY_CANDIDATE` and proposes nothing.
- **Phone.** A digit run is a phone number only where an explicit `+` prefix, a
  phone label on the line, or a trunk zero *inside the header block* justifies
  it, with 9 to 15 digits and no `/` as a separator — and, before any of those,
  only where no digit group of the run reads as a calendar year. "01 2020 -
  12 2024" is a period wherever it appears, including in the header and next to
  the word "portable"; "06 11 22 33 44" and "0470 12 34 56" are untouched,
  because the test is on the grouping, not on four-digit groups being
  suspicious. A real number carrying a year-shaped group is missed rather than
  a period being announced, which is the trade-off this slice always takes. No
  country code or area code is ever added to a number the CV wrote without one.
- **URLs.** GitHub and LinkedIn are decided by hostname, which is a fact about
  the URL. A host written without a scheme is recognised only from a closed
  list and only where the hostname really ends, so `github.com.evil.invalid`
  and `github.community` are never truncated into a GitHub link; written in
  full, such a host is kept whole and classified on what it actually is.
  Anything else is `PORTFOLIO_URL` only where a label of the closed dictionary
  ("portfolio", "site", "website"…) introduces it earlier on the same line, and
  `PROFESSIONAL_URL` otherwise — a personal-looking domain is never promoted to
  a portfolio on a guess.
- **Entries.** Education, experience, project, certification and language
  sections are cut into blocks on the separator the section actually uses —
  blank lines, else list markers, else one entry per line — and the block is
  kept as written. One narrower separator comes first, in an `EXPERIENCE`
  section only: where the body opens on a line written as three or more
  non-empty parts separated by `|`, and holds at least two such lines, those
  lines are the separator (`EXPERIENCE_PIPE_DELIMITED_BLOCK`) and everything
  under one — bullets and continuation lines included — belongs to it. That
  case is exactly the one the fallbacks cannot see: an entry opening on a
  non-bullet line inside a bulleted section would otherwise be swallowed by the
  bullet above it. The rule reads the punctuation of a line and nothing else:
  the parts are never split into an employer, a role or a date, and a body that
  fails either precondition is cut exactly as before. No institution, employer,
  role, date, duration or diploma level is derived anywhere: that reading is
  Phase 3.4.
- **Skills.** Mentions come only from a recognised `SKILLS` section, split on
  the separators the CV used. There is no level of any kind in the model — no
  proficiency, no confidence, no score — so "Azure Data Platform (avancé)" is
  one mention whose text is `Azure Data Platform (avancé)`, and an umbrella
  mention is never expanded into the technologies that usually go with it.

Candidates are deduplicated by type and by their comparison value — the
`normalized_value` where one exists, the compared form of the text otherwise —
keeping the first occurrence in document order with its own text and
provenance. A CV repeating its email in a header and a footer proposes it once,
and so does one writing its number as `+33 6 00 00 00 00` and `+33600000000`.
The `fingerprint` is that comparison value hashed with the type and the
extractor version: it identifies a value, not a document, so the same address
in two CVs fingerprints the same and `cv_sha256` is what ties a candidate to
its file.

Phase 3.2B stops there. It creates **no** `profile_facts` row, persists nothing,
adds no migration and no table, and not one candidate is a verified fact.
Phase 3.3B does now carry candidates into the fact store, but it does so from
*outside* this package: no module here imports `services/digital_twin/facts`
and none imports the bridge either, and a test asserts both. Institutions,
employers, canonical roles and normalized dates are Phase 3.4 and do not
exist; skill aliases are resolved by Phase 3.4A, downstream of a human's
acceptance and never from a candidate, and no skill level is derived anywhere.
Matching, eligibility, ranking and scores are further out still.
Phase 3.2 as a whole is finished as a *reading* of a document, and is not a
validated Master CV.

## Validated profile facts (Phase 3.3A)

`services/digital_twin/facts/` is where a claim about the person becomes, or
fails to become, knowledge. It is the anti-hallucination foundation of the
Digital Twin, and it holds one chain:

```text
profiles ─ profile_facts ─ profile_fact_provenance
  (3.1A)     one claim       the evidence for it
                 │
                 └─ PROPOSED ─┬─ ACCEPTED ─┬─ CORRECTED → new ACCEPTED fact
                              │            └─ REJECTED
                              ├─ REJECTED
                              └─ CORRECTED → new ACCEPTED fact
```

The design is three refusals.

**It refuses a second definition of truth.** A fact is verified when, and only
when, its status is `ACCEPTED`. There is no `verified` column, in this table or
any other; `ProfileFact.is_verified` is a computed property over the status.
Storing both a status and a boolean would be storing the same thing twice, and
the copy that drifts is the copy that gets believed. `list_verified_profile_facts`
filters on `ACCEPTED` in SQL, so a proposal, a rejection or a correction cannot
reach a later phase by accident, and no caller can forget the filter.

**It refuses to overwrite.** A correction writes a *new* fact carrying the
corrected value, `ACCEPTED` because a person typing the right value is an
explicit human validation; gives it a `USER_INPUT` provenance; marks the
previous fact `CORRECTED`; and points it at its replacement — all in one
transaction that commits whole or rolls back whole. The old row keeps its own
`value`, so successive corrections build a chain in which every value ever
proposed stays readable. No `UPDATE profile_facts SET value = ...` exists in the
package, and a test reads the SQL the module actually executes to keep it that
way.

**It refuses to confuse evidence with a decision.** Provenance lives in its own
table and says only where a value was read and by which rule: source type,
locator, CV digest, parser and extractor versions, candidate fingerprint, rule
id, pages, section and index. It carries no score, no confidence and no level,
none of which a click or a regular expression can produce. Every fact the
repository creates gets at least one provenance row in the same transaction,
because a fact with no evidence is an assertion nobody could check, and
`UNIQUE (fact_id, provenance_key)` stops the same proof from being recorded
twice and looking like corroboration. A field the source did not carry stays
`NULL` rather than being filled with something plausible.

The persistence style is Phase 3.1A's: plain SQLite, no ORM, the same connection
factory and the same migration runner, and one explicit `BEGIN IMMEDIATE` per
multi-step write. Every mutation is scoped by `profile_id` as well as `fact_id`,
so a valid id from another profile is reported as missing rather than mutated.
`REJECTED` and `CORRECTED` are terminal; re-accepting an accepted fact and
re-rejecting a rejected one are idempotent and keep the original decision date;
every other move raises an explicit business error. The exact schema, the
transition table and the provenance columns are in
[database.md](database.md#profile-facts-and-their-provenance).

Phase 3.3A stops at the store. It does not know what an `ExtractedCandidate`
is: the mapping lives outside it, in the Phase 3.3B bridge described below, and
what this package offers that bridge is one primitive,
`ensure_profile_fact_proposal`. Nothing here generates a Master CV, a cover
letter, an application, a form or an adapted CV, and nothing computes an
eligibility, a match, a ranking or a score. No preference, availability,
mobility, eligibility or matching table exists, and no administrable alias
table either; the skill tables `0008` adds are a projection *of* this store,
described below, and nothing in this package knows about them. No network call,
no model and no remote write path takes part.

## CV candidates into proposed facts (Phase 3.3B)

`services/digital_twin/cv/fact_bridge.py` is the one place a CV candidate
becomes a claim about a person, and `services/digital_twin/cv/review_cli.py` is
the one place a person decides about it:

```text
PDF ─ parse_cv_pdf ─ extract_candidates ─ fact_bridge ─ PROPOSED fact
     (3.2A)            (3.2B)             (3.3B)         (3.3A)
                                                            │
                                              human review ─┴─ ACCEPT / REJECT
                                              (review_cli)     CORRECT / SKIP
                                                               / QUIT
```

The bridge exists so that neither side has to know about the other. The
candidate package still imports no fact module and the fact package still
imports no CV module; the translation happens once, in the open, in a file that
imports both. It is built out of three refusals of its own.

**It refuses an implicit mapping.** `CANDIDATE_TYPE_TO_FACT_TYPE` is written out
entry by entry, is total over `CandidateType`, and is checked to be total at
import time, so a candidate type added later without a decided meaning breaks
loudly instead of having its candidates silently dropped. The two taxonomies are
allowed to disagree and one of them does: `NAME_CANDIDATE` proposes a plain
`NAME`, because "candidate" describes the reading, not the confirmed identity.
Nothing maps to `PREFERENCE`, `AVAILABILITY`, `MOBILITY` or `CAREER_OBJECTIVE`:
no rule produces those, and a mapping to a category nothing feeds would be a
promise the extractor does not keep.

**It refuses to interpret.** `raw_text` becomes `value` and `normalized_value`
becomes `normalized_value`, both verbatim, and the candidate's provenance is
copied field for field — digest, parser and extractor versions, candidate
fingerprint, rule id, pages, section and index. No skill alias, no skill level,
no employer, institution, date or country parsed out of a value, no canonical
role and no rewording. Skill aliases are resolved by Phase 3.4A, downstream of
a human's acceptance and never here. `source_locator`
stays `NULL` on purpose, because the only locator this side could supply is the
local path of the PDF and a CV filename usually carries the person's name.

**It refuses to decide.** Every fact the import creates is `PROPOSED`. There is
no threshold, no confidence, no score, no accept-all and no auto-accept
anywhere in the path, and the bridge never calls `accept_profile_fact` at all.

Idempotence is keyed on the **evidence**, not on the text.
`ensure_profile_fact_proposal` resolves the deterministic `provenance_key` of a
candidate, looks for a fact of *this profile* that key already justifies —
through a join, because a provenance key is unique per fact rather than per
database — and returns it if there is one, or creates the fact and its
provenance together if there is not. It reports `created` either way, and it
raises rather than choosing if two facts of one profile share a proof or if a
known proof would suddenly justify a different type of fact. The lookup ignores
status on purpose: a fact already `ACCEPTED`, `REJECTED` or `CORRECTED` is
returned as it stands, so a decision a human already took survives the next
import and a refused reading never comes back as a fresh proposal. Two different
CVs carry two different digests, so they are two proofs and two proposals;
consolidating several versions of a CV into one reading is not attempted here.

Each candidate is its own `BEGIN IMMEDIATE` transaction, so the import is
restartable rather than all-or-nothing: a run that stops after N candidates
leaves those N facts complete with their evidence, and re-running imports the
rest without duplicating any of them.

The review CLI is deliberately the one command in the project that prints CV
content — a person cannot accept a value they are not shown — and the exception
is bounded to the review prompt: never the closing summary, which is counters,
never the structured log, which carries counters, versions, canonical types and
ids, and never a file, because the command writes none. It refuses a non-SQLite
backend before connecting and before asking for an address, it reads an existing
profile and never creates one, and it offers exactly five answers. Any other
input prints a notice and asks again rather than falling through to a default.
Quitting is safe and resuming is expected: undecided facts stay `PROPOSED`,
decided ones are never offered again, and a second run re-imports nothing.

Phase 3.3B reuses the `0007` schema and adds no migration and no table. It
generates no Master CV PDF, no cover letter, no application and no adapted CV,
and it computes no eligibility, match, ranking or score. There is no web
interface, no HTTP endpoint and no authentication for any of it, and no network
call or model takes part.

## Reconciling a re-read CV (Phase 3.3C)

A CV is read by a *campaign*: one document digest, one parser version, one
extractor version. When the parser or the extractor is fixed, the same file is
read again — and Phase 3.3B, whose idempotence is keyed on the evidence, sees a
new campaign as a new proof for every candidate. Importing it the ordinary way
would therefore propose the whole document a second time, including the
readings a person already accepted.

`services/digital_twin/cv/reconciliation.py` is the re-read. It compares the
two campaigns for one profile and one document and splits the newer one in
three:

```text
old campaign facts ─┬─ same fact_type + same value ─ unchanged ─ attach the new
   (3.3B import)    │                                            proof, keep the
                    │                                            fact and its
                    │                                            decision
                    ├─ no counterpart in the new  ── superseded ─ finalize may
                    │  campaign                                   REJECT it
                    │
new candidates ─────┴─ no counterpart in the old  ── changed ──── PROPOSED, for
                       campaign                                   a human
```

**Identity is exact equality, and deliberately nothing else.** Two readings are
the same reading only when the profile, the CV provenance, the digest, the old
campaign's two versions and the `fact_type` all match and the `value` equals
the candidate's `raw_text` byte for byte. There is no normalization, no case
folding, no whitespace collapsing, no substring rule, no edit distance, no
similarity, no fingerprint comparison and no model anywhere in the module; a
test walks its source to keep it that way. A text differing by one character is
a different thing to say about the person and becomes a proposal, because the
failure worth engineering against is a changed reading slipping in under an old
acceptance. Where the old campaign holds two facts for one reading, the module
raises instead of choosing: picking one would silently decide which of two
human decisions counts.

**Status takes no part in identity.** A reading the person corrected is still
the same reading: `prepare` attaches the new proof to the `CORRECTED` fact and
leaves both it and its `USER_INPUT` replacement exactly as they are. Attaching
evidence is not a decision and re-opens nothing.

The operation is split in two, with a person in between.
`prepare_cv_fact_reconciliation` attaches the new campaign's provenance to every
unchanged reading through `ensure_profile_fact_provenance` — the no-op form of
the append, so a second `prepare` writes nothing and raises nothing — and puts
every changed reading through the ordinary 3.3B bridge as a `PROPOSED` fact. It
accepts nothing, corrects nothing and rejects nothing, and the review CLI of
Phase 3.3B is what answers the proposals.

`finalize_cv_fact_reconciliation` recomputes the same plan from the same three
inputs, then, **before any mutation**, resolves each changed reading through its
own evidence and requires it to be ground truth: `ACCEPTED`, or `CORRECTED`
with an `ACCEPTED` replacement. One still `PROPOSED`, one `REJECTED`, one whose
fact is missing and one whose proof justifies several facts each stop the run
with not a single old fact touched. Only once they are all answered does each
superseded reading that is still `ACCEPTED` become `REJECTED` — meaning "that
type and that text, under that segmentation, is no longer active truth". The
row stays, its value stays, its own campaign's provenance stays, an already
rejected one is a no-op, a terminal `CORRECTED` one is left as the person left
it, and one nobody ever decided is left `PROPOSED`, because turning an
unanswered proposal into a refusal would be the module deciding. There is no
`DELETE` in the package.

Each attachment, each proposal and each rejection is its own `BEGIN IMMEDIATE`
transaction, so both operations are restartable rather than all-or-nothing, and
a second complete run of either writes nothing. Every report they return is
counters, canonical fact types, version strings and the document digest — no
value, no `raw_text` — and the module itself prints and logs nothing.

Phase 3.3C reuses the `0007` schema and adds no migration and no table. It
writes to `profile_facts` and `profile_fact_provenance` and to nothing else: it
imports no skill, opportunity or migration module, and it never runs
`synchronize_profile_skills` — reconciling non-`SKILL` readings leaves the
accepted `SKILL` facts, and therefore the Phase 3.4A projection, exactly where
they were. The local command is in
[operations.md](operations.md#cv-reconciliation-phase-33c).

## Normalized profile skills (Phase 3.4A)

`services/digital_twin/skills/` derives one reading from facts a person already
accepted, and adds no new truth:

```text
profile_facts ── ACCEPTED + SKILL ── normalize_skill ── profile_skills
   (3.3A)          (the filter)        (3.4A)              │
                                                           └─ profile_skill_evidence
                                                                    │
                                                              profile_facts
                                                                    │
                                                          profile_fact_provenance
```

`profile_facts` stays the source of truth. The projection reads
`fact_type = 'SKILL' AND status = 'ACCEPTED'`, written into the SQL rather than
passed in, so a `PROPOSED`, `REJECTED` or `CORRECTED` fact and an `ACCEPTED`
fact of any other type are invisible to it and no caller can widen the filter.
Nothing from the Phase 3.2B extractor reaches a skill without passing through a
fact a human accepted: this package imports no CV module.

It is built out of four refusals.

**It refuses to infer a level.** There is no level, proficiency, score,
confidence or seniority column in `0008`, no such field in the package, and no
count that could be read as one. Several accepted facts naming one skill are
several *evidences* of one association, never "more" of that skill, and a
mention like `Azure Data Platform (avancé)` stays one skill whose canonical name
still carries the parenthesis. Recording a level that is genuinely known is a
separate slice, and it starts by defining what evidence would prove it.

**It refuses to guess what somebody meant.** The comparison key is conservative
and exactly four operations — Unicode NFKC, trim, inner whitespace runs
collapsed to one space, casefold — so punctuation, symbols and accents all
survive it and `C`, `C++` and `C#` are three keys and three skills. On top of
it sits a **closed** registry of five aliases: `PowerBI → Power BI`,
`Postgres → PostgreSQL`, `sklearn → Scikit-learn`, `ML → Machine Learning`,
`IA → Artificial Intelligence`. Each canonical form resolves to its own entry,
so an alias and the canonical spelling of it are one skill. There is no
stemming, no fuzzy matching, no edit distance, no similarity, no punctuation or
accent stripping, no splitting of a mention and no enrichment of a name; a
mention no entry knows is kept literally, and a neighbouring technology is
never substituted for it. Two canonical skills claiming one key is a collision
the registry refuses at import time rather than resolving by priority. Every
result explains itself: the technical input, the comparison key, the canonical
key and name, `SKILL_NORMALIZER_VERSION` and the named rule that applied.

**It refuses to append blindly.** `synchronize_profile_skills` is a
reconciliation inside one `BEGIN IMMEDIATE` transaction: the profile must
exist, the accepted facts are read inside the transaction, associations still
justified are left exactly as they are — ids and timestamps included — what is
missing is created, evidence pointing at a fact that stopped being an accepted
skill fact is deleted, and an association left with no evidence at all is
deleted with it. Running it twice on unchanged facts writes nothing and reports
`changed=false`. A fact corrected or rejected after a run therefore stops
justifying a skill at the next run, and its `ACCEPTED` replacement becomes the
current proof, without anything else being asked of the operator.

**It refuses to write back.** No statement in the package inserts, updates or
deletes a `profile_facts` or `profile_fact_provenance` row, and a test reads
the SQL to keep it that way. The evidence table duplicates no provenance
column either: it records what the projection itself decided — which normalizer
version, which normalization rule — and points at the fact for everything else,
so the audit chain stays a chain rather than a copy that can drift. A canonical
`skills` row is never deleted: it is shared vocabulary, and on its own it says
nothing about any profile.

Phase 3.4A stops there. It adds no administrable alias table, no preference,
availability, mobility or career objective — structured experiences and
projects are the separate Phase 3.4B1 projection below, structured education,
certifications and languages the Phase 3.4B2 one, and what a person states
about themselves the Phase 3.4C one — no opportunity
constraint, no
eligibility rule, no skill extraction from an offer, no TF-IDF, no cosine
similarity, no matching, no match score, no ranking, no recommendation, no
notification, no CV adaptation and no auto-apply. There is no HTTP endpoint, no
web interface and no remote write path, and no network call or model takes
part. The exact schema is in
[database.md](database.md#normalized-profile-skills), and the local command is
in [operations.md](operations.md#profile-skills-phase-34a).

## Structured profile experiences and projects (Phase 3.4B1)

`services/digital_twin/structured_profile/` derives a second reading from facts
a person already accepted, and adds no new truth:

```text
profile_facts  (the source of truth, Phase 3.3A)
    |
    +-> profile_skills          (Phase 3.4A, above)
    +-> profile_experiences     ── structure_experience
    +-> profile_projects        ── structure_project
    +-> profile_educations      ── structure_education       (Phase 3.4B2)
    +-> profile_certifications  ── structure_certification   (Phase 3.4B2)
    +-> profile_languages       ── structure_language        (Phase 3.4B2)
    +-> profile_availability                                 (Phase 3.4C)
    +-> profile_mobility                                     (Phase 3.4C)
    +-> profile_preferences                                  (Phase 3.4C)
    +-> profile_career_objectives                            (Phase 3.4C)

    every row → its fact_id → profile_facts → profile_fact_provenance
```

`profile_facts` stays the source of truth. The projection reads
`fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'`, written into
the SQL rather than passed in, so a `PROPOSED`, `REJECTED` or `CORRECTED` fact
and an `ACCEPTED` fact of any other type are invisible to it and no caller can
widen the filter. This package imports no CV module: nothing from the Phase
3.2B extractor reaches a row without passing through a fact a human accepted.

It is built out of four refusals.

**It refuses to invent.** Every projected fragment is text the document itself
delimited with punctuation it wrote. `EXPERIENCE_PIPE_HEADER_V1` reads a first
line written `segment | segment | segment` — at least three segments that all
carry text, not opened by a list marker, with exactly one explicit period among
the segments after the first two — and stores the first as the role, the second
as the organization and the temporal one as the period, each trimmed and
otherwise untouched. `PROJECT_BULLET_COLON_V1` removes at most one list marker,
splits on the first `:` that is not a URL scheme, and stores the two sides;
a period leaves the title only as a final parenthesis holding a closed range of
two four-digit years. An explicit period is a **whole** fragment matching one
closed form — a year, `MM/YYYY`, a month from a closed French/English registry
with a year, a dash range of two of those, a range closed by one of the
open-end markers, a school year `YYYY/YYYY` kept verbatim, or a closed range
qualified by a parenthesis holding exactly one of those same open-end markers
(`2025-2026 (en cours)`), from which nothing is read: no `current` flag, no end
date, no duration and no employment status. A parenthesis holding free text —
`(6 mois)`, `(stage)`, `(Paris)` — is not one. No employer is
deduced from a sentence, no role from a technology, no seniority from the word
"stage", no duration, no calendar date from a school year and no skill from a
description. Structured education, certifications and languages are the
Phase 3.4B2 extension below, read by their own rules and never by these two.

**It refuses to guess rather than decline.** A rule applies or it does not:
there is no partial credit and no score. Zero periods in a header, two of them,
a date written where the role belongs, an empty side of a colon — each falls
back to `UNPARSED_V1`, and an `UNPARSED_V1` row carries `NULL` in every
fragment. **A `NULL` is worth more than an invented value.** The fact keeps the
full wording either way, so declining loses nothing. What is never allowed is
dropping the fact: every accepted fact is projected, exactly once, which is why
`experience_rows` always equals `accepted_experience_facts`.

**It refuses to append blindly.** `synchronize_structured_profile_entries` is a
reconciliation inside one `BEGIN IMMEDIATE` transaction: the profile must
exist, the accepted facts are read inside the transaction, a row the current
rules would write identically is left exactly as it is — id and timestamp
included — what is missing is created, a row pointing at a fact that stopped
being verified is deleted, and a row the rules now read differently is replaced
whole rather than patched. Running it twice on unchanged facts writes nothing
and reports `changed=false`. A fact corrected or rejected after a run therefore
stops being projected at the next run, and its `ACCEPTED` replacement takes its
place, without anything else being asked of the operator.

**It refuses to write back, or sideways.** No statement in the package inserts,
updates or deletes a `profile_facts` or `profile_fact_provenance` row, and none
names `profile_skills` or `profile_skill_evidence` at all — the Phase 3.4A
projection is a neighbour, not a dependency, and no skill is ever inferred from
an experience or a project. A test reads the SQL to keep it that way. The two
tables duplicate no provenance column either: they record what the projection
itself decided — which structurer version, which structuring rule — and point
at the fact for everything else, so the audit chain stays a chain rather than a
copy that can drift. `0009` makes the profile scope a database rule too, with a
composite foreign key on `(fact_id, profile_id)`.

Phase 3.4B1 stops there. Structured education, certifications and languages are
the Phase 3.4B2 extension below, and availability, mobility, preferences and
career objectives are the separate Phase 3.4C slice after it — they come from
the person, never from a document. It adds no eligibility rule, no opportunity constraint, no skill
inference, no skill level, no matching, no match score, no TF-IDF, no cosine
similarity, no ranking, no recommendation, no notification, no CV adaptation
and no auto-apply. There is no HTTP endpoint, no web interface and no remote
write path, and no network call or model takes part. The exact schema is in
[database.md](database.md#structured-profile-experiences-and-projects), and the
local command is in
[operations.md](operations.md#structured-profile-entries-phase-34b1-and-34b2).

## Structured education, certifications and languages (Phase 3.4B2)

Phase 3.4B2 is the **same** package, the same command and the same transaction,
reading three more fact types:

    fact_type IN ('EDUCATION', 'CERTIFICATION', 'LANGUAGE')
    AND status = 'ACCEPTED'

Migration `0010` adds `profile_educations`, `profile_certifications` and
`profile_languages` alongside the two tables `0009` created, with the same
composite foreign key on `(fact_id, profile_id)`, the same `UNIQUE(fact_id)`,
the same absence of any provenance column, and the same absence of any
`verified`, `confidence`, `score`, `seniority`, `match_score` or
`inferred_level` column.

`STRUCTURED_PROFILE_VERSION` stays `structured-profile-v1`, and that is a
decision rather than an omission. A version is a statement about *how a row was
produced*; `EXPERIENCE_PIPE_HEADER_V1`, `PROJECT_BULLET_COLON_V1` and
`UNPARSED_V1` read exactly what they read before, over exactly the same
temporal grammar, so the rows they produced were produced exactly as
`structured-profile-v1` says. **Adding a fact type is backward-compatible by
construction**: it introduces new rules for new types and changes none of the
existing readings, so no existing row is rewritten and a synchronization run
after this slice leaves every experience and project row where it was, id and
timestamp included. The day one of the older readings changes, the version
moves and every row is rewritten — that mechanism is untouched.

**Punctuation proves that segments exist; it never proves what they are
about.** That is the one idea this slice adds, and it is why education has its
own rules rather than reusing the experience one. A CV writes
`role | employer | dates` in that order and only that order; it writes a
diploma and a school in either. So no education rule ever reads a segment by
its position. Each of them asks **two** closed registries, by whole word, with
no stemming, no plural folding and no fuzzy comparison: institution markers
(`université`, `university`, `école`, `school`, `institut`, `institute`,
`faculté`, `faculty`, `college`) and programme markers (`master`, `bachelor`,
`licence`, `diplôme`, `diploma`, `degree`, `ingénieur`, `doctorat`).

**Each role needs its own exclusive proof**: one segment marked as an
institution and **not** as a programme, the other marked as a programme and
**not** as an institution. One marker is not enough, because a marker on one
segment proves nothing about the other. `University Diploma in AI | Sorbonne`
is the case that shows it — the first segment carries `university` and is
nevertheless the programme, since it carries `diploma` too, while the second
carries no marker at all. A rule trusting the institution marker alone would
store that inversion as a structured fact; requiring an exclusive proof of each
role leaves both fields `NULL` instead. A segment proving both roles at once
proves neither, and two segments proving the same role prove nothing. When the
proofs are there, the marked-as-institution one is the institution and the
other the programme, whichever order they were written in.

Because the marker does the work, the number of segments does not have to be
three. `EDUCATION_PIPE_EXPLICIT_V1` reads a header holding **one** explicit
period plus exactly two segments carrying those proofs.
`EDUCATION_PIPE_INSTITUTION_PROGRAM_V1` reads the shorter shape a CV writes
just as often — **exactly two** segments, **neither** of them an explicit
period, carrying the same proofs — and leaves `period_text` `NULL`, because a header
with no date is a header with no date and no year is ever looked for inside the
words. The experience rule still needs three segments, and for a reason that
does not apply here: it reads by position, so two segments leave it nothing to
anchor on. Requiring both segments to be free of a period is what keeps the
short rule honest — in `Université Exemple | 2020 - 2022` the unmarked segment
is a date, and calling it a programme would be exactly the invention this
package refuses, so that fact stays unparsed.

When a single period is certain and the distinction is not — a role with no
proof, a segment proving both roles at once, both segments proving the same
role, or a third segment remaining — the reading stops
halfway on purpose: `EDUCATION_PIPE_PERIOD_ONLY_V1` keeps the period verbatim
and the following lines as the description, and leaves `institution_text` and
`program_text` `NULL`. That is not partial credit: the period is *certain*, and
what is uncertain is absent rather than approximated. Everything else is
`EDUCATION_UNPARSED_V1`, with every fragment `NULL`.

`CERTIFICATION_EXPLICIT_V1` reads labels the document wrote, never a position
and never a phrase. Every segment of the first line must be readable — a whole
explicit period, or a `label: value` whose label is a closed registry entry
(`certification`, `certificat`, `certificate`; `délivré par`, `issued by`,
`issuer`, `organisme`, …) — the certification label must appear exactly once,
and no label may repeat. A fact stating an intention — a whole word of a closed
registry: `préparation`, `objectif`, `prévu`, `planned`, … — is never read as a
certification at all, anywhere in the fact. There is no `obtained`,
`obtained_at` or `expires_at` column, so naming a certification never records
holding one, no issuer is invented and no date is invented.

`LANGUAGE_EXPLICIT_PROFICIENCY_V1` applies when a single-line fact, once at
most **one** list marker has been removed, carries one explicit separator — a
trailing parenthesis, a colon, a pipe, or a dash the document spaced on both
sides — and everything on its right is a **whole** form
of the closed proficiency registry (`A1`…`C2`, `débutant`, `intermédiaire`,
`avancé`, `courant`, `fluent`, `native`, `natif`, `bilingue`, `bilingual`).
A CV writes its languages as a bulleted list as often as not and the Phase 3.2B
extractor keeps a bullet block's source text, so `• Anglais : C1` reaches the
structurer with its marker; the marker is punctuation the list wrote, not part
of the language's name, and storing `• Anglais` would be storing the layout.
Exactly one is removed, never two.

The registry decides *whether* the fragment is a level; it never translates
one. `proficiency_text` is stored verbatim, so **"courant" stays "courant"**:
there is no CEFR column, no mapping onto one, and no level derived from a
diploma, from a country or from a project written in English. Anything else is
`LANGUAGE_UNPARSED_V1`, with both fragments `NULL`.

The rest is unchanged: one `BEGIN IMMEDIATE` transaction for all five types,
the same reconciliation — create what is missing, delete what stopped being
justified, replace whole what the rules now read differently — the same
idempotence, the same profile scope, and the same refusal to write to
`profile_facts`, `profile_fact_provenance`, `skills`, `profile_skills` or
`profile_skill_evidence`. No skill is inferred from a diploma, a certification
or a language.

Phase 3.4B2 stops there. Availability, mobility, preferences and career
objectives are the Phase 3.4C slice below, and no rule in this package produces
one. It adds no eligibility rule, no opportunity constraint, no skill
inference, no level of any kind, no matching, no match score, no TF-IDF, no
cosine similarity, no ranking, no recommendation, no notification, no CV
adaptation and no auto-apply. There is no HTTP endpoint, no web interface and
no remote write path, and no network call or model takes part. The exact schema
is in
[database.md](database.md#structured-profile-education-certifications-and-languages),
and the command is the same one, in
[operations.md](operations.md#structured-profile-entries-phase-34b1-and-34b2).

## Explicit profile input (Phase 3.4C)

`services/digital_twin/preferences/` records and projects the four things a CV
cannot say: when somebody can start, where they will go, what they are looking
for, and what they are aiming for.

```text
explicit user input (a person types it)
    |
    v
profile_facts  (ACCEPTED, with USER_INPUT provenance)
    |
    +-> profile_availability        AVAILABILITY
    +-> profile_mobility            MOBILITY
    +-> profile_preferences         PREFERENCE
    +-> profile_career_objectives   CAREER_OBJECTIVE

    every row → its fact_id → profile_facts → profile_fact_provenance
    no accepted fact  →  no row  →  UNKNOWN
```

The reading is `fact_type` and `status = 'ACCEPTED'` **and** an `EXISTS` over
`profile_fact_provenance` requiring `source_type = 'USER_INPUT'`, all three
written into the SQL rather than passed in. Acceptance alone is not the
contract here: a `CV`, `GITHUB` or `OTHER_ACCEPTED_EVIDENCE` proof is the wrong
kind of evidence for a statement about what somebody wants, so an `ACCEPTED`
fact resting only on one is invisible to the projection — and to the write
side, which asks the same question through the same function, so the rows can
never describe a different set of facts than a `set_*` call decides against.
`EXISTS` rather than a join, so a fact carrying several `USER_INPUT` proofs is
projected once and not once per proof.

The difference from 3.4B is where the claim comes from, and it is the whole
point of the slice. `0009`/`0010` project what a **document** said about a past
that already happened; `0011` projects what a **person** said about what they
want next. **What somebody has done is not what somebody wants**, so nothing
here is inferred, in either direction: no availability date from a CV period, a
diploma year or the clock — this package imports no clock at all; no mobility
from an address, a city, a country or a past employer, and no geocoding, no
country lookup and no region expansion anywhere; no work mode from a past
remote job; no preferred domain from a skill, a project or a CV section; no
convention status from being a student; no visa need from a nationality or a
location; and no career objective from a CV's professional title.

The package is five modules with one responsibility each: `models.py` holds the
closed registries and the frozen, self-validating statements; `codec.py` the
canonical JSON both ways; `repository.py` the projection and its reconciliation;
`service.py` the create/no-op/correct decision; `cli.py` the six commands.

**Canonical JSON is what makes the rest work.** A fact's value has exactly one
encoding — keys sorted, no padding, closed registries in declaration order, free
text keeping the person's order after an exact first-occurrence dedup — so "the
person restated what they already said" is a string comparison rather than a
guess, and restating a preference in another order is a no-op instead of a
correction nobody made. Decoding is strict in the other direction: a value whose
own re-encoding is not byte-identical is refused rather than repaired, because
there is no best-effort reading of a preference.

**Every domain is a singleton**, so each `set-*` resolves to `CREATED`,
`UNCHANGED` or `CORRECTED`, reusing the existing lifecycle — the correction is
`correct_profile_fact`, which keeps the old value readable and points it at its
replacement. The one new primitive is
`record_verified_user_input_fact`, the missing first step of that same
reasoning: a person typing their own availability is not a proposal somebody
else needs to review, so the first statement is `ACCEPTED` directly, with its
`USER_INPUT` provenance, in one transaction. Nothing about the lifecycle is
bypassed — the fact can still be corrected or rejected, its status still moves
only through the facts package, and it still cannot exist without evidence.

**A domain holding two `ACCEPTED` facts is refused out loud**, and the whole
synchronization rolls back with it. Picking the most recent one would decide,
on nobody's behalf, which of two things somebody said counts.

**An absence is `UNKNOWN`, and `UNKNOWN` has no row.** `0011` seeds nothing,
defaults nothing and creates no "unknown" row, so nothing downstream can read
"never said" as "said no". `ConventionStatus.UNKNOWN` and
`VisaSponsorshipRequired.UNKNOWN` are not a contradiction: those sit inside a
preference the person did state, where "I don't know yet" is the answer they
gave.

Phase 3.4C stops there. It parses no offer and computes no eligibility, produces no `ELIGIBLE`/`NOT_ELIGIBLE` verdict, compares
no location and no date against an offer, and adds no matching, match score,
TF-IDF, cosine similarity, ranking, recommendation or notification. It is the
profile side of Phases 3.5 and 3.6, and neither is implemented. There is no HTTP
endpoint, no web interface and no remote write path, and no network call or
model takes part. The exact schema is in
[database.md](database.md#explicit-profile-input), and the commands are in
[operations.md](operations.md#explicit-profile-input-phase-34c).

## Opportunity constraints (Phase 3.5A)

`services/collector/extractors/opportunity_constraints/` reads a posting and
records what **the posting** asks for. It is the mirror image of Phase 3.4C:
that slice records what a person says they want, this one records what an offer
says it requires, and **the two are never compared here**. Joining them is
Phase 3.6, ranking them is Phase 4, and neither is implemented.

```text
opportunities
    -> extract_opportunity_constraints   (pure, offline, deterministic)
         -> opportunity_constraints
              +-> opportunity_constraint_locations
              +-> opportunity_education_requirements
              +-> opportunity_constraint_evidence
              +-> opportunity_constraint_conflicts
```

The package is six modules with one responsibility each: `models.py` holds the
closed registries and the frozen readings, `text.py` turns a collected
description into text the rules can read, `rules.py` is the closed rule
registry, `extractor.py` is the pure reading and the fingerprint,
`repository.py` the transactional projection, `service.py` the orchestration
and `cli.py` the three commands. The extractor opens no database: it is handed
an `OpportunitySource` value object and returns an `ExtractedConstraints`,
which is what makes it testable in memory against real postings without writing
anywhere.

**Two registries are imported rather than redefined.** `OpportunityType` and
`WorkMode` come from `services.digital_twin.preferences.models`, the Phase 3.4C
vocabulary, so the profile side and the offer side speak one language and
Phase 3.6 cannot end up comparing two registries that merely look alike. The
import direction is safe — that module imports only the standard library — and
a test pins the names to the same objects so a future divergence fails loudly.

**Text normalization is load-bearing, not cosmetic.** The Greenhouse collector
stores its `content` field verbatim, and that field is HTML with its angle
brackets escaped as entities; nothing upstream unescapes it, because the
LinkedIn alert collector produces no description at all and no other path ever
needed to. A rule looking for `minimum 3 years` in `&lt;p&gt;Minimum 3
years&lt;/p&gt;` would find nothing and report UNKNOWN for a posting that said
it plainly. So `text.py` unescapes entities, drops tags on a line break, and
collapses whitespace — and changes no word beyond that.

**UNKNOWN is the default and is never FALSE.** Every value needs a named rule
and an explicit fragment; without one the field is UNKNOWN, stored as `NULL`. A
title naming seniority states no number of years, an address states no
attendance policy, a country states no visa policy, and the word "internship"
states neither an agreement nor a duration nor a student.

**Contradictions are recorded, not resolved.** Two strong readings that
disagree leave the field UNKNOWN and write a conflict row naming the values and
the rules. The unit is the **slot**, not the kind: a posting can disagree with
itself about how much experience it wants and, separately, about whether it
insists, and those are two contradictions with two answers rather than one row
mixing `36-` with `REQUIRED`. The one documented exception is the opportunity type, where the
title outranks the description — the precedent is the Phase 2 classifier, which
already decides it that way, and a posting titled "PFE" whose body says
"internship" is not contradicting itself.

**Everything asserted is explainable**, projected places included: a location
read from the collected `location` or `country` field carries its own rule and
its own evidence, so no projection is a value a reader cannot trace. Each value
carries the rule id, the source field and the minimal fragment matched, capped so evidence stays a
pointer into the posting rather than a copy of it. There is no confidence and
no score: a rule fired or it did not.

Idempotence is `source_fingerprint` plus `extractor_version`, the pattern
Phase 2 qualification already uses. The exact schema is in
[database.md](database.md#opportunity-constraints) and the commands are in
[operations.md](operations.md#opportunity-constraints-phase-35a).

Phase 3.5A stops there. Skill and language requirements are **not** extracted:
counting every mention of Python or SQL as a requirement would fill the
projection with the contents of "our stack includes…" paragraphs and with
skills a posting said it would teach, and a requirement nobody wrote is exactly
what this slice refuses to invent. Doing it properly needs sentence-level
context, which is Phase 3.5B. `opportunity_skill_requirements` exists now,
empty, only so that 3.5B extends the shared `skills` catalogue rather than
starting a rival one.

## Data-processing boundaries

- Opportunity persistence provides repeat-observation idempotence for a source.
- Cross-source duplicate audit is read-only and heuristic. Its
  `STRONG_CANDIDATE`, `POSSIBLE_CANDIDATE`, and `WEAK_CANDIDATE` labels are review
  aids, never automatic merge decisions.
- Human decisions are persisted separately. Physical merge requires an explicit
  `CONFIRMED_DUPLICATE` decision and an explicit canonical opportunity; merge and
  rollback are transactional and preserve audit history.
- Qualification is deterministic, explainable, versioned derived data. It is not
  personalized matching, ranking, or a recommendation model, and geography is
  metadata rather than an exclusion rule.
- Source run history is recorded evidence, not a judgement. A run row states
  what one attempt observed, with unknown metrics left `NULL` rather than
  filled with zeros, and an unfinished attempt left `RUNNING` rather than given
  an invented ending.
- The user/profile root records ownership, not knowledge. It stores who owns
  a Digital Twin and the stable profile that owns nothing yet, and it asserts
  no fact about the person.
- The CV parser describes a document, not a person. A `DetectedSection` states
  that a recognised heading introduced these lines on these pages; it does not
  state that the person holds a diploma, a skill or a job. No parsed value is
  validated, and none of it is stored.
- A profile fact is a decision, not a reading. A `profile_facts` row states
  what a human decided about one claim, and its provenance states where that
  claim was read; the two are separate rows on purpose. `ACCEPTED` is the only
  thing that means verified, a correction adds a fact instead of replacing a
  value, and a rejection is kept rather than deleted, so the record of what was
  once proposed survives the decision taken about it.
- Importing a CV proposes; it never confirms. The bridge turns every candidate
  into a `PROPOSED` fact and stops there, its identity is the evidence rather
  than the text, and the only thing that can make one of those facts verified is
  a person answering the review prompt. An import that runs twice proposes
  nothing twice, and an import can never revive a claim somebody already
  refused.
- Re-reading a CV reconciles; it confirms nothing. A newer campaign that reads
  the same type and the same text is the same reading, so it attaches its proof
  to the fact that already exists rather than duplicating it, and a reading
  that changed is a proposal a person answers. Retiring an old reading is a
  `REJECTED` status and never a deletion, it happens only after every new
  reading has been confirmed, and an old proposal nobody ever answered is left
  as it is.
- A skill is a projection, not a source. A `profile_skills` row exists because
  an `ACCEPTED` `SKILL` fact says so, and it stops existing when no accepted
  fact says so any more. It carries no level and no count, holding a skill
  says nothing about how well, and a `skills` row on its own is vocabulary
  rather than a claim about anybody.
- A CV candidate is a reading, not a fact. An `ExtractedCandidate` states that
  one named rule found this text on this page of this section; it does not
  state that the person is called that, works there or knows that. Nothing is
  accepted, corrected or rejected, nothing carries a level or a score, and
  nothing is written anywhere. Where a rule cannot justify a reading, the
  extractor produces no candidate and says so in a warning rather than
  guessing. Reading an unknown address returns an explicit
  absence and never invents a profile; a user and its profile are created only
  by the explicit `ensure_user_profile` operation behind `init-profile`.
- Source health reads that evidence back without adding to it. It derives one
  entry per known source at read time, and the only judgement it makes is the
  repeated-zero anomaly described below. Deciding that a `RUNNING` row is stale
  remains a separate concern that does not exist yet.

## Outside Phase 2

There is no production scheduler or continuous deployment path, authenticated
LinkedIn-session scraper, automatic application flow, personalized ranking,
CV-to-offer recommendation engine, or ML recommendation model. The Digital
Twin exists only as the empty `users`/`profiles` root added by Phase 3.1A, the
read-only CV parser added by Phase 3.2A, the unverified candidates added by
Phase 3.2B, the validated fact store added by Phase 3.3A, the bridge and
local review command added by Phase 3.3B, the re-read reconciliation added by
Phase 3.3C, the skill projection added by Phase 3.4A, the structured
experiences and projects added by Phase 3.4B1, the structured education,
certifications and languages added by Phase 3.4B2, and the availability,
mobility, preferences and career objectives a person states, added by Phase
3.4C, all described above. Phase 3.5A adds the constraints an **opportunity**
states, which is the offer side of the same future comparison and is joined to
no profile. No
matching, ranking or scoring is derived
from any of them, no CV candidate is ever imported as anything but a proposal,
no skill level is inferred from anything, no role, employer, duration or
seniority is inferred from a description, and the only review that exists is a
terminal command — there is no web profile interface and no Master CV PDF.
Source health detects exactly one anomaly, read-only, and does nothing with it
beyond returning and displaying it. There is no alerting of any kind: no email,
no web push, no notification path, no scheduler and no GitHub Actions schedule,
no stale-`RUNNING` detection, no automatic retry, and no self-healing collector.
Those possible later capabilities must not be inferred from the implemented
qualification taxonomy, the recorded run history, the source health read model,
or reserved package names.
