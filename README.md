# Opportunity Radar AI

Opportunity Radar AI is a locally operated opportunity-collection and review
prototype for Data & AI roles. Phase 2 implements a complete local path from
three real sources, through SQLite persistence and deterministic qualification,
to a read-only FastAPI API and a server-rendered Next.js interface.

Implemented now:

- `RadarAgent` collection from the public Scale AI and Artefact Greenhouse
  boards and from LinkedIn Job Alert emails read with Gmail's read-only API;
- transactional opportunity persistence in local SQLite;
- read-only cross-source duplicate auditing, a human decision registry, and
  explicit, transactional, reversible merging of confirmed duplicates;
- persistent, versioned Data/AI qualification (`qualification-rules-v1`);
- persistent source run history, and a read-only, deterministic source health
  read model over it that flags three consecutive successful runs finding zero
  items;
- `GET /api/opportunities` and a Next.js home page that displays its results and
  links to each original offer;
- `GET /api/source-health` and a Next.js `/source-health` page showing each
  source's Enabled flag, last run, status, items found, new items, relevant
  items, and errors.

Phase 3 has started, and only these six slices of it exist:

- **Phase 3.1A** — the Digital Twin root: a `users` row, the one `profiles` row
  it owns, and a local CLI to create or read that pair. The profile holds no
  fact about the person.
- **Phase 3.2A** — the CV parser foundation: reading a local PDF, conservative
  text normalization, per-page provenance, and deterministic detection of
  French/English section headings. It creates no Profile Fact, stores nothing,
  and treats nothing it reads as a verified fact.
- **Phase 3.2B** — structured **unverified candidates** read out of that parse:
  identity, contact details, links, education, experience, project,
  certification and language entries, and skill mentions, each with the page it
  came from and the named rule that produced it. A candidate means "a rule
  found this text in this document", never "this is true of the person".
  Nothing is validated and nothing is stored.
- **Phase 3.3A** — the persistent anti-hallucination foundation: a
  `profile_facts` table, a separate `profile_fact_provenance` evidence table,
  and the PROPOSED / ACCEPTED / CORRECTED / REJECTED validation cycle. A fact is
  verified when, and only when, its status is `ACCEPTED`; there is no `verified`
  column anywhere. A correction never overwrites: it writes a new `ACCEPTED`
  fact with the person's own evidence, marks the previous one `CORRECTED`, and
  links the two, so every value that was ever proposed stays readable.
- **Phase 3.3B** — the bridge between the two, plus the local human review. A
  closed, total mapping turns each `CandidateType` into a `ProfileFactType`;
  each candidate becomes a **`PROPOSED`** fact carrying the CV's own provenance
  — digest, parser and extractor versions, candidate fingerprint, rule, pages,
  section — and the import is idempotent on that evidence, so re-running one CV
  proposes nothing twice. A local CLI then shows each undecided proposal and
  asks a person to accept, reject, correct, skip or quit.
- **Phase 3.3C** — a non-destructive reconciliation of a *re-read* CV with the
  facts an older parser and extractor already produced for the same document. A
  reading the newer campaign produces byte for byte under the same fact type is
  the same reading: the new evidence is attached to the fact that already
  exists, which keeps its id, its value and the decision a human took about it,
  so re-reading a CV creates no duplicate. A reading that changed — the same
  text under another type, or a block cut on different boundaries — becomes a
  `PROPOSED` fact like any other, and only once a person has answered every one
  of them does `finalize` retire the old readings they replaced, by marking
  them `REJECTED` and never by deleting anything. Identity is exact equality:
  no normalization, no fuzzy matching, no similarity and no model.
- **Phase 3.4A** — a deterministic projection of the **verified** skill facts
  onto normalized skills. It reads `fact_type = 'SKILL' AND status = 'ACCEPTED'`
  and nothing else, normalizes each mention with a closed registry of five
  aliases (`PowerBI → Power BI`, `Postgres → PostgreSQL`,
  `sklearn → Scikit-learn`, `ML → Machine Learning`,
  `IA → Artificial Intelligence`) over a conservative technical key, and
  reconciles `skills`, `profile_skills` and `profile_skill_evidence` so that a
  fact later corrected or rejected stops justifying a skill at the next run.
  **No level is inferred**: no proficiency, no score, no confidence, no
  seniority, and several facts naming one skill are several proofs of one
  association, never "more" of it.
- **Phase 3.4B1** — a deterministic projection of the **verified** experience
  and project facts onto structured rows. It reads
  `fact_type IN ('EXPERIENCE', 'PROJECT') AND status = 'ACCEPTED'` and nothing
  else, and it names a fragment only where the document itself delimited it: a
  pipe-delimited header (`role | organization | period`) for an experience, a
  colon after an optional list marker for a project, and a period only in a
  closed set of explicit temporal forms. **A `NULL` is worth more than an
  invented value**: where the wording is not unambiguous the fragment stays
  `NULL` and the row records `UNPARSED_V1` — the accepted fact is still
  projected, never dropped. No employer is deduced from a sentence, no role
  from a technology, no seniority from the word "stage", no duration, no
  calendar date from a school year and no skill from a project description.
- **Phase 3.4B2** — the same projection, extended to the **verified**
  education, certification and language facts. It reads
  `fact_type IN ('EDUCATION', 'CERTIFICATION', 'LANGUAGE') AND status =
  'ACCEPTED'` and adds `profile_educations`, `profile_certifications` and
  `profile_languages`. **Punctuation proves that segments exist; it never
  proves what they are about**, so a pipe-delimited diploma line is read as a
  school and a programme only when **each of the two roles carries its own
  exclusive proof** in a closed, tiny registry — one segment marked as an
  institution (`université`, `école`, `institut`, `faculté`, `college`, …) and
  not as a programme, the other marked as a programme (`master`, `licence`,
  `diplôme`, `degree`, `ingénieur`, …) and not as an institution. Never by
  position, and whether the line holds two segments or three. One marker is
  never enough: `University Diploma in AI | Sorbonne` would be read backwards
  by a rule that trusted it, so both fields stay `NULL` there. When the proofs
  are missing, only the explicit period is kept and both stay `NULL`; when they
  are there and the line carries no date, `period_text` stays `NULL` rather
  than a year being looked for inside the words. A language line keeps its
  level verbatim after at most one list marker is removed, so `• Anglais : C1`
  stores `Anglais`, never `• Anglais`. A certification is read only from explicit labels
  (`Certification : … | Délivré par : …`), never from a sentence, and a stated
  intention ("préparation à", "objectif") is never turned into a credential. A
  language level is read only when it is a whole form of a closed registry
  (`A1`…`C2`, `débutant`, `courant`, `fluent`, `bilingue`, …) and is stored
  **verbatim**: "courant" never becomes `C1`, "fluent" never becomes `C2`, and
  no CEFR level is computed from anything. No `Bac+N` is derived from the word
  "Master", no issuer, obtention date or expiry date is invented, and no
  language is deduced from a project written in English.
- **Phase 3.4C** — the availability, mobility, preferences and career
  objectives a person **states about themselves**. Nothing in this slice comes
  from a CV, and nothing in it can: it reads
  `fact_type IN ('AVAILABILITY', 'MOBILITY', 'PREFERENCE', 'CAREER_OBJECTIVE')
  AND status = 'ACCEPTED'` **and requires the fact to carry `USER_INPUT`
  provenance**, then projects each onto its own singleton row in
  `profile_availability`, `profile_mobility`, `profile_preferences` and
  `profile_career_objectives`. Acceptance alone is not enough here, unlike in
  3.4A and 3.4B: those project what a document said about a past that
  happened, so a human accepting the reading is the whole question, whereas
  these four are things a person says about what they want — a `CV`, `GITHUB`
  or `OTHER_ACCEPTED_EVIDENCE` proof is not weaker evidence for them, it is the
  wrong kind entirely, and a fact resting only on it is never projected. Each
  value holds **canonical JSON**, so the same
  statement always has the same bytes and restating it is a no-op rather than a
  correction. **An absent statement is `UNKNOWN`, and `UNKNOWN` has no row**:
  no seeded row, no default row, no "unknown" row, and never a `FALSE`. No
  availability is computed from a CV period, a diploma year or the clock; no
  mobility from an address, a city, a country or a past employer; no work mode
  from a past remote job; no preferred domain from a skill, a project or a CV
  section; no convention status from being a student; no visa need from a
  nationality or a location; and no career objective from a CV's professional
  title.
- **Phase 3.5A** — the other side of the same coin: what an **opportunity**
  requires. `services/collector/extractors/opportunity_constraints/` reads a
  posting and records the kind of opportunity, the education levels, the
  experience in months, the duration, the start at the precision it was
  written, the places, the work mode, and what it says about visa sponsorship,
  work authorization and internship agreements — projected by migration `0012`
  onto `opportunity_constraints` and its evidence, location, education and
  conflict tables. **It is never compared to a profile**: joining the two is
  Phase 3.6 and neither it nor any ranking exists. Absence is UNKNOWN, stored
  as `NULL`, and never FALSE — a posting silent about visas has not refused to
  sponsor, an address is not an attendance policy, "Senior" is not a number of
  years, and an internship implies neither an agreement nor a duration. Every
  asserted value carries the rule that fired and the minimal fragment it
  matched; two readings that disagree assert nothing and record the
  contradiction. Skills and languages are deliberately not extracted yet — that
  is 3.5B.

**No CV candidate is ever accepted automatically.** An extraction is a reading
of a document, not a truth about a person, so every fact the import creates is
`PROPOSED` and stays there until somebody decides. There is no accept-all, no
auto-accept, no confidence and no threshold anywhere in the path, and only
`ACCEPTED` facts are usable by later phases.

Phase 3.2 as a whole is therefore still **not** a validated Master CV, and
neither is Phase 3.3. What 3.3A adds is the reliable place a validated fact
lives and the reading — `list_verified_profile_facts` — that returns only
`ACCEPTED` facts; what 3.3B adds is the honest way a CV reaches it; what 3.4A
adds is one derived reading of those accepted facts and no new truth. No Master
CV PDF is generated. Of Phase 3.4, only the projections above exist — the
normalized skills, the structured experiences, projects, education,
certifications and languages, and the availability, mobility, preferences and
career objectives a person states: no employer, institution, canonical role or
computed date deduced from prose, no administrable alias table and no skill
level. Nothing about what somebody wants is ever read out of what they have
done. There is no eligibility, matching, ranking or score, and no web profile
interface of any kind.

This is a locally validated prototype, not a production deployment. It does not
schedule continuous collection, scrape authenticated LinkedIn pages, apply to
jobs, rank opportunities for a person, match a CV against an offer, or provide
ML recommendations. The Digital Twin exists only as the slices listed
above: there is no validated Master CV, no generated Master CV PDF, no skill
level, no inferred seniority or duration, no eligibility rule, and no match
score. What a person states about their availability, mobility, preferences
and objectives is recorded and projected, and it is compared to nothing: no
offer constraint is stored, no location or date is matched against an offer,
and an absent statement stays `UNKNOWN` rather than being read as a "no". Reconciling a re-read CV
proposes and retires readings; it confirms none of them by itself. Skills exist only as the
projection of facts a person already accepted, and holding a skill says nothing
about how well. Experiences and projects exist only as the same kind of
projection, and a fragment nobody wrote explicitly stays `NULL`. CV candidates do
reach `profile_facts` now, but only as proposals a person reviews by hand — no
import accepts anything, and the review is a local terminal command, not a web
interface. Phase 3.2B itself still produces candidates in memory only: it adds
no table, no migration and no database write, and it does not import the fact
package. Qualification is deterministic categorization, not
personalized matching. Source health is a read model and a page: it sends no
notification, retries nothing, reschedules nothing, and judges no `RUNNING` run
stale.

## Repository structure

- `services/collector/`: collectors, orchestration, persistence, qualification,
  and duplicate-review tools.
- `services/api/`: read-only FastAPI opportunity API.
- `services/digital_twin/`: the user/profile root, the local CV PDF parser, the
  unverified candidate extractor built on it, the validated `profile_facts`
  store with its provenance, the bridge and review CLI that turn candidates
  into proposals a human decides, the skill and structured-entry projections
  derived from the facts those decisions accepted, and the availability,
  mobility, preferences and career objectives a person states explicitly.
- `apps/web/`: Next.js web application.
- `config/sources.yaml`: operational source catalogue.
- `migrations/`: ordered SQLite/Foundation SQL migrations.
- `tests/`: unit, integration, and opt-in live tests.
- `docs/`: architecture, database, source, and operating details.

## Prerequisites and installation

- Python 3.12
- Node.js 20 or later
- npm 10 or later

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cd apps/web
npm install
cd ../..
```

Docker is not required.

## Phase 2 local quick start

From the repository root, select the persistent operational SQLite database and
keep the Gmail OAuth files in an ignored local directory:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
export GMAIL_OAUTH_CLIENT_SECRET_PATH=.secrets/gmail-oauth-client.json
export GMAIL_TOKEN_PATH=.secrets/gmail-token.json

python -m services.collector.cli.migrate_configured --apply
python -m services.collector.cli.run_radar --once --apply
```

The normal `RadarAgent` run processes every enabled, active source independently
and then performs one global qualification reconciliation. A source failure does
not undo successful source transactions; qualification has its own result in the
run summary. Use `--source linkedin_job_alert_email` only to debug that one
source. Migrations are always explicit.

Start the read-only API in one terminal:

```bash
export DATABASE_BACKEND=sqlite
export SQLITE_DATABASE_PATH=.data/opportunity-radar.db
python -m uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

Start the web application in another:

```bash
cd apps/web
OPPORTUNITY_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Open <http://localhost:3000> for the opportunities, and
<http://localhost:3000/source-health> for the state of each source. The API
reads existing SQLite data; it neither collects opportunities nor applies
migrations. Detailed setup, duplicate review, security guidance, and debug
commands are in [docs/operations.md](docs/operations.md).

## Database boundary

Persistent local SQLite is the operational Phase 2 database. Turso/libSQL is
retained only as a Foundation/read-only experiment where applicable. Remote
Turso opportunity, qualification, decision, and merge writes are not part of the
operational path and must not be enabled or treated as a prerequisite.

## Phase 2 validation snapshot

The disposable end-to-end validation run on **2026-08-12** observed three real
active sources, 373 opportunities and 373 persisted qualifications. All 373
classifier comparisons matched; the duplicate audit examined 37,711 cross-source
pairs and retained no candidates in that dataset. FastAPI-to-Next.js rendering
and original-offer links were exercised successfully. At that snapshot the
Python suite reported 392 passed and 10 skipped, the web suite reported 16
passed, and web lint and production build succeeded. Live sources change, so
373 is not an expected or hard-coded production count.
