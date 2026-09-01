# Database

## Operational database

Persistent local **SQLite** is the operational Phase 2 database. Connections
enable foreign-key enforcement, and collection, qualification, duplicate
decision, and merge writes are local SQLite operations.

Turso/libSQL remains only a Foundation/read-only connectivity and schema
experiment where applicable. Although runtime Foundation utilities retain
backend selection, remote Turso opportunity writes are disabled and are not an
operational Phase 2 path. Do not enable or recommend Turso writes.

## Migrations

Ordered SQL files are applied transactionally and recorded in
`schema_migrations`. A failed file is rolled back and its version is not
recorded. The migrations currently present are:

1. `0001_opportunity_foundation.sql`: `sources`, `opportunities`, and
   `opportunity_sources` plus indexes;
2. `0002_deduplication_decisions.sql`: the explicit duplicate-review registry;
3. `0003_deduplication_merges.sql`: merge history and source-movement history;
4. `0004_opportunity_qualifications.sql`: persistent versioned qualification;
5. `0005_source_runs.sql`: persistent history of attempted source runs;
6. `0006_user_profile_foundation.sql`: the `users` identity root and the
   `profiles` Digital Twin root;
7. `0007_profile_facts.sql`: the validated `profile_facts` and their
   `profile_fact_provenance` evidence.

Apply every pending migration to configured local SQLite explicitly:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.collector.cli.migrate_configured --apply
```

Neither `RadarAgent` nor the FastAPI application applies migrations implicitly.
`python -m services.collector.cli.db_health` performs a read-only `SELECT 1`, and
`python -m services.collector.cli.db_schema` performs read-only Foundation
schema verification.

## Opportunity persistence

Each collected source batch is one transaction: source metadata is upserted,
then opportunities and their `opportunity_sources` observations are created or
refreshed. Repeat observations use `(source_id, source_url)` to find the existing
source occurrence, preserve discovery/first-seen timestamps, and refresh
last-seen data. A later `NULL` does not erase an existing optional value.

That same-source behavior is not the whole deduplication design. Cross-source
duplicate handling is deliberately review-driven:

- `audit_duplicates` examines pairs read-only and classifies retained candidates;
- a review scan may stage eligible candidates in `deduplication_decisions`, and a
  reviewer explicitly records `CONFIRMED_DUPLICATE` or `NOT_DUPLICATE`;
- candidate classifications are not `AUTO_MERGE` decisions, and similarity alone
  never changes opportunity rows;
- merge requires a confirmed pair and an explicit canonical opportunity;
- one SQLite transaction moves the merged row's `opportunity_sources` to the
  canonical opportunity, fills permitted missing canonical fields, records the
  before/after state and moves, and marks the other opportunity inactive with
  status `merged_duplicate` rather than deleting it;
- rollback is transactional and restores recorded state/source ownership only
  when LIFO and data-drift safety checks pass.

The retained tombstone, decision, merge, and movement rows make applied changes
auditable and reversible instead of destructive.

## Qualification persistence

Migration `0004` stores one current derived result per eligible opportunity,
including explanatory signals, classifier version, a SHA-256 input fingerprint,
and timestamps. The current classifier version is `qualification-rules-v1`.
Reconciliation leaves a row unchanged when both its input fingerprint and
classifier version match; otherwise it inserts or updates atomically. Merged,
inactive duplicates are excluded. `RadarAgent` runs this global reconciliation
once after its source loop; the standalone CLI remains available for audit or
maintenance.

Closed taxonomy values are:

- **Qualification:** `CORE_TARGET`, `ADJACENT_TARGET`, `OUT_OF_SCOPE`,
  `UNCERTAIN`.
- **Primary domain:** `DATA_ENGINEERING`, `DATA_SCIENCE`,
  `MACHINE_LEARNING_AI`, `GENAI_LLM`, `MLOPS_ML_PLATFORM`, `BI_ANALYTICS`,
  `DATA_QUALITY_GOVERNANCE`, `OTHER_DATA_AI`, `NON_TARGET`, `UNKNOWN`.
- **Opportunity type:** `PFE`, `INTERNSHIP`, `GRADUATE`, `APPRENTICESHIP`,
  `JOB`, `UNKNOWN`.
- **Employment type:** `FULL_TIME`, `PART_TIME`, `CONTRACT`, `TEMPORARY`,
  `UNKNOWN`.
- **Listing quality:** `NORMAL_LISTING`, `POSSIBLE_NON_JOB_PAGE`,
  `INSUFFICIENT_CONTENT`.

These classifications do not implement personalized matching or ranking.

## User and profile root

Migration `0006` adds the two roots Phase 3 builds on, and nothing else.

`users` is the identity and ownership root:

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `email` | `NOT NULL`, `UNIQUE COLLATE NOCASE`, and a `CHECK` that refuses an empty or untrimmed value |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`profiles` is the stable Digital Twin root:

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `user_id` | `NOT NULL UNIQUE`, `FOREIGN KEY → users(id) ON DELETE CASCADE` |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`UNIQUE(user_id)` is what makes **one user own at most one profile** a database
rule rather than an application convention, and the cascade expresses that a
profile exists only for as long as its owner does. The migration creates the
two tables and inserts no row: an identity is created by an explicit local
command, never by applying a migration.

`profiles` deliberately carries no factual column, and `0007` adds none. A
headline, a location, an education level, mobility, or availability are facts
with a provenance, and provenance is the subject of `profile_facts` below.
Putting them here would make the root itself the place where facts are
overwritten without any record of where they came from.

`services/digital_twin/repository.py` owns the two operations on these tables.
`ensure_user_profile` normalizes the address, then creates or returns the pair
inside one explicit transaction, so a first call creates one user and one
profile and any later call with the same address — in any case, with any
surrounding spaces — returns the same ids and creates nothing. Any failure
rolls the whole transaction back, so a user is never left without the profile it
owns. `get_user_profile_by_email` reads the pair back and answers an explicit
absence for an unknown address; a user row without its profile is refused as a
broken invariant rather than answered with an invented profile.

Address normalization lives in `services/digital_twin/identity.py`: trim, then
lowercase the whole address, with conservative rejection of a manifestly
malformed value rather than an RFC 5322 validator. `COLLATE NOCASE` on the
column keeps the same guarantee at the database level.

Initialise or read the local profile without putting an address in the shell
history:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m services.digital_twin.cli init-profile   # prompts, no echo
python -m services.digital_twin.cli show-profile
```

The command prints `user_id` and `profile_id` only, never the address, and
never logs it. A non-SQLite backend is refused locally, before any connection.

Not implemented by that slice: `cv_versions`, preferences, eligibility,
matching, scoring, and any profile HTTP API. `profile_facts` arrives with
`0007` below, the skills projected from it with `0008`, and the structured
experiences and projects projected from it with `0009`.

The Phase 3.2A CV parser adds no table and no migration. It reads a local PDF
and returns a result in memory; nothing it extracts is written to the database,
and no `profile_facts` row exists yet. See
[operations.md](operations.md#cv-parser-phase-32a).

The Phase 3.2B candidate extractor adds no table and no migration either. It
turns that in-memory parse into unverified candidates that also stay in memory:
no candidate table, no `cv_versions`, no SQLite write, and no row in `users`,
`profiles` or `profile_facts`. See
[operations.md](operations.md#cv-candidate-extraction-phase-32b).

The Phase 3.3B bridge adds no table and no migration either. It writes
candidates into the `profile_facts` and `profile_fact_provenance` tables `0007`
already created, always as `PROPOSED` rows, and it stores no candidate table of
its own: there is no import log, no second registry and no `cv_versions`. See
[operations.md](operations.md#cv-review-phase-33b).

`0008` adds the Phase 3.4A skill projection described in
[Normalized profile skills](#normalized-profile-skills) below, `0009` the
Phase 3.4B1 structured projection described in
[Structured profile experiences and projects](#structured-profile-experiences-and-projects),
`0010` the Phase 3.4B2 extension described in
[Structured profile education, certifications and languages](#structured-profile-education-certifications-and-languages),
and `0011` the Phase 3.4C explicit input described in
[Explicit profile input](#explicit-profile-input).
None of the four adds a column to `profiles`, `profile_facts` or
`profile_fact_provenance`, and the migrations stop there.

## Profile facts and their provenance

Migration `0007` adds the persistent anti-hallucination foundation, and nothing
else: two tables, no column on `profiles`, no seeded row.

### `profile_facts`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_type` | `NOT NULL`, non-empty and already trimmed |
| `value` | `NOT NULL`, non-blank; the source's own wording, kept verbatim |
| `normalized_value` | nullable; set only where a technical normal form is unambiguous, and non-blank when present |
| `status` | `NOT NULL`, one of `PROPOSED`, `ACCEPTED`, `CORRECTED`, `REJECTED` |
| `replaced_by_fact_id` | nullable, `FOREIGN KEY → profile_facts(id) ON DELETE RESTRICT` |
| `created_at`, `updated_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |
| `decided_at` | nullable: when a human decided |

A table `CHECK` makes the four shapes of a fact a database rule rather than an
application convention:

| status | `decided_at` | `replaced_by_fact_id` |
| --- | --- | --- |
| `PROPOSED` | `NULL` | `NULL` |
| `ACCEPTED` | set | `NULL` |
| `REJECTED` | set | `NULL` |
| `CORRECTED` | set | set |

A proposal has been decided by nobody, so it carries no decision date; a
decision is always dated; and only a corrected fact carries a replacement — and
a corrected fact always carries one, which is what keeps the previous value
reachable instead of lost. A fact cannot replace itself, `decided_at` cannot
precede `created_at`, and a partial unique index on `replaced_by_fact_id` makes
a replacement replace exactly one fact, so a correction chain stays a chain.

There is deliberately **no `verified` column**, and no column named anything
like it. "Verified" has exactly one definition — `status = 'ACCEPTED'` — because
two places to say the same thing is one place too many, and the one that drifts
is the one that gets believed. `ProfileFact.is_verified` is a computed Python
property over the status, never a stored second answer. There is no confidence,
no score, no proficiency and no skill level anywhere in the slice either.

No factual column is added to `profiles`. A fact belongs beside the evidence
that produced it, never in the root that owns it, where it could be overwritten
without a trace.

### `profile_fact_provenance`

Evidence, stored separately from the decision it supports. Every fact the
repository creates gets at least one provenance row in the same transaction: a
fact with no evidence would be an assertion nobody could check.

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `fact_id` | `NOT NULL`, `FOREIGN KEY → profile_facts(id) ON DELETE CASCADE` |
| `source_type` | `NOT NULL`, one of `CV`, `GITHUB`, `USER_INPUT`, `OTHER_ACCEPTED_EVIDENCE` |
| `provenance_key` | `NOT NULL`, non-empty, `UNIQUE (fact_id, provenance_key)` |
| `source_locator` | nullable |
| `cv_sha256` | nullable; exactly 64 lowercase hex characters when present |
| `parser_version`, `extractor_version` | nullable |
| `candidate_fingerprint` | nullable; hexadecimal when present |
| `rule_id` | nullable |
| `page_numbers` | nullable; canonical JSON array of ascending page numbers written without spaces, `'[1,2]'` |
| `section_type`, `section_index` | nullable |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

Those columns are exactly what one Phase 3.2B `ExtractedCandidate` already
carries — `cv_sha256`, `parser_version`, `extractor_version`, `fingerprint`,
`rule_id`, the pages, the section and its index — and the Phase 3.3B bridge
stores them field for field, without loss and without adding anything the
candidate did not carry. `source_locator` stays `NULL` for CV evidence: the only
locator that side could supply is the local path of the PDF, and a CV filename
usually carries the person's name.

`UNIQUE (fact_id, provenance_key)` is what stops the same proof from being
recorded twice for one fact, so a duplicate can never look like corroboration.
The key may be given explicitly; left unset, the repository derives it as a pure
function of the evidence fields, with no clock and no counter in it, so the same
proof always resolves to the same key. Evidence accumulates and is never
rewritten: a second, genuinely different proof is added beside the first.

A provenance row never invents what its source did not carry. An absent page,
rule or version stays `NULL` and is never defaulted to a plausible value, and
`NULL` means "this source did not carry it", never zero.

### The validation cycle

`services/digital_twin/facts/repository.py` owns every operation, in the style
of the user/profile root: plain SQLite, no ORM, and one explicit
`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` around each multi-step write, so a
failure rolls the whole operation back.

| from | to | how |
| --- | --- | --- |
| — | `PROPOSED` | `propose_profile_fact`, with its first provenance |
| `PROPOSED` | `ACCEPTED` | `accept_profile_fact` |
| `PROPOSED` | `REJECTED` | `reject_profile_fact` |
| `PROPOSED` | `CORRECTED` | `correct_profile_fact` |
| `ACCEPTED` | `REJECTED` | `reject_profile_fact` |
| `ACCEPTED` | `CORRECTED` | `correct_profile_fact` |

`REJECTED` and `CORRECTED` are terminal. Accepting an already-accepted fact and
rejecting an already-rejected one are idempotent no-ops that leave the original
`decided_at` alone, so re-clicking never restamps the moment the real decision
was taken. Every other move — accepting a rejection, correcting a correction —
raises `InvalidFactTransitionError` rather than being silently absorbed.

Every mutation is scoped by `profile_id` as well as by `fact_id`. A fact id from
another profile is reported as missing rather than mutated, so no cross-profile
write is possible even with a valid id.

### Correction never overwrites

A correction is one transaction that does five things and commits or rolls back
as a whole:

1. the new value is written as a **new** `profile_facts` row, `ACCEPTED`,
   because a person typing the right value is an explicit human validation and
   not another proposal to review;
2. that new fact receives a `USER_INPUT` provenance;
3. the previous fact becomes `CORRECTED`;
4. its `replaced_by_fact_id` points at the replacement;
5. its own `value` is left exactly as it was.

There is no `UPDATE profile_facts SET value = ...` anywhere in the package, and
a test asserts that on the SQL the module actually executes. Successive
corrections therefore build a chain in which every value ever proposed stays
readable, and only the last link is `ACCEPTED`.

### The verified reading

`list_verified_profile_facts(connection, profile_id)` is the one reading later
phases are meant to build on. It filters on `status = 'ACCEPTED'` in SQL, so a
`PROPOSED`, `REJECTED` or `CORRECTED` fact cannot leak into it and a caller
cannot forget the filter. `list_profile_facts` is the separate review and audit
reading that deliberately shows every status.

### Importing a CV into these tables (Phase 3.3B)

`ensure_profile_fact_proposal` is the primitive the CV bridge uses, and the only
one that writes a fact whose existence depends on what is already there. In one
`BEGIN IMMEDIATE` transaction it resolves the deterministic `provenance_key` of
the evidence, joins `profile_facts` to `profile_fact_provenance` to find a fact
**of this profile** that key already justifies, and either returns that fact or
creates a `PROPOSED` fact and its provenance row together. It reports whether it
created anything, so a caller can count new proposals without comparing rows.

The join is what scopes the lookup: `provenance_key` is unique per fact, not per
profile and not per database, so the same proof may legitimately exist under
another profile and must stay invisible from here. Two rules are enforced rather
than guessed at — if two facts of one profile share a proof, or if a known proof
would suddenly justify a different `fact_type`, the call raises instead of
choosing.

The lookup ignores `status`. A fact already `ACCEPTED`, `REJECTED` or
`CORRECTED` is returned as it stands, so re-importing a CV never revives a claim
that was refused and never duplicates one that was confirmed. Because the key
includes `cv_sha256`, two different CVs are two different proofs and produce two
proposals; no consolidation across CV versions is attempted.

### Re-reading one CV into these same tables (Phase 3.3C)

A newer parser or extractor reading the same file is a **new campaign**: same
`cv_sha256`, different `parser_version` and `extractor_version`, therefore a
different `provenance_key` for every candidate. Left to
`ensure_profile_fact_proposal` alone, that would propose the whole document
again, so Phase 3.3C compares the two campaigns first and writes through two
primitives instead of one.

`list_profile_facts_by_cv_evidence` reads the older campaign back:
`profile_facts` joined to `profile_fact_provenance` on `profile_id`,
`source_type = 'CV'`, `cv_sha256`, `parser_version` and `extractor_version`.
`source_type` is written into the statement rather than passed in, so a
person's own `USER_INPUT` corrections can never be read back as if a document
had produced them, and `DISTINCT` keeps a fact carrying several proofs of one
campaign from being listed twice. `status` takes no part: a fact belongs to the
campaign that read it whatever was decided about it since.

`ensure_profile_fact_provenance` is then the primitive for a reading that has
not changed: it inserts a `profile_fact_provenance` row for an existing fact
unless that exact `provenance_key` is already attached to that exact fact, in
which case it returns the existing row and reports `created = false`. Where
`add_profile_fact_provenance` lets `UNIQUE (fact_id, provenance_key)` refuse a
duplicate, this is the no-op form of the same write, so a reconciliation can be
re-run. It refuses rather than guesses in the same two ways as the proposal
primitive — several facts of one profile sharing a proof, or that proof already
justifying a *different* fact — and it reads nothing about the fact's status,
because attaching evidence is not a decision.

No `profile_facts` row is created for an unchanged reading, so a re-read never
duplicates a fact, and none is ever deleted: retiring an old reading is an
`ACCEPTED → REJECTED` transition of the ordinary cycle, leaving the row, its
`value`, its `normalized_value` and its own campaign's provenance in place. A
fact that is already `REJECTED`, one that is terminal `CORRECTED` and one still
`PROPOSED` are all left exactly as they are.

Phase 3.3C adds no table, no column and no migration. The local command is in
[operations.md](operations.md#cv-reconciliation-phase-33c).

### Not implemented by these slices

Nothing in `0007` normalizes an institution, an employer, a date or a canonical
role, and no preference, availability, mobility, eligibility or matching table
exists anywhere. Skill aliases are normalized by `0008` below, and by nothing
else. No Master CV, cover letter, application, form or CV adaptation is
generated from these facts, and no future application flow may write to them
directly. There is no HTTP endpoint, no web interface and no authentication
over these tables: the only review that exists is the local terminal command
described in [operations.md](operations.md#cv-review-phase-33b), and the
reconciliation above confirms nothing on its own either.

## Normalized profile skills

Migration `0008` adds the Phase 3.4A projection of **verified** skill facts, and
nothing else: three tables, no column on any existing table, no seeded row.

`profile_facts` stays the source of truth. Every row below is derived from
facts whose status is already `ACCEPTED`, and the projection reads
`fact_type = 'SKILL' AND status = 'ACCEPTED'` and nothing else.

**No level exists in this schema.** There is no level, proficiency, score,
confidence, seniority or occurrence-count column, and none may be added without
first defining what evidence would prove it. Several accepted facts naming one
skill are several evidences of one association, never "more" of that skill.

No row of these three tables is ever updated by the projection, so none carries
`updated_at`: reconciliation inserts what is missing and deletes what has
stopped being justified.

### `skills`

The canonical identity of a skill, shared by every profile.

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `canonical_key` | `NOT NULL UNIQUE`, non-empty and already trimmed; the conservative technical key |
| `canonical_name` | `NOT NULL`, non-empty and already trimmed; the display form |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

A row here is **vocabulary, not a claim**: it says the canonical name exists
under this key, never that anybody holds it. Only a `profile_skills` row says
that, and a `skills` row left behind by a rejected fact is harmless.

### `profile_skills`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `skill_id` | `NOT NULL`, `FOREIGN KEY → skills(id) ON DELETE RESTRICT` |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`UNIQUE(profile_id, skill_id)` is what makes **one profile holds one skill at
most once** a database rule rather than an application convention: two accepted
facts naming the same skill are two evidences of this single row. The cascade
expresses that a projection exists only for as long as the profile it describes;
the `RESTRICT` expresses that a skill still held cannot be deleted from the
vocabulary. Indexes: `idx_profile_skills_profile`, `idx_profile_skills_skill`.

### `profile_skill_evidence`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_skill_id` | `NOT NULL`, `FOREIGN KEY → profile_skills(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, `FOREIGN KEY → profile_facts(id) ON DELETE CASCADE` |
| `normalizer_version` | `NOT NULL`, non-empty and already trimmed |
| `normalization_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`UNIQUE(fact_id)` — global, not per association — says that one verified fact
justifies exactly one skill of one profile: a fact appearing under two skills
would mean the projection read one claim two ways, which is a defect rather
than corroboration. Several facts may justify the same association, which is
what an alias and its canonical spelling produce. Index:
`idx_profile_skill_evidence_profile_skill`.

The table deliberately **does not duplicate `profile_fact_provenance`**. It
records only what the projection itself decided — which normalizer version,
which normalization rule — and points at the fact for everything else, so the
audit chain stays a chain:

```text
profile_skill → profile_skill_evidence → profile_fact → profile_fact_provenance
```

### The normalizer

`SKILL_NORMALIZER_VERSION` is `skill-normalizer-v1`. The comparison key is
exactly four operations — Unicode NFKC, trim, inner whitespace runs collapsed
to one space, casefold — so punctuation, symbols and accents survive and `C`,
`C++` and `C#` are three keys and three skills. On top of it sits a **closed**
v1 alias registry:

| written | canonical |
| --- | --- |
| `PowerBI` | `Power BI` |
| `Postgres` | `PostgreSQL` |
| `sklearn` | `Scikit-learn` |
| `ML` | `Machine Learning` |
| `IA` | `Artificial Intelligence` |

Each canonical form resolves to its own entry, so an alias and the canonical
spelling of it are one skill; the rule recorded on the evidence distinguishes
the two (`ALIAS_REGISTRY_V1`, `CANONICAL_FORM_V1`, or `LITERAL_V1` for a
mention no entry knows). Two canonical skills claiming one key is a collision
the registry refuses at import time. There is no stemming, no fuzzy matching,
no edit distance, no similarity, no punctuation or accent stripping, no
splitting of a mention and no enrichment of a name, and the registry is not
administrable from the database: growing it is a code change that moves the
normalizer version.

### Synchronizing

`synchronize_profile_skills(connection, profile_id)` is a reconciliation, not
an append, and the whole of it is one `BEGIN IMMEDIATE` transaction:

1. the profile must exist — synchronizing creates no user and no profile;
2. the `ACCEPTED` `SKILL` facts are read **inside** the transaction;
3. evidence pointing at a fact that is no longer one of them is deleted;
4. missing canonical skills, associations and evidences are created;
5. an association left with no evidence at all is deleted;
6. `profile_facts` and `profile_fact_provenance` are never written.

A second run on unchanged facts writes nothing, keeps every id and timestamp,
and reports `changed=false`. A fact corrected or rejected after a run stops
justifying a skill at the next run, and its `ACCEPTED` replacement becomes the
current proof. See [operations.md](operations.md#profile-skills-phase-34a).

### Not implemented by `0008`

No administrable alias table, no preference, availability, mobility or career
objective — the structured project, experience, education, certification and
language tables belong to `0009` and `0010`, and the availability, mobility,
preference and career objective tables to `0011`, all described below; no
opportunity constraint, no eligibility rule, no skill extraction
from an offer, no TF-IDF, no cosine similarity, no matching, no `match_score`,
no ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. There is no HTTP endpoint and no remote write path over these
tables.

## Structured profile experiences and projects

Migration `0009` adds the Phase 3.4B1 projection of **verified** experience and
project facts, and nothing else: two tables, one composite index on
`profile_facts`, no column on any existing table, no seeded row.

`profile_facts` stays the source of truth, for this projection and for the
Phase 3.4B2 one that `0010` adds:

```text
profile_facts  (the only place a claim is decided)
    |
    +-> profile_skills          (0008)
    +-> profile_experiences     (0009)
    +-> profile_projects        (0009)
    +-> profile_educations      (0010)
    +-> profile_certifications  (0010)
    +-> profile_languages       (0010)
    +-> profile_availability        (0011)
    +-> profile_mobility            (0011)
    +-> profile_preferences         (0011)
    +-> profile_career_objectives   (0011)
```

**A `NULL` is worth more than an invented value** — in every one of them.

Every row below is derived from a fact whose status is already `ACCEPTED`, and
the projection reads
`fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'` and nothing
else.

**A `NULL` is worth more than an invented value.** Every fragment column is
nullable, and a fragment is written only when the document itself delimited it
with punctuation it wrote. No employer is deduced from a sentence, no role from
a technology, no seniority from the word "stage", no duration, and no calendar
date from a school year. There is no `verified`, `confidence`, `score`,
`proficiency`, `seniority` or `match_score` column, for the same reason there
is none in `0007` or `0008`.

No row of these tables is ever updated by the projection, so neither carries
`updated_at`: reconciliation inserts what is missing, deletes what has stopped
being justified, and replaces — delete then insert — a row the current rules
would write differently.

### `profile_experiences`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, part of the composite key below |
| `role_text` | nullable; non-blank and already trimmed when present |
| `organization_text` | nullable; same rule |
| `period_text` | nullable; same rule. The fragment the document wrote, never a computed date |
| `description_text` | nullable; same rule. The remaining lines of the fact, as written |
| `structurer_version` | `NOT NULL`, non-empty and already trimmed |
| `structuring_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

### `profile_projects`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, part of the composite key below |
| `title_text` | nullable; non-blank and already trimmed when present |
| `period_text` | nullable; same rule |
| `description_text` | nullable; same rule |
| `structurer_version` | `NOT NULL`, non-empty and already trimmed |
| `structuring_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`UNIQUE(fact_id)` — global, not per profile — says that one verified fact is
projected exactly once: a fact appearing twice would mean the projection read
one claim two ways, which is a defect rather than corroboration.

Both tables carry a **composite** foreign key,
`FOREIGN KEY (fact_id, profile_id) REFERENCES profile_facts(id, profile_id)`,
supported by the unique index `idx_profile_facts_id_profile` that `0009`
creates. It makes "a projection may not point at another profile's fact" a
database rule rather than an application convention. Indexes:
`idx_profile_experiences_profile`, `idx_profile_projects_profile`.

Neither table duplicates `profile_fact_provenance`. It records only what the
projection itself decided — which structurer version, which structuring rule —
and points at the fact for everything else, so the audit chain stays a chain:

```text
profile_experience → profile_fact → profile_fact_provenance
profile_project    → profile_fact → profile_fact_provenance
```

There is no `source_type`, `cv_sha256`, `parser_version`, `extractor_version`
or `provenance_key` column here.

### The structuring rules

`STRUCTURED_PROFILE_VERSION` is `structured-profile-v1`. Three rules exist, and
they are exhaustive.

`EXPERIENCE_PIPE_HEADER_V1` applies when the fact's first line is written as an
explicit pipe header: it does not open with a list marker, it holds at least
three `|`-separated segments that all carry text once trimmed, and **exactly
one** segment after the first two is an explicit period. `role_text` is then
the first segment, `organization_text` the second, `period_text` the temporal
one, and `description_text` the remaining lines of the fact. A segment the rule
does not name — a city, a contract type — is not projected; the fact keeps it,
and this slice adds no column for it.

`PROJECT_BULLET_COLON_V1` applies when the first line — after at most one list
marker has been removed — carries an explicit `:` with text on both sides. A
colon immediately followed by `/` is a scheme, not a separator. `title_text` is
the left side, `description_text` the right side followed by the remaining
lines. A period is split out of the title in one shape only: a final
parenthesis holding a closed range of two four-digit years, and only if a
non-empty title survives its removal. `(2024)`, `(promotion 2024)` and
`(septembre 2024 - juin 2025)` all stay part of the title.

An explicit period, for the experience rule, is a **whole** fragment matching
one closed form: a four-digit year, `MM/YYYY`, a month named in the closed
French/English registry followed by a year, a dash-separated range of two of
those, a range closed by one of the open-end markers (`présent`, `aujourd'hui`,
`today`, `now`, `en cours`, …), a school year written `YYYY/YYYY`, or a
**closed** range followed by a parenthesis holding **exactly** one of those
same open-end markers — `2025-2026 (en cours)`. Whatever the form, the fragment
is stored as written and never converted into calendar dates. The parenthesised
qualifier is read as nothing at all: no `current` flag, no end date, no
duration and no employment status exists in this schema. A parenthesis holding
free text is not a period, so `2022-2024 (6 mois)`, `2022-2024 (stage)`,
`2022-2024 (Paris)` and `2022-2024 (approx.)` are refused, and so is
`2024 (en cours)` — one end is written, not two. `depuis 2023`, `6 mois` and
`printemps 2024` are not periods either.

`UNPARSED_V1` is everything else. The fact is still projected, with every
fragment `NULL`: no accepted fact is ever dropped silently, and no fragment is
guessed to fill the row. Zero explicit periods in a header, two of them, a date
written where the role belongs, an empty side of a colon — each is a reason to
decline, because choosing would be inventing.

There is no stemming, no fuzzy matching, no edit distance, no similarity, no
embedding, no LLM and no API call anywhere in the rules, and no clock: the same
fact value always produces the same reading.

### Synchronizing

`synchronize_structured_profile_entries(connection, profile_id)` is a
reconciliation, not an append, and the whole of it is one `BEGIN IMMEDIATE`
transaction:

1. the profile must exist — synchronizing creates no user and no profile;
2. the `ACCEPTED` `EXPERIENCE` and `PROJECT` facts are read **inside** the
   transaction;
3. each of them is structured deterministically, and every one of them is
   projected — `experience_rows` always equals `accepted_experience_facts`;
4. a row pointing at a fact that is no longer verified is deleted;
5. a row the current rules would write differently is replaced whole;
6. `profile_facts`, `profile_fact_provenance`, `skills`, `profile_skills` and
   `profile_skill_evidence` are never written.

A second run on unchanged facts writes nothing, keeps every id and timestamp,
and reports `changed=false`. A fact corrected or rejected after a run stops
being projected at the next run, and its `ACCEPTED` replacement takes its
place. See [operations.md](operations.md#structured-profile-entries-phase-34b1-and-34b2).

### Not implemented by `0009`

No availability, mobility, preference or career objective; no eligibility rule,
no opportunity constraint, no skill inferred from an experience or a project,
no skill level, no matching, no `match_score`, no TF-IDF, no cosine similarity,
no ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. Structured education, certifications and languages are `0010`,
below, and availability, mobility, preferences and career objectives are
`0011`, below that. There is no HTTP endpoint and no remote write path over
these tables.

## Structured profile education, certifications and languages

Migration `0010` adds the Phase 3.4B2 projection of **verified** education,
certification and language facts, and nothing else: three tables, three
indexes, no column on any existing table, no seeded row. It creates no index on
`profile_facts`: the composite identity its foreign keys need is
`idx_profile_facts_id_profile`, which `0009` already created and this migration
reuses.

Every row is derived from a fact whose status is already `ACCEPTED`, and the
projection reads
`fact_type IN ('EDUCATION', 'CERTIFICATION', 'LANGUAGE') AND status =
'ACCEPTED'` and nothing else.

**A `NULL` is worth more than an invented value.** Every fragment column is
nullable, and a fragment is written only when the document itself delimited it
with punctuation it wrote *and* a closed registry could say what the fragment
is. No diploma is deduced from an institution and no institution from a
diploma, no `Bac+N` or study level from the word "Master", no calendar date
from a school year, no certification is assumed obtained, no issuer, obtention
date or expiry date is invented, no language is deduced from a text written in
one, and no CEFR level is computed from "courant" or "fluent". There is no
`verified`, `confidence`, `score`, `seniority`, `match_score` or
`inferred_level` column, for the same reason there is none in `0007`, `0008` or
`0009`.

No row of these tables is ever updated by the projection, so none carries
`updated_at`: reconciliation inserts what is missing, deletes what has stopped
being justified, and replaces — delete then insert — a row the current rules
would write differently.

### `profile_educations`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, part of the composite key below |
| `institution_text` | nullable; non-blank and already trimmed when present |
| `program_text` | nullable; same rule |
| `period_text` | nullable; same rule. The fragment the document wrote, never a computed date |
| `description_text` | nullable; same rule. The remaining lines of the fact, as written |
| `structurer_version` | `NOT NULL`, non-empty and already trimmed |
| `structuring_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

### `profile_certifications`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, part of the composite key below |
| `certification_text` | nullable; non-blank and already trimmed when present |
| `issuer_text` | nullable; same rule |
| `period_text` | nullable; same rule |
| `description_text` | nullable; same rule |
| `structurer_version` | `NOT NULL`, non-empty and already trimmed |
| `structuring_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

There is no `obtained`, `obtained_at`, `expires_at` or `credential_id` column: a
CV line naming a certification does not state that it was obtained, when, or
when it lapses.

### `profile_languages`

| column | rule |
| --- | --- |
| `id` | `INTEGER PRIMARY KEY` |
| `profile_id` | `NOT NULL`, `FOREIGN KEY → profiles(id) ON DELETE CASCADE` |
| `fact_id` | `NOT NULL UNIQUE`, part of the composite key below |
| `language_text` | nullable; non-blank and already trimmed when present |
| `proficiency_text` | nullable; same rule. The wording the document used, verbatim |
| `structurer_version` | `NOT NULL`, non-empty and already trimmed |
| `structuring_rule_id` | `NOT NULL`, non-empty and already trimmed |
| `created_at` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` |

`proficiency_text` is deliberately **not** a CEFR column: it has no enumeration
and no check constraining it to `A1..C2`, because storing "courant" as `C1`
would be a translation nobody made.

Like `0009`, all three carry `UNIQUE(fact_id)` — global, not per profile — and
a **composite** foreign key,
`FOREIGN KEY (fact_id, profile_id) REFERENCES profile_facts(id, profile_id)`,
so a projection may not point at another profile's fact. Indexes:
`idx_profile_educations_profile`, `idx_profile_certifications_profile`,
`idx_profile_languages_profile`.

No table duplicates `profile_fact_provenance`. Each records only what the
projection itself decided — which structurer version, which structuring rule —
and points at the fact for everything else:

```text
profile_education     → profile_fact → profile_fact_provenance
profile_certification → profile_fact → profile_fact_provenance
profile_language      → profile_fact → profile_fact_provenance
```

There is no `source_type`, `cv_sha256`, `parser_version`, `extractor_version`
or `provenance_key` column here.

### The Phase 3.4B2 structuring rules

`STRUCTURED_PROFILE_VERSION` stays `structured-profile-v1`. Adding fact types
is backward-compatible by construction: `EXPERIENCE_PIPE_HEADER_V1`,
`PROJECT_BULLET_COLON_V1` and `UNPARSED_V1` read exactly what they read before,
so the rows they produced were produced exactly as `structured-profile-v1` says
they were, and no existing row is rewritten. The version moves the day one of
those readings changes.

**Punctuation proves that segments exist; it never proves what they are
about.** A CV writes `role | employer | dates` in that order; it writes a
diploma and a school in either, so the education rules never read a segment by
its position.

All three education rules start from an explicit pipe header — no opening list
marker, `|`-separated segments that all carry text once trimmed — and all three
tell a school from a programme the same way, from **two** closed registries:

| registry | entries |
| --- | --- |
| `INSTITUTION_MARKERS` | `université`, `universite`, `university`, `école`, `ecole`, `school`, `institut`, `institute`, `faculté`, `faculte`, `faculty`, `college`, `collège`, `collége` |
| `PROGRAM_MARKERS` | `master`, `mastère`, `mastere`, `bachelor`, `licence`, `diplôme`, `diplome`, `diploma`, `degree`, `ingénieur`, `ingenieur`, `ingénieure`, `ingenieure`, `doctorat`, `doctorate` |

Both are matched on **whole words**, folded for comparison only. There is no
stemming, no plural folding, no prefix match and no fuzzy comparison:
`Universitaire`, `Universités`, `Masterclass` and `Licences` prove nothing. The
two registries do not overlap, and a test asserts it.

**Each role needs its own exclusive proof.** A pair of segments is named only
when one is marked as an institution and **not** as a programme, and the other
is marked as a programme and **not** as an institution. One marker is never
enough, because a marker on one segment proves nothing about the other:
`University Diploma in AI | Sorbonne` carries `university` on the segment that
is the *programme* — it carries `diploma` too — while the school carries no
marker at all, so a rule trusting the institution marker alone would store the
two fields the wrong way round. A segment proving both roles at once proves
neither, and two segments proving the same role prove nothing. The
marked-as-institution segment is then `institution_text` and the other
`program_text`, whichever order they appear in.

The minimum number of segments is **two** here, unlike the three the experience
rule requires. That rule needs three because it reads segments by position;
these read none by position, so two written segments carry — or fail to carry —
exactly the same proofs as three.

`EDUCATION_PIPE_EXPLICIT_V1` applies when the header holds **exactly one**
explicit period, exactly two segments remain besides it, and those two carry
the exclusive proofs above. The remaining lines are `description_text`.

`EDUCATION_PIPE_INSTITUTION_PROGRAM_V1` applies when the header holds
**exactly two** segments, **neither** of them an explicit period, and those two
carry the same proofs. `period_text` stays `NULL`: a header with no date is a header
with no date, and no year is looked for inside the words. Both segments must be
free of an explicit period — in `Université Exemple | 2020 - 2022` the unmarked
segment is a date, and reading it as a programme would be an invention, so that
fact stays unparsed.

`EDUCATION_PIPE_PERIOD_ONLY_V1` applies when a single period is certain and
that distinction is not — a role with no proof, a segment proving both roles at
once, both segments proving the same role, or more than two segments remaining.
`period_text` is the fragment verbatim,
`description_text` the remaining lines, and `institution_text` and
`program_text` stay `NULL`. The certain part is preserved without the uncertain
part being invented.

`EDUCATION_UNPARSED_V1` is everything else, with every fragment `NULL`: no pipe
header, an empty segment, a line opened by a list marker, several explicit
periods, a two-segment header holding a date, a dateless two-segment header
whose two roles are not both exclusively proven, and any dateless header of
three segments or more — where the third segment would have no honest home.

`CERTIFICATION_EXPLICIT_V1` applies when the fact states no intention — no
whole word of the closed registry `préparation`, `preparation`, `préparer`,
`preparer`, `objectif`, `objectifs`, `objective`, `goal`, `prévu`, `prevu`,
`prévue`, `prevue`, `planned`, `futur`, `future` appears anywhere in it — and
when every `|`-separated segment of the first line (after at most one list
marker) is readable: either a whole explicit period, or an explicit
`label: value` whose label is a closed registry entry. The certification labels
are `certification`, `certificat`, `certificate`; the issuer labels are
`délivré par`, `délivrée par`, `delivre par`, `delivree par`, `émis par`,
`emis par`, `issued by`, `issuer`, `organisme`, `éditeur`, `editeur`. The
certification label must appear exactly once and no label or period may repeat.
An issuer nobody wrote stays `NULL`; a period nobody wrote stays `NULL`.

`CERTIFICATION_UNPARSED_V1` is everything else, with every fragment `NULL` —
including every stated intention, so "Préparation à la certification X" and
"Objectif : certification X" never become a held credential.

`LANGUAGE_EXPLICIT_PROFICIENCY_V1` applies when the fact is a single line
which, once at most **one** list marker has been removed, carries one explicit
separator — a trailing parenthesis, a `:` that is not a URL scheme, a `|`, or a
dash the document spaced on both sides — and everything on its right is a
**whole** form of the closed proficiency registry:
`a1`, `a2`, `b1`, `b2`, `c1`, `c2`, `débutant`, `debutant`, `intermédiaire`,
`intermediaire`, `avancé`, `avance`, `courant`, `fluent`, `native`, `natif`,
`bilingual`, `bilingue`. Case is folded to **compare** and never to store:
`language_text` and `proficiency_text` are both kept exactly as typed, so
`courant` stays `courant` and `C1` stays `C1`.

The list marker is removed with the same conservative helper the project rule
uses, and exactly one is removed. A CV writes its languages as a bulleted list
as often as not and the Phase 3.2B extractor keeps a bullet block's source
text, so `• Anglais : C1` reaches the structurer with its marker; that marker
is punctuation the list wrote, not part of the language's name, and
`language_text` would otherwise hold the layout. A second marker is content and
stays where the document put it.

`LANGUAGE_UNPARSED_V1` is everything else, with both fragments `NULL`: no
separator, a dash the document did not space, a right side the registry does
not hold whole, an empty side, or a fact spanning several lines — which this
table has no column to keep.

There is no stemming, no fuzzy comparison, no edit distance, no similarity, no
embedding, no LLM and no API call anywhere in these rules, and no clock: the
same fact value always produces the same reading.

### Synchronizing

The same `synchronize_structured_profile_entries(connection, profile_id)`, the
same single `BEGIN IMMEDIATE` transaction, now covering five fact types and
five tables. Every accepted fact of every projected type is projected exactly
once, so `education_rows` equals `accepted_education_facts`,
`certification_rows` equals `accepted_certification_facts` and `language_rows`
equals `accepted_language_facts`. `skills`, `profile_skills` and
`profile_skill_evidence` are never written: no skill is inferred from a
diploma, a certification or a language.

### Not implemented by `0010`

No availability, mobility, preference or career objective — those are `0011`,
below, and they come from the person rather than from a document; no
eligibility rule, no opportunity constraint, no skill inference, no study
level, no CEFR computation, no `match_score`, no TF-IDF, no cosine similarity,
no ranking, no recommendation, no notification, no CV adaptation and no
auto-apply. There is no HTTP endpoint and no remote write path over these
tables.

## Explicit profile input

Migration `0011` adds the Phase 3.4C projection of the availability, mobility,
preferences and career objectives a person **states about themselves**, and
nothing else: four tables, no index on any existing table, no column on any
existing table, no seeded row. It creates no index on `profile_facts`: the
composite identity its foreign keys need is `idx_profile_facts_id_profile`,
which `0009` already created and this migration reuses.

Every row is derived from a fact whose status is already `ACCEPTED`, and the
projection reads
`fact_type IN ('AVAILABILITY', 'MOBILITY', 'PREFERENCE', 'CAREER_OBJECTIVE')
AND status = 'ACCEPTED'` and nothing else. **And, unlike every earlier
projection, acceptance alone is not enough**: the same statement requires the
fact to carry `USER_INPUT` provenance, with

```sql
AND EXISTS (
        SELECT 1 FROM profile_fact_provenance AS p
         WHERE p.fact_id = f.id AND p.source_type = 'USER_INPUT'
    )
```

so an `ACCEPTED` `PREFERENCE`, `AVAILABILITY`, `MOBILITY` or
`CAREER_OBJECTIVE` fact evidenced only by a `CV`, a `GITHUB` page or an
`OTHER_ACCEPTED_EVIDENCE` derivation is never projected. `EXISTS` rather than a
join: a fact carrying several `USER_INPUT` proofs is projected once, not once
per proof.

```text
explicit user input (a person types it)
    |
    v
profile_facts  (ACCEPTED, USER_INPUT provenance)
    |
    +-> profile_availability       → fact_id → profile_facts → profile_fact_provenance
    +-> profile_mobility           → fact_id → ...
    +-> profile_preferences        → fact_id → ...
    +-> profile_career_objectives  → fact_id → ...

    no accepted fact  →  no row  →  UNKNOWN
```

**Nothing here is read from a CV, and nothing in it can be.** No availability
date is computed from a CV period, a diploma year or the clock; no mobility
from a postal address, a city, a country or a past employer; no work mode from
a past remote job; no preferred domain from a skill, a project or a section
title; no convention status from being a student; no visa need from a
nationality or a location; and no career objective from a CV's professional
title.

**Absence is `UNKNOWN`, and `UNKNOWN` has no row.** No seeded row, no default
row, no "unknown" row: a profile with no accepted `AVAILABILITY` fact simply
has no `profile_availability` row, and that absence is the whole representation
of "we do not know". A missing preference is never stored or read as `FALSE` —
"this person does not want remote" and "this person never said" are different
claims, and only the first is knowledge.

### The four tables

Each is a **singleton**: `profile_id` is the primary key, so a person has one
availability, one mobility, one set of preferences and one set of career
objectives, and restating any of them corrects the previous statement rather
than adding a second one. Each also carries `fact_id NOT NULL UNIQUE`, the same
composite foreign key on `(fact_id, profile_id)` as `0009`/`0010`, an
`input_version` and a `created_at`.

| table | what it holds |
| --- | --- |
| `profile_availability` | `availability_status` (`AVAILABLE_NOW` / `AVAILABLE_FROM`) and `available_from`, a `YYYY-MM-DD` date or `NULL`. A table-level `CHECK` makes the two forms exclusive: `AVAILABLE_NOW` forbids a date, `AVAILABLE_FROM` requires one |
| `profile_mobility` | `mobility_scope` (`OPEN` / `RESTRICTED`) and `locations_json`. A `CHECK` refuses a `RESTRICTED` row naming nowhere; `OPEN` with an empty array is the ordinary form |
| `profile_preferences` | `opportunity_types_json` and `work_modes_json` (both non-empty), `preferred_domains_json` and `constraints_json` (both may be empty), `convention_status` (`UNKNOWN` / `AVAILABLE` / `NOT_AVAILABLE`) and `visa_sponsorship_required` (`UNKNOWN` / `YES` / `NO`) |
| `profile_career_objectives` | `objectives_json`, at least one objective |

Every `*_json` column holds **canonical** JSON — object keys sorted, no
insignificant whitespace, closed registries in declaration order, free text in
the person's own order after an exact first-occurrence dedup. Canonical means
two identical statements are byte-identical, which is what lets the service
tell "the same value again" (a no-op) from "a new value" (a correction).

The closed registries are, in their canonical order: `opportunity_types` —
`PFA`, `PFE`, `SUMMER_INTERNSHIP`, `PRE_HIRE_INTERNSHIP`, `ALTERNANCE`,
`INTERNSHIP`, `FIRST_JOB`, `JUNIOR_ROLE`; `work_modes` — `ON_SITE`, `HYBRID`,
`REMOTE`.

### The fact values

The fact `value` is the canonical JSON itself, under
`input_version = 'explicit-profile-input-v1'`:

```json
{"available_from":null,"status":"AVAILABLE_NOW"}
{"available_from":"2026-09-01","status":"AVAILABLE_FROM"}
{"locations":[],"scope":"OPEN"}
{"locations":["Casablanca","Rabat"],"scope":"RESTRICTED"}
{"constraints":[],"convention_status":"UNKNOWN","opportunity_types":["PFE"],"preferred_domains":[],"visa_sponsorship_required":"UNKNOWN","work_modes":["HYBRID"]}
{"objectives":["Rejoindre une équipe data en alternance"]}
```

A value whose own re-encoding is not byte-identical is refused rather than
repaired: there is no best-effort reading of a preference.

### What the projection may not do

No row of these tables is ever updated, so none carries `updated_at`:
reconciliation inserts what is missing, deletes what has stopped being
justified, and replaces — delete then insert — a row the current facts would
now decode differently. A second run on unchanged facts writes nothing.

No table repeats `source_type`, `provenance_key` or any other evidence column:
those live once, in `profile_fact_provenance`, and `fact_id` is the way to
them. There is no `verified`, `confidence`, `score`, `match_score`,
`eligibility` or `ranking` column either.

A domain holding two `ACCEPTED` facts is a business integrity failure, and the
synchronization refuses it out loud and rolls the whole run back rather than
picking the most recent one.

### Not implemented by `0011`

Opportunity constraints are `0012`, below — they describe postings, not
people, and nothing joins the two. No eligibility result, no
`ELIGIBLE`/`NOT_ELIGIBLE` verdict, no comparison of a profile's location or
date against an offer's, no matching, no `match_score`, no TF-IDF, no cosine
similarity, no ranking, no recommendation and no notification. This is the
profile side of Phases 3.5 and 3.6, and neither is implemented. There is no
HTTP endpoint and no remote write path over these tables.

## Source run history

Migration `0005` stores one row per attempted execution of a source by
`RadarAgent`. The row is written **before** collection begins and the same row
is closed when the attempt ends, so one attempt is always exactly one row and an
attempt that started always leaves proof that it started.

The status taxonomy is the closed set that lifecycle needs:

| status | `finished_at` | `error_type` | `error_message` |
| --- | --- | --- | --- |
| `RUNNING` | `NULL` | `NULL` | `NULL` |
| `SUCCESS` | set | `NULL` | `NULL` |
| `FAILED` | set | set | set, or `NULL` if redaction empties it |

- **`RUNNING`**: the attempt was persisted and began; no ending has been
  recorded yet.
- **`SUCCESS`**: collection and opportunity persistence both completed.
- **`FAILED`**: the attempt raised; `error_type` names the exception and
  `error_message` carries a redacted, diagnosable message.

The schema enforces exactly those three shapes, and the persistence layer allows
only `RUNNING → SUCCESS` and `RUNNING → FAILED`. A row that already reached a
terminal state can never be finalized again.

A crash or an interruption therefore leaves a `RUNNING` row rather than no row
at all — that surviving row is the evidence, and `finished_at` is never invented
for an attempt that did not end. Deciding when a `RUNNING` row has been left
behind too long is a separate concern and is deliberately not implemented here.

`NULL` means "this run did not know it" and is never replaced by a zero, which
would read later as a real observation:

- `items_found` is the number of candidates the collector actually returned. It
  is `NULL` when collection itself failed, and a real count when collection
  succeeded and persistence then failed.
- `new_items` is the number of opportunities actually created. It is only known
  on a successful run.
- `relevant_items` is always `NULL` today: qualification runs once globally
  after the whole source loop, so per-source relevance is genuinely unknown at
  the moment a run is recorded.
- `pages_checked`, `http_status`, and `parser_version` are always `NULL` today:
  no current collector paginates, exposes a reliable transport status to the
  agent, or carries a versioned parser. A collector may opt in by exposing
  `run_metrics()`; only known, well-typed keys are stored.

`error_message` is redacted before it is stored. Credential assignments,
authorization schemes, URL user-info, JWTs, and long opaque blobs are removed
and the message is length-bounded. No token, credential, or OAuth secret is
persisted or logged.

### `sources.last_run_at`

`sources.last_run_at` is the `finished_at` of that source's most recent
**completed attempt**, successful or failed. It is not "last successful run" and
not "last time data changed".

Starting a run does **not** touch it: an attempt that has only started has not
finished, and an interrupted `RUNNING` row must never look like a completed run.
Only finalization stamps it, in the same transaction that closes the run, so the
stamp can never disagree with the run that produced it. A source that has never
completed an attempt keeps `NULL`.

Starting a run registers a never-persisted source so the run's foreign key
resolves; a source that already exists is left entirely untouched.

### Transaction boundaries

Starting and finalizing are each one transaction, both separate from the
opportunity batch they describe. A later source failing never rolls back an
earlier committed one, and a source that fails still records the fact that it
failed.

The two ends are treated differently on purpose:

- if the **start** cannot be persisted, the source is not collected at all —
  running it would produce work that could never be audited. It is reported as a
  failed source for that run and the remaining sources continue;
- if the **finalization** fails after the row exists, the failure is logged and
  the row is left `RUNNING`. It is never deleted, and never rewritten into a
  state the attempt did not reach, so the unfinalized attempt stays visible.

### Deleting a source

`source_runs.source_id` uses `ON DELETE RESTRICT`, like the other audit tables.
A source that carries run history cannot be deleted, so its audit trail cannot
be discarded as a side effect of removing the source.

## Source health

Source health is **derived at read time** from `sources` and `source_runs`. It
adds no migration, no `anomalies` table, and no persisted health column: nothing
about it is stored, so the answer cannot drift from the runs it describes.
`services/collector/database/source_health.py` holds the read model; nothing in
it writes, and reading health for a source the database has never seen creates
no row for it.

Each entry carries `source_id`, `enabled`, `last_run_at`, the latest run's
`status`, `items_found`, `new_items`, `relevant_items`, `error_type` and
`error_message`, plus the derived `zero_result_streak`, `anomaly_code` and
`anomaly_message`.

`last_run_at` is the `started_at` of the most recent run, so it always describes
the very run whose status and metrics are shown beside it, and a run that is
still `RUNNING` shows its own start rather than an older run's end. For a source
that has never run it is `NULL`.

This is deliberately **not** the column `sources.last_run_at`, which keeps its
own meaning from the run-history slice: the `finished_at` of the most recent
*completed* attempt. That column is not substituted here — a stamp with no run
behind it would claim an execution the read model cannot show, and it would
disagree with the status displayed next to it whenever the latest attempt has
not finished.

A source is listed when the validated `config/sources.yaml` catalogue configures
it or when the database already holds it, once either way. The configured
`enabled` flag wins over the persisted one: the current configuration is the
most direct authority on whether a source is enabled, whereas `sources.enabled`
records what a run observed. Opportunity persistence does refresh that row —
`type`, `enabled`, `category`, `country`, `frequency_minutes` and `status` are
upserted with `ON CONFLICT(id) DO UPDATE` — but the row is absent for a source
that has never run and lags the configuration until a run refreshes it.

### The consecutive-zero rule

Exactly one anomaly is detected: **three consecutive successful runs that each
found exactly zero items**, counted from the newest run backwards.

- the constant is `ZERO_RESULT_STREAK_THRESHOLD = 3`;
- `items_found IS NULL` means *unknown* and is never read as a zero;
- a `FAILED` run is not a successful zero;
- a `RUNNING` run has not finished and is not a completed zero;
- a `SUCCESS` with `items_found > 0` ends the streak;
- a run whose `items_found` is unknown ends the streak;
- any run that is not a zero-result success ends the streak, so
  `zero_result_streak` is 0 whenever the latest run failed, is still running, or
  did not measure `items_found`.

Past the threshold the entry carries `anomaly_code = "ZERO_RESULTS_STREAK"` and
a deterministic `anomaly_message` built from a fixed template and the observed
count. No language model, no heuristic phrasing, no severity beyond that count.

This distinguishes the two situations the run history would otherwise conflate:
a source that genuinely has nothing new (one or two zero runs, no anomaly) and a
collector or parser that may have broken (three or more, anomaly).

### Deliberately not a state machine

The truth about a source stays the real `source_runs.status` of its latest run —
`RUNNING`, `SUCCESS` or `FAILED`, or nothing at all. No `HEALTHY`/`DEGRADED`/
`CRITICAL` taxonomy is introduced, persisted, or derived. The anomaly is exposed
beside the status, never folded into it.

`relevant_items` stays `NULL` for every current collector and is reported as
unknown rather than invented.

### Not yet implemented

No alert is raised from any of this: nothing emails, notifies, retries, reruns,
or repairs a source, nothing is scheduled, and no `RUNNING` row is judged stale
against any age threshold — no such threshold is defined. Source health is a
read model and a page, not an alerting system.

## Read-only application API

FastAPI exposes `GET /api/opportunities` with a bounded `limit` (1–100). It
returns the count and summary fields for visible, active rows, including
`original_url` and `description_length`; it does not expose the stored full
description. The service opens existing configured storage read-only for the
request and does not collect data or alter schema.

`GET /api/source-health` takes no parameter and returns
`{"items": [...], "returned": n}` with one entry per known source, ordered by
`source_id`. Unknown values are serialized as `null` and never as zero.

The read is read-only in the strong sense. The database is opened through a
`mode=ro` SQLite URI (`connect_readonly_database`) and then also set
`query_only`, so the request cannot create the database file, a schema, a table,
or a row: a missing or not-yet-migrated path answers `503` and stays missing
rather than becoming an empty database. Source health is defined over the
operational SQLite database only; any other configured backend is refused
locally, before any connection is attempted, so no remote connector is called
and no network call is made. Validated settings are still loaded first, as for
any request, so a configured remote URL and token are read from the environment
like any other setting; source health never uses them to open or contact
anything, and they reach neither the response nor the logs.

## Opportunity constraints

Migration `0012` adds the Phase 3.5A projection of **what a posting itself
requires**, and nothing else: six tables, no column on any existing table, no
seeded row.

It is the offer side, and it stops there. Nothing in these tables mentions a
person, a profile, a fit or a verdict, and no column here is meant to hold one
later: comparing a posting to somebody is Phase 3.6, ranking is Phase 4, and
neither exists.

```text
opportunities
    +-> opportunity_constraints            (one row per posting read)
          +-> opportunity_constraint_locations
          +-> opportunity_education_requirements
          +-> opportunity_experience_requirements
          +-> opportunity_constraint_evidence
          +-> opportunity_constraint_conflicts
          +-> opportunity_skill_requirements   (reserved here, filled by 0013)
```

`opportunities` is the source and is never written: no row is updated, no
column added, and a reading whose source text changed is deleted and rebuilt
rather than patched.

### NULL is UNKNOWN, and UNKNOWN is never FALSE

Every scalar in `opportunity_constraints` is nullable, and `NULL` means one
thing everywhere: no closed rule found an explicit statement. The Python enums
carry an `UNKNOWN` member so the extractor's output is total; the repository
maps that member to `NULL` on the way in and back on the way out, so the
database has a single spelling of "not asserted" and each `CHECK` enumerates
only affirmative values — `visa_sponsorship` accepts `AVAILABLE` and
`NOT_AVAILABLE`, and the literal string `UNKNOWN` is refused by SQLite.

`visa_sponsorship IS NULL` therefore means **the posting was silent**, and
silence is not a policy. The same holds throughout: a posting with an address
has not required attendance, a posting written in English has not required
English, a posting for an internship has not required a school agreement or
stated a duration, and a posting in a given country has not refused to sponsor.

### The tables

| table | what it holds |
| --- | --- |
| `opportunity_constraints` | the scalars: `opportunity_type` (the Phase 3.4C registry, shared with the profile side), duration bounds in months, `start_year`/`start_month`/`start_day` with a `start_precision` of `DATE`/`MONTH`/`YEAR`, `work_mode`, `visa_sponsorship`, `work_authorization`, `convention_requirement`, plus `extractor_version`, `source_fingerprint` and `extracted_at` |
| `opportunity_constraint_locations` | the places the posting named, in order: the collected `location` and `country`, trimmed and exactly deduplicated. No geocoding, no country deduced from a city, no region expanded |
| `opportunity_education_requirements` | every level named, with `requirement_mode` `MINIMUM` or `EXACT`. Several rows are several accepted levels, not a contradiction. `BAC_PLUS_5` and `MASTER` stay separate levels |
| `opportunity_experience_requirements` | **every experience the posting asked for, one row each**, in months, with an optional `REQUIRED`/`PREFERRED` obligation. A row must assert a bound or an obligation. Nothing ranks them |
| `opportunity_constraint_evidence` | why each value was asserted: the kind, the source field, the `rule_id`, and the **minimal fragment** matched, capped at 200 characters. A pointer into the posting, never a copy of it |
| `opportunity_constraint_conflicts` | the readings that disagreed and were therefore not used, as canonical JSON arrays of the values and the rules, keyed by `constraint_slot` |
| `opportunity_skill_requirements` | created here, **always empty after a 3.5A run**, and filled by `0013` — see [Opportunity skill and language requirements](#opportunity-skill-and-language-requirements). It exists in `0012` to pin the offer side to the `skills` vocabulary `0008` created, so 3.5B extends one catalogue instead of starting a rival. Its `skill_id` is `ON DELETE RESTRICT`, like `profile_skills`: a term an offer still requires cannot be deleted out from under it |

Three separate claims are kept apart on purpose, and no rule turns one into
another: `visa_sponsorship` is what the **employer** offers to do,
`work_authorization` is what the **applicant** must already hold, and what a
**person** needs lives in `profile_preferences.visa_sponsorship_required`, on
the other side of the database entirely.

### Contradictions

When two strong readings of one posting disagree — "fully remote" three lines
above "fully on-site" — the scalar stays `NULL` and a row lands in
`opportunity_constraint_conflicts` naming the values and the rules.

**A conflict is keyed by its slot, not by its kind.** A *kind* is the subject a
reader browses by; a *slot* is the thing that can actually disagree with
itself. `UNIQUE (opportunity_id, constraint_slot)` is the key, and
`constraint_kind` stays for browsing, derived from the slot in code so the two
cannot drift apart.

One relation is applied before a disagreement is declared: the four specific
internship kinds — `PFE`, `PFA`, `SUMMER_INTERNSHIP`, `PRE_HIRE_INTERNSHIP` —
are `INTERNSHIP` said more precisely, so a posting naming both stores the
precise one and no conflict. Two specific kinds still conflict.

`EDUCATION`, `EXPERIENCE` and `LOCATION` are absent from the slot registry:
several levels, several requirements or several places are several answers,
never a disagreement. A conflict now means one thing only — **the same global
property of the offer was explicitly asserted two incompatible ways** — and
nothing in the table any longer means "the posting asked for two different
things". Choosing
between them would turn a defect in the posting into a fact about it. The one
documented exception is the opportunity type, where the title outranks the
description exactly as the Phase 2 classifier already decides it: a posting
titled "PFE" whose body says "internship" is one thing at two grains.

### Idempotence

`extractor_version` is `EXTRACTOR_VERSION`, the version of the code that
produced the row, and no caller can name another: a label that could be chosen
per run would stop describing the rules that explain the row, which is the only
reason to store it. Making a new version means editing that constant.

`source_fingerprint` is a SHA-256 over canonical JSON of exactly the fields the
extractor reads — title, description, location, country, `remote_type` and the
qualification's `opportunity_type` — with no clock, no row id and no
dict-ordering dependence. The same pair `(source_fingerprint,
extractor_version)` means nothing is rewritten, not even a timestamp; an edited
description recomputes; and a new `extractor_version` recomputes even when the
text is identical. This is the pattern `0004` already uses for qualification.

### Not implemented by `0012`

No opportunity is compared to a profile. No eligibility result, no
`ELIGIBLE`/`NOT_ELIGIBLE` verdict, no `match_score`, no TF-IDF, no cosine
similarity, no ranking, no recommendation and no notification. There is no
`confidence` and no `score` column: a closed rule either found explicit
evidence or it did not, and a number between the two would only invite a
threshold. Skill and language requirements are not extracted — see 3.5B. There
is no HTTP endpoint and no remote write path over these tables.

(`opportunities` does carry `relevance_score`, `eligibility_score`,
`match_score`, `priority_score` and `interview_potential_score` from `0001`.
They are Phase 1 leftovers; Phase 3.5A neither reads nor writes them, and a
test asserts they stay `NULL`.)

## Opportunity skill and language requirements

Migration `0013` adds the Phase 3.5B reading of **which skills and which
languages a posting explicitly asks for**: five tables, no column on any
existing table, and **no seeded row of any kind**. It does not recreate
`opportunity_skill_requirements` — `0012` created that table for exactly this
purpose, and there is no `opportunity_skill_requirements_v2`, no `offer_skills`
and no `required_skills` anywhere.

```text
opportunities
    +-> opportunity_constraints                       (3.5A, migration 0012)
          +-> opportunity_skill_requirements          (0012, filled by 0013)
          |     +-> opportunity_skill_requirement_evidence
          +-> opportunity_language_requirements
          |     +-> opportunity_language_requirement_evidence
          +-> opportunity_requirement_ambiguities
          +-> opportunity_requirement_extraction_state
```

**Every table hangs off `opportunity_constraints`, not off `opportunities`.**
3.5B is structurally a continuation of 3.5A, so a posting with no 3.5A
projection cannot receive a 3.5B one, and a 3.5A re-synchronization — which
deletes and rebuilds the constraint row — cascades the whole 3.5B reading away,
extraction state included. A stale state can never outlive the projection it
was computed beside.

| Table | What one row is |
| --- | --- |
| `opportunity_skill_requirements` (from `0012`) | one technology the posting asks for, `REQUIRED` or `PREFERRED`, pointing at the shared `skills` vocabulary. `UNIQUE (opportunity_id, skill_id)`: `REQUIRED` outranks `PREFERRED` when a posting says both |
| `opportunity_skill_requirement_evidence` | one mention: the minimal fragment (≤ 200 chars), the `rule_id`, the `source_field`, the heading it sat under, and `observed_requirement` — the level **that mention** stated, which may be weaker than the projected one |
| `opportunity_language_requirements` | one language, `REQUIRED` or `PREFERRED`, with `proficiency_text` as the posting wrote it or `NULL`. `UNIQUE (opportunity_id, language_key)` |
| `opportunity_language_requirement_evidence` | the same, plus `observed_proficiency_text`: the level *this* mention named |
| `opportunity_requirement_ambiguities` | a demand the extractor understood and deliberately refused to store: `ALTERNATIVE_GROUP_UNSUPPORTED`, `COMPOUND_SKILL_EXPRESSION_UNSUPPORTED` or `CONFLICTING_LANGUAGE_PROFICIENCY`, with the fragment, the rule and the heading. Deduplicated per posting on everything the row holds |
| `opportunity_requirement_extraction_state` | that a posting **was read**, at which version, from which text: `source_fingerprint`, `extractor_version`, `extracted_at` |

### The vocabulary is shared and never seeded

A `skills` row says a canonical name exists. It does not say a person holds the
skill (`profile_skills` says that) and it does not say a posting wants it
(`opportunity_skill_requirements` says that). So `0013` inserts nothing: the
catalogue of ~115 recognisable terms lives in Python, and a vocabulary row is
created — transactionally, inside the same write as the requirement that needed
it — the first time a real posting is found to require that term. Three hundred
pre-inserted names would be three hundred rows nobody observed, and a later
reader could not tell them from the ones a posting produced.

Canonical keys come from the Phase 3.4A normalizer, not from a second one, so
the offer side and the profile side compute the same key for the same
technology. An existing row is reused and **never renamed**: one profile
spelling a skill differently must not change what the other side reads. A term
that no posting requires any more is left in `skills`, exactly as `0008` allows
— it is vocabulary, not a claim.

### What produces a row, and what does not

A requirement needs a section that says the items under it are demands or
preferences, or a sentence that says so itself. These produce **nothing**:

```text
Our stack includes Python, Spark and Kafka.   -> nothing
You will build pipelines using Python.        -> nothing
We use SQL across the company.                -> nothing
Training in Python will be provided.          -> nothing
No prior Python experience is required.       -> nothing
```

Signals are read in a fixed order: a cancelling clause, then a local marker,
then the section, then nothing. So `Python preferred` under `Required
Qualifications` is `PREFERRED`, and `Must have strong Python skills` is
`REQUIRED` with no section at all. An unrecognised title-shaped line resets the
context to neutral rather than letting a requirements heading leak downward.

**The unit is the clause, not the sentence.** Two technologies in one sentence
can carry two different levels, and one of them can be cancelled without
touching the other:

```text
Python required and Spark preferred.                 -> Python REQUIRED, Spark PREFERRED
Python preferred and SQL required.                   -> Python PREFERRED, SQL REQUIRED
No Python experience required, but SQL is required.  -> SQL REQUIRED only
```

The cut happens only in the text **between** two matched terms and only at a
connector, so terms joined by a bare connector stay in one clause: `Python and
SQL required` is two requirements and `Python, SQL and Spark required` is three.
This is a different question from one technology named twice, where `REQUIRED`
still beats `PREFERRED`.

Matching is on token boundaries that are **Unicode-aware** and that know about
`+`, `#`, `&` and combining marks, and on longest-alias-first, non-overlapping
spans: `PostgreSQL` never yields `SQL`, `PySpark` never yields `Spark`,
`Google` never yields `Go`, `C`, `C++` and `C#` are three technologies, and
`Réseaux`, `Régression`, `Réalisation` and `Câblage` yield nothing at all. One-
and two-character aliases must be written as the catalogue writes them, so
ordinary prose cannot produce `Go`, `C` or `R`.

### Alternatives are refused, on the record

"Python or R required" states one requirement satisfiable two ways. Storing
`Python REQUIRED` **and** `R REQUIRED` would turn the employer's choice into
two obligations, and would let a later phase reject somebody the posting would
have accepted with either. So neither is stored, and a row lands in
`opportunity_requirement_ambiguities` — because without it, an explicit demand
this version cannot represent would be indistinguishable from a posting that
never mentioned either technology.

`and` still gives two requirements.

### A bare slash is not an `or`, and not an `and`

Running the extractor over the real corpus produced 175 refusals, and most were
right: `Python or JavaScript`, `AWS, GCP, or Azure`,
`TensorFlow, PyTorch, or HuggingFace` and `English, Dutch or French` are choices
and stay refused. One recurring family was not a choice at all —
`AI/ML engineering`, `AI/ML APIs`, `ML/LLM-powered system`. Nobody writing those
is offering to accept either half; it is one field written with a slash.

Neither available reading was right, so a third exists. A closed registry —
`COMPOUND_SKILL_EXPRESSIONS` in `requirements/skill_catalog.py`, keyed by
**canonical skill key** so `AI/ML` and `ML/AI` are one entry and every alias is
covered — names the slashed expressions that mean one thing. They are refused
under `COMPOUND_SKILL_EXPRESSION_UNSUPPORTED`: neither half is stored, and the
row says the posting used a combined expression this version cannot represent.

Everything else keeps the conservative reading. `Python/R`,
`JavaScript/TypeScript`, `C/C++` and `TensorFlow/PyTorch` are refused as
choices, `Python/Julia` is guarded even though the catalogue knows one half,
and `and/or` is a written `or`. There is no rule of the shape "two AI skills
around a slash are one expression", and nothing turns a slash into a
conjunction.

`bilingual English/French` still gives two languages, and so does
`Bilingualism (English/French) is a significant asset` — the marker knows the
nouns (`bilingualism`, `bilinguisme`) as well as the adjectives, because that is
how the corpus wrote it. Without such a marker, `English/French required` stays
refused; with an explicit `or`, so does `Bilingual English or French required`.

### One refusal per thing refused

An ambiguity row holds the kind, the reason, the rule, the fragment and the
heading — and deliberately **not** the terms of the group it refused, since
storing those would be storing half a requirement. So when one sentence produces
two refusals agreeing on all five fields, the second carries nothing the first
does not, and only the first is kept; positions are then renumbered from zero.
Two refusals differing in any field — a different fragment, a different reason,
a different kind — are two facts and both survive.

### Language proficiency is never translated

`B2` is stored as `B2`, `Fluent` as `Fluent`, `Native` as `Native`. Nothing maps
`Fluent` to `C1` or `Native` to `C2`: those are somebody's convention, not the
employer's sentence, and `profile_languages` in `0010` keeps proficiency as
written for the same reason.

No language is inferred from a country, a city, a nationality, a company name or
the language the advertisement itself is written in. A posting written in
English has not required English; a posting in Paris has not required French;
"work with English-speaking customers" and "the French market" are descriptions
of work, not demands.

When one posting demands one language at two incompatible levels — "English B2
required" beside "English C1 required" — the language stays `REQUIRED`, because
that part is not in dispute, and only `proficiency_text` becomes `NULL`, with a
`CONFLICTING_LANGUAGE_PROFICIENCY` ambiguity saying why. A `PREFERRED` mention
naming another level does not blur a `REQUIRED` one: only observations at the
projected level are consulted.

### Evidence

`evidence_text` is the minimal fragment, capped at 200 characters — a pointer
into the posting, never a copy of it. `context_heading_text` is the heading the
fragment sat under, stored **separately and verbatim**: a posting whose
"Required Qualifications" section lists "Python" is explained by two texts, not
by an invented sentence reading "Required Qualifications > Python".

`observed_requirement` is what *this* fragment stated. A posting that preferred
Python in one line and required it in another projects one `REQUIRED` row and
keeps both mentions, and the preferred one still reads `PREFERRED` — so an
audit never finds a fragment claiming to have demanded something it did not.

### Idempotence

`extractor_version` is `REQUIREMENT_EXTRACTOR_VERSION`
(`opportunity-requirements-v3`), the version of the code that produced the row,
and no caller can name another. `v2` made token boundaries Unicode-aware and
moved a requirement's level from the sentence to the clause; `v3` separated the
slash from the `or`, taught the bilingual marker its nouns, and deduplicated
identical refusals. Each changes what a given description reads as, so an older
row is recomputed rather than trusted. It is deliberately **not**
`opportunity-constraints-v3`: 3.5A and 3.5B change for different reasons, and
one shared label would make every skill fix recompute every start date.

`source_fingerprint` is a SHA-256 over canonical JSON of exactly the field 3.5B
reads — the **description**, and nothing else. Not the title: a title names a
role, and a name is not a demand. Not the location or the country: neither says
anything about a skill or a language. Putting an unread field in the digest
would make an edit nobody's rules looked at recompute every posting.

`opportunity_requirement_extraction_state` is what makes zero an answer. A
posting that names no technology and no language produces no requirement row,
no evidence row and no ambiguity row — and so does a posting nobody has run the
extractor over. The state row tells the two apart, so a coverage figure never
counts unread postings as postings requiring nothing.

One posting is written whole or not at all: the vocabulary it needs, its skill
requirements, their evidence, its language requirements, their evidence, its
ambiguities and its state, in one transaction. A failure rolls back everything,
the newly created `skills` rows included, so a requirement never survives
without its evidence and a state row never claims an extraction that partly
failed.

### Not implemented by `0013`

No opportunity is compared to a profile. No query in the whole slice names
`profiles`, `profile_skills`, `profile_languages` or `profile_facts`. No
eligibility result, no `ELIGIBLE`/`NOT_ELIGIBLE` verdict, no `candidate_has`, no
`missing_skill`, no `skill_gap`, no `match_score`, no TF-IDF, no cosine
similarity, no embedding, no fuzzy matching, no ranking, no recommendation, no
notification and no auto-apply. There is no `confidence` and no `score` column.
There is no HTTP endpoint and no remote write path over these tables, and no
LLM, model download or network call takes part in producing a single row.

## Opportunity eligibility

Migration `0014` adds the Phase 3.6 decision: whether **one person** could apply
to **one posting**. It is the only place in this schema where a row names a user
and an opportunity at once, and everything before it was kept apart on purpose —
`0012` and `0013` say so in their own comments, and `0006` through `0011` never
looked at a posting.

```text
    users ─┐
           ├─> opportunity_eligibilities          (one current decision)
    opportunities ─┘   +-> eligibility_rule_results   (why, rule by rule)
```

The question is narrow: *given what this posting explicitly demands, and what
this person's reliable facts state, is there a known reason they could not
apply?* Not "would they be hired", not "are they a good fit", and not "how good
a fit". There is no score here, no percentage, no rank and no weight, and none
of the columns can be turned into one: `status` is categorical and the counters
count rule outcomes, not a numerator and a denominator. Matching is Phase 4 and
does not exist.

### UNKNOWN is not INELIGIBLE, and the schema enforces it

That is the whole design, and the CHECK constraints exist to make the opposite
**unstorable** rather than merely discouraged. A person whose CV never named a
language has not said they do not speak it; a posting that never named a degree
has not demanded one. Three constraints write the aggregator into the schema:

```sql
CHECK (status <> 'INELIGIBLE' OR violated_count > 0)
CHECK (status <> 'UNKNOWN'    OR (violated_count = 0 AND blocking_unknown_count > 0))
CHECK (status <> 'ELIGIBLE'   OR (violated_count = 0 AND blocking_unknown_count = 0))
```

Read together they say the thing this phase exists to say: a decision is
`INELIGIBLE` only when a hard rule was **contradicted**, and an absent fact
produces `UNKNOWN` instead. Nothing can promote an UNKNOWN into a refusal,
however many of them there are.

### A decision belongs to a person

`UNIQUE (user_id, opportunity_id)`, not `UNIQUE (opportunity_id)`. Eligibility
is not a property of a posting, and storing it as one would make a second user's
answer overwrite the first's. History is not kept: a recomputation replaces the
row, because a stale verdict beside a fresh one is two answers to one question.

### The tables

| table | holds |
| --- | --- |
| `opportunity_eligibilities` | one current verdict per `(user_id, opportunity_id)`, its engine version, its input digest, the five rule-outcome counters and the blocking-unknown subset, and its timestamps |
| `eligibility_rule_results` | one row per rule the engine ran, in evaluation order: the dimension, the rule code, the outcome, whether it could block, how the posting stated the requirement, the reason code, a deterministic sentence, and a logical pointer to each side of the comparison |

### Five rule outcomes, and the difference between the last three

| outcome | means |
| --- | --- |
| `SATISFIED` | an applicable requirement exists and reliable facts meet it |
| `VIOLATED` | a hard applicable requirement exists and reliable facts **contradict** it — only ever a contradiction, never a gap |
| `UNKNOWN` | a hard applicable requirement exists and the facts needed to answer are not there |
| `NOT_APPLICABLE` | the posting asked for nothing on this dimension |
| `NOT_EVALUATED` | something was asked and `eligibility-rules-v1` deliberately declines to decide it |

Every decision keeps its whole reasoning, the rules that passed included.
Stopping at the first contradiction would give an operator one reason where the
posting gave three.

### What may block

`is_blocking` is what the aggregator counts, and it is narrow by construction: a
rule blocks only when it belongs to one of six hard dimensions — `EDUCATION`,
`ENROLLMENT`, `EXPERIENCE`, `LANGUAGE`, `WORK_AUTHORIZATION`, `CONVENTION` —
**and** the posting stated the requirement as `REQUIRED`. Five more CHECKs make
the phase's promises properties of the schema rather than of the code that fills
it:

* a skill requirement can never block, **not even a `REQUIRED` one**, because
  "Python or R" written as two bullets is two `REQUIRED` rows and one choice,
  and rejecting somebody over the alternative the posting did not insist on is
  the one mistake this phase is built to avoid;
* an ambiguity can never block — `0013` recorded a refusal to *represent* a
  demand, and a refusal is not a demand;
* mobility, location, availability, duration, start date and work mode can never
  block, because this version does not evaluate them;
* a `PREFERRED` requirement can never block, on any dimension;
* nothing that cannot block can ever be `VIOLATED`, and the advisory and
  deferred dimensions cannot claim a hard outcome by using `UNKNOWN` either —
  an unverified skill is `NOT_EVALUATED`, which no aggregator counts.

### Evidence is pointed at, never copied

`requirement_ref` and `profile_ref` are stable logical pointers —
`LANGUAGE#english`, `SKILL#python`, `EDUCATION#BAC_PLUS_5:MINIMUM`,
`profile_preferences#convention_status`. Not row ids, because `0013` rebuilds its
rows whole and an id would dangle after the next extraction; and not copies of
the evidence, because Phase 3.4 and Phase 3.5B already store the words, and
duplicating them here would create a second, divergent record of the same proof.
Neither column ever holds a posting's text or a person's, and neither does
`explanation`, which is a deterministic rendering of `reason_code` and the same
normalized values — a courtesy for a reader, never something to parse.

### Idempotence

`input_fingerprint` is a SHA-256 over the canonical JSON of **exactly what the
rules read** — the posting's education, experience, language, work
authorization, convention and skill requirements, the ambiguities `0013`
refused to resolve, this person's projected education, enrolment, languages,
skills, stated convention capability and stated sponsorship need — plus
`eligibility-rules-v1`. The digest domain is the two input dataclasses the rules
are handed, so "in the digest" and "read by a rule" are one set by construction.

A changed telephone number, a new GitHub URL, an edited portfolio link, a
widened mobility or a moved availability date is not a field of either input, so
none of them is in the digest and none recomputes anything. A version bump
recomputes everything, which is what a version is for. Nothing volatile takes
part: no clock, no `evaluated_at`, no `updated_at`, no row id and no database
ordering, so two runs a week apart over unchanged data produce the same
sixty-four characters — which is what makes `unchanged=N, changed=false` a fact
about the data rather than about the run.

### Not implemented by `0014`

No match score, no skill-similarity score, no TF-IDF, no cosine similarity, no
embedding, no fuzzy matching, no ranking, no priority, no interview-potential
score, no recommendation, no notification and no auto-apply. No `confidence` and
no percentage column, and nothing that could be divided into one. Three
dimensions cannot currently be decided at all and answer `UNKNOWN` or
`NOT_APPLICABLE` rather than guessing: education level and experience duration,
because Phase 3.4 normalizes neither, and current enrolment, because neither
phase represents it. The historical `opportunities.eligibility_score` column is
not written, read or repurposed. No LLM, model download or network call takes
part in producing a single row.
