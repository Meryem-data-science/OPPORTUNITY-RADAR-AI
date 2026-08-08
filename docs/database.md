# Database

The production storage target is **Turso / libSQL**. Phase 0.2A uses Python's
standard-library `sqlite3` module only for local development and real schema
validation; SQLite is not presented as the final production connection.

Runtime settings select the `sqlite` or `turso` connection backend.

## SQLite

The standard-library SQLite connection remains available for local development,
tests, migrations, and schema validation. Foreign keys are enabled on every
connection.

## Turso

The Python Turso path uses the official `libsql` client and receives its database URL
and authentication token only from validated `Settings`. The connection layer
is prepared. Remote connectivity and `SELECT 1` have been validated manually;
the shared Foundation schema has also been applied and verified manually.

The Next.js server uses `@libsql/client` with `TURSO_DATABASE_URL` and
`TURSO_AUTH_TOKEN`. Its health service performs only `SELECT` statements and
does not expose either setting to browser code or health responses.

## Migrations

SQLite and Turso use the same ordered files in `migrations/`. The runner splits
each SQL script into complete statements, starts a transaction, executes the
statements, records the version in `schema_migrations`, and commits. An error
causes a rollback and the version is not recorded.

The configured migration command is:

```bash
python -m services.collector.cli.migrate_configured --apply
```

SQLite requires only `--apply`. Turso additionally requires
`RUN_TURSO_LIVE_MIGRATION=1`; without both explicit permissions, no connection
is opened and no remote write is attempted. Real Turso migration application
has not been performed in Codex Cloud and must be validated manually in WSL.

## Healthcheck

The Web application exposes a server-rendered `/health` page and
`GET /api/health`. Both use the same read-only service to verify `SELECT 1`,
the four Foundation tables in `sqlite_schema`, and migration version `0001`.
The API returns HTTP 200 only when every check passes, and HTTP 503 with a
sanitized response otherwise.

Run the configured backend healthcheck with:

```bash
python -m services.collector.cli.db_health
```

It opens the selected connection, executes only `SELECT 1`, verifies the
result, and closes the connection. It creates no schema and writes no data.

After migrations, verify the four foundation tables without writing data:

```bash
python -m services.collector.cli.db_schema
```

## Currently implemented

- Ordered, transactional SQL migrations tracked in `schema_migrations`.
- A shared SQLite/Turso migration runner and configured migration CLI.
- Read-only foundation schema verification.
- SQLite and Turso connection selection with explicit dependency errors.
- A local SQLite connection with foreign-key enforcement.
- Foundation tables: `sources`, `opportunities`, and `opportunity_sources`.
- A migration CLI requiring an explicit local database path:

  ```bash
  python -m services.collector.cli.migrate --database /tmp/opportunity-radar.db
  ```

The foundation migration creates schema only and inserts no sources or
opportunities.

## Not yet implemented

- Live Web-to-Turso health validation (requires manual credentials outside Codex).
- Real source and opportunity data.
- Collectors, matching, or an application-facing database API.
