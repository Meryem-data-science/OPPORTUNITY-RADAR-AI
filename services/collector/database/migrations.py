"""Minimal SQL migration runner backed by the Python standard library."""

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3


DEFAULT_MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations"
MIGRATION_FILENAME = re.compile(r"^(?P<version>\d+)_[a-z0-9_]+\.sql$")


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
        raise ValueError("Migration versions must be unique")
    return migrations


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_directory: str | Path = DEFAULT_MIGRATIONS_DIRECTORY,
) -> list[str]:
    """Apply each pending migration atomically and return applied versions."""
    _ensure_migration_table(connection)
    applied_versions = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    pending = [
        migration
        for migration in discover_migrations(migrations_directory)
        if migration.version not in applied_versions
    ]
    applied: list[str] = []

    for migration in pending:
        sql = migration.path.read_text(encoding="utf-8")
        version = migration.version.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            "INSERT INTO schema_migrations (version) "
            f"VALUES ('{version}');\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error:
            connection.rollback()
            raise
        applied.append(migration.version)

    return applied
