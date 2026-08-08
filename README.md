# Opportunity Radar AI

Opportunity Radar AI is the foundation of a future platform for students and
recent graduates in Data & AI to discover and manage real professional
opportunities.

This repository currently contains only:

- an importable, executable Python collector service shell;
- a local SQLite migration runner for schema validation;
- structured JSON logging for the Python service;
- validated runtime configuration loaded from environment variables;
- SQLite/Turso connection selection with a read-only database healthcheck;
- shared transactional migrations and read-only schema verification;
- a minimal Next.js application with a real, read-only Turso health page;
- empty configuration contracts and initial documentation;
- foundation tests and continuous-integration checks.

Collection, matching, deduplication, Gmail integration, application tracking,
recommendations, NLP, and machine learning are **Planned**. They are not
implemented in this phase.

## Repository structure

- `services/collector/`: Python service and reserved domain packages.
- `apps/web/`: Next.js web application.
- `config/`: empty YAML configuration contracts for future catalogues.
- `tests/`: Python foundation tests and reserved test suites.
- `migrations/`: versioned SQL migrations for the database schema.
- `docs/`: current state and architectural direction.
- `scripts/`: reserved for future operational scripts.

## Prerequisites

- Python 3.12
- Node.js 20 or later
- npm 10 or later

Docker is not required.

## Python setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
pytest
```

Run the service shell from the repository root:

```bash
python -m services.collector.main
```

Apply the SQL migrations to an explicit local SQLite database used for
development or validation:

```bash
python -m services.collector.cli.migrate --database /tmp/opportunity-radar.db
```

Turso / libSQL is the production storage target. Remote connectivity and the
Foundation migration have been validated manually outside Codex.

Check the configured database connection with a read-only `SELECT 1`:

```bash
python -m services.collector.cli.db_health
```

Apply migrations to the configured backend and verify the foundation schema:

```bash
python -m services.collector.cli.migrate_configured --apply
python -m services.collector.cli.db_schema
```

Remote Turso migration additionally requires the explicit
`RUN_TURSO_LIVE_MIGRATION=1` safeguard and has not been run from Codex Cloud.

## Web setup

```bash
cd apps/web
npm install
npm run dev
```

The development server is available at <http://localhost:3000> by default.
The server-rendered `/health` page and JSON `GET /api/health` endpoint perform
read-only connectivity, Foundation schema, and migration-version checks. Set
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in the server process before
starting Next.js, then validate the API manually:

```bash
TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... npm run dev
curl http://localhost:3000/api/health
```

Never commit either credential. They are server-only variables; no
`NEXT_PUBLIC_` database setting is used. Use `npm test`, `npm run lint`, and
`npm run build` for frontend validation. Live Web-to-Turso validation is still
required outside Codex.
