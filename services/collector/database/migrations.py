"""Backend-neutral SQL migration discovery and execution."""

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3

from services.collector.database.connection import DatabaseConnection


DEFAULT_MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"
MIGRATION_FILENAME = re.compile(r"^(?P<version>\d+)_[a-z0-9_]+\.sql$")
_SQL_COMMENT = re.compile(r"--[^\n]*(?:\n|$)|/\*.*?\*/", re.DOTALL)


class MigrationError(RuntimeError):
    """Raised when a migration script is invalid or cannot be applied."""


@dataclass(frozen=True)
class Migration:
    """A versioned SQL migration discovered on disk."""

    version: str
    path: Path


def discover_migrations(
    migrations_directory: str | Path = DEFAULT_MIGRATIONS_DIRECTORY,
) -> list[Migration]:
    """Return valid SQL migrations in version order."""
    directory = Path(migrations_directory)
    migrations: list[Migration] = []

    for path in directory.glob("*.sql"):
        match = MIGRATION_FILENAME.fullmatch(path.name)
        if match:
            migrations.append(Migration(match.group("version"), path))

    migrations.sort(key=lambda migration: (int(migration.version), migration.path.name))
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("Migration versions must be unique")
    return migrations


def _contains_sql(fragment: str) -> bool:
    return bool(_SQL_COMMENT.sub("", fragment).strip())


def split_sql_statements(script: str) -> list[str]:
    """Split a SQL script using SQLite's complete-statement parser."""
    statements: list[str] = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            if _contains_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""

    if _contains_sql(buffer):
        raise MigrationError("SQL migration contains an incomplete statement")
    return statements


def _ensure_migration_table(connection: DatabaseConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _apply_migration(connection: DatabaseConnection, migration: Migration) -> None:
    statements = split_sql_statements(migration.path.read_text(encoding="utf-8"))
    connection.execute("BEGIN")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            (migration.version,),
        )
        connection.execute("COMMIT")
    except Exception as error:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            raise MigrationError(
                f"Migration {migration.version} failed and rollback was unsuccessful"
            ) from error
        raise MigrationError(f"Migration {migration.version} failed") from error


def apply_migrations(
    connection: DatabaseConnection,
    migrations_directory: str | Path = DEFAULT_MIGRATIONS_DIRECTORY,
) -> list[str]:
    """Apply each pending migration transactionally and return applied versions."""
    _ensure_migration_table(connection)
    applied_versions = {
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    pending = [
        migration
        for migration in discover_migrations(migrations_directory)
        if migration.version not in applied_versions
    ]

    for migration in pending:
        _apply_migration(connection, migration)
    return [migration.version for migration in pending]
