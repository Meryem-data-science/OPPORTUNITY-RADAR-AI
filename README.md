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

Phase 3 has started, and only these two slices of it exist:

- **Phase 3.1A** — the Digital Twin root: a `users` row, the one `profiles` row
  it owns, and a local CLI to create or read that pair. The profile holds no
  fact about the person.
- **Phase 3.2A** — the CV parser foundation: reading a local PDF, conservative
  text normalization, per-page provenance, and deterministic detection of
  French/English section headings. It creates no Profile Fact, stores nothing,
  and treats nothing it reads as a verified fact.

This is a locally validated prototype, not a production deployment. It does not
schedule continuous collection, scrape authenticated LinkedIn pages, apply to
jobs, rank opportunities for a person, match a CV against an offer, or provide
ML recommendations. The Digital Twin exists only as the two slices listed above:
there is no validated Master CV, no accept/correct/reject workflow, no
`profile_facts`, no skill table or skill level, no eligibility rule, and no
match score. Qualification is deterministic categorization, not personalized
matching. Source health is a read model and a page: it sends no
notification, retries nothing, reschedules nothing, and judges no `RUNNING` run
stale.

## Repository structure

- `services/collector/`: collectors, orchestration, persistence, qualification,
  and duplicate-review tools.
- `services/api/`: read-only FastAPI opportunity API.
- `services/digital_twin/`: the user/profile root and the local CV PDF parser.
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
